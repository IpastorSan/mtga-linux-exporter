"""Anchor cards: user-known (name, count) pairs used to disambiguate the
real collection dictionary from other <int,int> maps in MTGA's heap.

Sources supported:
  - CLI flags: --anchor "Name:count" (repeatable)
  - JSON file: --anchors-file path.json  ({"anchors":[{"name":..,"count":..},...]})
  - JSON stdin: --anchors-stdin (same schema, read from stdin)
  - Interactive TTY prompt (last resort)

Every anchor resolves to *all* printings of that card name (a set of
grpIds). Scoring sums counts across the set — the user rarely knows
which printing they own for reprinted cards.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from card_db import CardDB

MIN_ANCHORS = 5


@dataclass
class Anchor:
    name: str
    expected_count: int
    grp_ids: set[int] = field(default_factory=set)


class AnchorError(ValueError):
    pass


def _resolve_one(db: CardDB, name: str, count: int) -> Anchor:
    if count < 1 or count > 4:
        raise AnchorError(f"invalid count {count} for {name!r}: must be 1-4")
    cards = db.by_name(name)
    if not cards:
        raise AnchorError(
            f"card not found in local DB: {name!r}. Names must match the "
            "printed English card name exactly (case-sensitive)."
        )
    grps = {c.grp_id for c in cards}
    return Anchor(name=name, expected_count=count, grp_ids=grps)


def from_pairs(db: CardDB, pairs: list[tuple[str, int]]) -> list[Anchor]:
    return [_resolve_one(db, n, c) for n, c in pairs]


def parse_flag(spec: str) -> tuple[str, int]:
    """Parse `--anchor "Lyra Dawnbringer:2"` into (name, count)."""
    if ":" not in spec:
        raise AnchorError(f"expected NAME:COUNT, got {spec!r}")
    name, _, count = spec.rpartition(":")
    name = name.strip()
    try:
        n = int(count.strip())
    except ValueError:
        raise AnchorError(f"count is not an integer in {spec!r}")
    if not name:
        raise AnchorError(f"empty card name in {spec!r}")
    return name, n


def from_json(db: CardDB, data: dict) -> list[Anchor]:
    if not isinstance(data, dict) or "anchors" not in data:
        raise AnchorError("JSON must be an object with an 'anchors' array")
    pairs: list[tuple[str, int]] = []
    for a in data["anchors"]:
        if not isinstance(a, dict) or "name" not in a or "count" not in a:
            raise AnchorError(f"anchor entry missing name/count: {a}")
        pairs.append((str(a["name"]), int(a["count"])))
    return from_pairs(db, pairs)


def from_file(db: CardDB, path: Path) -> list[Anchor]:
    return from_json(db, json.loads(path.read_text()))


def from_stdin(db: CardDB) -> list[Anchor]:
    return from_json(db, json.loads(sys.stdin.read()))


def prompt_interactive(db: CardDB) -> list[Anchor]:
    """Ask the user for anchor cards on an interactive TTY."""
    print(
        f"\nEnter at least {MIN_ANCHORS} rare/mythic cards you're CERTAIN "
        "you own,\nwith their exact copy counts. These anchors let the "
        "scanner\nidentify the real collection out of many candidate "
        "in-memory maps.\n\n"
        "Format:  <card name>:<count>       (e.g.  Lyra Dawnbringer:2)\n"
        "Type an empty line when done.\n",
        file=sys.stderr,
    )
    anchors: list[Anchor] = []
    seen: set[str] = set()
    while True:
        prompt_label = f"anchor #{len(anchors)+1}> "
        try:
            line = input(prompt_label).strip()
        except EOFError:
            break
        if not line:
            if len(anchors) < MIN_ANCHORS:
                print(
                    f"need at least {MIN_ANCHORS} anchors, have {len(anchors)}.",
                    file=sys.stderr,
                )
                continue
            break
        try:
            name, count = parse_flag(line)
            if name in seen:
                print(f"already added {name!r}, skipping.", file=sys.stderr)
                continue
            a = _resolve_one(db, name, count)
        except AnchorError as e:
            print(f"error: {e}", file=sys.stderr)
            continue
        anchors.append(a)
        seen.add(name)
        print(
            f"  OK  {name}: {count} (printings: {sorted(a.grp_ids)})",
            file=sys.stderr,
        )
    return anchors


def score(block: dict[int, int], anchors: list[Anchor]) -> tuple[int, list[dict]]:
    """Return (matched_count, per-anchor breakdown for reporting)."""
    matched = 0
    breakdown: list[dict] = []
    for a in anchors:
        found = sum(block[gid] for gid in a.grp_ids if gid in block)
        status = "ok" if found == a.expected_count and found > 0 else (
            "miss" if found == 0 else "bad"
        )
        if status == "ok":
            matched += 1
        breakdown.append({
            "name": a.name,
            "expected": a.expected_count,
            "found": found,
            "status": status,
        })
    return matched, breakdown
