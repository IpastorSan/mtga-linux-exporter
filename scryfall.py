"""Minimal Scryfall client for image URIs and MTGA format legality.

MTGA's local DB has no image URLs and no format legality info. We hit
Scryfall's `/cards/arena/<grp_id>` endpoint to fill both gaps. Responses
are persisted per grp_id under ~/.cache/mtg-explorer/scryfall/ so re-runs
are offline. (The bulk /cards/collection endpoint does NOT accept
arena_id as an identifier, so per-card GET is the sane path.)

Only stdlib (urllib) — the project has no pip dependencies.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterable

ARENA_URL = "https://api.scryfall.com/cards/arena/{grp_id}"
UA = "mtg-explorer/1.0 (https://github.com/ignaciopastorsan)"
DEFAULT_CACHE = Path.home() / ".cache" / "mtg-explorer" / "scryfall"

# Scryfall's legality keys covering Arena-playable formats. Note the two
# Brawl variants: `standardbrawl` == in-client "Brawl" (Standard sets),
# `brawl` == "Historic Brawl" (Historic pool).
MTGA_FORMATS = ("standard", "alchemy", "historic", "explorer", "timeless",
                "standardbrawl", "brawl")

PLAYABLE = {"legal", "restricted"}


class ScryfallError(RuntimeError):
    pass


class ScryfallClient:
    def __init__(self, cache_dir: Path = DEFAULT_CACHE, delay: float = 0.1,
                 quiet: bool = False):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.delay = delay
        self.quiet = quiet
        self.stats = {"cached": 0, "fetched": 0, "missing": []}

    def _cache_path(self, grp_id: int) -> Path:
        return self.cache_dir / f"{int(grp_id)}.json"

    def _read_cache(self, grp_id: int) -> dict | None:
        p = self._cache_path(grp_id)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def _write_cache(self, grp_id: int, payload: dict) -> None:
        self._cache_path(grp_id).write_text(json.dumps(payload))

    def _log(self, msg: str) -> None:
        if not self.quiet:
            print(msg, file=sys.stderr)

    def fetch(self, grp_ids: Iterable[int]) -> dict[int, dict]:
        """Return {grp_id: scryfall_card_json} for every ID we can resolve.

        Cached IDs are served locally; the rest are fetched one by one
        with a courtesy delay between requests.
        """
        wanted = sorted({int(g) for g in grp_ids})
        results: dict[int, dict] = {}
        to_fetch: list[int] = []
        for g in wanted:
            hit = self._read_cache(g)
            if hit is None:
                to_fetch.append(g)
            elif hit.get("_missing"):
                # Prior run confirmed Scryfall has no arena_id match.
                self.stats["missing"].append(g)
            else:
                results[g] = hit
                self.stats["cached"] += 1

        total = len(to_fetch)
        for i, g in enumerate(to_fetch, 1):
            if i % 25 == 0 or i == total:
                self._log(f"  scryfall {i}/{total}")
            card = self._get_arena(g)
            if card is None:
                self._write_cache(g, {"_missing": True, "arena_id": g})
                self.stats["missing"].append(g)
            else:
                self._write_cache(g, card)
                results[g] = card
                self.stats["fetched"] += 1
            if i < total:
                time.sleep(self.delay)

        return results

    def _get_arena(self, grp_id: int) -> dict | None:
        req = urllib.request.Request(
            ARENA_URL.format(grp_id=grp_id),
            headers={"User-Agent": UA, "Accept": "application/json"},
        )
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                if e.code == 429 and attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                if e.code >= 500 and attempt < 2:
                    time.sleep(1 + attempt)
                    continue
                raise ScryfallError(f"scryfall HTTP {e.code} for grp_id={grp_id}: {e.reason}") from e
            except urllib.error.URLError as e:
                if attempt < 2:
                    time.sleep(1 + attempt)
                    continue
                raise ScryfallError(f"scryfall network error for grp_id={grp_id}: {e.reason}") from e
        return None


def enrich_from_scryfall(sf_card: dict) -> dict:
    """Flatten a Scryfall card payload into the fields we store per card.

    Handles double-faced cards (image_uris per face).
    """
    out: dict = {
        "scryfall_id": sf_card.get("id"),
        "scryfall_uri": sf_card.get("scryfall_uri"),
    }
    if "image_uris" in sf_card:
        out["image_uris"] = sf_card["image_uris"]
    faces = sf_card.get("card_faces") or []
    face_images = [
        f["image_uris"] for f in faces if isinstance(f, dict) and "image_uris" in f
    ]
    if face_images:
        out["card_faces_image_uris"] = face_images
        out.setdefault("image_uris", face_images[0])

    legal = sf_card.get("legalities") or {}
    formats_legal = sorted(
        f for f in MTGA_FORMATS if legal.get(f) in PLAYABLE
    )
    out["formats_legal"] = formats_legal
    return out
