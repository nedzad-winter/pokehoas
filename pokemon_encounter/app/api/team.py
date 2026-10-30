"""Team management: team (positions 1-6) and box, held items, healing,
level evolution (triggered by player in Pokemon menu, see evolution.py).

Each endpoint works exclusively with the requesting user's Pokemon - other
Pokemon return 403. During an ongoing battle, team changes are locked (the
battle holds a snapshot of it).
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import abilities, autoheal, battle, breeding, db, economy, evs, healing, held_items, i18n, mega, rarity, seed, tms
from .. import evolution
from ..economy import EconomyError
from .. import moves as moves_mod
from ..auth import get_current_user

router = APIRouter(prefix="/api", tags=["team"])

PARTY_SIZE = 6


class PartyOrder(BaseModel):
    order: list[int]  # owned_pokemon.id in desired order (position 1..6)


class HeldItemChange(BaseModel):
    item: str | None = None  # None = remove


class FavoriteChange(BaseModel):
    favorite: bool


class LockedChange(BaseModel):
    locked: bool


class LockManyIn(BaseModel):
    ids: list[int]  # owned_pokemon.id (party or box)
    locked: bool


class MoveSelection(BaseModel):
    moves: list[str]


class EvolveIn(BaseModel):
    # Target species; only needed if multiple level evolutions are possible.
    to_species_id: int | None = None


def _species(species_id: int) -> dict[str, Any]:
    return db.query_one("SELECT * FROM species WHERE id = ?", (species_id,))


def _shiny_sprite_url(sprite_url: str | None) -> str | None:
    if not sprite_url:
        return None
    return sprite_url.replace("/sprites/pokemon/", "/sprites/pokemon/shiny/")


def _base_stats(species: dict[str, Any]) -> dict[str, int]:
    return {
        "hp": species["base_hp"], "atk": species["base_atk"], "def": species["base_def"],
        "spa": species["base_spa"], "spd": species["base_spd"], "spe": species["base_spe"],
    }


def describe_owned(row: dict[str, Any]) -> dict[str, Any]:
    species = _species(row["species_id"])
    combatant = battle.combatant_from_owned(row)
    growth = species.get("growth_rate", "medium_fast")
    level = row["level"]
    return {
        "id": row["id"],
        "species_id": row["species_id"],
        "species": {
            "id": species["id"], "name": species["name"], "name_de": species.get("name_de"), "name_en": species.get("name_en"),
            "type1": species["type1"], "type2": species.get("type2"),
            "generation": species.get("generation"),
            "sprite_url": species.get("sprite_url"),
            "shiny_sprite_url": _shiny_sprite_url(species.get("sprite_url")),
            "base_stats": _base_stats(species),
            # Box filters: evolution stage (1 = base form) and rarity tier.
            "stage": evolution.stage_of(species["id"]),
            "rarity_tier": rarity.tier_for(species),
        },
        "name": row.get("nickname") or combatant.name,
        "level": level,
        "exp": row["exp"],
        "exp_level_start": battle.exp_for_level(level, growth),
        "exp_next_level": battle.exp_for_level(level + 1, growth) if level < 100 else None,
        "current_hp": combatant.current_hp,
        "max_hp": combatant.max_hp,
        "fainted": combatant.fainted,
        "ivs": battle._as_ivs(row["ivs"]),
        # Haushaltsweite IV-Untergrenze dieser Art (species.min_ivs, siehe
        # economy.py) - fuer den IV-Chart im Pokemon-Menue, NICHT die IVs
        # dieses einzelnen Pokemon.
        "min_ivs": economy.species_min_ivs(row["species_id"]),
        # Fleisspunkte (EVs) - im Gegensatz zu den IVs vom Spieler selbst
        # gesetzt, ueber Vitamine/Federn/Beeren (siehe app/evs.py).
        "evs": evs.describe(row),
        # Eigene (individuelle) Werte dieses Pokémon - aus Basiswert + IV + Level
        # berechnet (siehe battle.stat_hp/stat_other), NICHT die Basiswerte der
        # Art. Zum Vergleichen zweier Pokémon derselben Art sind das die
        # relevanten Werte (Pokémon-Menue zeigt diese statt sp.base_stats).
        "stats": {"hp": combatant.max_hp} | {key: combatant.stat(key) for key in ("atk", "def", "spa", "spd", "spe")},
        "nature": row.get("nature"),
        "gender": row.get("gender"),
        "shiny": bool(row.get("shiny")),
        "favorite": bool(row.get("favorite")),
        # Locked: can't be released from the box (see `_release`); legendary
        # and mythical catches arrive locked automatically (rarity.auto_lock).
        "locked": bool(row.get("locked")),
        "party_slot": row.get("party_slot"),
        "held_item": row.get("held_item"),
        "held_item_info": held_items.get(row.get("held_item")),
        # Mega-Form, die dieses Pokémon mit seinem getragenen Stein im Kampf
        # erreicht (None = kein passender Stein angelegt, siehe mega.py).
        "mega_form": mega.describe(mega.form_for(row["species_id"], row.get("held_item"))),
        # Alle Steine, die zu dieser Art ueberhaupt passen - Hinweis im
        # Pokémon-Menue, auch ohne angelegten Stein.
        "mega_forms": [mega.describe(form) for form in mega.forms_for_species(row["species_id"])],
        "ability": row.get("ability"),
        "ability_info": abilities.describe(row.get("ability")),
        "moves": [moves_mod.describe(key) for key in combatant.moves],
        # Jetzt moegliche Level-Entwicklungen (leer = keine). Entwickelt wird
        # nur auf Wunsch per POST /team/{id}/evolve, nie automatisch.
        "level_evolutions": evolution.level_evolutions(row),
        # Item-Entwicklungen, fuer die das noetige Item schon im Beutel liegt
        # (leer = keines, oder Item fehlt). Anwenden weiterhin ueber
        # POST /pokemon/{id}/use-item, wie aus dem Beutel-Tab.
        "item_evolutions": evolution.item_evolutions(row, row["user_id"]),
    }


@router.get("/pokedex")
def get_pokedex(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    rows = db.query(
        """
        SELECT s.id, s.name, s.name_de, s.name_en, s.type1, s.type2, s.generation, s.sprite_url,
               s.is_legendary, s.is_mythical, s.evolves_from,
               s.base_hp, s.base_atk, s.base_def, s.base_spa, s.base_spd, s.base_spe,
               COALESCE(d.seen_count, 0) AS seen_count,
               COALESCE(d.caught_count, 0) AS caught_count,
               COALESCE(d.shiny_caught, 0) AS shiny_caught,
               o.max_level
        FROM species s
        LEFT JOIN dex_entry d ON d.species_id = s.id AND d.user_id = ?
        LEFT JOIN (
            SELECT species_id, MAX(level) AS max_level FROM owned_pokemon
            WHERE user_id = ? GROUP BY species_id
        ) o ON o.species_id = s.id
        ORDER BY s.id
        """,
        (current_user["id"], current_user["id"]),
    )
    for row in rows:
        row["rarity_tier"] = rarity.tier_for(row)
        # Same evolution stage (1-3) the box filters on, kept here so the
        # Pokédex filter bar can offer the identical "stage" control.
        row["stage"] = evolution.stage_of(row["id"])
        # True while at least one of this species currently sits in the
        # user's team or box (released/traded-away Pokemon are deleted from
        # owned_pokemon, so a present row always means "currently owned").
        row["owned_now"] = row["max_level"] is not None
    return {
        "seen": sum(1 for row in rows if row["seen_count"]),
        "caught": sum(1 for row in rows if row["caught_count"]),
        "total": len(rows),
        "entries": rows,
    }


@router.get("/pokedex/{species_id}")
def get_pokedex_entry(
    species_id: int, current_user: dict = Depends(get_current_user)
) -> dict[str, Any]:
    species = _species(species_id)
    if species is None:
        raise HTTPException(status_code=404, detail=i18n.t(current_user["language"], "error.unknown_species"))
    dex = db.query_one(
        "SELECT seen_count, caught_count, shiny_caught, first_seen_at, first_caught_at "
        "FROM dex_entry WHERE species_id = ? AND user_id = ?",
        (species_id, current_user["id"]),
    ) or {"seen_count": 0, "caught_count": 0, "shiny_caught": 0,
          "first_seen_at": None, "first_caught_at": None}
    return {
        "species": {
            "id": species["id"], "name": species["name"], "name_de": species.get("name_de"), "name_en": species.get("name_en"),
            "type1": species["type1"], "type2": species.get("type2"),
            "generation": species["generation"],
            "sprite_url": species.get("sprite_url"),
            "shiny_sprite_url": _shiny_sprite_url(species.get("sprite_url")),
            "is_legendary": bool(species.get("is_legendary")),
            "is_mythical": bool(species.get("is_mythical")),
            "rarity_tier": rarity.tier_for(species),
            "base_stats": _base_stats(species),
        },
        "seen": bool(dex["seen_count"]),
        "caught": bool(dex["caught_count"]),
        "shiny_caught": bool(dex["shiny_caught"]),
        "first_seen_at": dex["first_seen_at"],
        "first_caught_at": dex["first_caught_at"],
        "evolution_line": evolution.line_for(species_id),
        # Household-wide IV floor already unlocked for this species (new
        # spawns never roll below it, see economy.species_min_ivs).
        "min_ivs": economy.species_min_ivs(species_id),
    }


@router.get("/team/{owned_id}")
def get_pokemon(owned_id: int, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    row = _own(owned_id, current_user["id"])
    result = describe_owned(row)
    species = _species(row["species_id"])
    selected = {move["key"] for move in result["moves"]}
    # Moves taught by a TM (tms.py) and inherited egg moves (breeding.py) sit
    # in the same pool - flagged, because they aren't level-up moves.
    taught = tms.taught_moves(row)
    egg = moves_mod.inherited_egg_moves(row)
    result["learned_moves"] = [
        {**moves_mod.describe(key), "selected": key in selected,
         "learn_level": moves_mod.learn_level(species, key),
         "via_tm": key in taught and moves_mod.learn_level(species, key) is None,
         "via_egg": key in egg and moves_mod.learn_level(species, key) is None}
        for key in moves_mod.learned_moves(species, row["level"], taught, egg)
    ]
    # Level-up moves still to come (`moves.learn_level` above the current level).
    entry = moves_mod.learnset(species) or {"level": {}}
    result["upcoming_moves"] = [
        {**moves_mod.describe(key), "learn_level": lvl}
        for key, lvl in sorted(entry["level"].items(), key=lambda item: (item[1], item[0]))
        if lvl > row["level"]
    ]
    result["evolution_line"] = evolution.line_for(row["species_id"])
    return result


@router.put("/team/{owned_id}/moves")
def set_moves(
    owned_id: int, payload: MoveSelection, current_user: dict = Depends(get_current_user)
) -> dict[str, Any]:
    user_id = current_user["id"]
    lang = current_user["language"]
    _not_in_battle(user_id)
    row = _own(owned_id, user_id)
    if not 1 <= len(payload.moves) <= moves_mod.MAX_MOVES:
        raise HTTPException(status_code=400, detail=i18n.t(lang, "error.moves_choose_range", max=moves_mod.MAX_MOVES))
    if len(set(payload.moves)) != len(payload.moves):
        raise HTTPException(status_code=400, detail=i18n.t(lang, "error.moves_no_duplicates"))
    learned = set(moves_mod.owned_learned_moves(_species(row["species_id"]), row))
    if any(key not in learned for key in payload.moves):
        raise HTTPException(status_code=400, detail=i18n.t(lang, "error.move_not_learned"))
    db.execute("UPDATE owned_pokemon SET selected_moves = ? WHERE id = ?", (json.dumps(payload.moves), owned_id))
    return get_pokemon(owned_id, current_user)


@router.post("/team/{owned_id}/evolve")
def evolve(
    owned_id: int, payload: EvolveIn | None = None, current_user: dict = Depends(get_current_user)
) -> dict[str, Any]:
    """Level-Entwicklung ausloesen. Antwort wie bei der Item-Anwendung
    (from/to/KP/Werte) plus die aktualisierte Detailansicht unter `pokemon`."""
    user_id = current_user["id"]
    _not_in_battle(user_id)
    _own(owned_id, user_id)
    try:
        result = evolution.evolve_by_level(
            user_id, owned_id, payload.to_species_id if payload else None
        )
    except EconomyError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    result["pokemon"] = get_pokemon(owned_id, current_user)
    return result


class EvolveManyIn(BaseModel):
    ids: list[int]  # owned_pokemon.id of the selected Pokemon


@router.post("/team/evolve-many")
def evolve_many(payload: EvolveManyIn, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """Evolves the selected Pokemon at once (box selection) - only those with
    exactly one level evolution and no item needed; the rest is returned as
    `skipped`."""
    user_id = current_user["id"]
    ids = list(dict.fromkeys(payload.ids))
    if not ids:
        raise HTTPException(status_code=400, detail=i18n.t(current_user["language"], "error.evolve_none_selected"))
    _not_in_battle(user_id)
    for owned_id in ids:
        _own(owned_id, user_id)
    return evolution.evolve_many_by_level(user_id, ids)


def _own(owned_id: int, user_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (owned_id,))
    if row is None:
        raise HTTPException(status_code=404, detail=i18n.t(db.user_language(user_id), "error.pokemon_not_found"))
    if row["user_id"] != user_id:
        raise HTTPException(status_code=403, detail=i18n.t(db.user_language(user_id), "error.not_your_pokemon"))
    return row


def _not_in_battle(user_id: str) -> None:
    if healing.has_ongoing_battle(user_id):
        raise HTTPException(
            status_code=409,
            detail=i18n.t(db.user_language(user_id), "error.team_locked_in_battle"),
        )


def _held_inventory(user_id: str) -> dict[str, int]:
    catalog = held_items.catalog()
    return {
        row["item_key"]: row["quantity"]
        for row in db.query(
            "SELECT item_key, quantity FROM inventory WHERE user_id = ? AND quantity > 0",
            (user_id,),
        )
        if row["item_key"] in catalog
    }


@router.get("/team")
def get_team(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    user_id = current_user["id"]
    healing.apply_regen(user_id)
    rows = db.query(
        "SELECT * FROM owned_pokemon WHERE user_id = ? "
        "ORDER BY party_slot IS NULL, party_slot, level DESC, id",
        (user_id,),
    )
    described = [describe_owned(row) for row in rows]
    breeding.advance(user_id)
    # Eier im Team belegen Teamplaetze, kaempfen aber nicht (siehe breeding.py).
    party_eggs = [egg for egg in breeding.list_eggs(user_id) if egg["location"] == "party"]
    return {
        "party": [p for p in described if p["party_slot"] is not None],
        "box": [p for p in described if p["party_slot"] is None],
        "party_eggs": party_eggs,
        "party_size": PARTY_SIZE,
        "held_items": held_items.catalog(),
        "held_inventory": _held_inventory(user_id),
        "heal": healing.heal_status(user_id),
        "auto_heal": autoheal.status(user_id),
        "in_battle": healing.has_ongoing_battle(user_id),
    }


@router.put("/team/party")
def set_party(payload: PartyOrder, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """Setzt das komplette Team in der gegebenen Reihenfolge (hinzufuegen,
    entfernen, umsortieren in einem). Alle anderen Pokémon wandern in die Box -
    wenn `box_item_return` an ist (Standard), legen sie dabei ihr getragenes
    Item automatisch ab, das wandert zurueck ins Inventar."""
    user_id = current_user["id"]
    lang = current_user["language"]
    _not_in_battle(user_id)
    order = payload.order
    if not order:
        raise HTTPException(status_code=400, detail=i18n.t(lang, "error.team_needs_one"))
    _, party_eggs = breeding.party_counts(user_id)
    if len(order) + party_eggs > PARTY_SIZE:
        raise HTTPException(status_code=400, detail=i18n.t(lang, "error.team_max_size", max=PARTY_SIZE))
    if len(set(order)) != len(order):
        raise HTTPException(status_code=400, detail=i18n.t(lang, "error.pokemon_once_in_team"))
    for owned_id in order:
        _own(owned_id, user_id)

    returns_items = db.user_box_item_return(user_id)
    returned_items: list[str] = []
    with db.cursor() as cur:
        boxed = []
        if returns_items:
            cur.execute(
                "SELECT id, held_item FROM owned_pokemon "
                "WHERE user_id = ? AND party_slot IS NOT NULL AND held_item IS NOT NULL",
                (user_id,),
            )
            boxed = [row for row in cur.fetchall() if row["id"] not in set(order)]
        cur.execute("UPDATE owned_pokemon SET party_slot = NULL WHERE user_id = ?", (user_id,))
        for slot, owned_id in enumerate(order, start=1):
            cur.execute(
                "UPDATE owned_pokemon SET party_slot = ? WHERE id = ? AND user_id = ?",
                (slot, owned_id, user_id),
            )
        for row in boxed:
            cur.execute("UPDATE owned_pokemon SET held_item = NULL WHERE id = ?", (row["id"],))
            returned_items.append(row["held_item"])
    for item_key in returned_items:
        _inventory_add(user_id, item_key, 1)
    return get_team(current_user)


def _inventory_add(user_id: str, item_key: str, delta: int) -> None:
    db.execute(
        """
        INSERT INTO inventory (item_key, user_id, quantity) VALUES (?,?,?)
        ON CONFLICT(item_key, user_id) DO UPDATE SET quantity = quantity + excluded.quantity
        """,
        (item_key, user_id, delta),
    )


@router.put("/team/{owned_id}/held-item")
def set_held_item(
    owned_id: int, payload: HeldItemChange, current_user: dict = Depends(get_current_user)
) -> dict[str, Any]:
    """Item anlegen (aus dem Inventar) oder ablegen (zurueck ins Inventar).
    Ein bereits getragenes Item wandert beim Tausch zurueck ins Inventar."""
    user_id = current_user["id"]
    lang = current_user["language"]
    _not_in_battle(user_id)
    row = _own(owned_id, user_id)
    new_item = payload.item or None
    if new_item is not None and held_items.get(new_item) is None:
        raise HTTPException(status_code=400, detail=i18n.t(lang, "error.unknown_item_key", item=new_item))
    if new_item == row.get("held_item"):
        return describe_owned(row)

    with db.cursor() as cur:
        if new_item is not None:
            cur.execute(
                "UPDATE inventory SET quantity = quantity - 1 "
                "WHERE item_key = ? AND user_id = ? AND quantity > 0",
                (new_item, user_id),
            )
            if cur.rowcount != 1:
                raise HTTPException(status_code=409, detail=i18n.t(lang, "error.item_not_in_inventory"))
        cur.execute("UPDATE owned_pokemon SET held_item = ? WHERE id = ?", (new_item, owned_id))
    if row.get("held_item"):
        _inventory_add(user_id, row["held_item"], 1)
    return describe_owned(_own(owned_id, user_id))


@router.put("/team/{owned_id}/favorite")
def set_favorite(
    owned_id: int, payload: FavoriteChange, current_user: dict = Depends(get_current_user)
) -> dict[str, Any]:
    """Favorite flag for filtering the box - purely cosmetic, so allowed even
    while a battle locks other team changes."""
    user_id = current_user["id"]
    _own(owned_id, user_id)
    db.execute(
        "UPDATE owned_pokemon SET favorite = ? WHERE id = ?",
        (1 if payload.favorite else 0, owned_id),
    )
    return describe_owned(_own(owned_id, user_id))


@router.put("/team/{owned_id}/locked")
def set_locked(
    owned_id: int, payload: LockedChange, current_user: dict = Depends(get_current_user)
) -> dict[str, Any]:
    """Lock flag to protect a Pokémon from being released - like favorite,
    cosmetic/protective only, so allowed even while a battle locks other team
    changes. Legendary/mythical catches start out locked (rarity.auto_lock);
    this lets the player lock/unlock any Pokémon by hand."""
    user_id = current_user["id"]
    _own(owned_id, user_id)
    db.execute(
        "UPDATE owned_pokemon SET locked = ? WHERE id = ?",
        (1 if payload.locked else 0, owned_id),
    )
    return describe_owned(_own(owned_id, user_id))


@router.post("/team/lock-many")
def set_locked_many(payload: LockManyIn, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """Locks/unlocks several Pokémon at once (box selection) - same rules as
    PUT /team/{id}/locked. Ownership of every id is checked first, so a bad id
    leaves all of them unchanged. Returns how many rows actually changed."""
    user_id = current_user["id"]
    ids = list(dict.fromkeys(payload.ids))
    if not ids:
        raise HTTPException(status_code=400, detail=i18n.t(current_user["language"], "error.lock_none_selected"))
    rows = [_own(owned_id, user_id) for owned_id in ids]
    flag = 1 if payload.locked else 0
    changed = [row["id"] for row in rows if int(row.get("locked") or 0) != flag]
    with db.cursor() as cur:
        for owned_id in changed:
            cur.execute("UPDATE owned_pokemon SET locked = ? WHERE id = ? AND user_id = ?", (flag, owned_id, user_id))
    return {"ok": True, "locked": bool(flag), "changed": len(changed), "ids": changed}


@router.post("/team/heal")
def heal(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    try:
        return healing.heal_team(current_user["id"])
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/team/auto-heal")
def auto_heal(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """Heals the team with the cheapest fitting potions (see autoheal.py)."""
    try:
        return autoheal.run(current_user["id"])
    except EconomyError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


# Bonbons pro 10 Level, die das freigelassene Pokemon hatte (Lv. 25 -> 2, Lv. 9 -> 0).
RARE_CANDY_LEVEL_STEP = 10


class ReleaseIn(BaseModel):
    ids: list[int]  # owned_pokemon.id of box Pokemon to release


def _release(user_id: str, lang: str, owned_ids: list[int]) -> list[dict[str, Any]]:
    """Gibt die Pokemon `owned_ids` (alle aus der Box) in einer Transaktion frei.
    Alle Pruefungen laufen vorher - schlaegt eine fehl, bleibt alles unveraendert.
    Liefert pro Pokemon Name, Level und den Bonbon-Anteil."""
    _not_in_battle(user_id)
    released = []
    for owned_id in owned_ids:
        row = _own(owned_id, user_id)
        if row["party_slot"] is not None:
            raise HTTPException(status_code=400, detail=i18n.t(lang, "error.release_only_from_box"))
        species = _species(row["species_id"])
        name = row.get("nickname") or species.get("name_en") or species.get("name_de") or species["name"]
        if row.get("locked"):
            raise HTTPException(status_code=400, detail=i18n.t(lang, "error.release_locked", name=name))
        released.append({
            "id": owned_id,
            "name": name,
            "level": row["level"],
            "rare_candy_awarded": row["level"] // RARE_CANDY_LEVEL_STEP,
        })

    candy = sum(entry["rare_candy_awarded"] for entry in released)
    with db.cursor() as cur:
        for entry in released:
            cur.execute("DELETE FROM owned_pokemon WHERE id = ? AND user_id = ?", (entry["id"], user_id))
        if candy:
            cur.execute(
                """
                INSERT INTO inventory (item_key, user_id, quantity) VALUES (?,?,?)
                ON CONFLICT(item_key, user_id) DO UPDATE SET quantity = quantity + excluded.quantity
                """,
                ("rare_candy", user_id, candy),
            )
    return released


@router.delete("/team/{owned_id}")
def release(owned_id: int, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """Laesst ein eigenes Pokemon endgueltig frei - nur aus der Box (wie bei
    anderen Team-Aenderungen: erst aus dem Team nehmen). Belohnung: 1 Rare
    Candy pro 10 Level, die das Pokemon hatte."""
    entry = _release(current_user["id"], current_user["language"], [owned_id])[0]
    return {"ok": True, **entry}


@router.post("/team/release")
def release_many(payload: ReleaseIn, current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """Gibt mehrere eigene Box-Pokemon auf einmal frei (Auswahl in der Box).
    Gleiche Regeln wie DELETE /team/{id}; die Bonbons aller Pokemon werden
    gesammelt gutgeschrieben."""
    lang = current_user["language"]
    ids = list(dict.fromkeys(payload.ids))  # doppelte Klicks ignorieren
    if not ids:
        raise HTTPException(status_code=400, detail=i18n.t(lang, "error.release_none_selected"))
    released = _release(current_user["id"], lang, ids)
    return {"ok": True, "released": released, "rare_candy_awarded": sum(e["rare_candy_awarded"] for e in released)}

@router.get("/species-names/{lang}")
def species_names(lang: str) -> dict[str, Any]:
    """Species names in `lang` (PokeAPI translations, see seed.NAME_LANGS).
    English is already the default display name, so it returns nothing."""
    if lang == "en" or lang not in seed.NAME_LANGS:
        return {"names": {}, "by_en": {}}
    return db.species_name_tables(lang)