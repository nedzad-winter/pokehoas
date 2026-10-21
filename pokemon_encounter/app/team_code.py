"""Team-Codes: das eigene Team als kurzer, kopierbarer Text.

Ein Code ist base64url (ohne Polster) ueber ein kompaktes JSON-Objekt:

    {"v": 1, "c": "<crc32-hex>", "o": "<Name>", "t": [[species_id, level,
      [hp,atk,def,spa,spd,spe], ability|null, held_item|null, [attacken]], ...]}

`v` ist die Formatversion (aendert sich das Format, steigt sie und alte Codes
werden sauber abgelehnt), `c` eine CRC32-Pruefsumme ueber die Team-Liste
(faengt Tippfehler beim Kopieren ab, ist ausdruecklich KEIN Schutz gegen
absichtliche Manipulation).

Deshalb gilt jeder eingelesene Code als NICHT vertrauenswuerdig: `decode`
prueft Groesse, Version, Pruefsumme und danach jeden einzelnen Wert gegen die
Spieldaten (bekannte Art, Level 1-100, IVs 0-31, auf diesem Level lernbare
Attacken, bekannte Faehigkeit/Item) und wirft sonst einen uebersetzten
`ValueError`. Was hier
herauskommt, ist immer ein Team, das es im Spiel auch geben koennte.
"""

from __future__ import annotations

import base64
import binascii
import json
import zlib
from typing import Any

from . import db, held_items, i18n, moves as moves_mod

CODE_VERSION = 1
MAX_TEAM_SIZE = 6
MAX_CODE_LENGTH = 4000   # Zeichen im Code selbst
MAX_PAYLOAD_BYTES = 8000  # dekodiertes JSON
MAX_NAME_LENGTH = 32
MAX_LEVEL = 100
MAX_IV = 31
IV_KEYS = ("hp", "atk", "def", "spa", "spd", "spe")


class CodeError(ValueError):
    """Ungueltiger Team-Code - die Meldung ist bereits uebersetzt."""


def _fail(lang: str, message_key: str, **params: Any) -> CodeError:
    return CodeError(i18n.t(lang, message_key, **params))


def _checksum(team: list[Any]) -> str:
    raw = json.dumps(team, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return format(zlib.crc32(raw) & 0xFFFFFFFF, "08x")


def _clean_name(name: str | None) -> str:
    text = "".join(ch for ch in (name or "") if ch.isprintable())
    return text.strip()[:MAX_NAME_LENGTH]


# ------------------------------------------------------------------ Encode

def encode(mons: list[dict[str, Any]], owner_name: str | None = None) -> str:
    """Team-Schnappschuss (siehe pvp.snapshot_for_user) -> Code."""
    team = [
        [
            int(mon["species_id"]), int(mon["level"]),
            [int(mon["ivs"].get(key, 0)) for key in IV_KEYS],
            mon.get("ability"), mon.get("held_item"), list(mon.get("moves") or []),
        ]
        for mon in mons[:MAX_TEAM_SIZE]
    ]
    payload: dict[str, Any] = {"v": CODE_VERSION, "c": _checksum(team), "t": team}
    name = _clean_name(owner_name)
    if name:
        payload["o"] = name
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


# ------------------------------------------------------------------ Decode

def _decode_payload(code: str, lang: str) -> dict[str, Any]:
    text = "".join((code or "").split())
    if not text:
        raise _fail(lang, "error.pvp_code_empty")
    if len(text) > MAX_CODE_LENGTH:
        raise _fail(lang, "error.pvp_code_too_long", max=MAX_CODE_LENGTH)
    try:
        raw = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as exc:
        raise _fail(lang, "error.pvp_code_invalid") from exc
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise _fail(lang, "error.pvp_code_too_long", max=MAX_CODE_LENGTH)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise _fail(lang, "error.pvp_code_invalid") from exc
    if not isinstance(payload, dict):
        raise _fail(lang, "error.pvp_code_invalid")
    if payload.get("v") != CODE_VERSION:
        raise _fail(lang, "error.pvp_code_version", version=CODE_VERSION)
    return payload


def _species_name(species: dict[str, Any], lang: str) -> str:
    """Anzeigename fuer Fehlermeldungen (die `name`-Spalte ist der interne Slug)."""
    if lang == "de" and species.get("name_de"):
        return species["name_de"]
    return species.get("name_en") or species.get("name_de") or species["name"]


def _int_in_range(value: Any, low: int, high: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if low <= value <= high else None


def _mon_from_entry(entry: Any, lang: str) -> dict[str, Any]:
    if not isinstance(entry, list) or len(entry) != 6:
        raise _fail(lang, "error.pvp_code_invalid")
    species_id, level, ivs, ability, held_item, move_keys = entry

    if _int_in_range(species_id, 1, 100000) is None:
        raise _fail(lang, "error.pvp_code_invalid")
    species = db.query_one("SELECT * FROM species WHERE id = ?", (species_id,))
    if species is None:
        raise _fail(lang, "error.pvp_code_unknown_species", id=species_id)
    name = _species_name(species, lang)
    if _int_in_range(level, 1, MAX_LEVEL) is None:
        raise _fail(lang, "error.pvp_code_bad_level", name=name, max=MAX_LEVEL)

    if not isinstance(ivs, list) or len(ivs) != len(IV_KEYS) \
            or any(_int_in_range(iv, 0, MAX_IV) is None for iv in ivs):
        raise _fail(lang, "error.pvp_code_bad_ivs", name=name, max=MAX_IV)

    if ability is not None:
        if not isinstance(ability, str):
            raise _fail(lang, "error.pvp_code_invalid")
        known = {
            row["ability_key"] for row in
            db.query("SELECT ability_key FROM species_ability WHERE species_id = ?", (species_id,))
        }
        if ability not in known:
            raise _fail(lang, "error.pvp_code_unknown_ability",
                        ability=ability, name=name)

    if held_item is not None and (not isinstance(held_item, str) or held_items.get(held_item) is None):
        raise _fail(lang, "error.pvp_code_unknown_item", item=held_item)

    if not isinstance(move_keys, list) or not 1 <= len(move_keys) <= moves_mod.MAX_MOVES \
            or len(set(move_keys)) != len(move_keys):
        raise _fail(lang, "error.pvp_code_bad_moves",
                    name=name, max=moves_mod.MAX_MOVES)
    # Bewusst NUR Attacke-bekannt + Levelgrenze, nicht "passt zum Typ": per TM
    # (app/tms.py) darf jedes Pokémon eine typfremde Attacke lernen, ein Code
    # aus einem Team mit TM-Attacken muss also einlesbar bleiben. Die
    # Levelgrenze bleibt - genau die prueft auch tms.teach.
    known = moves_mod.all_moves()
    for key in move_keys:
        if not isinstance(key, str) or key not in known \
                or known[key]["min_level"] > level:
            raise _fail(lang, "error.pvp_code_unknown_move", move=str(key), name=name)

    return {
        "species_id": species_id,
        "level": level,
        "ivs": {name: int(value) for name, value in zip(IV_KEYS, ivs)},
        "ability": ability,
        "held_item": held_item,
        "moves": list(move_keys),
    }


def decode(code: str, lang: str = "en") -> tuple[list[dict[str, Any]], str]:
    """Code -> (Team-Schnappschuss, Name des Besitzers). Wirft `CodeError`."""
    payload = _decode_payload(code, lang)
    team = payload.get("t")
    if not isinstance(team, list) or not team:
        raise _fail(lang, "error.pvp_code_empty_team")
    if len(team) > MAX_TEAM_SIZE:
        raise _fail(lang, "error.pvp_code_too_many", max=MAX_TEAM_SIZE)
    if payload.get("c") != _checksum(team):
        raise _fail(lang, "error.pvp_code_checksum")
    owner = payload.get("o")
    if owner is not None and not isinstance(owner, str):
        raise _fail(lang, "error.pvp_code_invalid")
    return [_mon_from_entry(entry, lang) for entry in team], _clean_name(owner)
