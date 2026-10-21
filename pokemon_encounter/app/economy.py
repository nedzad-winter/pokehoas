"""Economy: money, shop and IV gain on repeat catches.

Answers the open questions "reward for catching" and "drops vs market" (README)
with a Poke Mart:

* Money is earned for successful catches (`award_catch`) and won battles
  (`award_battle_win`). The amount scales with level and rarity of the wild
  Pokemon; all dials are settings (see `DEFAULT_SETTINGS` in db.py), prices are
  in `data/shop.json`.
* Balance goal: an evolution stone costs about 3-5 average encounters in income
  (calculation see `expected_reward` or tests).
* Repeat catch (PokeRogue idea): whoever catches a species whose evolution
  family they already own, improves the IVs of their existing family members
  (`apply_repeat_catch_ivs`) - per member exactly one random, not-yet-maxed
  value increases by a few points.
* IV minimum per species (`species.min_ivs`, starts at 5 on every IV): every
  catch raises one not-yet-maxed value of the caught species the same way
  (`raise_species_min_ivs`); spawns clamp their random IVs to it
  (`spawn.roll_ivs`) and so does the catch itself (`clamp_ivs_to_floor`, covers
  encounters rolled before the floor rose), so after enough catches new spawns
  are perfect.

Battle/catch endpoints only call the hooks here - logic stays in this module so
encounters.py changes stay small.
"""

from __future__ import annotations

import json
import logging
import random
from datetime import datetime
from pathlib import Path
from typing import Any

from . import battle, db, god_mode, held_items, i18n, tms

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"
IV_KEYS = ["hp", "atk", "def", "spa", "spd", "spe"]
IV_MAX = 31
# Startwert der IV-Untergrenze pro Art (species.min_ivs == NULL). Stored values
# below it are lifted to it on read, so existing databases need no migration.
IV_MIN_START = 5
IV_LABELS = {
    "hp": "HP", "atk": "Attack", "def": "Defense",
    "spa": "Sp. Atk", "spd": "Sp. Def", "spe": "Speed",
}
# Hard lock in addition to "buyable": false in catalog.
NEVER_BUYABLE = {"master_ball"}
MAX_PURCHASE_QUANTITY = 99

_catalog: list[dict[str, Any]] | None = None


class EconomyError(Exception):
    """Business logic error with HTTP status - routes convert to HTTPException."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# -------------------------------------------------------------------- Catalog

def catalog() -> list[dict[str, Any]]:
    """All items from `data/shop.json`, plus every TM (`tms.catalog_entries`) -
    gym rewards, a few of them (`price` in `data/tms.json`) also buyable,
    cached."""
    global _catalog
    if _catalog is None:
        data = json.loads((DATA_DIR / "shop.json").read_text(encoding="utf-8"))
        items = data["items"]
        for item in items:
            item.setdefault("sprite_url", held_items.sprite_url_for(item["key"]))
        _catalog = items + tms.catalog_entries()
    return _catalog


def catalog_by_key() -> dict[str, dict[str, Any]]:
    return {item["key"]: item for item in catalog()}


def is_buyable(item: dict[str, Any]) -> bool:
    return bool(item.get("buyable", True)) and item["key"] not in NEVER_BUYABLE


def unit_price(item: dict[str, Any], multiplier: float | None = None) -> int:
    if multiplier is None:
        multiplier = float(db.get_setting("shop_price_multiplier", 1.0))
    return max(1, int(round(item["price"] * multiplier)))


def item_label(item_key: str, lang: str = "en") -> str:
    item = catalog_by_key().get(item_key)
    if item:
        return (item.get("label_de") if lang == "de" else None) or item["label"]
    if item_key in battle.BALLS:
        return battle.ball_label(item_key, lang)
    return item_key


# ---------------------------------------------------------------------- Geld

def economy_settings() -> dict[str, Any]:
    """Alle Wirtschafts-Einstellungen auf einmal (ein DB-Zugriff statt vieler)."""
    values = {key: value for key, value in db.DEFAULT_SETTINGS.items()
              if key.startswith(("money_", "iv_repeat_", "shop_", "starting_money"))}
    stored = db.all_settings()
    values.update({key: stored[key] for key in values if key in stored})
    return values


def reward_amount(
    species: dict[str, Any],
    level: int,
    *,
    shiny: bool = False,
    kind: str = "catch",
    cfg: dict[str, Any] | None = None,
) -> int:
    """Geldbelohnung fuer ein wildes Pokémon. `kind`: 'catch' | 'battle'.

    Formel siehe Kommentar bei den Einstellungen in db.py. Gerundet auf 10.
    """
    cfg = cfg or economy_settings()
    catch_rate = min(255, max(3, int(species.get("catch_rate") or 45)))
    base_exp = max(0, int(species.get("base_exp") or 64))
    rarity = (
        1.0
        + float(cfg["money_rarity_factor"]) * (255 - catch_rate) / 252
        + float(cfg["money_base_exp_factor"]) * base_exp / 100
    )
    if species.get("is_legendary") or species.get("is_mythical"):
        rarity *= float(cfg["money_legendary_multiplier"])

    amount = (float(cfg["money_base"]) + float(cfg["money_per_level"]) * int(level)) * rarity
    if shiny:
        amount *= float(cfg["money_shiny_multiplier"])
    amount *= float(
        cfg["money_battle_multiplier"] if kind == "battle" else cfg["money_catch_multiplier"]
    )
    return max(0, int(round(amount / 10.0)) * 10)


def balance(user_id: str) -> int:
    row = db.query_one("SELECT money FROM app_user WHERE id = ?", (user_id,))
    return int(row["money"] or 0) if row else 0


def credit(user_id: str, amount: int) -> int:
    """Schreibt Geld gut (oder ab, bei negativem Betrag) und gibt den neuen Stand zurueck."""
    with db.cursor() as cur:
        cur.execute("UPDATE app_user SET money = money + ? WHERE id = ?", (int(amount), user_id))
        cur.execute("SELECT money FROM app_user WHERE id = ?", (user_id,))
        row = cur.fetchone()
    return int(row["money"]) if row else 0


def _award(user_id: str, encounter_row: dict[str, Any], kind: str, multiplier: float = 1) -> int:
    species = db.query_one("SELECT * FROM species WHERE id = ?", (encounter_row["species_id"],))
    if species is None:
        return 0
    amount = reward_amount(
        species, int(encounter_row["level"]), shiny=bool(encounter_row.get("shiny")), kind=kind
    )
    amount = int(amount * multiplier)
    if amount:
        credit(user_id, amount)
    return amount


def award_catch(user_id: str, encounter_row: dict[str, Any]) -> int:
    """Geld fuer einen erfolgreichen Fang. Gibt den gutgeschriebenen Betrag zurueck."""
    return _award(user_id, encounter_row, "catch")


def award_battle_win(user_id: str, encounter_row: dict[str, Any], multiplier: float = 1) -> int:
    """Geld fuer einen gewonnenen Kampf (wildes Pokémon besiegt). Gibt den Betrag zurueck.

    `multiplier` doubles the amount when the team holds an Amulet Coin (`battle.on_battle_won`)."""
    return _award(user_id, encounter_row, "battle", multiplier=multiplier)


def grant_starting_money(user_id: str) -> int:
    """Startgeld bei der Starter-Wahl (analog zum Startinventar)."""
    amount = int(db.get_setting("starting_money", db.DEFAULT_SETTINGS["starting_money"]))
    if amount > 0:
        credit(user_id, amount)
    return amount


def expected_reward(level: int, kind: str = "catch") -> float:
    """Erwartete Belohnung bei gegebenem Level, gemittelt ueber die Spawn-
    Wahrscheinlichkeit aller Arten im Pokédex (fuer Balancing/Tests)."""
    from . import rarity
    from .spawn import _maxed_ivs_spawn_factor, _rarity_weight  # lokal: vermeidet Import-Zyklen

    cfg = economy_settings()
    multipliers = rarity.tier_multipliers()
    maxed_ivs_factor = _maxed_ivs_spawn_factor()
    species_list = db.query("SELECT * FROM species")
    total_weight = sum(_rarity_weight(sp, multipliers, maxed_ivs_factor) for sp in species_list)
    if not total_weight:
        return 0.0
    return sum(
        _rarity_weight(sp, multipliers, maxed_ivs_factor) * reward_amount(sp, level, kind=kind, cfg=cfg)
        for sp in species_list
    ) / total_weight


# ---------------------------------------------------------------------- Shop

def purchase(user_id: str, item_key: str, quantity: int = 1) -> dict[str, Any]:
    """Kauft `quantity` Stueck. Geldpruefung, Abbuchung und Gutschrift ins
    Inventar laufen in EINER Transaktion - bei zu wenig Geld passiert nichts."""
    lang = db.user_language(user_id)
    item = catalog_by_key().get(item_key)
    if item is None:
        raise EconomyError(400, i18n.t(lang, "error.unknown_item", item=item_key))
    free = god_mode.active(user_id)  # god mode: everything for sale, nothing costs
    if not is_buyable(item) and not free:
        raise EconomyError(400, i18n.t(lang, "error.item_not_buyable", item=item_label(item_key, lang)))
    if isinstance(quantity, bool) or not isinstance(quantity, int) \
            or not 1 <= quantity <= MAX_PURCHASE_QUANTITY:
        raise EconomyError(400, i18n.t(lang, "error.quantity_range", max=MAX_PURCHASE_QUANTITY))

    # Einstellung VOR der Transaktion lesen - db.cursor() ist nicht reentrant.
    price = 0 if free else unit_price(item)
    total = price * quantity

    with db.cursor() as cur:
        cur.execute(
            "UPDATE app_user SET money = money - ? WHERE id = ? AND money >= ?",
            (total, user_id, total),
        )
        if cur.rowcount != 1:
            raise EconomyError(409, i18n.t(lang, "error.not_enough_money"))
        cur.execute(
            """
            INSERT INTO inventory (item_key, user_id, quantity) VALUES (?,?,?)
            ON CONFLICT(item_key, user_id) DO UPDATE SET quantity = quantity + excluded.quantity
            """,
            (item_key, user_id, quantity),
        )
        cur.execute("SELECT money FROM app_user WHERE id = ?", (user_id,))
        money = cur.fetchone()["money"]
        cur.execute(
            "SELECT quantity FROM inventory WHERE item_key = ? AND user_id = ?",
            (item_key, user_id),
        )
        owned = cur.fetchone()["quantity"]

    return {
        "ok": True, "item_key": item_key, "quantity": quantity,
        "unit_price": price, "total": total, "money": money, "owned": owned,
    }


# -------------------------------------------------------- IV-Zuwachs (Fang)

def evolution_family(species_id: int) -> set[int]:
    """Alle Arten derselben Entwicklungsfamilie (ueber `species.evolves_from`)."""
    parents = {
        row["id"]: row["evolves_from"]
        for row in db.query("SELECT id, evolves_from FROM species")
    }
    roots: dict[int, int] = {}

    def root(sid: int) -> int:
        if sid in roots:
            return roots[sid]
        seen = {sid}
        current = sid
        while parents.get(current) and parents[current] not in seen:
            current = parents[current]
            seen.add(current)
        roots[sid] = current
        return current

    target = root(species_id)
    return {sid for sid in parents if root(sid) == target} | {species_id}


def _as_ivs(value: Any) -> dict[str, int]:
    return value if isinstance(value, dict) else json.loads(value or "{}")


def repeat_catch_gain_range(cfg: dict[str, Any] | None = None) -> tuple[int, int]:
    """(min, max) IV-Punkte pro Wiederholungsfang, robust gegen Unsinn in der DB."""
    cfg = cfg or economy_settings()
    low = min(IV_MAX, max(1, int(cfg.get("iv_repeat_catch_min_gain", 1))))
    high = min(IV_MAX, max(low, int(cfg.get("iv_repeat_catch_max_gain", 2))))
    return low, high


def roll_repeat_catch_gain(
    ivs: dict[str, Any], low: int, high: int, rng: random.Random | Any = random
) -> tuple[str, int, int] | None:
    """Ein zufaelliger, noch nicht maximaler Wert steigt um `low`..`high` Punkte.

    Gibt (Wert, vorher, nachher) zurueck oder None, wenn schon alles bei 31 ist.
    """
    candidates = [key for key in IV_KEYS if int(ivs.get(key, 0)) < IV_MAX]
    if not candidates:
        return None
    key = rng.choice(candidates)
    before = int(ivs.get(key, 0))
    return key, before, min(IV_MAX, before + rng.randint(low, high))


def apply_repeat_catch_ivs(
    user_id: str,
    encounter_row: dict[str, Any],
    exclude_owned_id: int | None = None,
    rng: random.Random | Any = random,
) -> list[dict[str, Any]]:
    """IV-Zuwachs fuer bereits besessene Mitglieder der Entwicklungsfamilie.

    Pro Familienmitglied steigt genau EIN zufaelliger Wert, der noch nicht bei
    31 ist, um zufaellig `iv_repeat_catch_min_gain`..`iv_repeat_catch_max_gain`
    Punkte (Standard 1-2, gedeckelt bei 31). Ist schon alles bei 31, passiert
    nichts. Die IVs des neuen Fangs spielen dafuer keine Rolle. Das frisch
    gefangene Pokémon selbst (`exclude_owned_id`) bleibt unveraendert.
    """
    cfg = economy_settings()
    if not cfg.get("iv_repeat_catch_enabled", True):
        return []
    low, high = repeat_catch_gain_range(cfg)

    family = sorted(evolution_family(int(encounter_row["species_id"])))
    placeholders = ",".join("?" * len(family))
    rows = db.query(
        f"""
        SELECT o.*, s.name AS species_name, s.name_de AS species_name_de,
               s.name_en AS species_name_en, s.base_hp
        FROM owned_pokemon o JOIN species s ON s.id = o.species_id
        WHERE o.user_id = ? AND o.species_id IN ({placeholders}) AND o.id != ?
        ORDER BY o.id
        """,
        (user_id, *family, exclude_owned_id if exclude_owned_id is not None else -1),
    )

    gains: list[dict[str, Any]] = []
    updates: list[tuple[str, int | None, int]] = []
    for row in rows:
        old = _as_ivs(row["ivs"])
        rolled = roll_repeat_catch_gain(old, low, high, rng)
        if rolled is None:
            continue
        key, before, after = rolled
        new = {**old, key: after}
        changes = [{"stat": key, "label": IV_LABELS[key], "from": before, "to": after}]

        current_hp = row["current_hp"]
        if current_hp is not None:
            # Hoeheres KP-IV hebt das Maximum - der Zuwachs kommt oben drauf,
            # damit ein volles Pokémon voll bleibt.
            ev_hp = battle._as_evs(row.get("evs")).get("hp", 0)
            old_max = battle.stat_hp(row["base_hp"], int(old.get("hp", 0)), row["level"], ev_hp,
                                     species_id=row["species_id"])
            new_max = battle.stat_hp(row["base_hp"], int(new.get("hp", 0)), row["level"], ev_hp,
                                     species_id=row["species_id"])
            if current_hp > 0:
                current_hp = min(new_max, current_hp + max(0, new_max - old_max))

        updates.append((json.dumps(new), current_hp, row["id"]))
        gains.append({
            "owned_id": row["id"],
            "species_id": row["species_id"],
            "name": row["nickname"] or row["species_name_en"] or row["species_name_de"] or row["species_name"],
            "changes": changes,
        })

    if updates:
        with db.cursor() as cur:
            cur.executemany(
                "UPDATE owned_pokemon SET ivs = ?, current_hp = ? WHERE id = ?", updates
            )
    return gains


def _min_ivs_from_value(stored_min_ivs: Any) -> dict[str, int]:
    """Core of `species_min_ivs`, operating on an already-read `min_ivs` value
    instead of querying by id - lets callers that already loaded the species
    row (e.g. `spawn._rarity_weight`, looping over the whole pool) skip a
    per-species DB roundtrip."""
    stored = _as_ivs(stored_min_ivs) if stored_min_ivs else {}
    return {
        key: min(IV_MAX, max(IV_MIN_START, int(stored.get(key, IV_MIN_START))))
        for key in IV_KEYS
    }


def species_min_ivs(species_id: int) -> dict[str, int]:
    """IV-Untergrenze einer Art fuer neue Spawns (Spalte `species.min_ivs`).

    Fehlt der Eintrag (NULL) oder einzelne Werte, gilt `IV_MIN_START`.
    """
    row = db.query_one("SELECT min_ivs FROM species WHERE id = ?", (int(species_id),))
    return _min_ivs_from_value(row["min_ivs"] if row else None)


def species_ivs_maxed(species_row: dict[str, Any]) -> bool:
    """True once every IV floor value of this species (`species.min_ivs`) is
    already 31 - another catch of it can no longer improve new spawns, since
    they already roll perfect IVs (see `spawn.roll_ivs`). `spawn._rarity_weight`
    lowers such a species' spawn weight instead of dropping it outright - it's
    still worth catching for the Pokedex, EVs and the repeat-catch IV bonus for
    less-maxed family members.

    Takes the already-loaded species row (only `min_ivs` is used) rather than
    an id, since every current caller already has the whole species list
    loaded for weighting.
    """
    floor = _min_ivs_from_value(species_row.get("min_ivs"))
    return all(value >= IV_MAX for value in floor.values())


def clamp_ivs_to_floor(ivs: dict[str, Any], species_id: int) -> dict[str, int]:
    """Lifts every IV of a caught Pokemon to the species' IV minimum.

    Spawns are already clamped on creation; this catches encounters that were
    rolled before the floor existed or rose (queued encounters keep their IVs).
    """
    floor = species_min_ivs(species_id)
    return {key: max(int(ivs.get(key, 0)), floor[key]) for key in IV_KEYS}


def raise_species_min_ivs(
    encounter_row: dict[str, Any], rng: random.Random | Any = random
) -> dict[str, Any] | None:
    """Hebt nach einem Fang die IV-Untergrenze der gefangenen Art.

    Gleiche Regel wie beim Wiederholungsfang: EIN zufaelliger, noch nicht
    maximaler Wert steigt um `iv_repeat_catch_min_gain`..`..._max_gain`. Die
    Untergrenze haengt an der Art und gilt damit bewusst haushaltsweit - jeder
    Fang irgendeines Nutzers zaehlt, auch wenn jeder seine eigene Warteschlange
    hat. Nach genug Faengen liegt sie ueberall bei 31.
    """
    cfg = economy_settings()
    if not cfg.get("iv_repeat_catch_enabled", True):
        return None
    low, high = repeat_catch_gain_range(cfg)
    species_id = int(encounter_row["species_id"])
    old = species_min_ivs(species_id)
    rolled = roll_repeat_catch_gain(old, low, high, rng)
    if rolled is None:
        return None
    key, before, after = rolled
    new = {**old, key: after}
    db.execute("UPDATE species SET min_ivs = ? WHERE id = ?", (json.dumps(new), species_id))
    species = db.query_one(
        "SELECT name, name_de, name_en FROM species WHERE id = ?", (species_id,)
    ) or {}
    return {
        "species_id": species_id,
        "name": species.get("name_en") or species.get("name_de") or species.get("name") or "",
        "min_ivs": new,
        "changes": [{"stat": key, "label": IV_LABELS[key], "from": before, "to": after}],
    }


def on_catch(
    user_id: str, encounter_row: dict[str, Any], new_owned_id: int | None = None
) -> dict[str, Any]:
    """Sammel-Haken fuer den Fang-Erfolgspfad: Geld + IV-Zuwachs + IV-Untergrenze.

    Gibt die Felder zurueck, die direkt in die Antwort von `/throw` wandern.
    """
    earned = award_catch(user_id, encounter_row)
    gains = apply_repeat_catch_ivs(user_id, encounter_row, new_owned_id)
    floor = raise_species_min_ivs(encounter_row)
    return {
        "money_earned": earned, "money": balance(user_id), "iv_gains": gains, "iv_floor": floor,
    }


def on_hatch(user_id: str, species_id: int, new_owned_id: int) -> dict[str, Any]:
    """Wie `on_catch`, aber nur die IV-Belohnungen (kein Geld): Schluepfen gibt
    dieselben IV-Zuwaechse wie ein Fang - Familienmitglieder und Art-Untergrenze."""
    gains = apply_repeat_catch_ivs(user_id, {"species_id": species_id}, new_owned_id)
    floor = raise_species_min_ivs({"species_id": species_id})
    return {"iv_gains": gains, "iv_floor": floor}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat()


# --------------------------------------------------------------- Rare Candy

RARE_CANDY = "rare_candy"
MAX_LEVEL = battle.MAX_LEVEL


def _owned_name(owned: dict[str, Any], species: dict[str, Any]) -> str:
    return owned.get("nickname") or species.get("name_en") or species.get("name_de") or species["name"]


def rare_candy_targets(user_id: str) -> list[dict[str, Any]]:
    """Eigene Pokemon, die noch ein Level gewinnen koennen (Auswahl im Beutel)."""
    rows = db.query(
        "SELECT * FROM owned_pokemon WHERE user_id = ? AND level < ? "
        "ORDER BY party_slot IS NULL, party_slot, level DESC, id",
        (user_id, MAX_LEVEL),
    )
    targets = []
    for row in rows:
        species = db.query_one("SELECT * FROM species WHERE id = ?", (row["species_id"],))
        targets.append({
            "owned_id": row["id"], "name": _owned_name(row, species), "level": row["level"],
            "in_party": row["party_slot"] is not None,
        })
    return targets


def use_rare_candy(user_id: str, owned_id: int, count: int = 1) -> dict[str, Any]:
    """Erhoeht das Level eines eigenen Pokemon um 1 je Rare Candy und
    verbraucht dabei genau so viele aus dem Inventar. `count` wird auf den
    Bestand und auf das Maximallevel begrenzt. KP skalieren wie bei einem
    normalen Levelaufstieg (gleicher Ansatz wie in `apply_repeat_catch_ivs`
    fuer current_hp)."""
    lang = db.user_language(user_id)
    owned = db.query_one(
        "SELECT * FROM owned_pokemon WHERE id = ? AND user_id = ?", (owned_id, user_id)
    )
    if owned is None:
        raise EconomyError(404, i18n.t(lang, "error.pokemon_not_found"))
    if owned["level"] >= MAX_LEVEL:
        raise EconomyError(400, i18n.t(lang, "error.already_max_level"))

    species = db.query_one("SELECT * FROM species WHERE id = ?", (owned["species_id"],))
    ivs = _as_ivs(owned["ivs"])
    stock = db.query_one(
        "SELECT quantity FROM inventory WHERE item_key = ? AND user_id = ?", (RARE_CANDY, user_id)
    )
    used = min(max(1, int(count)), MAX_LEVEL - owned["level"], max(1, stock["quantity"] if stock else 0))
    new_level = owned["level"] + used
    old_max = battle.stat_hp(species["base_hp"], int(ivs.get("hp", 0)), owned["level"],
                             species_id=species["id"])
    new_max = battle.stat_hp(species["base_hp"], int(ivs.get("hp", 0)), new_level,
                             species_id=species["id"])
    current_hp = owned["current_hp"]
    if current_hp is not None and current_hp > 0:
        current_hp = min(new_max, current_hp + max(0, new_max - old_max))
    growth = species.get("growth_rate", "medium_fast")
    new_exp = max(owned["exp"], battle.exp_for_level(new_level, growth))

    with db.cursor() as cur:
        cur.execute(
            "UPDATE inventory SET quantity = quantity - ? "
            "WHERE item_key = ? AND user_id = ? AND quantity >= ?",
            (used, RARE_CANDY, user_id, used),
        )
        if cur.rowcount != 1:
            raise EconomyError(409, i18n.t(lang, "error.item_not_in_bag", item=item_label(RARE_CANDY, lang)))
        cur.execute(
            "UPDATE owned_pokemon SET level = ?, exp = ?, current_hp = ? WHERE id = ? AND level < ?",
            (new_level, new_exp, current_hp, owned_id, MAX_LEVEL),
        )
        if cur.rowcount != 1:
            # Raised inside the transaction: the candy decrement is rolled back too.
            raise EconomyError(400, i18n.t(lang, "error.already_max_level"))

    from . import achievements  # lazy: achievements -> rewards -> economy would else be circular

    new_achievements = achievements.check_level100(
        user_id, [{"owned_id": owned_id, "level_to": new_level}], lang,
    )
    name = _owned_name(owned, species)
    message = (i18n.t(lang, "rare_candy.result_multi", name=name, count=used, level=new_level)
               if used > 1 else i18n.t(lang, "rare_candy.result", name=name, level=new_level))
    for ach in new_achievements:
        message += " " + i18n.t(lang, "battle.log.achievement_unlocked", title=ach["title"])
        if ach.get("reward"):
            message += " " + i18n.t(lang, "battle.log.achievement_reward", label=ach["reward"]["label"])
    return {
        "ok": True, "owned_id": owned_id, "item_key": RARE_CANDY, "level": new_level, "used": used,
        "current_hp": current_hp, "max_hp": new_max,
        "message": message, "achievements": new_achievements,
    }
