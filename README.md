# mtg-explorer

Extract data from a running **MTG Arena** client on **Linux/Proton**.

This is the *data-fetching* module. Processing (deck-builder export,
Standard-legality filtering, format-legal subsets, etc.) will live in a
separate module.

## What it does

- `mtg-fetch check` — verify prerequisites
- `mtg-fetch collect` — dump your **owned-card collection** as JSON/CSV
- `mtg-fetch card` — look up a card by name or grpId in MTGA's local DB
- `mtg-fetch enrich` — attach the **full card model** to a collection
  (mana cost, oracle text, colors, P/T, images, MTGA format legality)

The collection is extracted by **scanning the live MTGA process's
memory** for the Mono `Dictionary<int,int>` that maps `grpId → owned
count`. Full rationale below in *Why memory scanning*. Enrichment pulls
everything else from MTGA's own SQLite DB, and only reaches Scryfall
for the two things MTGA doesn't ship: image URLs and format legality.

---

## Requirements

- Linux with Steam + Proton (tested: Arch/Omarchy + Proton Experimental)
- MTG Arena installed at `~/.steam/steam/steamapps/common/MTGA/` (Steam App ID `2141910`)
- Python 3.10+ (stdlib only, no `pip install` needed)
- One-time: relax the kernel's ptrace restriction so we can read `/proc/<pid>/mem`:
  ```bash
  sudo sysctl kernel.yama.ptrace_scope=0
  ```
  Persist across reboots by adding `kernel.yama.ptrace_scope = 0` to
  `/etc/sysctl.d/10-ptrace.conf`. Alternatively, run `mtg-fetch collect`
  with `sudo` — the memory read is passive, but the tool never modifies
  MTGA.

---

## Quick start

```bash
# 1. Launch MTGA via Steam, log in, click into the Collection tab once.
#    (This ensures the collection dict is populated in RAM.)

# 2. Verify prerequisites.
./mtg-fetch check

# 3. Extract collection — you'll be prompted for 5 anchor cards.
./mtg-fetch collect --format csv --out collection.csv

# Non-interactive (LLM / script friendly):
./mtg-fetch collect \
    --anchor "Lyra Dawnbringer:2" \
    --anchor "Omniscience:1" \
    --anchor "Dream Eater:1" \
    --anchor "Massacre Wurm:1" \
    --anchor "Vivien Reid:1" \
    --format json --out collection.json --quiet
```

Output JSON (see [`examples/collection.sample.json`](examples/collection.sample.json) for a full 5-card sample):
```json
{
  "meta": {
    "pid": 109427,
    "address": "0x241036020",
    "kind": "dict",
    "unique_cards": 2227,
    "total_copies": 3814,
    "anchors_matched": 5,
    "anchors_total": 5,
    "anchor_breakdown": [
      {"name": "Lyra Dawnbringer", "expected": 2, "found": 2, "status": "ok"},
      ...
    ]
  },
  "cards": [
    {"grp_id": 9135, "count": 1, "name": "Mind Stone", "set": "WTH",
     "collector_number": "153", "rarity": "common", "is_primary": true},
    ...
  ]
}
```

### Enrich the collection

`collect` produces a lean record per card. To attach the full model —
mana cost, oracle text, colors, power/toughness, images, MTGA format
legality — pipe it through `enrich`:

```bash
./mtg-fetch enrich collection.json --out enriched.json
```

First run takes 3–5 minutes (Scryfall asks for a ~100 ms delay between
requests). Responses cache to `~/.cache/mtg-explorer/scryfall/` — re-runs
are near-instant, and if the process is interrupted the cached cards
persist so re-running only fetches the remainder. `--no-scryfall` stays
fully offline (local fields only, no images, no legality).

Each enriched card gains (see [`examples/enriched.sample.json`](examples/enriched.sample.json) for the full sample, including a double-faced card):
```json
{
  "grp_id": 67156, "name": "Lyra Dawnbringer", "count": 1,
  "mana_cost": "{3}{W}{W}", "mana_value": 5,
  "type_line": "Legendary Creature — Angel",
  "types": ["Creature"], "subtypes": ["Angel"], "supertypes": ["Legendary"],
  "colors": ["W"], "color_identity": ["W"],
  "power": "5", "toughness": "5",
  "oracle_text": "Flying\nFirst strike\nLifelink\nOther Angels ...",
  "flavor_text": "\"You are not alone. You never were.\"",
  "artist": "Chris Rahn",
  "image_uris": { "png": "https://cards.scryfall.io/png/...", ... },
  "formats_legal": ["alchemy", "brawl", "historic", "standard", "timeless"],
  "scryfall_id": "93be6799-7b9d-44d4-84dc-2961692b5a85"
}
```

Watch progress in another terminal:
```bash
ls ~/.cache/mtg-explorer/scryfall/ | wc -l
```

---

## Why anchor cards?

MTGA's managed heap contains many `Dictionary<int,int>` maps beyond the
collection — vault-progress caches, draft records, event scores, etc.
Any of them can look like a plausible collection at first glance.

**Anchors are 5+ rare/mythic cards you're 100 % certain you own, with
exact copy counts.** The scanner picks the candidate block whose values
for those specific cards match your stated counts. False positives on
random `<int,int>` maps effectively can't survive 5 simultaneous exact
matches.

Cards with multiple printings are handled automatically — the tool sums
the count across every printing's grpId. E.g. "Lyra Dawnbringer:2" is
satisfied by 2× DAR, 2× FDN, or 1× DAR + 1× FDN.

**Good anchor picks:** rare or mythic cards, not tokens, not
digital-only. Names must match the exact printed English name
(case-sensitive) — the tool reads MTGA's own enUS localization table.

---

## CLI reference

```
mtg-fetch check    [--format text|json]
mtg-fetch collect  [--pid PID]
                   [--anchor "NAME:COUNT"]... | [--anchors-file PATH]
                                              | [--anchors-stdin]
                                              | (interactive prompt)
                   [--format json|jsonl|csv]  [--out PATH]
                   [--min-block N] [--max-region-mb N]
                   [--strict] [--quiet]
mtg-fetch card     [--name NAME] | [--grp-id N]  [--format text|json]
mtg-fetch enrich   <input.json>  [--out PATH] [--no-scryfall] [--quiet]
```

### Exit codes

| Code | Meaning                                                    |
|-----:|------------------------------------------------------------|
|    0 | success                                                    |
|    2 | usage / invalid arguments                                  |
|   10 | MTGA not running or a prerequisite check failed            |
|   11 | cannot read `/proc/<pid>/mem` (ptrace or perms)            |
|   12 | anchors missing or invalid                                 |
|   13 | scan produced no candidate blocks                          |
|   14 | `--strict` was set and anchor scores didn't all match      |
|   15 | `enrich`: input file missing or malformed                  |
|   16 | `enrich`: Scryfall unreachable after retries               |

### LLM-friendly usage

- Default `collect` output is JSON with metadata.
- Human text goes to stderr; data goes to stdout.
- Non-zero exit codes on every failure mode, distinct per cause.
- Interactive prompt is skipped when stdin is not a TTY — pass anchors
  via `--anchor`, `--anchors-file`, or `--anchors-stdin` instead.
- Add `--quiet` to suppress the stderr progress ticker.
- Add `--strict` if the caller wants a hard failure on anchor mismatch.

---

## Why memory scanning? (the story)

We tried three log/network-based approaches first. All dead:

### Attempt 1 — parse `Player.log` for `PlayerInventory.GetPlayerCardsV3`

This is the widely-cited "read MTGA's logs for the collection" trick.
`Player.log` in `AppData/LocalLow/.../MTGA/` on Windows historically
contained a `GetPlayerCardsV3` payload — a JSON dict of
`{arena_id: count}`. On Linux Proton it lives in the equivalent path
inside the Proton prefix.

**Reality:** WOTC removed `GetPlayerCardsV3` from the API in
**August 2021** and never replaced it. The endpoint doesn't exist on
the wire or in logs anymore. Verified against `mtgatool-desktop`'s
`InStartHook.ts` type and `manasight`'s 2026 log format guide:
> *"the player's card collection (owned cards) does not appear in any
> `==>` / `<==` API call in current logs"*
> — https://blog.manasight.gg/arena-log-format-guide/

Additionally, on Linux/Proton the file we initially assumed was the
gameplay log (`AppData/LocalLow/Wizards Of The Coast/MTGA/Player.log`)
is actually Unity's engine crash log. The real MTGA RPC log is in the
game install itself:
`~/.steam/steam/steamapps/common/MTGA/MTGA_Data/Logs/Logs/UTC_Log - *.log`.
Neither log contains owned-cards data now.

### Attempt 2 — mitmproxy the game's HTTPS traffic

Plan: install a mitmproxy CA into the Proton prefix's Wine cert store,
route MTGA through the proxy, decode the collection request.

**Reality:** MTGA doesn't just fail to log the collection — it doesn't
ask the server for it either. Login (`StartHook`) returns
gold/gems/wildcards/vault progress/deck definitions but **not the owned
cards list**. `riQQ/MtgaProto`'s protobuf schema (auto-generated from
`Wizards.MDN.GreProtobuf.Unity.dll`) has zero collection messages.
Even bypassing MTGA's TLS pinning in `TcpConnection.ProcessConnect` via
a dnSpyEx patch of `Assembly-CSharp.dll` — an established MTGate
technique — would show no collection payload on the wire.

The client keeps the collection locally, syncing only *deltas* (packs
opened, cards crafted) up to WOTC.

### Attempt 3 — Untapped.gg Companion under Wine

Works, but is a third-party Windows binary hooking a running Wine
process and uploading to a cloud service. Rejected: we want local
data, no external dependencies.

### What worked — scan the running MTGA process's memory

Ported from [NthPhantom10/MTGA-collection-exporter](https://github.com/NthPhantom10/MTGA-collection-exporter)
(Windows/pymem) to Linux/Proton via `/proc/<pid>/mem`. On the first
attempt it produced 2,227 unique cards, 3,814 copies, with all 5 anchor
cards matching exactly — Lyra Dawnbringer even split correctly as
1× DAR + 1× FDN across two different printing grpIds. To our knowledge
this is the first published Linux port of the approach.

**How it works:**

1. **Locate the process.** `pgrep` on `comm == "MTGA.exe"` picks the
   Wine child, not a Proton wrapper. If multiple, use largest RSS.
2. **Enumerate memory.** Parse `/proc/<pid>/maps`, keep `rw-p`
   anonymous regions and `[heap]`. Skip shared libs, vDSO, vvar.
3. **Scan for two patterns.**
   - **Mono `Dictionary<int,int>` entries** (16 bytes each):
     `(hashCode, next, key, value)`. For `int` keys,
     `int.GetHashCode()` returns the value itself, so `hashCode == key`
     — a very distinctive marker. `next` is either `-1` (0xFFFFFFFF) or
     a small non-negative index. We match runs of ≥40 such entries with
     `key ∈ [1000, 500000)` and `value ∈ [1, 400]`.
   - **Packed `(key, value)` pairs** (8 bytes each) — fallback for
     alternative storage shapes.
4. **Score by anchors.** Each candidate block gets a match count:
   how many anchor names have `sum(block[gid] for gid in printings) ==
   expected_count`. Higher = more likely the real collection.
5. **Emit best block.** Join `grp_id` against MTGA's own SQLite card DB
   at `MTGA_Data/Downloads/Raw/Raw_CardDatabase_*.mtga` for names/sets/
   collector numbers/rarity. No network calls.

Reading the region bytes into an `array.array("I")` for C-level bulk
u32 unpacking cut scan time from tens of minutes (naive Python
list-of-ints) to well under a minute on a 2.5 GB address space.

---

## Files

| File               | Purpose                                             |
|--------------------|-----------------------------------------------------|
| `mtg-fetch`        | executable entry point                              |
| `cli.py`           | argparse subcommands, output formatting             |
| `memory_scan.py`   | `/proc/pid/mem` scan for Dictionary/packed blocks   |
| `anchors.py`       | anchor parsing, prompting, scoring                  |
| `card_db.py`       | wrapper around MTGA's bundled SQLite card DB        |
| `scryfall.py`      | cached Scryfall client (used by `enrich` only)      |
| `README.md`        | this file (user-facing docs)                        |
| `AGENTS.md`        | agent runbook (Claude / Codex / Cursor / etc.)      |
| `.claude.md`       | thin pointer to `AGENTS.md`                         |

---

## Known limitations

- Requires MTGA to be running and to have loaded the Collection tab at
  least once this session (so the dict is resident in RAM).
- Wine-Mono heap layout could theoretically differ from native .NET;
  the current heuristic worked first try on Proton Experimental, but
  future Proton or MTGA updates might require tuning `--min-block` or
  the range constants in `memory_scan.py`.
- Cards with multiple owned printings show as separate rows keyed on
  grpId — the join-by-name is left to downstream processing.
- MTGA's local card DB is only as fresh as your last game update. If
  you own something released the day of extraction that MTGA hasn't
  synced its DB for yet, the grpId will appear with an empty name.
- `enrich`'s Scryfall lookup is keyed on `arena_id`. For very recent
  sets (e.g. the day the Marvel's Spider-Man crossover dropped) Scryfall
  may not have populated the arena_id mapping yet — those cards land in
  `meta.enrichment.scryfall.missing` and get local fields only until
  Scryfall backfills.

---

## References

**Collection extraction:**
- [NthPhantom10/MTGA-collection-exporter](https://github.com/NthPhantom10/MTGA-collection-exporter) — Windows pymem reference implementation
- [MTGate](https://github.com/MTGate/MTGate) — reverse-engineered MTGA client protocol
- [riQQ/MtgaProto](https://github.com/riQQ/MtgaProto) — auto-generated protobuf schema
- [manasight arena log format guide 2026](https://blog.manasight.gg/arena-log-format-guide/) — authoritative statement that owned-cards is no longer in logs/API
- [mtgatool-desktop](https://github.com/mtgatool/mtgatool-desktop) — actively maintained TS log parser

**Enrichment:**
- [Scryfall API](https://scryfall.com/docs/api) — image URIs and format legality (per-card `/cards/arena/<arena_id>` endpoint)
- MTGA's own bundled SQLite card DB at `MTGA_Data/Downloads/Raw/Raw_CardDatabase_*.mtga` — everything else (mana cost, oracle text, colors, P/T, artist, etc.)

---

## Legal

> mtga-linux-exporter is unofficial Fan Content permitted under the
> [Wizards of the Coast Fan Content Policy](https://company.wizards.com/en/legal/fancontentpolicy).
> Not approved/endorsed by Wizards. Portions of the materials used are
> property of Wizards of the Coast. ©Wizards of the Coast LLC.

**Magic: The Gathering** and **MTG Arena** are trademarks of Wizards of
the Coast LLC. This project is **not affiliated with, endorsed by, or
sponsored by** Wizards of the Coast or Hasbro. No Wizards trademarks,
logos, mana symbols, or card art assets are distributed in this
repository — card metadata is read at runtime from the user's own
installed copy of MTG Arena, and card images are fetched from Scryfall.

Downstream users of the enriched output must follow
[Scryfall's data & image guidelines](https://scryfall.com/docs/api)
(don't modify or crop card images, don't obscure artist credits, don't
imply Scryfall endorsement).

See [DISCLAIMER.md](DISCLAIMER.md) for the full notice.
