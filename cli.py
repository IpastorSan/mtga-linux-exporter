#!/usr/bin/env python3
"""mtg-fetch — extract data from a running MTG Arena client on Linux/Proton.

Subcommands:
  check        Verify prerequisites (MTGA running, ptrace scope, DB present).
  collect      Extract the owned-card collection via process-memory scan.
  card         Look up a card in MTGA's local database by name or grpId.

All data output goes to stdout. Human status messages go to stderr.
Use `--format json` (default for `collect`) for machine consumption.
Non-zero exit code always indicates failure — see codes at the bottom.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path
from typing import Any

import anchors as anchors_mod
from anchors import AnchorError
from card_db import RARITY_NAMES, CardDB
from memory_scan import (
    DEFAULT_MIN_BLOCK,
    find_mtga_pid,
    scan_process,
)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NO_MTGA = 10
EXIT_PERMISSION = 11
EXIT_NO_ANCHORS = 12
EXIT_NO_CANDIDATE = 13
EXIT_ANCHOR_MISMATCH = 14
EXIT_ENRICH_INPUT = 15
EXIT_SCRYFALL = 16


# ---------- shared helpers ----------

def _log(msg: str, *, quiet: bool = False) -> None:
    if not quiet:
        print(msg, file=sys.stderr)


def _load_db(quiet: bool = False) -> CardDB:
    try:
        db = CardDB()
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(EXIT_USAGE)
    _log(f"loaded local card DB: {len(db.all_grp_ids())} non-token grpIds", quiet=quiet)
    return db


def _card_dict(card, count: int | None = None, *, full: bool = False) -> dict[str, Any]:
    d: dict[str, Any] = {
        "grp_id": card.grp_id,
        "name": card.name,
        "set": card.set,
        "collector_number": card.collector_number,
        "rarity": RARITY_NAMES.get(card.rarity, str(card.rarity)),
        "is_primary": card.is_primary,
    }
    if count is not None:
        d["count"] = count
    if full:
        d.update({
            "mana_cost": card.mana_cost,
            "mana_value": card.mana_value,
            "type_line": card.type_line,
            "types": list(card.types),
            "subtypes": list(card.subtypes),
            "supertypes": list(card.supertypes),
            "colors": list(card.colors),
            "color_identity": list(card.color_identity),
            "power": card.power,
            "toughness": card.toughness,
            "oracle_text": card.oracle_text,
            "flavor_text": card.flavor_text,
            "artist": card.artist,
            "art_id": card.art_id,
            "is_digital_only": card.is_digital_only,
            "is_rebalanced": card.is_rebalanced,
        })
    return d


# ---------- subcommand: check ----------

def cmd_check(args: argparse.Namespace) -> int:
    report: dict[str, Any] = {"ok": True, "checks": {}}

    # MTGA process
    pid = find_mtga_pid()
    report["checks"]["mtga_running"] = {
        "ok": pid is not None,
        "pid": pid,
    }
    if pid is None:
        report["ok"] = False

    # ptrace scope
    try:
        scope = int(open("/proc/sys/kernel/yama/ptrace_scope").read().strip())
    except OSError:
        scope = None
    ptrace_ok = scope == 0 or os.geteuid() == 0
    report["checks"]["ptrace_scope"] = {
        "ok": ptrace_ok,
        "value": scope,
        "hint": None if ptrace_ok else (
            "run `sudo sysctl kernel.yama.ptrace_scope=0` "
            "or invoke this CLI with sudo"
        ),
    }
    if not ptrace_ok:
        report["ok"] = False

    # Card DB
    try:
        db = CardDB()
        report["checks"]["card_db"] = {
            "ok": True,
            "grp_ids": len(db.all_grp_ids()),
        }
        db.close()
    except FileNotFoundError as e:
        report["checks"]["card_db"] = {"ok": False, "error": str(e)}
        report["ok"] = False

    # /proc/pid/mem readability spot check
    if pid is not None and ptrace_ok:
        try:
            fd = os.open(f"/proc/{pid}/mem", os.O_RDONLY)
            os.close(fd)
            report["checks"]["mem_readable"] = {"ok": True}
        except OSError as e:
            report["checks"]["mem_readable"] = {"ok": False, "error": str(e)}
            report["ok"] = False

    if args.format == "json":
        json.dump(report, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        for name, info in report["checks"].items():
            status = "OK  " if info.get("ok") else "FAIL"
            extra = " ".join(f"{k}={v}" for k, v in info.items() if k != "ok")
            print(f"  [{status}] {name}   {extra}")
        print(f"\noverall: {'OK' if report['ok'] else 'FAIL'}")

    return EXIT_OK if report["ok"] else EXIT_NO_MTGA


# ---------- subcommand: card ----------

def cmd_card(args: argparse.Namespace) -> int:
    db = _load_db(quiet=True)
    results = []
    if args.grp_id is not None:
        c = db.by_grp_id(args.grp_id)
        results = [c] if c else []
    elif args.name:
        results = db.by_name(args.name)
    else:
        print("error: pass --name or --grp-id", file=sys.stderr)
        return EXIT_USAGE

    if not results:
        print("[]" if args.format == "json" else "no matches", file=sys.stdout)
        return EXIT_OK

    if args.format == "json":
        json.dump([_card_dict(c, full=True) for c in results], sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        for c in results:
            print(
                f"{c.grp_id:>7}  {c.set:<5}  {c.collector_number:<6}  "
                f"{RARITY_NAMES.get(c.rarity, c.rarity):<8}  "
                f"{'PRIMARY' if c.is_primary else 'alt    '}  {c.name}"
            )
    return EXIT_OK


# ---------- subcommand: collect ----------

def _gather_anchors(args: argparse.Namespace, db: CardDB) -> list:
    if args.anchor:
        pairs = [anchors_mod.parse_flag(s) for s in args.anchor]
        return anchors_mod.from_pairs(db, pairs)
    if args.anchors_file:
        return anchors_mod.from_file(db, Path(args.anchors_file))
    if args.anchors_stdin:
        return anchors_mod.from_stdin(db)
    if sys.stdin.isatty() and sys.stderr.isatty():
        return anchors_mod.prompt_interactive(db)
    raise AnchorError(
        "no anchors provided. Use --anchor NAME:COUNT (repeatable), "
        "--anchors-file PATH, or --anchors-stdin, or run interactively."
    )


def _emit_collection(
    block: dict[int, int],
    db: CardDB,
    fmt: str,
    out: Path | None,
    metadata: dict,
) -> None:
    rows = []
    for grp, count in sorted(block.items()):
        card = db.by_grp_id(grp)
        if card is None:
            rows.append({"grp_id": grp, "count": count, "name": None,
                         "set": None, "collector_number": None,
                         "rarity": None, "is_primary": None})
        else:
            rows.append(_card_dict(card, count))

    if fmt == "csv":
        stream = open(out, "w", newline="", encoding="utf-8") if out else sys.stdout
        try:
            w = csv.writer(stream)
            w.writerow(["grp_id", "count", "name", "set", "collector_number",
                        "rarity", "is_primary"])
            for r in rows:
                w.writerow([
                    r["grp_id"], r["count"], r["name"] or "",
                    r["set"] or "", r["collector_number"] or "",
                    r["rarity"] or "", int(bool(r["is_primary"])),
                ])
        finally:
            if out:
                stream.close()
    elif fmt == "jsonl":
        stream = open(out, "w", encoding="utf-8") if out else sys.stdout
        try:
            for r in rows:
                stream.write(json.dumps(r) + "\n")
        finally:
            if out:
                stream.close()
    else:  # json
        payload = {"meta": metadata, "cards": rows}
        stream = open(out, "w", encoding="utf-8") if out else sys.stdout
        try:
            json.dump(payload, stream, indent=2)
            stream.write("\n")
        finally:
            if out:
                stream.close()


def cmd_collect(args: argparse.Namespace) -> int:
    db = _load_db(quiet=args.quiet)

    try:
        anchors = _gather_anchors(args, db)
    except AnchorError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_NO_ANCHORS

    if len(anchors) < anchors_mod.MIN_ANCHORS:
        print(
            f"error: need at least {anchors_mod.MIN_ANCHORS} anchors, "
            f"got {len(anchors)}",
            file=sys.stderr,
        )
        return EXIT_NO_ANCHORS

    pid = args.pid or find_mtga_pid()
    if pid is None:
        print("error: MTGA.exe process not found — launch MTGA first",
              file=sys.stderr)
        return EXIT_NO_MTGA
    _log(f"scanning MTGA pid={pid}", quiet=args.quiet)

    def progress(idx: int, total: int) -> None:
        if args.quiet:
            return
        if idx % 100 == 0 or idx == total - 1:
            print(f"  region {idx+1}/{total}", file=sys.stderr, end="\r")

    try:
        candidates = scan_process(
            pid,
            min_block=args.min_block,
            max_region_bytes=args.max_region_mb * 1024 * 1024,
            progress=progress if not args.quiet else None,
        )
    except PermissionError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_PERMISSION

    if not args.quiet:
        print("", file=sys.stderr)  # clear \r

    if not candidates:
        print(
            "error: no candidate blocks found. Open MTGA's Collection tab, "
            "then retry. Try --min-block 20 if still empty.",
            file=sys.stderr,
        )
        return EXIT_NO_CANDIDATE

    scored = []
    for c in candidates:
        matched, breakdown = anchors_mod.score(c.block, anchors)
        scored.append((matched, c.entries, c, breakdown))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)

    top_n = min(5, len(scored))
    _log(f"found {len(scored)} candidates. top {top_n}:", quiet=args.quiet)
    for rank, (matched, size, c, breakdown) in enumerate(scored[:top_n], 1):
        _log(
            f"  #{rank}  kind={c.kind}  entries={size}  "
            f"anchors={matched}/{len(anchors)}  @0x{c.address:x}",
            quiet=args.quiet,
        )

    best_matched, best_size, best_c, best_breakdown = scored[0]

    if best_matched < len(anchors) and args.strict:
        print(
            f"error: strict mode — best block matched only "
            f"{best_matched}/{len(anchors)} anchors",
            file=sys.stderr,
        )
        return EXIT_ANCHOR_MISMATCH

    if best_matched == 0:
        print(
            "warning: 0 anchors matched. Result is a guess.",
            file=sys.stderr,
        )

    metadata = {
        "pid": pid,
        "address": f"0x{best_c.address:x}",
        "kind": best_c.kind,
        "unique_cards": best_size,
        "total_copies": sum(best_c.block.values()),
        "anchors_matched": best_matched,
        "anchors_total": len(anchors),
        "anchor_breakdown": best_breakdown,
    }

    out_path = Path(args.out) if args.out else None
    _emit_collection(best_c.block, db, args.format, out_path, metadata)

    if out_path and not args.quiet:
        _log(
            f"wrote {out_path}: {best_size} unique / "
            f"{sum(best_c.block.values())} copies, "
            f"{best_matched}/{len(anchors)} anchors matched",
            quiet=False,
        )
    return EXIT_OK


# ---------- subcommand: enrich ----------

def cmd_enrich(args: argparse.Namespace) -> int:
    src = Path(args.input)
    if not src.exists():
        print(f"error: input file not found: {src}", file=sys.stderr)
        return EXIT_ENRICH_INPUT
    try:
        raw = json.loads(src.read_text())
    except json.JSONDecodeError as e:
        print(f"error: {src} is not valid JSON: {e}", file=sys.stderr)
        return EXIT_ENRICH_INPUT

    # Accept either {meta, cards:[...]} or a bare list of records.
    if isinstance(raw, dict) and "cards" in raw:
        meta = dict(raw.get("meta") or {})
        cards_in = raw["cards"] or []
    elif isinstance(raw, list):
        meta = {}
        cards_in = raw
    else:
        print("error: expected object with 'cards' or a list of card records",
              file=sys.stderr)
        return EXIT_ENRICH_INPUT

    if not all(isinstance(r, dict) and "grp_id" in r for r in cards_in):
        print("error: every record must be an object with a 'grp_id' field",
              file=sys.stderr)
        return EXIT_ENRICH_INPUT

    db = _load_db(quiet=args.quiet)
    _log(f"enriching {len(cards_in)} records", quiet=args.quiet)

    unknown: list[int] = []
    enriched: list[dict[str, Any]] = []
    for rec in cards_in:
        grp = int(rec["grp_id"])
        card = db.by_grp_id(grp)
        if card is None:
            unknown.append(grp)
            enriched.append(dict(rec))  # pass through as-is
            continue
        merged = _card_dict(card, count=rec.get("count"), full=True)
        # Preserve extra fields already on the input record (e.g. custom tags).
        for k, v in rec.items():
            if k not in merged:
                merged[k] = v
        enriched.append(merged)

    if unknown:
        _log(f"warning: {len(unknown)} grp_ids not in local DB (kept as-is)",
             quiet=args.quiet)

    sf_stats: dict[str, Any] = {}
    if not args.no_scryfall:
        try:
            from scryfall import ScryfallClient, ScryfallError, enrich_from_scryfall
        except ImportError as e:
            print(f"error: {e}", file=sys.stderr)
            return EXIT_SCRYFALL

        client = ScryfallClient(quiet=args.quiet)
        try:
            sf = client.fetch(int(r["grp_id"]) for r in enriched)
        except ScryfallError as e:
            print(f"error: {e}", file=sys.stderr)
            return EXIT_SCRYFALL
        for rec in enriched:
            card = sf.get(int(rec["grp_id"]))
            if card:
                rec.update(enrich_from_scryfall(card))
        sf_stats = {
            "cached": client.stats["cached"],
            "fetched": client.stats["fetched"],
            "missing": client.stats["missing"],
        }

    meta["enrichment"] = {
        "mtga_db": str(Path(db.db_path).name),
        "unknown_grp_ids": unknown,
    }
    if sf_stats:
        meta["enrichment"]["scryfall"] = sf_stats

    out_path = Path(args.out) if args.out else None
    payload = {"meta": meta, "cards": enriched}
    stream = open(out_path, "w", encoding="utf-8") if out_path else sys.stdout
    try:
        json.dump(payload, stream, indent=2)
        stream.write("\n")
    finally:
        if out_path:
            stream.close()

    if out_path and not args.quiet:
        _log(f"wrote {out_path}: {len(enriched)} cards", quiet=False)
    return EXIT_OK


# ---------- argparse plumbing ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mtg-fetch",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exit codes:\n"
            f"  {EXIT_OK}   ok\n"
            f"  {EXIT_USAGE}   usage / invalid args\n"
            f"  {EXIT_NO_MTGA}  MTGA not running or precheck failed\n"
            f"  {EXIT_PERMISSION}  cannot read /proc/*/mem\n"
            f"  {EXIT_NO_ANCHORS}  anchors missing or invalid\n"
            f"  {EXIT_NO_CANDIDATE}  no candidate blocks in memory\n"
            f"  {EXIT_ANCHOR_MISMATCH}  strict mode: anchors did not match\n"
            f"  {EXIT_ENRICH_INPUT}  enrich: bad input file\n"
            f"  {EXIT_SCRYFALL}  enrich: scryfall unreachable\n"
        ),
    )
    subs = p.add_subparsers(dest="cmd", required=True)

    pc = subs.add_parser("check", help="verify runtime prerequisites")
    pc.add_argument("--format", choices=["text", "json"], default="text")
    pc.set_defaults(func=cmd_check)

    pcard = subs.add_parser("card", help="look up card metadata")
    pcard.add_argument("--name", help="exact English card name")
    pcard.add_argument("--grp-id", type=int, help="MTGA grpId")
    pcard.add_argument("--format", choices=["text", "json"], default="text")
    pcard.set_defaults(func=cmd_card)

    pco = subs.add_parser("collect", help="extract owned-card collection")
    pco.add_argument("--pid", type=int, help="MTGA.exe PID (auto if omitted)")
    pco.add_argument(
        "--anchor", action="append", default=[],
        metavar="NAME:COUNT",
        help='repeatable, e.g. --anchor "Lyra Dawnbringer:2"',
    )
    pco.add_argument("--anchors-file", metavar="PATH",
                     help='JSON: {"anchors":[{"name":..,"count":..},...]}')
    pco.add_argument("--anchors-stdin", action="store_true",
                     help="read anchors JSON from stdin")
    pco.add_argument(
        "--format", choices=["json", "jsonl", "csv"], default="json",
        help="output format (default json for machine consumption)",
    )
    pco.add_argument("--out", metavar="PATH", help="write to file (stdout if omitted)")
    pco.add_argument("--min-block", type=int, default=DEFAULT_MIN_BLOCK,
                     help=f"min entries in a candidate block (default {DEFAULT_MIN_BLOCK})")
    pco.add_argument("--max-region-mb", type=int, default=512,
                     help="skip regions larger than this many MB")
    pco.add_argument("--strict", action="store_true",
                     help=f"fail (exit {EXIT_ANCHOR_MISMATCH}) if any anchor mismatches")
    pco.add_argument("--quiet", action="store_true", help="suppress stderr progress")
    pco.set_defaults(func=cmd_collect)

    pen = subs.add_parser(
        "enrich",
        help="enrich a collection JSON with full card metadata + Scryfall data",
    )
    pen.add_argument("input", help="input JSON (e.g. output of `collect`)")
    pen.add_argument("--out", metavar="PATH",
                     help="output file (stdout if omitted)")
    pen.add_argument("--no-scryfall", action="store_true",
                     help="local-only: skip Scryfall (no images, no legality)")
    pen.add_argument("--quiet", action="store_true",
                     help="suppress stderr progress")
    pen.set_defaults(func=cmd_enrich)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
