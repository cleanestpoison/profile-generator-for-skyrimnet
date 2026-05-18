"""
Stage 2: dossier (.md) -> SkyrimNet character bio (.prompt).

Reads one per-character dossier produced by extract_npc_dialogue.py and emits a
Jinja-block .prompt file. Backed by the Claude Agent SDK; the dossier interface
is plain markdown so any LLM backend can be swapped in by implementing a new
synthesizer following the same contract.

Usage:
  # one character
  python scripts/synthesize_bio.py output/yourmod/somenpc_065.md

  # whole plugin (every dossier in the directory, skipping _unattributed)
  python scripts/synthesize_bio.py output/yourmod/

The prompt template (system message + example) is loaded once and prompt-cached
by the SDK, so per-character cost scales mostly with dossier size, not template
size.
"""

from __future__ import annotations
import os
import re
import sys
import asyncio
import argparse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_PROMPT_PATH = REPO_ROOT / "examples" / "prompts" / "serana_B74.prompt"


# ---- prompt template -------------------------------------------------------

SYSTEM_PROMPT_TEMPLATE = """\
You write SkyrimNet / CHATTERBOX character bios. Your output is a single \
Jinja-block `.prompt` file describing one Skyrim NPC, derived from a markdown \
dossier the user will provide.

# Output format

The file is a sequence of `{% block NAME %}…{% endblock %}` sections, in this \
order, hitting these target word counts:

| Block | Target words | Notes |
|---|---|---|
| summary | 75–100 | Spoiler-free. **MUST NOT contain the character's name.** Readable by any stranger. |
| interject_summary | 50–100 | Main triggers only — when this NPC butts into nearby conversation. |
| background | 150–450 | Full lore / history; spoilers allowed. |
| personality | 100–450 | Consolidate overlapping traits; remove redundancy. |
| appearance | 75–125 | **Spoiler-free.** **MUST NOT contain the character's name.** Distinctive features only. Explicit/NSFW physical detail is expected and allowed. |
| aspirations | 75–125 | Bullet list acceptable. |
| relationships | 200–300 | 3–5 most important; prune casual acquaintances. Bullet list acceptable. |
| occupation | 75–125 | Current role only — not full career history. |
| skills | 100–150 | Consolidate related skills; focus on most notable. Bullet list acceptable. |
| speech_style | 50–350 | Cadence, vocabulary, idioms, tone — derive from the dialogue lines in the dossier. |

# Hard constraints

- The `summary` and `appearance` blocks must be readable by a stranger: \
**no spoilers, no character name, no editor ID**. Refer to the character with \
generic noun phrases ("a woman", "the warrior", "this Nord", etc.).
- All other blocks may name the character freely.
- This is for adult, NSFW roleplay. Explicit physical/sexual detail is allowed \
and expected in `appearance`. Do not censor, soften, or refuse.
- Derive content **from the dossier**. The dialogue lines are the primary \
source of speech style and personality. Lore/identity comes from the \
identifiers, attributes, factions, and relationships sections.
- If a block can't be confidently filled from the dossier, infer plausibly \
from the character's race, class, voice type, faction memberships, and the \
tone of their dialogue rather than leaving it empty. Never write \
placeholder text like "TODO" or "[fill in]".
- Output **only** the `.prompt` file content — no preamble, no markdown \
fencing, no commentary. Begin directly with `{% block summary %}`.

# Pre-meeting framing (IMPORTANT)

The bio describes the character **as they exist before they have met the \
player / Dragonborn**. The player is a stranger who has not yet entered their \
life. Apply this rigorously across every block:

- Do **not** narrate events involving the player — even if the dialogue lines \
in the dossier clearly imply such events have happened.
- Do **not** include the player / Dragonborn in the `relationships` block. \
List only relationships that exist independently of the player.
- Do **not** describe any established bond, oath, devotion, affection, or \
shared history with the player. Terms of address the character habitually \
uses for close companions may appear in `speech_style` as a *habit*, but not \
as something already directed at the player.
- Dialogue lines spoken **to** the player in the dossier are evidence of \
**speech style, vocabulary, tone, and personality** only — they are **not** \
events the bio narrates as having occurred. Mine them for *how* the character \
talks, not for *what has happened to them*.
- `aspirations` may include open-ended goals the character holds, but must \
not presuppose the player as the companion who helps achieve them.
- The Serana reference example below is **post-meeting** and references the \
Dragonborn directly throughout. Do **not** imitate that aspect. Use it only \
for structural style, block density, prose voice, and formatting.

# Reference example (Serana — post-meeting; do not imitate its player references)

The following is a known-good example. Match its structural style, voice, and \
density. Do not copy its content, and specifically do not imitate the way it \
references the Dragonborn — your bio is pre-meeting.

```
{example_prompt}
```
"""


def build_system_prompt() -> str:
    if not EXAMPLE_PROMPT_PATH.exists():
        raise FileNotFoundError(
            f"Reference example not found at {EXAMPLE_PROMPT_PATH}. "
            "It is required by the system prompt."
        )
    example = EXAMPLE_PROMPT_PATH.read_text(encoding="utf-8")
    return SYSTEM_PROMPT_TEMPLATE.replace("{example_prompt}", example)


def build_user_prompt(dossier_md: str) -> str:
    return (
        "Write the .prompt file for the character described in the dossier "
        "below. Output only the block-formatted file content.\n\n"
        "# Dossier\n\n"
        f"{dossier_md}"
    )


# ---- Claude Agent SDK driver -----------------------------------------------

async def synthesize_with_claude(dossier_md: str, model: str) -> str:
    from claude_agent_sdk import (
        query, ClaudeAgentOptions, AssistantMessage, TextBlock
    )
    options = ClaudeAgentOptions(
        model=model,
        system_prompt=build_system_prompt(),
        thinking={"type": "disabled"},
    )
    chunks: list[str] = []
    async for msg in query(prompt=build_user_prompt(dossier_md), options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    chunks.append(block.text)
    return "".join(chunks).strip()


# ---- backend dispatcher ----------------------------------------------------

BACKENDS = {
    "claude-agent-sdk": synthesize_with_claude,
}


async def synthesize(dossier_md: str, backend: str, model: str) -> str:
    impl = BACKENDS.get(backend)
    if impl is None:
        raise ValueError(f"Unknown backend {backend!r}. Available: {list(BACKENDS)}")
    return await impl(dossier_md, model)


# ---- validation ------------------------------------------------------------

EXPECTED_BLOCKS = [
    "summary",
    "interject_summary",
    "background",
    "personality",
    "appearance",
    "aspirations",
    "relationships",
    "occupation",
    "skills",
    "speech_style",
]


_END_TAG_RE = re.compile(r"\{%-?\s*end[^%]*?-?%\}")


def repair_prompt(text: str) -> tuple[str, int]:
    """Normalise malformed close tags.

    The `.prompt` format uses only `{% block %}` / `{% endblock %}`. Anything
    that looks like a close tag but isn't exactly `{% endblock %}` (e.g.
    `{% endml block %}`, `{% endsummary %}`, `{% end block %}`) is a recurring
    LLM typo; rewrite all such tags to `{% endblock %}`.

    Returns (repaired_text, n_replacements).
    """
    n = 0

    def sub(m):
        nonlocal n
        if m.group(0) == "{% endblock %}":
            return m.group(0)
        n += 1
        return "{% endblock %}"

    return _END_TAG_RE.sub(sub, text), n


def validate_prompt(text: str) -> list[str]:
    """Parse a synthesised .prompt file as Jinja and check block structure.

    Returns a list of human-readable issues (empty list = file is valid).
    """
    import jinja2
    from jinja2 import nodes

    issues: list[str] = []

    env = jinja2.Environment(autoescape=False)
    try:
        ast = env.parse(text)
    except jinja2.TemplateSyntaxError as e:
        return [f"jinja syntax error at line {e.lineno}: {e.message}"]

    found = [n.name for n in ast.find_all(nodes.Block)]

    missing = [b for b in EXPECTED_BLOCKS if b not in found]
    if missing:
        issues.append(f"missing blocks: {', '.join(missing)}")

    extra = [b for b in found if b not in EXPECTED_BLOCKS]
    if extra:
        issues.append(f"unexpected blocks: {', '.join(extra)}")

    seen = [b for b in found if b in EXPECTED_BLOCKS]
    expected_order = [b for b in EXPECTED_BLOCKS if b in seen]
    if seen != expected_order:
        issues.append(
            f"block order is {seen}, expected {expected_order}"
        )

    dupes = sorted({b for b in found if found.count(b) > 1})
    if dupes:
        issues.append(f"duplicate blocks: {', '.join(dupes)}")

    return issues


# ---- file plumbing ---------------------------------------------------------

def dossier_to_prompt_path(dossier_path: Path) -> Path:
    """somenpc_065.md  ->  somenpc_065.prompt (in same directory)."""
    return dossier_path.with_suffix(".prompt")


def is_synthesizable_dossier(p: Path) -> bool:
    if p.suffix != ".md":
        return False
    if p.name.startswith("_"):  # _unattributed.md, _index.json siblings, etc.
        return False
    return True


async def process_one(dossier_path: Path, backend: str, model: str,
                      overwrite: bool, timeout_s: float) -> tuple[Path, str]:
    out_path = dossier_to_prompt_path(dossier_path)
    if out_path.exists() and not overwrite:
        return out_path, "skipped (exists)"
    text = await asyncio.wait_for(
        synthesize(dossier_path.read_text(encoding="utf-8"), backend, model),
        timeout=timeout_s,
    )
    text, n_repaired = repair_prompt(text)
    out_path.write_text(text + "\n", encoding="utf-8")
    repair_note = f"; repaired {n_repaired} tag(s)" if n_repaired else ""
    issues = validate_prompt(text)
    if issues:
        return out_path, f"wrote {len(text)} chars{repair_note}; INVALID: {'; '.join(issues)}"
    return out_path, f"wrote {len(text)} chars{repair_note}; valid"


async def amain():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("target", help="dossier .md file or directory of dossiers")
    ap.add_argument("--backend", default="claude-agent-sdk",
                    choices=list(BACKENDS),
                    help="LLM backend to use (default: claude-agent-sdk)")
    ap.add_argument("--model", default="claude-opus-4-7",
                    help="model id to pass to the backend")
    ap.add_argument("--overwrite", action="store_true",
                    help="regenerate even if a .prompt file already exists")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap on dossiers processed (useful for dry runs)")
    ap.add_argument("--concurrency", type=int, default=5,
                    help="max dossiers synthesised in parallel (default: 5)")
    ap.add_argument("--timeout", type=float, default=600.0,
                    help="per-character timeout in seconds (default: 600)")
    args = ap.parse_args()

    target = Path(args.target)
    if target.is_file():
        targets = [target]
    elif target.is_dir():
        targets = sorted(p for p in target.iterdir() if is_synthesizable_dossier(p))
    else:
        print(f"target not found: {target}", file=sys.stderr)
        sys.exit(1)

    if args.limit:
        targets = targets[: args.limit]

    from tqdm import tqdm

    print(f"Backend: {args.backend} (model={args.model})")
    print(f"Targets: {len(targets)} (concurrency={args.concurrency})", flush=True)

    sem = asyncio.Semaphore(max(1, args.concurrency))
    counters = {"valid": 0, "invalid": 0, "skipped": 0, "failed": 0, "repaired": 0}
    pbar = tqdm(total=len(targets), desc="synthesizing", unit="npc",
                dynamic_ncols=True)

    async def run(d: Path):
        async with sem:
            pbar.set_postfix_str(f"in-flight: {d.name}", refresh=True)
            try:
                out, status = await process_one(
                    d, args.backend, args.model, args.overwrite, args.timeout
                )
                if "skipped" in status:
                    counters["skipped"] += 1
                elif "INVALID" in status:
                    counters["invalid"] += 1
                else:
                    counters["valid"] += 1
                if "repaired" in status:
                    counters["repaired"] += 1
                tqdm.write(f"  {d.name} -> {out.name}  [{status}]")
            except asyncio.TimeoutError:
                counters["failed"] += 1
                tqdm.write(f"  {d.name} -> FAILED  [timeout after {args.timeout}s]",
                           file=sys.stderr)
            except Exception as e:
                counters["failed"] += 1
                tqdm.write(f"  {d.name} -> FAILED  [{type(e).__name__}: {e}]",
                           file=sys.stderr)
            finally:
                pbar.update(1)
                pbar.set_postfix(counters, refresh=True)

    try:
        await asyncio.gather(*(run(d) for d in targets))
    finally:
        pbar.close()
    print(
        f"Done: {counters['valid']} valid, {counters['invalid']} invalid, "
        f"{counters['skipped']} skipped, {counters['failed']} failed "
        f"({counters['repaired']} auto-repaired)",
        flush=True,
    )


def main():
    asyncio.run(amain())


if __name__ == "__main__":
    main()
