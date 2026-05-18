"""
Stage 1: ESP -> one self-contained character dossier per NPC.

For every NPC the plugin attributes dialogue to, emits a markdown dossier with:
  - identifiers (FormID, RefID, suffix used in the .prompt filename)
  - basic attributes (race, sex, class, voice type, factions, relationships)
  - the full set of dialogue lines they speak, grouped by topic and quest

This dossier is the hand-off to stage 2: any LLM (Claude, GPT, local model)
can read one .md file and produce a complete .prompt bio.

Speaker attribution is done via INFO conditions:
  - GetIsID (CTDA func 72)        — direct NPC FormID match
  - GetIsAliasRef (CTDA func 566) — resolved through parent QUST alias forced
                                    references (ALFR -> ACHR -> NPC)
  - alias-default fallback         — when an INFO has no narrowing condition
                                    and its parent quest has exactly one alias
                                    statically resolvable to an NPC, attribute
                                    to that NPC.

Outputs:
  output/<basename>/<edid>_<refid_suffix>.md   — one dossier per attributed NPC
  output/<basename>/_index.json                — roster summary
  output/<basename>/_unattributed.md           — lines we couldn't assign
"""

from __future__ import annotations
import os
import sys
import json
import struct
from collections import defaultdict
from typing import Optional, Iterable

import esplib

# CTDA function indices — empirically verified on Skyrim
CTDA_GetIsID       = 72
CTDA_GetIsAliasRef = 566

# ACBS flags
ACBS_FEMALE    = 0x00000001
ACBS_ESSENTIAL = 0x00000002
ACBS_UNIQUE    = 0x00000020
ACBS_PROTECTED = 0x00000800

# Skyrim relationship ranks (RELA DATA[8])
RELA_RANK = {
    4: "Lover", 3: "Ally", 2: "Confidant", 1: "Friend",
    0: "Acquaintance",
    -1: "Rival", -2: "Foe", -3: "Enemy", -4: "Archnemesis",
}

EMOTION = {0: "Neutral", 1: "Anger", 2: "Disgust", 3: "Fear",
           4: "Sad", 5: "Happy", 6: "Surprise", 7: "Puzzled"}


# ---- helpers ----------------------------------------------------------------

def _fid(form_id) -> int:
    return int(form_id.value) if hasattr(form_id, "value") else int(form_id)

def fmt_fid(value: Optional[int]) -> Optional[str]:
    return f"{value & 0xFFFFFFFF:08X}" if value else None

def refid_suffix(refid_hex: str) -> str:
    return refid_hex[-3:].upper()

def _read_str(record, sig: str) -> Optional[str]:
    sr = record.get_subrecord(sig)
    if sr is None:
        return None
    plugin = record.plugin
    if sr.size == 4 and plugin is not None and plugin.is_localized:
        return plugin.resolve_string(sr.get_uint32())
    return sr.get_string()

def safe_filename(name: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in (name or "")).strip("_").lower() or "unnamed"


# ---- DIAL -> INFO walking ---------------------------------------------------

def iter_dial_with_children(plugin) -> Iterable[tuple[object, list]]:
    for top in plugin.groups:
        if top.group_type != 0 or top.label != "DIAL":
            continue
        current_dial = None
        infos: list = []
        for item in top.records:
            cls = type(item).__name__
            if cls == "Record" and getattr(item, "signature", "") == "DIAL":
                if current_dial is not None:
                    yield current_dial, infos
                current_dial = item
                infos = []
            elif cls == "GroupRecord" and item.group_type == 7:
                for sub in item.records:
                    if type(sub).__name__ == "Record" and getattr(sub, "signature", "") == "INFO":
                        infos.append(sub)
        if current_dial is not None:
            yield current_dial, infos


# ---- NPC enrichment ---------------------------------------------------------

def parse_npc_attrs(npc, name_lookup: dict[int, str]) -> dict:
    acbs = npc.get_subrecord("ACBS")
    flags = struct.unpack_from("<I", bytes(acbs.data), 0)[0] if acbs else 0
    rnam  = npc.get_subrecord("RNAM")
    cnam  = npc.get_subrecord("CNAM")
    vtck  = npc.get_subrecord("VTCK")

    factions: list[dict] = []
    for s in npc.get_subrecords("SNAM"):
        b = bytes(s.data)
        if len(b) >= 5:
            fid = struct.unpack_from("<I", b, 0)[0]
            rank = b[4]
            factions.append({
                "form_id": fmt_fid(fid),
                "edid": name_lookup.get(fid),
                "rank": rank,
            })

    return {
        "form_id": fmt_fid(_fid(npc.form_id)),
        "edid": npc.editor_id,
        "name": npc.full_name,
        "sex": "Female" if flags & ACBS_FEMALE else "Male",
        "essential":  bool(flags & ACBS_ESSENTIAL),
        "protected":  bool(flags & ACBS_PROTECTED),
        "unique":     bool(flags & ACBS_UNIQUE),
        "race_form_id":  fmt_fid(rnam.get_uint32()) if rnam else None,
        "race":          name_lookup.get(rnam.get_uint32()) if rnam else None,
        "class_form_id": fmt_fid(cnam.get_uint32()) if cnam else None,
        "class":         name_lookup.get(cnam.get_uint32()) if cnam else None,
        "voice_form_id": fmt_fid(vtck.get_uint32()) if vtck else None,
        "voice":         name_lookup.get(vtck.get_uint32()) if vtck else None,
        "factions":      factions,
    }


def parse_relationships(plugin, name_lookup: dict[int, str]) -> dict[int, list[dict]]:
    """Return npc_fid -> list of relationship dicts (other party, rank, assoc keyword)."""
    out: dict[int, list[dict]] = defaultdict(list)
    for rela in plugin.get_records_by_signature("RELA"):
        data = rela.get_subrecord("DATA")
        if not data:
            continue
        b = bytes(data.data)
        if len(b) < 16:
            continue
        a       = struct.unpack_from("<I", b, 0)[0]
        b_fid   = struct.unpack_from("<I", b, 4)[0]
        rank    = struct.unpack_from("<i", b, 8)[0]
        assoc   = struct.unpack_from("<I", b, 12)[0]
        rec = {
            "rank":      rank,
            "rank_name": RELA_RANK.get(rank, str(rank)),
            "assoc_form_id": fmt_fid(assoc) if assoc else None,
            "assoc":      name_lookup.get(assoc),
        }
        out[a].append({**rec, "other_form_id": fmt_fid(b_fid),
                       "other": name_lookup.get(b_fid), "direction": "from"})
        out[b_fid].append({**rec, "other_form_id": fmt_fid(a),
                           "other": name_lookup.get(a), "direction": "to"})
    return out


# ---- ACHR -> NPC ------------------------------------------------------------

def build_achr_to_npc(plugin) -> tuple[dict[int, int], dict[int, list[int]]]:
    """Return (achr_fid -> npc_fid, npc_fid -> [achr_fid, …])."""
    achr_to_npc: dict[int, int] = {}
    npc_to_achr: dict[int, list[int]] = defaultdict(list)
    for achr in plugin.get_records_by_signature("ACHR"):
        name_sr = achr.get_subrecord("NAME")
        if not name_sr:
            continue
        base = name_sr.get_uint32()
        achr_fid = _fid(achr.form_id)
        achr_to_npc[achr_fid] = base
        npc_to_achr[base].append(achr_fid)
    return achr_to_npc, dict(npc_to_achr)


# ---- QUST alias resolution --------------------------------------------------

def build_quest_aliases(plugin, achr_to_npc: dict[int, int]) -> dict[int, dict[int, int]]:
    """quest_fid -> { alias_idx -> npc_fid } for aliases with a forced ACHR ref."""
    out: dict[int, dict[int, int]] = defaultdict(dict)
    for qust in plugin.get_records_by_signature("QUST"):
        qfid = _fid(qust.form_id)
        current_idx: Optional[int] = None
        for sr in qust.subrecords:
            sig = sr.signature
            if sig in ("ALST", "ALLS"):
                current_idx = sr.get_uint32()
            elif sig == "ALFR" and current_idx is not None:
                ref_fid = sr.get_uint32()
                npc_fid = achr_to_npc.get(ref_fid)
                if npc_fid is not None:
                    out[qfid][current_idx] = npc_fid
            elif sig == "ALED":
                current_idx = None
    return dict(out)


# ---- speaker attribution ----------------------------------------------------

def _info_ctda_iter(info):
    for c in info.get_subrecords("CTDA"):
        b = bytes(c.data)
        if len(b) < 16:
            continue
        func = struct.unpack_from("<H", b, 8)[0]
        p1 = struct.unpack_from("<I", b, 12)[0]
        yield func, p1, b


def resolve_speaker(info, parent_qfid: Optional[int],
                    quest_aliases: dict[int, dict[int, int]]) -> tuple[Optional[int], str]:
    """Return (npc_fid, attribution_method) or (None, 'unattributed')."""
    for func, p1, _ in _info_ctda_iter(info):
        if func == CTDA_GetIsID and p1 != 0:
            return p1, "GetIsID"
        if func == CTDA_GetIsAliasRef and parent_qfid is not None:
            npc = quest_aliases.get(parent_qfid, {}).get(p1)
            if npc is not None:
                return npc, "GetIsAliasRef"
    # Fallback: parent quest has exactly one statically-resolvable alias →
    # attribute to it. Aggressive but high-yield for follower mods.
    if parent_qfid is not None:
        aliases = quest_aliases.get(parent_qfid, {})
        if len(aliases) == 1:
            return next(iter(aliases.values())), "alias_default"
    return None, "unattributed"


# ---- INFO line decoding -----------------------------------------------------

def parse_info_lines(info) -> list[dict]:
    lines: list[dict] = []
    cur: Optional[dict] = None
    plugin = info.plugin

    def push():
        nonlocal cur
        if cur is not None and cur.get("text"):
            lines.append(cur)
        cur = None

    for sr in info.subrecords:
        sig = sr.signature
        if sig == "TRDT":
            push()
            b = bytes(sr.data)
            etype = struct.unpack_from("<I", b, 0)[0] if len(b) >= 4 else 0
            cur = {"emotion": EMOTION.get(etype, str(etype)), "text": None}
        elif sig == "NAM1":
            text = (
                plugin.resolve_string(sr.get_uint32())
                if (sr.size == 4 and plugin is not None and plugin.is_localized)
                else sr.get_string()
            )
            if cur is None:
                cur = {"emotion": None, "text": text}
            else:
                cur["text"] = text
    push()
    return lines


# ---- main extraction --------------------------------------------------------

def extract(plugin_path: str) -> dict:
    plugin = esplib.Plugin.load(plugin_path)

    # name_lookup: any FormID in this plugin -> editor_id (for display).
    # Covers RACE/CLAS/VTYP/FACT/KYWD/AssociationType/NPC.
    name_lookup: dict[int, str] = {}
    for sig in ("RACE", "CLAS", "VTYP", "FACT", "KYWD", "ASTP", "NPC_"):
        for r in plugin.get_records_by_signature(sig):
            if r.editor_id:
                name_lookup[_fid(r.form_id)] = r.editor_id

    # ACHR mapping
    achr_to_npc, npc_to_achr = build_achr_to_npc(plugin)
    quest_aliases = build_quest_aliases(plugin, achr_to_npc)

    # NPC roster
    npcs_by_fid: dict[int, dict] = {}
    for npc in plugin.get_records_by_signature("NPC_"):
        attrs = parse_npc_attrs(npc, name_lookup)
        fid = _fid(npc.form_id)
        ref_ids = [fmt_fid(r) for r in npc_to_achr.get(fid, [])]
        attrs["ref_ids"] = ref_ids
        attrs["primary_ref_id"] = ref_ids[0] if ref_ids else None
        attrs["refid_suffix"] = refid_suffix(ref_ids[0]) if ref_ids else None
        npcs_by_fid[fid] = attrs

    # Relationships
    rels = parse_relationships(plugin, name_lookup)
    for fid, rs in rels.items():
        if fid in npcs_by_fid:
            npcs_by_fid[fid]["relationships"] = rs

    # Quest names
    quests: dict[int, dict] = {}
    for q in plugin.get_records_by_signature("QUST"):
        quests[_fid(q.form_id)] = {"edid": q.editor_id, "name": q.full_name}

    # Walk DIAL -> INFO and attribute lines
    lines_by_speaker: dict[int, dict] = defaultdict(lambda: {"topics": defaultdict(lambda: {"lines": []})})
    counts = defaultdict(int)
    method_counts = defaultdict(int)

    for dial_rec, info_recs in iter_dial_with_children(plugin):
        topic_text = _read_str(dial_rec, "FULL")
        topic_edid = dial_rec.editor_id
        topic_fid = _fid(dial_rec.form_id)
        qnam = dial_rec.get_subrecord("QNAM")
        parent_qfid = qnam.get_uint32() if qnam else None
        quest_meta = quests.get(parent_qfid) if parent_qfid else None

        for info in info_recs:
            text_lines = parse_info_lines(info)
            if not text_lines:
                continue

            speaker_fid, method = resolve_speaker(info, parent_qfid, quest_aliases)
            method_counts[method] += 1

            key = speaker_fid if speaker_fid is not None else 0
            counts["attributed" if speaker_fid else "unattributed"] += len(text_lines)

            entry = lines_by_speaker[key]
            topic = entry["topics"][topic_fid]
            if "topic_text" not in topic:
                topic.update({
                    "topic_form_id": fmt_fid(topic_fid),
                    "topic_edid": topic_edid,
                    "topic_text": topic_text,
                    "quest_form_id": fmt_fid(parent_qfid) if parent_qfid else None,
                    "quest_name": quest_meta["name"] if quest_meta else None,
                    "quest_edid": quest_meta["edid"] if quest_meta else None,
                })
            for ln in text_lines:
                topic["lines"].append({**ln, "method": method})

    # Build per-NPC payloads
    out: dict[int, dict] = {}
    for fid, payload in lines_by_speaker.items():
        topics = []
        for t in payload["topics"].values():
            topics.append(dict(t, lines=list(t["lines"])))
        if fid == 0:
            out[0] = {"meta": {"name": "_unattributed"}, "topics": topics}
            continue
        meta = npcs_by_fid.get(fid) or {
            "form_id": fmt_fid(fid), "edid": None, "name": None,
            "primary_ref_id": None, "refid_suffix": None,
            "race": None, "sex": None, "class": None, "voice": None,
            "factions": [], "relationships": [],
            "external": True,  # not defined in this plugin
        }
        out[fid] = {"meta": meta, "topics": topics}

    return {
        "plugin": os.path.basename(plugin_path),
        "is_localized": plugin.is_localized,
        "is_esm_flag": plugin.is_esm,
        "is_esl": plugin.is_esl,
        "counts": dict(counts),
        "attribution_methods": dict(method_counts),
        "npcs_total_in_plugin": len(npcs_by_fid),
        "speakers": out,
    }


# ---- markdown dossier rendering --------------------------------------------

def _cluster_key(text: str) -> str:
    """Normalised fingerprint that ignores name-token variants. Collapses any
    consecutive run of words containing an uppercase letter into a single
    placeholder so that "Thank you, Aelyn!" and "Thank you, Bree!" map together.
    """
    import re
    t = re.sub(r"\b[A-Z][\w']*(?:\s+[A-Z][\w']*)*\b", "<N>", text)
    t = re.sub(r"\s+", " ", t).strip().lower()
    return t[:80]


def _cluster_lines(lines: list[dict]) -> list[tuple[dict, int]]:
    """Collapse near-duplicate lines that share emotion + normalised fingerprint."""
    seen: dict[tuple, list] = {}
    order: list[tuple] = []
    for ln in lines:
        text = (ln.get("text") or "").strip()
        key = (ln.get("emotion"), _cluster_key(text))
        if key not in seen:
            seen[key] = [ln, 1]
            order.append(key)
        else:
            seen[key][1] += 1
    return [(seen[k][0], seen[k][1]) for k in order]


def render_dossier(speaker: dict, plugin_name: str) -> str:
    m = speaker["meta"]
    lines: list[str] = []
    lines.append(f"# {m.get('name') or m.get('edid') or m.get('form_id')}")
    lines.append("")
    lines.append(f"_Source plugin: `{plugin_name}`_")
    lines.append("")
    lines.append("## Identifiers")
    lines.append(f"- Editor ID: `{m.get('edid')}`")
    lines.append(f"- Form ID: `{m.get('form_id')}`")
    lines.append(f"- Reference ID (primary): `{m.get('primary_ref_id')}`")
    lines.append(f"- Filename suffix: `{m.get('refid_suffix')}`")
    if m.get("ref_ids") and len(m["ref_ids"]) > 1:
        lines.append(f"- All RefIDs: {', '.join(f'`{r}`' for r in m['ref_ids'])}")
    if m.get("external"):
        lines.append("- _Note: NPC base record lives in a master, not this plugin — only lines added here are listed below._")
    lines.append("")

    lines.append("## Attributes")
    for label, key in [("Sex", "sex"), ("Race", "race"), ("Class", "class"),
                       ("Voice type", "voice")]:
        if m.get(key):
            lines.append(f"- {label}: {m[key]}")
    badges = [k for k in ("essential", "protected", "unique") if m.get(k)]
    if badges:
        lines.append(f"- Flags: {', '.join(badges)}")
    lines.append("")

    if m.get("factions"):
        lines.append("## Factions")
        for f in m["factions"]:
            tag = f["edid"] or f["form_id"]
            lines.append(f"- {tag} (rank {f['rank']})")
        lines.append("")

    if m.get("relationships"):
        lines.append("## Relationships")
        for r in m["relationships"]:
            other = r.get("other") or r.get("other_form_id")
            assoc = r.get("assoc") or r.get("assoc_form_id") or "—"
            lines.append(f"- {other}: {r['rank_name']} (association: {assoc})")
        lines.append("")

    total_lines = sum(len(t["lines"]) for t in speaker["topics"])
    lines.append(f"## Dialogue ({total_lines} lines, {len(speaker['topics'])} topics)")
    lines.append("")

    # Sort topics: named/quest topics first, then by line count
    def topic_sort_key(t):
        return (
            -len(t["lines"]),
            (t.get("quest_name") or t.get("quest_edid") or "~"),
            (t.get("topic_text") or t.get("topic_edid") or ""),
        )

    for t in sorted(speaker["topics"], key=topic_sort_key):
        header_bits = []
        if t.get("topic_text"):
            header_bits.append(f'"{t["topic_text"]}"')
        elif t.get("topic_edid"):
            header_bits.append(f"({t['topic_edid']})")
        if t.get("quest_name") or t.get("quest_edid"):
            header_bits.append(f"— quest: {t.get('quest_name') or t.get('quest_edid')}")
        lines.append(f"### {' '.join(header_bits) or t.get('topic_form_id')}")
        for ln, count in _cluster_lines(t["lines"]):
            emo = ln.get("emotion") or "—"
            suffix = f"  _(×{count} variants)_" if count > 1 else ""
            lines.append(f"- _{emo}_: {ln['text']}{suffix}")
        lines.append("")

    return "\n".join(lines)


# ---- main -------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("usage: extract_npc_dialogue.py <plugin.esp> [out_dir]")
        sys.exit(1)
    path = sys.argv[1]
    base = os.path.splitext(os.path.basename(path))[0]
    out_dir = sys.argv[2] if len(sys.argv) > 2 else os.path.join("output", safe_filename(base))
    os.makedirs(out_dir, exist_ok=True)

    result = extract(path)
    speakers = result["speakers"]

    print(f"Plugin: {result['plugin']} (localized={result['is_localized']} esm={result['is_esm_flag']} esl={result['is_esl']})")
    print(f"NPCs in plugin           : {result['npcs_total_in_plugin']}")
    print(f"Attributed lines         : {result['counts'].get('attributed', 0)}")
    print(f"Unattributed lines       : {result['counts'].get('unattributed', 0)}")
    print(f"Attribution by method    : {result['attribution_methods']}")
    print()

    # Per-speaker dossier files
    written = 0
    nameless_skipped = 0
    nameless_lines = 0
    roster = []
    for fid, sp in speakers.items():
        m = sp["meta"]
        n_lines = sum(len(t["lines"]) for t in sp["topics"])
        if fid == 0:
            fname = "_unattributed.md"
        else:
            # Skip NPCs with no FULL name — utility actors, generic triggers,
            # or external base records whose name lives in an unloaded master.
            # They produce noise dossiers that aren't useful as character cards.
            if not m.get("name"):
                nameless_skipped += 1
                nameless_lines += n_lines
                continue
            label = safe_filename(m["name"])
            suffix = m.get("refid_suffix") or (m.get("form_id") or "")[-3:]
            fname = f"{label}_{suffix}.md"
        with open(os.path.join(out_dir, fname), "w", encoding="utf-8") as f:
            f.write(render_dossier(sp, result["plugin"]))
        written += 1
        roster.append({
            "file": fname,
            "form_id": m.get("form_id"),
            "edid": m.get("edid"),
            "name": m.get("name"),
            "refid_suffix": m.get("refid_suffix"),
            "line_count": n_lines,
            "external": bool(m.get("external")),
        })

    roster.sort(key=lambda r: (-r["line_count"], r.get("edid") or ""))
    with open(os.path.join(out_dir, "_index.json"), "w", encoding="utf-8") as f:
        json.dump({
            "plugin": result["plugin"],
            "counts": result["counts"],
            "attribution_methods": result["attribution_methods"],
            "npcs_total_in_plugin": result["npcs_total_in_plugin"],
            "files": roster,
        }, f, indent=2, ensure_ascii=False)

    print(f"Wrote {written} dossier file(s) to {out_dir}/")
    if nameless_skipped:
        print(f"Skipped {nameless_skipped} nameless NPC(s) ({nameless_lines} line(s)) — no FULL name on base record.")
    print()
    print("Top speakers:")
    for r in roster[:8]:
        tag = r["name"] or r["edid"] or r["form_id"] or "?"
        ext = " [external]" if r["external"] else ""
        print(f"  {r['line_count']:>5}  {tag}{ext}  -> {r['file']}")


if __name__ == "__main__":
    main()
