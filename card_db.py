"""Read MTGA's bundled SQLite card database.

MTGA ships a full card DB inside its install:
    <MTGA_ROOT>/MTGA_Data/Downloads/Raw/Raw_CardDatabase_*.mtga
This module wraps it so we can resolve grpId <-> card metadata without
touching the network. Names come from the enUS localization table.
"""

from __future__ import annotations

import glob
import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path


def _find_mtga_root() -> Path:
    """Locate the MTGA install.

    Honours $MTGA_ROOT, then walks every Steam library registered in
    libraryfolders.vdf — MTGA is frequently installed to a secondary
    library rather than the default ~/.steam one.
    """
    env = os.environ.get("MTGA_ROOT")
    if env:
        return Path(env).expanduser()

    default = Path.home() / ".steam/steam/steamapps/common/MTGA"
    candidates = [default]

    vdf = Path.home() / ".steam/steam/steamapps/libraryfolders.vdf"
    try:
        for lib in re.findall(r'"path"\s+"([^"]+)"', vdf.read_text()):
            candidates.append(Path(lib) / "steamapps/common/MTGA")
    except OSError:
        pass

    for root in candidates:
        if (root / "MTGA_Data" / "Downloads" / "Raw").is_dir():
            return root
    return default


DEFAULT_MTGA_ROOT = _find_mtga_root()

RARITY_NAMES = {1: "basic", 2: "common", 3: "uncommon", 4: "rare", 5: "mythic"}


@dataclass(frozen=True)
class Card:
    grp_id: int
    name: str
    set: str
    collector_number: str
    rarity: int
    is_primary: bool
    is_token: bool
    is_digital_only: bool
    is_rebalanced: bool
    mana_cost: str
    mana_value: int
    power: str
    toughness: str
    type_line: str
    types: tuple[str, ...]
    subtypes: tuple[str, ...]
    supertypes: tuple[str, ...]
    colors: tuple[str, ...]
    color_identity: tuple[str, ...]
    oracle_text: str
    flavor_text: str
    artist: str
    art_id: int


# ---------- helpers ----------

_MANA_TOKEN = re.compile(r"o(\([^)]+\)|\d+|X|Y|Z|[A-Z](?:/[A-Z0-9])?)")


def parse_mana_cost(raw: str) -> tuple[str, int]:
    """Convert MTGA `o`-notation to `{X}` symbolic form + mana value.

    Examples:
      "o3oWoW"      -> ("{3}{W}{W}", 5)
      "o(B/G)o2"    -> ("{B/G}{2}", 3)
      "oXoUoR"      -> ("{X}{U}{R}", 2)  # X counts as 0
    """
    if not raw:
        return "", 0
    parts: list[str] = []
    mv = 0
    for m in _MANA_TOKEN.finditer(raw):
        sym = m.group(1)
        if sym.startswith("("):
            sym = sym[1:-1]  # "(B/G)" -> "B/G"
            parts.append("{" + sym + "}")
            mv += 1
        elif sym.isdigit():
            parts.append("{" + sym + "}")
            mv += int(sym)
        else:
            parts.append("{" + sym + "}")
            if sym not in ("X", "Y", "Z"):
                mv += 1
    return "".join(parts), mv


_TAG = re.compile(r"<[^>]+>")
# MTGA embeds mana/tap glyphs in oracle text as `{oX}` — normalize to `{X}`.
_INLINE_MANA = re.compile(r"\{o([^}]+)\}")


def _strip_tags(text: str) -> str:
    return _TAG.sub("", text)


def _normalize_symbols(text: str) -> str:
    return _INLINE_MANA.sub(r"{\1}", text)


COLOR_LETTERS = {
    "White": "W", "Blue": "U", "Black": "B", "Red": "R", "Green": "G",
    "Colorless": "C",
}


def _color_letter(name: str) -> str:
    return COLOR_LETTERS.get(name, name)


class CardDB:
    def __init__(self, mtga_root: Path = DEFAULT_MTGA_ROOT):
        raw = mtga_root / "MTGA_Data" / "Downloads" / "Raw"
        matches = sorted(glob.glob(str(raw / "Raw_CardDatabase_*.mtga")))
        if not matches:
            raise FileNotFoundError(f"No CardDatabase file under {raw}")
        # Prefer newest (largest mtime) if the client staged multiple.
        self.db_path = max(matches, key=lambda p: Path(p).stat().st_mtime)
        self._con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        self._by_grp: dict[int, Card] = {}
        self._by_name: dict[str, list[Card]] = {}
        self._load()

    def _load(self) -> None:
        cur = self._con.cursor()

        # Localizations: LocId -> {Formatted: text}. Preferred Formatted=0
        # (plain), fallback to 1 (HTML — tags stripped) then 2.
        loc: dict[int, dict[int, str]] = {}
        for lid, fmt, text in cur.execute(
            "SELECT LocId, Formatted, Loc FROM Localizations_enUS"
        ):
            loc.setdefault(int(lid), {})[int(fmt)] = text or ""

        def loc_get(lid: int) -> str:
            if not lid:
                return ""
            variants = loc.get(int(lid))
            if not variants:
                return ""
            if 0 in variants:
                return variants[0]
            if 1 in variants:
                return _strip_tags(variants[1])
            return next(iter(variants.values()))

        # Enums: Type -> {Value: name}
        enums: dict[str, dict[int, str]] = {}
        for etype, val, lid in cur.execute(
            "SELECT Type, Value, LocId FROM Enums"
        ):
            enums.setdefault(etype, {})[int(val)] = loc_get(lid)

        card_types = enums.get("CardType", {})
        subtypes = enums.get("SubType", {})
        supertypes = enums.get("SuperType", {})
        card_colors = enums.get("CardColor", {})
        colors_enum = enums.get("Color", {})

        # Abilities: Id -> TextId (fallback if pair TextId is 0)
        ability_text_id: dict[int, int] = {}
        for aid, tid in cur.execute("SELECT Id, TextId FROM Abilities"):
            ability_text_id[int(aid)] = int(tid or 0)

        def decode_csv(raw: str, table: dict[int, str]) -> tuple[str, ...]:
            if not raw:
                return ()
            out: list[str] = []
            for tok in raw.split(","):
                tok = tok.strip()
                if not tok:
                    continue
                try:
                    v = int(tok)
                except ValueError:
                    continue
                name = table.get(v)
                if name:
                    out.append(name)
            return tuple(out)

        def build_type_line(type_lid: int, subtype_lid: int) -> str:
            head = loc_get(type_lid).strip()
            tail = loc_get(subtype_lid).strip()
            if head and tail:
                # em-dash used in printed cards
                return f"{head} — {tail}"
            return head or tail

        def build_oracle(ability_ids_raw: str) -> str:
            if not ability_ids_raw:
                return ""
            lines: list[str] = []
            for pair in ability_ids_raw.split(","):
                pair = pair.strip()
                if not pair or ":" not in pair:
                    continue
                aid_s, tid_s = pair.split(":", 1)
                try:
                    aid = int(aid_s)
                    tid = int(tid_s)
                except ValueError:
                    continue
                if tid == 0:
                    tid = ability_text_id.get(aid, 0)
                text = loc_get(tid).strip()
                if text:
                    lines.append(_normalize_symbols(text))
            return "\n".join(lines)

        rows = cur.execute("""
            SELECT c.GrpId, c.TitleId, c.ExpansionCode, c.CollectorNumber,
                   c.Rarity, c.IsPrimaryCard, c.IsToken, c.IsDigitalOnly,
                   c.IsRebalanced,
                   c.OldSchoolManaText, c.Power, c.Toughness,
                   c.TypeTextId, c.SubtypeTextId, c.FlavorTextId,
                   c.Types, c.Subtypes, c.Supertypes,
                   c.Colors, c.ColorIdentity,
                   c.AbilityIds, c.ArtistCredit, c.ArtId
            FROM Cards c
            WHERE c.IsToken = 0
        """)
        for (grp, title_id, exp, cn, rarity, primary, token, digital,
             rebalanced, mana_raw, power, toughness,
             type_lid, subtype_lid, flavor_lid,
             types_raw, subtypes_raw, supertypes_raw,
             colors_raw, color_id_raw,
             ability_ids_raw, artist, art_id) in rows:
            name = loc_get(int(title_id))
            mana_cost, mana_value = parse_mana_cost(mana_raw or "")
            colors_names = decode_csv(colors_raw or "", card_colors)
            ci_names = decode_csv(color_id_raw or "", colors_enum)
            card = Card(
                grp_id=int(grp),
                name=name,
                set=(exp or "").upper(),
                collector_number=str(cn or ""),
                rarity=int(rarity or 0),
                is_primary=bool(primary),
                is_token=bool(token),
                is_digital_only=bool(digital),
                is_rebalanced=bool(rebalanced),
                mana_cost=mana_cost,
                mana_value=mana_value,
                power=(power or ""),
                toughness=(toughness or ""),
                type_line=build_type_line(int(type_lid or 0), int(subtype_lid or 0)),
                types=decode_csv(types_raw or "", card_types),
                subtypes=decode_csv(subtypes_raw or "", subtypes),
                supertypes=decode_csv(supertypes_raw or "", supertypes),
                colors=tuple(_color_letter(c) for c in colors_names),
                color_identity=tuple(_color_letter(c) for c in ci_names),
                oracle_text=build_oracle(ability_ids_raw or ""),
                flavor_text=loc_get(int(flavor_lid or 0)),
                artist=(artist or ""),
                art_id=int(art_id or 0),
            )
            self._by_grp[card.grp_id] = card
            self._by_name.setdefault(card.name, []).append(card)

    def by_grp_id(self, grp_id: int) -> Card | None:
        return self._by_grp.get(int(grp_id))

    def by_name(self, name: str) -> list[Card]:
        return list(self._by_name.get(name, []))

    def all_grp_ids(self) -> set[int]:
        return set(self._by_grp.keys())

    def close(self) -> None:
        self._con.close()
