# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project goal

Given a Skyrim `.esp` plugin, produce one **`<character>_<refIdSuffix>.prompt`** bio file per NPC in the mod, suitable for use as a SkyrimNet / CHATTERBOX character card driving an LLM-roleplayed NPC. The pipeline is two stages:

1. **Extract** — `scripts/extract_npc_dialogue.py` walks the ESP and produces one self-contained markdown **dossier** per attributed NPC (identifiers + race/sex/class/voice/factions/relationships + every dialogue line, grouped by topic and quest). The dossier is the universal LLM-agnostic interface to stage 2.
2. **Synthesise** — `scripts/synthesize_bio.py` reads a dossier, hands it to an LLM (Claude Agent SDK by default), and writes the `.prompt` bio file beside it. Other backends can be added by implementing the `synthesize(dossier_md, model) -> str` contract and registering them in the `BACKENDS` dict.

This is for adult, NSFW roleplay. Generated content is **uncensored**; do not soften, sanitise, or refuse explicit physical descriptions when synthesising bios.

## Repo layout

```
scripts/
  extract_npc_dialogue.py           — stage 1: ESP → per-NPC .md dossiers
  synthesize_bio.py                 — stage 2: dossier → .prompt via Claude Agent SDK
examples/
  prompts/serana_B74.prompt         — reference example, used as a few-shot in stage 2
output/<plugin_basename>/           — per-plugin dir, contains all dossiers + prompts
  <name>_<refid_suffix>.md          — dossier (stage 1 output)
  <name>_<refid_suffix>.prompt      — bio (stage 2 output, alongside the dossier)
  _unattributed.md                  — dialogue we couldn't pin to an NPC
  _index.json                       — roster summary
requirements.txt
.gitignore
CLAUDE.md
```

## The `.prompt` output format (the goal)

Filename: `<character_name_lower>_<last3hex>.prompt`, where the suffix is the **last three hex digits of the NPC's RefID** (not FormID), e.g. `serana_B74.prompt`. Disambiguates NPCs that share a first name.

The file is a Jinja-style template made of `{% block NAME %}…{% endblock %}` sections. Required blocks, in this order, with target word counts:

| Block | Target | Notes |
|---|---|---|
| `summary` | 75–100 words | **Spoiler-free. Must NOT contain the character's name.** Readable by any stranger. |
| `interject_summary` | 50–100 words | Main triggers only — when this NPC butts into nearby conversation. |
| `background` | 150–450 words | Full lore / history, spoilers allowed. |
| `personality` | 100–450 words | Consolidate overlapping traits; remove redundancy. |
| `appearance` | 75–125 words | **Spoiler-free.** Distinctive features only. Explicit/NSFW physical detail is expected and allowed. |
| `aspirations` | 75–125 words | Bullet list acceptable. |
| `relationships` | 200–300 words | 3–5 most important; prune casual acquaintances. Bullet list acceptable. |
| `occupation` | 75–125 words | Current role only — not full career history. |
| `skills` | 100–150 words | Consolidate related skills; focus on most notable. Bullet list acceptable. |
| `speech_style` | 50–350 words | Cadence, vocabulary, idioms, tone — derivable from extracted dialogue lines. |

**Hard constraints** (`examples/prompts/serana_B74.prompt` follows them all — use as ground truth):
- `summary` and `appearance` must be readable by a stranger: **no spoilers, no character name**.
- Other blocks may name the character freely.
- NSFW physical/sexual detail is allowed and expected; do not censor.

## Commands

```bash
pip install -r requirements.txt

# Stage 1: ESP → output/<plugin>/<name>_<refid>.md per NPC + _index.json
python scripts/extract_npc_dialogue.py "path/to/YourMod.esp"

# Stage 2 (one character): writes <name>_<refid>.prompt next to the dossier
python scripts/synthesize_bio.py output/yourmod/somenpc_743.md

# Stage 2 (whole plugin): every dossier in the directory, _unattributed skipped
python scripts/synthesize_bio.py output/yourmod/

# Useful flags on stage 2:
#   --model claude-sonnet-4-6   cheaper/faster, still good
#   --overwrite                 regenerate even if a .prompt already exists
#   --limit 3                   cap targets (dry runs)
#   --backend <name>            swap LLM backend (currently: claude-agent-sdk)
```

Dependencies: `esplib` (parsing), `claude-agent-sdk` (synthesis). Stage 2 calls Claude through the SDK, which uses the user's existing Claude Code CLI auth — no separate API key plumbing needed. No test suite yet.

## Architecture (stage 1 — extractor)

`scripts/extract_npc_dialogue.py` is a **domain layer over `esplib`**. `esplib` decodes the binary TES5 record format; this script knows what the records *mean* for dialogue and how to attribute lines to a speaking NPC.

### Skyrim records that matter

| Sig | Role |
|---|---|
| `NPC_` | NPC base record — defines who a character is. Carries the FormID. |
| `ACHR` | Placed actor — an instance of an NPC in a cell. Carries the **RefID** (the placement's own FormID). The filename suffix in `<name>_<3hex>.prompt` is the last 3 hex of this RefID. ACHRs live nested inside `CELL`/`WRLD` groups, not at the top level — `plugin.get_records_by_signature("ACHR")` recurses for us. |
| `DIAL` | Dialogue topic (player prompt + category). |
| `INFO` | A dialogue entry owned by a DIAL. May hold multiple response lines. |
| `CTDA` | Condition attached to an INFO (and other records). Speaker attribution lives here. |
| `QUST` | Quest. Owns dialogue (via DIAL `QNAM`) and aliases (referenceable speakers). |

### DIAL → INFO walking (positional, not by FormID)

```
DIAL group (top-level, group_type=0)
├── DIAL record           — topic
├── GroupRecord type=7    — "Topic Children", label = parent DIAL form id
│   └── INFO records      — entries owned by that DIAL
├── DIAL record
├── GroupRecord type=7
│   └── INFO records
```

`iter_dial_with_children()` stitches each DIAL to its following Topic-Children group. The parent relationship is **positional in the group**, not stored on the INFO — don't try to recover it via form-id lookups.

A single INFO holds multiple response lines: each is a `TRDT` (emotion, response number) followed by `NAM1` (spoken text). `parse_info_lines()` starts a new line dict on every `TRDT` and attaches subsequent text to it.

### Speaker attribution via CTDA

Each `CTDA` is a 32-byte fixed-layout struct. The fields we read:

| Offset | Field |
|---|---|
| 0 | operator + flags byte |
| 4–7 | comparison value (float) |
| 8–9 | function index (uint16) |
| 12–15 | param1 (uint32 — usually a FormID, sometimes a small index) |
| 16–19 | param2 |

Function indices used for speaker attribution (empirically verified against vanilla Skyrim FormIDs):

- **`72` = `GetIsID`** — param1 is an NPC FormID. The speaker is that NPC. The workhorse.
- **`566` = `GetIsAliasRef`** — param1 is an alias index in the parent QUST. Resolved through `build_quest_aliases()`, which scans every QUST's alias blocks (`ALST`/`ALLS` ... `ALFR` ... `ALED`) for forced ACHR references and maps them back to base NPCs.
- **alias-default fallback** — when no narrowing condition exists and the parent quest has exactly one statically-resolvable alias, the line is attributed to that alias's NPC. Aggressive but high-yield for follower mods. On AX ValSerano this attribution method alone catches ~1200 lines.

Anything left over goes to `_unattributed.md`. On AX ValSerano: ~3700 attributed (GetIsID 1900 / alias_default 1200 / GetIsAliasRef 40), ~1700 unattributed. Remaining gap is mostly `SCEN` (scene-driven) dialogue — parsing scene actor/action assignments would close most of it.

### NPC ↔ RefID mapping and enrichment

`build_achr_to_npc()` walks every `ACHR` and reads its `NAME` subrecord (the base NPC FormID), inverting to give each NPC its list of RefIDs. The first RefID becomes `primary_ref_id`, and its last 3 hex digits become `refid_suffix` — the filename suffix.

`parse_npc_attrs()` reads each NPC's `ACBS` (flags including the female bit), `RNAM` (race), `CNAM` (class), `VTCK` (voice type), and `SNAM` (faction memberships with rank). FormID references are resolved against a `name_lookup` dict built across `RACE`/`CLAS`/`VTYP`/`FACT`/`KYWD`/`ASTP`/`NPC_` records so the dossier shows editor IDs instead of raw form ids.

`parse_relationships()` walks `RELA` records (16-byte `DATA`: parent fid, child fid, signed rank, association keyword fid) and attaches both directions to the relevant NPCs.

NPCs referenced by `GetIsID` but **not defined in this plugin** (e.g. vanilla Serana at `0001B07F`) get a stub dossier marked `[external]` with the FormID only — masters aren't loaded, so we can't resolve their names, races, or RefIDs. The user can either rerun with masters loaded (future work) or skip these.

### Dialogue line clustering

`_cluster_lines()` collapses near-duplicate lines per topic by replacing any consecutive run of capitalised words with `<N>` and using the normalised first-80-chars as the cluster key. This catches Skyrim's name-variant stamping (e.g. "Thank you, Aelyn!" / "Thank you, Bree!" / … become one line with a `(×N variants)` annotation). Without this, Val Serano's dossier alone is hundreds of redundant "Thank you, &lt;PlayerName&gt;!" entries.

### Strings and localization

`plugin.is_localized` decides whether a string subrecord holds inline bytes or a 4-byte ID into the external `.STRINGS`/`.DLSTRINGS`/`.ILSTRINGS` tables. The `_read_str` helper and the inline check inside `parse_info_lines` branch on this and call `plugin.resolve_string()` for IDs. Any new code that reads `FULL`/`NAM1` text **must do this branching** — calling `sr.get_string()` directly returns garbage for localized plugins.

## Output format

### Stage 1 (per-character dossier — markdown)

One file per attributed NPC at `output/<plugin>/<name>_<refid_suffix>.md`. Sections:

1. `# <Name>` heading + source plugin
2. `## Identifiers` (editor id, form id, primary ref id, filename suffix, alt RefIDs, external flag)
3. `## Attributes` (sex, race, class, voice type, badge flags)
4. `## Factions` (resolved edid + rank)
5. `## Relationships` (other party + rank name + association keyword)
6. `## Dialogue (N lines, M topics)` — topics sorted by line count desc, each topic shows quest context, then `- _Emotion_: text  (×N variants)` lines

The dossier is the **stable contract** between stage 1 and stage 2. Any LLM that can read markdown can synthesise from it; the Claude Agent SDK driver is just one consumer. `_index.json` next to the dossiers is a roster summary for tooling.

### Stage 2 (bio — Jinja-block prompt)

`output/<plugin>/<name>_<refid_suffix>.prompt`, written next to its dossier. Format and constraints exactly per the spec table above. The synthesiser sends three things to the LLM: the system prompt with the block spec + constraints + the Serana example as a few-shot, and the dossier as the user message. The system prompt is large and identical across NPCs, so caching it (manually for raw API, automatically by the Agent SDK transport) makes per-character cost dominated by dossier size.

### Stage 2 swappability

`scripts/synthesize_bio.py` exposes a `BACKENDS` registry keyed by name → `async (dossier_md, model) -> str`. The current entry is `claude-agent-sdk`. Add a new backend by writing an async function, registering it in the dict, and selecting it with `--backend`. Don't change the dossier format to fit a backend — keep stage 1 output backend-agnostic.

Form ids and ref ids are 8-char uppercase hex strings throughout (via `fmt_fid()`); raw integers stay inside parsing functions only.
