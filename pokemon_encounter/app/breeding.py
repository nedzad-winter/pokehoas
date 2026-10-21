"""Zucht wie in den Hauptspielen: Pension -> Ei -> Schluepfen.

Pension (`daycare`, eine pro Nutzer): Zwei eigene Pokemon werden als Paar
angemeldet. Kompatibel sind sie wie im Hauptspiel, wenn

* keines in der Ei-Gruppe "Unbekannt" (`no-eggs`: Legendaere, Babys) ist,
* und entweder genau eines Ditto ist (Ditto + Ditto geht nicht)
* oder beide eine Ei-Gruppe teilen und unterschiedliches Geschlecht haben.

Solange das Paar angemeldet ist, liegt nach `daycare_egg_minutes` (gleiche
Art) bzw. doppelt so lange (verschiedene Arten / Ditto) ein Ei bereit, das
abgeholt werden muss - wie im Spiel wartet immer hoechstens EIN Ei. Die
Eltern bleiben dabei normal nutzbar (Annahme: sie aus allen Team-/Kampf-
Abfragen herauszunehmen waere unverhaeltnismaessig).

Das Ei (`pokemon_egg`) enthaelt schon das komplette Individuum: Art = Basis-
form der Mutter (bzw. des Nicht-Ditto-Elternteils), 3 IVs von den Eltern
geerbt (5 mit Fatumknoten), Wesen per Ewigstein, Faehigkeit bevorzugt von
der Mutter. Schillernd wird es mit der normalen Shiny-Rate.

Schluepfen: Ein Ei braucht `hatch_counter x STEPS_PER_CYCLE` Schritte. Es
sammelt sie NUR im Team (`location = 'party'`, belegt dort einen der sechs
Plaetze) oder in einem Inkubator (`location = 'incubator'`, wie in Pokemon GO,
kein Teamplatz noetig). Schrittquellen:

* der Schritt-Sensor des Nutzers (derselbe wie beim Buddy, `buddy.apply_reading`)
* passiv `egg_passive_steps_per_hour`, damit Eier auch ohne Sensor schluepfen

Flammkoerper/Magmapanzer/Dampfantrieb im Team verdoppeln die Schritte.
Ist das Ziel erreicht, schluepft das Ei auf Knopfdruck (`hatch`) als Lv.-1-
Pokemon. Ein Inkubator (Item `incubator`) haelt `INCUBATOR_USES` Eier aus
und verschwindet danach.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import datetime, timedelta
from typing import Any

import httpx

from . import battle, db, economy, i18n, moves as moves_mod, rarity, seed, spawn

log = logging.getLogger(__name__)

DITTO_ID = 132
NO_EGGS = "no-eggs"
DITTO_GROUP = "ditto"
# Schritte pro Ei-Zyklus (Gen 8+). Ein Ei braucht hatch_counter Zyklen.
STEPS_PER_CYCLE = 257
DEFAULT_HATCH_COUNTER = 20
PARTY_SIZE = 6
HATCH_LEVEL = 1

INCUBATOR_ITEM = "incubator"
INCUBATOR_USES = 3
DESTINY_KNOT = "destiny_knot"
EVERSTONE = "everstone"
HATCH_BOOST_ABILITIES = {"flame_body", "magma_armor", "steam_engine"}
# Chance, die versteckte Faehigkeit der Mutter bzw. ihre regulaere zu vererben.
HIDDEN_ABILITY_INHERIT_CHANCE = 0.6
ABILITY_INHERIT_CHANCE = 0.8

LOCATIONS = ("box", "party", "incubator")

# Ersatz-Ei-Gruppen nach Typ, solange die PokeAPI-Daten fehlen (Offline-Start,
# Nachtrag laeuft noch). Grob, aber so ist Zucht nie komplett blockiert.
_TYPE_EGG_GROUPS = {
    "normal": "ground", "fire": "ground", "water": "water1", "grass": "plant",
    "electric": "fairy", "ice": "ground", "fighting": "humanshape", "poison": "indeterminate",
    "ground": "ground", "flying": "flying", "psychic": "humanshape", "bug": "bug",
    "rock": "mineral", "ghost": "indeterminate", "dragon": "dragon", "dark": "indeterminate",
    "steel": "mineral", "fairy": "fairy",
}


class BreedingError(Exception):
    """Fachlicher Fehler mit HTTP-Status und i18n-Key - die Routen uebersetzen."""

    def __init__(self, status_code: int, key: str, **params: Any) -> None:
        super().__init__(key)
        self.status_code = status_code
        self.key = key
        self.params = params


def _now() -> datetime:
    return datetime.now().astimezone()


def _parse(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value or "")
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


# ------------------------------------------------------------- Artdaten

def _species(species_id: int) -> dict[str, Any] | None:
    return db.query_one("SELECT * FROM species WHERE id = ?", (species_id,))


def egg_groups(species: dict[str, Any]) -> list[str]:
    """Ei-Gruppen aus `species.egg_groups` (PokeAPI), sonst Ersatz nach Typ."""
    raw = species.get("egg_groups")
    if raw:
        try:
            groups = json.loads(raw)
            if isinstance(groups, list) and groups:
                return [str(g) for g in groups]
        except ValueError:
            pass
    if species["id"] == DITTO_ID:
        return [DITTO_GROUP]
    if species.get("is_legendary") or species.get("is_mythical") \
            or rarity.tier_for(species) in ("legendary", "mythical", "paradox"):
        return [NO_EGGS]
    types = [species.get("type1"), species.get("type2")]
    return sorted({_TYPE_EGG_GROUPS.get(t, "ground") for t in types if t})


def hatch_steps(species: dict[str, Any]) -> int:
    counter = species.get("hatch_counter")
    if not counter:
        tier = rarity.tier_for(species)
        counter = {"legendary": 120, "mythical": 120, "paradox": 50,
                   "pseudo_legendary": 40}.get(tier, DEFAULT_HATCH_COUNTER)
    return int(counter) * STEPS_PER_CYCLE


def base_species_id(species_id: int) -> int:
    """Erste Stufe der Entwicklungsreihe (evolves_from bis zum Ende)."""
    current, seen = species_id, set()
    while current not in seen:
        seen.add(current)
        row = _species(current)
        parent = (row or {}).get("evolves_from")
        if not parent or _species(parent) is None:
            return current
        current = parent
    return current


def compatibility(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Pruefung zweier owned_pokemon-Zeilen. Ergebnis: `ok`, `reason` (i18n-Key
    bei Inkompatibilitaet), `mother` (Zeile, deren Art das Ei bestimmt),
    `egg_species_id`, `factor` (1 = gleiche Art, 2 = sonst)."""
    result: dict[str, Any] = {"ok": False, "reason": None, "mother": None,
                              "egg_species_id": None, "factor": 2}
    if a["id"] == b["id"]:
        result["reason"] = "breeding.incompatible_same"
        return result
    sa, sb = _species(a["species_id"]), _species(b["species_id"])
    ga, gb = set(egg_groups(sa)), set(egg_groups(sb))
    if NO_EGGS in ga or NO_EGGS in gb:
        result["reason"] = "breeding.incompatible_undiscovered"
        return result
    a_ditto, b_ditto = DITTO_GROUP in ga, DITTO_GROUP in gb
    if a_ditto and b_ditto:
        result["reason"] = "breeding.incompatible_two_ditto"
        return result
    if a_ditto or b_ditto:
        mother = b if a_ditto else a
    else:
        if not ga & gb:
            result["reason"] = "breeding.incompatible_groups"
            return result
        genders = {a.get("gender"), b.get("gender")}
        if genders != {"male", "female"}:
            result["reason"] = "breeding.incompatible_gender"
            return result
        mother = a if a.get("gender") == "female" else b
    result.update(
        ok=True, mother=mother,
        egg_species_id=base_species_id(mother["species_id"]),
        factor=1 if a["species_id"] == b["species_id"] else 2,
    )
    return result


# --------------------------------------------------------------- Teamplatz

def party_counts(user_id: str) -> tuple[int, int]:
    """(Pokemon im Team, Eier im Team)."""
    mons = db.query_one(
        "SELECT COUNT(*) AS n FROM owned_pokemon WHERE user_id = ? AND party_slot IS NOT NULL",
        (user_id,),
    )["n"]
    eggs = db.query_one(
        "SELECT COUNT(*) AS n FROM pokemon_egg WHERE user_id = ? AND location = 'party'",
        (user_id,),
    )["n"]
    return mons, eggs


def free_party_slot(user_id: str) -> int | None:
    """Freier Team-Platz fuer ein neues Pokemon (Fang, Schluepfen, Admin) -
    Eier im Team belegen ebenfalls einen der sechs Plaetze."""
    mons, eggs = party_counts(user_id)
    if mons + eggs >= PARTY_SIZE:
        return None
    used = {
        row["party_slot"]
        for row in db.query(
            "SELECT party_slot FROM owned_pokemon WHERE user_id = ? AND party_slot IS NOT NULL",
            (user_id,),
        )
    }
    return next((slot for slot in range(1, PARTY_SIZE + 1) if slot not in used), None)


# -------------------------------------------------------------- Schritte

def _boost(user_id: str) -> bool:
    row = db.query_one(
        "SELECT 1 FROM owned_pokemon WHERE user_id = ? AND party_slot IS NOT NULL "
        f"AND ability IN ({','.join('?' * len(HATCH_BOOST_ABILITIES))}) LIMIT 1",
        (user_id, *sorted(HATCH_BOOST_ABILITIES)),
    )
    return row is not None


def credit_steps(user_id: str, steps: float) -> int:
    """Schritte allen bruetenden Eiern (Team + Inkubator) gutschreiben.
    Rueckgabe: tatsaechlich gutgeschriebene Schritte pro Ei."""
    amount = int(round(steps)) * (2 if _boost(user_id) else 1)
    if amount <= 0:
        return 0
    db.execute(
        "UPDATE pokemon_egg SET steps_done = MIN(steps_total, steps_done + ?) "
        "WHERE user_id = ? AND location IN ('party', 'incubator')",
        (amount, user_id),
    )
    return amount


def passive_steps_per_hour() -> float:
    return max(0.0, float(db.get_setting(
        "egg_passive_steps_per_hour", db.DEFAULT_SETTINGS["egg_passive_steps_per_hour"])))


def advance(user_id: str, now: datetime | None = None) -> None:
    """Passive Schritte seit dem letzten Aufruf verbuchen (lazy wie
    healing.apply_regen). Vor jeder Ortsaenderung eines Eies aufrufen, damit
    die Zeit dem bisherigen Ort gutgeschrieben wird."""
    now = now or _now()
    user = db.get_user(user_id) or {}
    last = _parse(user.get("egg_steps_at"))
    if last is not None and now > last:
        steps = passive_steps_per_hour() * (now - last).total_seconds() / 3600
        # Nur ganze Schritte verbuchen, Rest bleibt fuer den naechsten Aufruf.
        whole = int(steps)
        if whole > 0:
            credit_steps(user_id, whole)
            per_hour = passive_steps_per_hour()
            consumed = timedelta(hours=whole / per_hour) if per_hour else now - last
            db.execute("UPDATE app_user SET egg_steps_at = ? WHERE id = ?",
                       ((last + consumed).isoformat(), user_id))
        return
    if last is None or now < last:
        db.execute("UPDATE app_user SET egg_steps_at = ? WHERE id = ?", (now.isoformat(), user_id))


# ---------------------------------------------------------------- Pension

def daycare_minutes() -> float:
    return max(1.0, float(db.get_setting(
        "daycare_egg_minutes", db.DEFAULT_SETTINGS["daycare_egg_minutes"])))


def _own(user_id: str, owned_id: int) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (owned_id,))
    if row is None or row["user_id"] != user_id:
        raise BreedingError(404, "error.pokemon_not_found")
    return row


def set_daycare(user_id: str, parent_a: int, parent_b: int, now: datetime | None = None) -> dict[str, Any]:
    a, b = _own(user_id, parent_a), _own(user_id, parent_b)
    check = compatibility(a, b)
    if not check["ok"]:
        raise BreedingError(400, check["reason"])
    current = db.query_one("SELECT * FROM daycare WHERE user_id = ?", (user_id,))
    if current and {current["parent_a"], current["parent_b"]} == {parent_a, parent_b}:
        return daycare_status(user_id, now)
    db.execute(
        "INSERT INTO daycare (user_id, parent_a, parent_b, since) VALUES (?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET parent_a = excluded.parent_a, "
        "parent_b = excluded.parent_b, since = excluded.since",
        (user_id, parent_a, parent_b, (now or _now()).isoformat()),
    )
    return daycare_status(user_id, now)


def clear_daycare(user_id: str) -> None:
    db.execute("DELETE FROM daycare WHERE user_id = ?", (user_id,))


def daycare_status(user_id: str, now: datetime | None = None) -> dict[str, Any] | None:
    """Paar + Ei-Timer; None ohne Paar. Ein inzwischen freigelassener
    Elternteil loest das Paar auf."""
    row = db.query_one("SELECT * FROM daycare WHERE user_id = ?", (user_id,))
    if row is None:
        return None
    parents = [
        db.query_one("SELECT * FROM owned_pokemon WHERE id = ? AND user_id = ?", (pid, user_id))
        for pid in (row["parent_a"], row["parent_b"])
    ]
    if any(p is None for p in parents):
        clear_daycare(user_id)
        return None
    check = compatibility(*parents)
    since = _parse(row["since"]) or _now()
    interval = timedelta(minutes=daycare_minutes() * check["factor"])
    ready_at = since + interval
    now = now or _now()
    return {
        "parent_a": row["parent_a"],
        "parent_b": row["parent_b"],
        "compatible": check["ok"],
        "reason": check["reason"],
        "same_species": check["factor"] == 1,
        "egg_species_id": check["egg_species_id"],
        "egg_ready": check["ok"] and now >= ready_at,
        "ready_at": ready_at.isoformat(),
        "interval_minutes": round(interval.total_seconds() / 60),
    }


def _inherit_ivs(a: dict[str, Any], b: dict[str, Any], species_id: int) -> dict[str, int]:
    ivs = spawn.roll_ivs(species_id)
    count = 5 if DESTINY_KNOT in (a.get("held_item"), b.get("held_item")) else 3
    parents = [battle._as_ivs(a["ivs"]), battle._as_ivs(b["ivs"])]
    for key in random.sample(list(spawn.STAT_KEYS), count):
        ivs[key] = int(random.choice(parents)[key])
    return ivs


def _inherit_nature(a: dict[str, Any], b: dict[str, Any]) -> str:
    holders = [p for p in (a, b) if p.get("held_item") == EVERSTONE and p.get("nature")]
    if holders:
        return random.choice(holders)["nature"]
    return random.choice(spawn.NATURES)


def _inherit_ability(mother: dict[str, Any], species_id: int) -> str | None:
    """Faehigkeit bevorzugt von der Mutter: versteckte mit 60 %, regulaere
    mit 80 % - sofern die Ei-Art sie ueberhaupt haben kann."""
    rows = db.query("SELECT ability_key, slot FROM species_ability WHERE species_id = ?", (species_id,))
    hidden = {r["ability_key"] for r in rows if r["slot"] == 2}
    regular = {r["ability_key"] for r in rows if r["slot"] != 2}
    ability = mother.get("ability")
    mother_hidden = db.query_one(
        "SELECT 1 FROM species_ability WHERE species_id = ? AND ability_key = ? AND slot = 2",
        (mother["species_id"], ability),
    ) is not None
    if ability and mother_hidden and hidden and random.random() < HIDDEN_ABILITY_INHERIT_CHANCE:
        return random.choice(sorted(hidden))
    if ability in regular and random.random() < ABILITY_INHERIT_CHANCE:
        return ability
    return spawn.roll_ability(species_id)


def _battle_moves(owned: dict[str, Any]) -> list[str]:
    """The four moves a Pokemon fights with: its selection, otherwise the
    default moveset (like battle.combatant_from_owned)."""
    try:
        selected = json.loads(owned.get("selected_moves") or "null") or []
    except ValueError:
        selected = []
    species = _species(owned["species_id"])
    return selected or (moves_mod.moveset_for(species, owned["level"]) if species else [])


def _inherit_egg_moves(a: dict[str, Any], b: dict[str, Any], species: dict[str, Any]) -> list[str]:
    """Egg moves as in Scarlet/Violet: every egg move of the hatchling's
    species that either parent knows (one of its four battle moves), at most
    four, in the order of the species' egg move list."""
    known = set(_battle_moves(a)) | set(_battle_moves(b))
    return [key for key in moves_mod.egg_moves(species) if key in known][:moves_mod.MAX_MOVES]


def create_egg(user_id: str, a: dict[str, Any], b: dict[str, Any], now: datetime | None = None) -> int:
    check = compatibility(a, b)
    if not check["ok"]:
        raise BreedingError(400, check["reason"])
    species = _species(check["egg_species_id"])
    location = "party" if free_party_slot(user_id) is not None else "box"
    egg_moves = _inherit_egg_moves(a, b, species)
    return db.execute(
        """
        INSERT INTO pokemon_egg (
            user_id, species_id, shiny, ivs, nature, gender, ability,
            steps_total, steps_done, location, parent_species_a, parent_species_b, created_at,
            egg_moves
        ) VALUES (?,?,?,?,?,?,?,?,0,?,?,?,?,?)
        """,
        (
            user_id, species["id"], int(spawn.roll_shiny(user_id)),
            json.dumps(_inherit_ivs(a, b, species["id"])), _inherit_nature(a, b),
            spawn.roll_gender(species["id"]), _inherit_ability(check["mother"], species["id"]),
            hatch_steps(species), location, a["species_id"], b["species_id"],
            (now or _now()).isoformat(), json.dumps(egg_moves) if egg_moves else None,
        ),
    )


def collect_egg(user_id: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or _now()
    advance(user_id, now)
    status = daycare_status(user_id, now)
    if status is None:
        raise BreedingError(400, "breeding.no_pair")
    if not status["egg_ready"]:
        raise BreedingError(409, "breeding.no_egg_yet")
    a = _own(user_id, status["parent_a"])
    b = _own(user_id, status["parent_b"])
    egg_id = create_egg(user_id, a, b, now)
    db.execute("UPDATE daycare SET since = ? WHERE user_id = ?", (now.isoformat(), user_id))
    return describe_egg(_egg(user_id, egg_id))


# -------------------------------------------------------------- Eier

def _egg(user_id: str, egg_id: int) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM pokemon_egg WHERE id = ?", (egg_id,))
    if row is None or row["user_id"] != user_id:
        raise BreedingError(404, "breeding.egg_not_found")
    return row


def describe_egg(row: dict[str, Any]) -> dict[str, Any]:
    """Das Ei fuer die Web-App - Art und Werte bleiben bis zum Schluepfen
    geheim, sichtbar sind nur Fortschritt, Ort und die Arten der Eltern."""
    total = max(1, int(row["steps_total"]))
    done = min(total, int(row["steps_done"]))
    parents = []
    for sid in (row.get("parent_species_a"), row.get("parent_species_b")):
        sp = _species(sid) if sid else None
        if sp:
            parents.append({"id": sp["id"], "name": sp["name"], "name_de": sp.get("name_de"),
                            "name_en": sp.get("name_en"), "sprite_url": sp.get("sprite_url")})
    return {
        "id": row["id"],
        "location": row["location"],
        "incubator_id": row.get("incubator_id"),
        "steps_done": done,
        "steps_total": total,
        "steps_left": total - done,
        "progress_percent": round(100 * done / total),
        "ready": done >= total,
        "parents": parents,
        "created_at": row["created_at"],
    }


def list_eggs(user_id: str) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT * FROM pokemon_egg WHERE user_id = ? "
        "ORDER BY CASE location WHEN 'party' THEN 0 WHEN 'incubator' THEN 1 ELSE 2 END, id",
        (user_id,),
    )
    return [describe_egg(row) for row in rows]


def incubators(user_id: str) -> dict[str, Any]:
    active = db.query(
        "SELECT i.id, i.uses_left, e.id AS egg_id FROM incubator i "
        "LEFT JOIN pokemon_egg e ON e.incubator_id = i.id "
        "WHERE i.user_id = ? ORDER BY i.id",
        (user_id,),
    )
    row = db.query_one(
        "SELECT quantity FROM inventory WHERE user_id = ? AND item_key = ?", (user_id, INCUBATOR_ITEM)
    )
    return {"in_bag": int(row["quantity"]) if row else 0, "uses_per_item": INCUBATOR_USES,
            "active": active}


def _take_incubator(user_id: str) -> int:
    """Freien, schon benutzten Inkubator nehmen, sonst einen neuen aus dem Beutel."""
    free = db.query_one(
        "SELECT i.id FROM incubator i WHERE i.user_id = ? AND i.uses_left > 0 "
        "AND NOT EXISTS (SELECT 1 FROM pokemon_egg e WHERE e.incubator_id = i.id) "
        "ORDER BY i.uses_left, i.id LIMIT 1",
        (user_id,),
    )
    if free:
        return free["id"]
    with db.cursor() as cur:
        cur.execute(
            "UPDATE inventory SET quantity = quantity - 1 "
            "WHERE item_key = ? AND user_id = ? AND quantity > 0",
            (INCUBATOR_ITEM, user_id),
        )
        if cur.rowcount != 1:
            raise BreedingError(409, "breeding.no_incubator")
        cur.execute(
            "INSERT INTO incubator (user_id, uses_left, created_at) VALUES (?,?,?)",
            (user_id, INCUBATOR_USES, _now().isoformat()),
        )
        return cur.lastrowid


def move_egg(user_id: str, egg_id: int, location: str, now: datetime | None = None) -> dict[str, Any]:
    if location not in LOCATIONS:
        raise BreedingError(400, "breeding.invalid_location")
    row = _egg(user_id, egg_id)
    if row["location"] == location:
        return describe_egg(row)
    advance(user_id, now)
    if location == "party":
        mons, eggs = party_counts(user_id)
        if mons + eggs >= PARTY_SIZE:
            raise BreedingError(400, "error.team_max_size", max=PARTY_SIZE)
    incubator_id = _take_incubator(user_id) if location == "incubator" else None
    db.execute(
        "UPDATE pokemon_egg SET location = ?, incubator_id = ? WHERE id = ?",
        (location, incubator_id, egg_id),
    )
    return describe_egg(_egg(user_id, egg_id))


def hatch(user_id: str, egg_id: int, now: datetime | None = None) -> dict[str, Any]:
    """Fertiges Ei schluepfen lassen. Das Pokemon uebernimmt den Teamplatz
    des Eies; aus Inkubator/Box kommt es ins Team, falls Platz ist."""
    now = now or _now()
    advance(user_id, now)
    row = _egg(user_id, egg_id)
    if row["steps_done"] < row["steps_total"]:
        raise BreedingError(409, "breeding.not_ready", steps=row["steps_total"] - row["steps_done"])
    species = _species(row["species_id"])
    # Wie beim Fang (api/encounters.py): nie unter der IV-Untergrenze der Art,
    # auch wenn das Ei vor deren Anstieg entstand. Muss vor `on_hatch` passieren.
    ivs = economy.clamp_ivs_to_floor(battle._as_ivs(row["ivs"]), species["id"])
    with db.cursor() as cur:
        cur.execute("DELETE FROM pokemon_egg WHERE id = ?", (egg_id,))
        if row.get("incubator_id"):
            cur.execute("UPDATE incubator SET uses_left = uses_left - 1 WHERE id = ?", (row["incubator_id"],))
            cur.execute("DELETE FROM incubator WHERE id = ? AND uses_left <= 0", (row["incubator_id"],))
    slot = free_party_slot(user_id)
    # Inherited egg moves come first in the hatchling's four moves (like the
    # games), the rest is its default moveset.
    egg_moves = moves_mod.inherited_egg_moves(row)
    selected = None
    if egg_moves:
        selected = list(egg_moves)
        for key in moves_mod.moveset_for(species, HATCH_LEVEL):
            if len(selected) < moves_mod.MAX_MOVES and key not in selected:
                selected.append(key)
    owned_id = db.execute(
        """
        INSERT INTO owned_pokemon (
            user_id, species_id, level, exp, shiny, ivs, nature, gender, ability,
            current_hp, party_slot, caught_at, caught_from, hp_updated_at,
            egg_moves, selected_moves
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?)
        """,
        (
            user_id, species["id"], HATCH_LEVEL,
            battle.exp_for_level(HATCH_LEVEL, species.get("growth_rate") or "medium_fast"),
            row["shiny"], json.dumps(ivs), row["nature"], row["gender"], row.get("ability"),
            battle.stat_hp(species["base_hp"], ivs["hp"], HATCH_LEVEL, species_id=species["id"]),
            slot, now.isoformat(), now.isoformat(),
            json.dumps(egg_moves) if egg_moves else None,
            json.dumps(selected) if selected else None,
        ),
    )
    # Schluepfen gibt dieselben IV-Belohnungen wie ein Fang (economy.on_hatch).
    iv_rewards = economy.on_hatch(user_id, species["id"], owned_id)
    # Geschluepft zaehlt im Pokedex wie gefangen (wie im Hauptspiel).
    db.execute(
        """
        INSERT INTO dex_entry (species_id, user_id, seen_count, caught_count, shiny_caught,
                                first_seen_at, first_caught_at)
        VALUES (?, ?, 1, 1, ?, ?, ?)
        ON CONFLICT(species_id, user_id) DO UPDATE SET
            caught_count = caught_count + 1,
            seen_count = MAX(dex_entry.seen_count, 1),
            shiny_caught = MAX(dex_entry.shiny_caught, excluded.shiny_caught),
            first_seen_at = COALESCE(dex_entry.first_seen_at, excluded.first_seen_at),
            first_caught_at = COALESCE(dex_entry.first_caught_at, excluded.first_caught_at)
        """,
        (species["id"], user_id, int(row["shiny"]), now.isoformat(), now.isoformat()),
    )
    lang = db.user_language(user_id)
    name = species.get("name_de") if lang == "de" and species.get("name_de") else (
        species.get("name_en") or species.get("name_de") or species["name"])
    return {"owned_id": owned_id, "species_id": species["id"], "name": name,
            "shiny": bool(row["shiny"]), "in_party": slot is not None,
            "iv_gains": iv_rewards["iv_gains"], "iv_floor": iv_rewards["iv_floor"],
            "message": i18n.t(lang, "breeding.hatched", name=name)}


# ------------------------------------------------------------- Uebersicht

def overview(user_id: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or _now()
    advance(user_id, now)
    mons, eggs = party_counts(user_id)
    user = db.get_user(user_id) or {}
    candidates = db.query(
        "SELECT o.id, o.species_id, o.nickname, o.level, o.gender, o.shiny, o.held_item, "
        "s.name, s.name_de, s.name_en, s.sprite_url FROM owned_pokemon o "
        "JOIN species s ON s.id = o.species_id WHERE o.user_id = ? "
        "ORDER BY o.party_slot IS NULL, o.party_slot, s.id, o.level DESC",
        (user_id,),
    )
    # Egg groups per candidate, so the frontend can filter the opposite-gender
    # dropdown to species that could actually pair with the current pick.
    for candidate in candidates:
        species = _species(candidate["species_id"])
        candidate["egg_groups"] = egg_groups(species) if species else []
    return {
        "eggs": list_eggs(user_id),
        "daycare": daycare_status(user_id, now),
        "incubators": incubators(user_id),
        "candidates": candidates,
        "party_pokemon": mons,
        "party_eggs": eggs,
        "party_size": PARTY_SIZE,
        "passive_steps_per_hour": passive_steps_per_hour(),
        "step_sensor": user.get("buddy_sensor"),
        "step_boost": _boost(user_id),
    }


# -------------------------------------------------------- PokeAPI-Nachtrag
# Ei-Gruppen und Ei-Zyklen kommen aus `/pokemon-species/{id}` - Muster wie
# `seed.backfill_name_en`. Bis dahin greifen die Ersatzwerte oben.
_task: "asyncio.Task | None" = None


def _species_missing_egg_data() -> list[int]:
    return [row["id"] for row in db.query("SELECT id FROM species WHERE egg_groups IS NULL")]


def parse_species_doc(doc: dict[str, Any]) -> tuple[list[str], int]:
    groups = [g["name"] for g in doc.get("egg_groups") or [] if g.get("name")]
    return groups, int(doc.get("hatch_counter") or DEFAULT_HATCH_COUNTER)


async def backfill(concurrency: int = 8) -> int:
    missing = _species_missing_egg_data()
    if not missing or not await seed.reachable():
        return 0
    updated = 0
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(timeout=20) as client:
        async def worker(dex_id: int) -> None:
            nonlocal updated
            try:
                async with sem:
                    doc = (await client.get(f"{seed.POKEAPI}/pokemon-species/{dex_id}")).json()
            except Exception:  # noqa: BLE001 - Netzwerk ist best effort
                return
            groups, counter = parse_species_doc(doc)
            if groups:
                db.execute("UPDATE species SET egg_groups = ?, hatch_counter = ? WHERE id = ?",
                           (json.dumps(groups), counter, dex_id))
                updated += 1

        await asyncio.gather(*(worker(dex_id) for dex_id in missing))
    return updated


async def _ensure_backfilled(after: "asyncio.Task | None" = None) -> None:
    if after is not None:
        try:
            await after
        except Exception:  # noqa: BLE001
            pass
    try:
        count = await backfill()
        if count:
            log.info("Ei-Gruppen nachgetragen: %s Spezies", count)
    except Exception as exc:  # noqa: BLE001
        log.warning("Ei-Gruppen-Nachtrag fehlgeschlagen: %s", exc)


def start_background_backfill(after: "asyncio.Task | None" = None) -> None:
    global _task
    _task = asyncio.create_task(_ensure_backfilled(after))
