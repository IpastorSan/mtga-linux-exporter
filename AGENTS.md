# AGENTS.md — mtg-explorer

Provider-agnostic instructions for coding agents (Claude, Codex,
Cursor, Cline, aider, etc.) working in this project.

## What this project is

A Linux/Proton data-fetching module for MTG Arena. Extracts the
player's owned-card collection by scanning MTGA's running-process
memory. See `README.md` for the user-facing story.

## When to use this tool (triggers)

Invoke `./mtg-fetch` when the user asks to:
- "extract / dump / export my MTG Arena collection"
- "get my card list from MTGA"
- "know what cards I own on Arena"
- Look up a specific card's grpId or metadata
- Verify their MTGA setup is scan-ready

## Runbook: extract a collection

**Prerequisites the user must satisfy — check these before running:**

1. **MTG Arena is running** (Steam app 2141910 via Proton).
2. **They've clicked the Collection tab at least once this session.**
   The collection Dictionary is only allocated in RAM after that.
3. **Ptrace is relaxed:** `sudo sysctl kernel.yama.ptrace_scope=0`.
   The tool's `check` subcommand verifies this.

Run `./mtg-fetch check --format json` first. If any check fails, stop
and surface the JSON to the user with the `hint` field verbatim.

### Anchor cards — you MUST prompt the user for these

The scan finds many `<int,int>` dicts in memory. To pick the real
collection, the user provides **5+ rare or mythic cards they own with
exact copy counts**. Do not guess these — you cannot know what the
user owns.

Prompt the user like this:

> To identify the correct memory block I need 5 rare or mythic cards
> you're 100 % sure you own, with exact copy counts (1–4). Format each
> as `Card Name:count`. Example: `Lyra Dawnbringer:2`.

Validate before running:
- Must be ≥ 5 entries
- Names must be the exact printed English name (case sensitive) — use
  `./mtg-fetch card --name "..."` if unsure
- Counts must be 1–4
- Avoid basic lands, tokens, digital-only cards

### Invoke non-interactively

```bash
./mtg-fetch collect \
    --anchor "Lyra Dawnbringer:2" \
    --anchor "Omniscience:1" \
    --anchor "Dream Eater:1" \
    --anchor "Massacre Wurm:1" \
    --anchor "Vivien Reid:1" \
    --format json --out collection.json --quiet
```

Alternatives:
- `--anchors-file path.json` where the file is
  `{"anchors":[{"name":"...","count":N}, ...]}`
- `--anchors-stdin` and pipe the same JSON via stdin

### Interpret the result

Success criterion: `meta.anchors_matched == meta.anchors_total`.
If less than a full match, **do not silently proceed** — show the
`meta.anchor_breakdown` to the user, ask them to verify their anchor
counts, and rerun. In automated pipelines pass `--strict` so a
mismatch returns exit code 14.

Do not paraphrase the meta block — it contains the address the block
was found at, which is useful for debugging.

## Runbook: card lookup

For a single card:
```bash
./mtg-fetch card --name "Fatal Push" --format json
./mtg-fetch card --grp-id 68499 --format json
```

Returns a JSON list of all printings (base + alt-art). `is_primary`
distinguishes the standard printing from alternate frames. JSON output
includes the full local record (mana cost, type line, oracle text,
power/toughness, colors, artist, etc.).

## Runbook: enrich a collection

```bash
./mtg-fetch enrich collection.json --out enriched.json
```

Adds the full local card model to every entry (mana cost, oracle text,
type line, colors, power/toughness, flavor, artist, art_id, digital-
only/rebalanced flags) and then hits Scryfall for the two things MTGA's
DB does not have:

- `image_uris` (small/normal/large/png/art_crop/border_crop URLs; per-
  face for double-faced cards under `card_faces_image_uris`)
- `formats_legal` — filtered to the six MTGA formats: `standard`,
  `alchemy`, `historic`, `explorer`, `timeless`, `standardbrawl`,
  `brawl` (Historic Brawl)

Scryfall responses are cached under `~/.cache/mtg-explorer/scryfall/`
as `<grp_id>.json`. Re-runs of `enrich` on the same collection make
zero network calls. Cards missing from Scryfall's Arena index get a
`{"_missing": true}` tombstone so they aren't re-fetched.

Flags:
- `--no-scryfall`: local-only enrichment (no images, no legality) —
  fully offline, instantaneous.
- `--quiet`: suppress progress lines on stderr.

Input accepted: either `./mtg-fetch collect`'s `{meta, cards:[...]}`
output, or a bare JSON list of `{grp_id, ...}` records. `count` and any
extra input fields are preserved.

Stats land under `meta.enrichment` — check `scryfall.missing` to spot
unresolvable grp_ids (usually alt-art rebalanced Alchemy cards).

**Expect the first run to be slow.** Scryfall asks for a 50–100 ms
delay between requests, so 2000+ cards takes 3–5 minutes end-to-end.
Subsequent runs are near-instant (all cache hits). If the process is
interrupted, cached cards persist — re-running only fetches the
remainder.

To watch progress in another terminal:
```bash
ls ~/.cache/mtg-explorer/scryfall/ | wc -l
```
Compare against `unique_cards` in `meta` — that's how many are left.

## Runbook: prerequisite check

```bash
./mtg-fetch check --format json
```

Returns a `{ok, checks}` payload. Use to gate any collect call.

## Output contract

All subcommands:
- Data → stdout
- Human/log messages → stderr
- JSON is default for machine consumption; CSV and JSONL are opt-in
- Exit codes are stable and distinct per failure mode

### Exit codes

| Code | Meaning                                              | Recovery |
|-----:|------------------------------------------------------|----------|
|   0  | success                                              | —        |
|   2  | usage / bad args                                     | Fix flags |
|  10  | MTGA not running or prereq failed                    | Ask user to launch MTGA + open Collection tab |
|  11  | `/proc/*/mem` unreadable                             | `sudo sysctl kernel.yama.ptrace_scope=0` |
|  12  | Missing/invalid anchors                              | Prompt user again |
|  13  | No candidate memory block found                      | Ask user to open Collection tab, retry with `--min-block 20` |
|  14  | `--strict`: anchors didn't fully match               | Show `meta.anchor_breakdown`, ask user to verify |
|  15  | `enrich`: input file missing or malformed            | Point user at a valid JSON |
|  16  | `enrich`: Scryfall network failure                   | Retry, or use `--no-scryfall` for offline only |

## Do

- Always run `check` before `collect` on a fresh session.
- Ask the user for anchors; never guess or default them.
- Preserve `meta` verbatim when relaying results.
- Suggest `--quiet` when embedding the tool in a pipeline.

## Do not

- Reintroduce log parsing for the collection. `PlayerInventory.GetPlayerCardsV3`
  was removed by WOTC in Aug 2021 and never replaced. Any hint that
  "just parse the log" is a dead lead — see README §"Why memory scanning".
- Attempt mitmproxy for the collection. The wire has no collection
  payload either.
- Install anything into the Proton prefix, patch `Assembly-CSharp.dll`,
  or rely on Untapped/Companion tools. Passive memory reads only.
- Download Scryfall's bulk file. MTGA ships its own SQLite DB at
  `MTGA_Data/Downloads/Raw/Raw_CardDatabase_*.mtga`; `card_db.py` reads
  it. Scryfall is only touched by the `enrich` subcommand, and only
  through the per-card `/cards/arena/<grp_id>` endpoint (with local
  caching) — for image URIs and format legality, which MTGA's DB
  omits. Do not add other Scryfall paths.
- Save anchor cards to disk automatically. Let the user decide.

## Files

- `mtg-fetch` — executable entry
- `cli.py` — argparse dispatcher, output formatting
- `memory_scan.py` — `/proc/pid/mem` reader + Dictionary/packed scanners
- `anchors.py` — anchor parsing, prompting, scoring
- `card_db.py` — MTGA SQLite reader (full card model)
- `scryfall.py` — cached Scryfall client (`enrich` only)
- `README.md` — user-facing docs
- `AGENTS.md` — this file (agent instructions)
- `.claude.md` — Claude-specific pointer to this file

## Downstream module (planned)

A separate processing module will consume the JSON output of
`./mtg-fetch collect` and add legality filters (Standard, Historic,
etc.), deck-builder exports (Moxfield, MTGGoldfish, Untapped), and
craft recommendations. That module will reuse `card_db.py`; do not
duplicate the SQLite join logic there.

## Coding conventions

- Python 3.10+, stdlib only. No `pip install`.
- Terse code, no defensive scaffolding for hypothetical futures.
- Comments only when the WHY is non-obvious.
- Match user scope: don't add speculative features.
