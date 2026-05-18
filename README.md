# SkyrimNet Dialogue Analyser

Turn a Skyrim `.esp` plugin into ready-to-use **SkyrimNet / CHATTERBOX** character bios — one `.prompt` file per NPC, derived from every line of dialogue that NPC speaks in the mod.

The pipeline runs in two stages:

1. **Extract** — parse the ESP, attribute every dialogue line to the NPC who speaks it, and write a self-contained markdown dossier per character.
2. **Synthesise** — hand each dossier to an LLM (Claude Agent SDK by default) and have it write the SkyrimNet bio `.prompt` file in the expected Jinja-block format.

The dossier is the universal LLM-agnostic interface between the two stages, so you can swap synthesiser backends without touching extraction.

> NSFW: generated bios are intended for adult roleplay and are uncensored by design.

---

## Requirements

- **Python 3.10+**
- **Claude Code CLI** installed and authenticated (the synthesis stage uses the Claude Agent SDK, which reuses your existing CLI auth — no separate API key needed)
- Python dependencies:

```bash
pip install -r requirements.txt
```

This pulls in:
- `esplib` — TES5 binary record parser
- `claude-agent-sdk` — Claude synthesis transport

---

## Quick start (GUI)

The easiest path on Windows:

```
run-gui.bat
```

(or `python scripts/gui.py` on any OS)

1. **Browse** to your `.esp` file.
2. The output dir auto-fills to `output/<plugin_name>/`.
3. Click **1. Extract dossiers** — produces one `.md` per NPC.
4. Pick a model and click **2. Synthesize bios** — produces one `.prompt` per dossier.
5. **Open output folder** to grab the `.prompt` files.

Options:
- **Model** — `claude-opus-4-7` (best, default), `claude-sonnet-4-6` (cheaper/faster), `claude-haiku-4-5-20251001` (cheapest).
- **Overwrite** — regenerate `.prompt` files that already exist.
- **Concurrency** — how many NPCs to synthesise in parallel (default 5).
- **Limit** — cap the number of NPCs processed (0 = all). Useful for dry runs.

---

## Quick start (CLI)

```bash
# Stage 1: ESP -> output/<plugin>/<name>_<refid>.md per NPC + _index.json
python scripts/extract_npc_dialogue.py "path/to/YourMod.esp"

# Stage 2 (one character)
python scripts/synthesize_bio.py output/yourmod/somenpc_743.md

# Stage 2 (whole plugin — every dossier in the dir, _unattributed skipped)
python scripts/synthesize_bio.py output/yourmod/
```

Useful flags on `synthesize_bio.py`:

| Flag | Effect |
|---|---|
| `--model claude-sonnet-4-6` | Cheaper/faster model |
| `--overwrite` | Regenerate even if a `.prompt` already exists |
| `--limit 3` | Cap NPCs processed |
| `--concurrency 5` | Parallel synthesis (default 5) |
| `--timeout 600` | Per-NPC timeout in seconds |
| `--backend claude-agent-sdk` | Swap LLM backend |

---

## Where the output lands

Everything goes under `output/<plugin_basename>/`:

```
output/yourmod/
├── _index.json                       — roster summary
├── _unattributed.md                  — dialogue we couldn't pin to an NPC
├── somenpc_065.md                    — stage 1 dossier
├── somenpc_065.prompt                — stage 2 bio (drop this into SkyrimNet)
├── anothernpc_743.md
├── anothernpc_743.prompt
└── ...
```

Filename convention: **`<character_name_lower>_<last3hex>.prompt`**, where the suffix is the last three hex digits of the NPC's RefID (not FormID). This disambiguates NPCs sharing a first name.

### Using the bios in SkyrimNet / CHATTERBOX

Drop each generated `.prompt` file into your SkyrimNet character cards directory. The file is a Jinja template with these blocks (target word counts in parentheses):

- `summary` (75–100, spoiler-free, no name)
- `interject_summary` (50–100)
- `background` (150–450)
- `personality` (100–450)
- `appearance` (75–125, spoiler-free, no name)
- `aspirations` (75–125)
- `relationships` (200–300)
- `occupation` (75–125)
- `skills` (100–150)
- `speech_style` (50–350)

See `examples/prompts/serana_B74.prompt` for a reference card — it's also used as a few-shot example during synthesis.

---

## Repo layout

```
scripts/
  extract_npc_dialogue.py   — stage 1: ESP -> per-NPC .md dossiers
  synthesize_bio.py         — stage 2: dossier -> .prompt via Claude Agent SDK
  gui.py                    — Tkinter wrapper around both stages
examples/
  prompts/serana_B74.prompt — reference bio (also used as a synthesis few-shot)
output/<plugin>/            — generated dossiers and prompts (gitignored)
run-gui.bat                 — Windows launcher for the GUI
requirements.txt
CLAUDE.md                   — deep design notes for the pipeline
```

---

## Notes & limitations

- **External NPCs** (referenced but defined in another plugin / master, e.g. vanilla Serana) get a stub dossier marked `[external]` — masters aren't loaded yet, so race/voice/RefID info isn't resolvable.
- **`_unattributed.md`** collects dialogue we couldn't pin to a speaker. Most leftovers are `SCEN` (scene-driven) dialogue.
- The synthesiser sends a large, identical system prompt per NPC; the Agent SDK prompt-caches it automatically, so per-character cost scales with dossier size, not template size.
- Add a new synthesis backend by writing an `async (dossier_md, model) -> str` function and registering it in the `BACKENDS` dict in `scripts/synthesize_bio.py`.
