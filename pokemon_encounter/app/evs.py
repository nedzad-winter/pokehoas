"""Effort values (EVs): yield of defeated opponents plus the items that train them.

An EV is a training point on ONE stat. Like in the main games from generation 3
on, every 4 EVs in a stat count like one more IV point, so the stat formulas in
`battle.py` use `floor(EV/4)`: 252 EVs (the per-stat cap) are worth +63 points
at level 100, +31 at level 50. Limits are settings (`ev_max_per_stat` 252,
`ev_max_total` 510), so a Pokemon can max out two stats and put the rest
somewhere else - EVs are the part of a Pokemon the player picks, in contrast to
its randomly rolled IVs.

EVs come from two sources:

* Defeated opponents: like in the main games every species has an EV yield
  (`species.ev_yield`, the `effort` of the PokeAPI stats - Pikachu 2 Speed,
  Geodude 1 Defense, ...). Every own Pokemon that took part in the fight and
  was still standing when the opponent fell gets its yield, capped at the
  limits; it keeps the EVs even if it faints later or the whole fight is lost.
  Wild battles (also auto-battle) and gym/league battles count; a catch doesn't
  (nothing was defeated) and neither do player
  battles (sparring against an AI copy would be farmable). The setting
  `ev_battle_gain_enabled` switches it off - then items are the only source.
* Items bought with money (`data/shop.json`, category "vitamin"), for
  players who don't want to depend on battles:

  - Vitamins (HP Up, Protein, Iron, Calcium, Zinc, Carbos): +10 EVs on their stat.
  - Wings (Health, Muscle, Resist, Genius, Clever, Swift Wing): +1 EV - cheap
    fine-tuning for the last few points below a cap.
  - Berries (Pomeg, Kelpsy, Qualot, Hondew, Grepa, Tamato): -10 EVs, the way
    back out of a misinvested point (EVs are permanent otherwise, and the 510
    total budget would stay spent).

An item that would have no effect (stat already at its cap, total budget spent,
berry on a stat without EVs) is rejected BEFORE it is consumed, exactly like
healing items (see medicine.py). EVs live in `owned_pokemon.evs` (JSON, see
`db._migrate_evs`); NULL/missing means all zero, so old save data needs no
backfill. Raising the HP EV raises max HP right away and adds the gained HP on
top of the current HP, like a level up does.

Use: `POST /api/pokemon/{id}/use-item` (Bag tab, own category "Vitamins &
Berries") - not in battle, where a changing max HP would be confusing. An
optional `quantity` (like Rare Candy's) applies several items at once; it is
clamped to the stock and to however many of them still have an effect, so a
stack of vitamins used in bulk never overshoots a cap and never wastes items
past it.
"""

from __future__ import annotations

import json
from typing import Any

from . import battle, db, economy, healing, i18n
from .economy import EconomyError

# Bag/shop category of every EV item (see `data/shop.json`).
CATEGORY = "vitamin"

STAT_KEYS = ("hp", "atk", "def", "spa", "spd", "spe")

# EVs per stat point (main-series formula, see battle.stat_hp/stat_other).
EV_PER_POINT = 4


# -------------------------------------------------------------- Limits

def limits() -> tuple[int, int]:
    """(max per stat, max in total) from the settings."""
    per_stat = int(db.get_setting("ev_max_per_stat", db.DEFAULT_SETTINGS["ev_max_per_stat"]))
    total_max = int(db.get_setting("ev_max_total", db.DEFAULT_SETTINGS["ev_max_total"]))
    return max(0, per_stat), max(0, total_max)


# -------------------------------------------------------------- Values

def as_evs(value: Any) -> dict[str, int]:
    """Full EV dict from DB column, dict or None - missing values are 0.
    Deliberately NOT clamped to the current limits: lowering a limit later
    only stops further training, it never takes points away."""
    raw = battle._as_evs(value)
    return {key: max(0, int(raw.get(key) or 0)) for key in STAT_KEYS}


def total(evs: dict[str, int]) -> int:
    return sum(max(0, int(evs.get(key) or 0)) for key in STAT_KEYS)


def points(ev: int) -> int:
    """Stat points an EV value is worth at level 100 (floor(EV/4))."""
    return max(0, int(ev)) // EV_PER_POINT


def describe(row: dict[str, Any]) -> dict[str, Any]:
    """EV block for the API (Pokemon detail view)."""
    evs = as_evs(row.get("evs"))
    per_stat, total_max = limits()
    return {
        "values": evs,
        "total": total(evs),
        "max_per_stat": per_stat,
        "max_total": total_max,
        "remaining": max(0, total_max - total(evs)),
        # Points this EV investment is worth at level 100 - the number the
        # player actually feels (see battle.stat_hp/stat_other).
        "points": {key: points(value) for key, value in evs.items()},
    }


# -------------------------------------------------------------- Catalog

def catalog() -> dict[str, dict[str, Any]]:
    return {key: item for key, item in economy.catalog_by_key().items()
            if item.get("category") == CATEGORY}


def get(item_key: str | None) -> dict[str, Any] | None:
    return catalog().get(item_key) if item_key else None


def is_ev_item(item_key: str | None) -> bool:
    return get(item_key) is not None


def label(item: dict[str, Any], lang: str) -> str:
    return (item.get("label_de") if lang == "de" else None) or item["label"]


def stat_of(item: dict[str, Any]) -> str:
    return str(item["ev_stat"])


def amount_of(item: dict[str, Any]) -> int:
    return int(item["ev_amount"])


# --------------------------------------------------------------- Effect

def compute_change(
    evs: dict[str, int], item: dict[str, Any],
    per_stat: int | None = None, total_max: int | None = None,
) -> tuple[dict[str, int], int] | str:
    """Pure rules function: effect of `item` on these EVs. Returns the new EVs
    plus the actually applied change, or the i18n key explaining why the item
    would have no effect. A gain is capped at both limits instead of being
    rejected - the last vitamin below a cap still gives what fits."""
    if per_stat is None or total_max is None:
        per_stat, total_max = limits()
    evs = as_evs(evs)
    stat, amount = stat_of(item), amount_of(item)
    current = evs.get(stat, 0)

    if amount > 0:
        if current >= per_stat:
            return "evs.error.stat_full"
        room_total = total_max - total(evs)
        if room_total <= 0:
            return "evs.error.total_full"
        delta = min(amount, per_stat - current, room_total)
    elif amount < 0:
        if current <= 0:
            return "evs.error.stat_empty"
        delta = -min(-amount, current)
    else:
        return "evs.error.no_effect"

    return {**evs, stat: current + delta}, delta


def compute_bulk_change(
    evs: dict[str, int], item: dict[str, Any], per_stat: int, total_max: int, count: int,
) -> tuple[dict[str, int], int, int, str | None]:
    """Applies `item` up to `count` times in a row (one item consumed per
    application), stopping as soon as it would have no further effect - same
    rule as `compute_change` for a single item, just repeated like Rare Candy's
    `count`. Returns (new_evs, total_delta, used, error) - `error` is the
    `compute_change` i18n key from the first application, only meaningful
    when `used` came out as 0."""
    current = dict(evs)
    total_delta = 0
    used = 0
    error: str | None = None
    for _ in range(max(0, count)):
        change = compute_change(current, item, per_stat, total_max)
        if isinstance(change, str):
            error = change
            break
        current, total_delta_step = change
        total_delta += total_delta_step
        used += 1
    return current, total_delta, used, error


def _max_hp(species: dict[str, Any], ivs: dict[str, int], level: int, ev_hp: int) -> int:
    return battle.stat_hp(species["base_hp"], ivs.get("hp", 15), level, ev_hp,
                          species_id=species.get("id"))


def _new_current_hp(current_hp: int | None, old_max: int, new_max: int) -> int | None:
    """HP after a changed HP EV: the gained max HP is added on top (like a
    level up), a loss is taken off the same way. NULL stays NULL (= full HP),
    a fainted Pokemon stays fainted."""
    if current_hp is None or old_max == new_max:
        return current_hp
    if current_hp <= 0:
        return 0
    return max(1, min(new_max, current_hp + (new_max - old_max)))


def _remaining(user_id: str, item_key: str) -> int:
    row = db.query_one("SELECT quantity FROM inventory WHERE item_key = ? AND user_id = ?",
                       (item_key, user_id))
    return int(row["quantity"]) if row else 0


def _summary(lang: str, name: str, item: dict[str, Any], stat: str,
             delta: int, value: int, per_stat: int, used: int = 1) -> str:
    base = "evs.result.raised" if delta > 0 else "evs.result.lowered"
    key = f"{base}_multi" if used > 1 else base
    return i18n.t(lang, key, name=name, stat=i18n.t(lang, f"iv.{stat}"),
                  amount=abs(delta), value=value, max=per_stat,
                  item=label(item, lang), used=used)


# ---------------------------------------------------------------- Apply

def apply_item(user_id: str, owned_id: int, item_key: str, count: int = 1) -> dict[str, Any]:
    """Applies an EV item to an own Pokemon (outside of battle), `count` times
    in a row - one item consumed per application, like Rare Candy's `count`.
    `count` is clamped to the stock and to however many applications still
    have an effect (stat/total cap); the rest stays in the bag, exactly like a
    single application that would have no effect is rejected before being
    consumed. Raises EconomyError (-> HTTP status) on invalid use."""
    lang = db.user_language(user_id)
    item = get(item_key)
    if item is None:
        raise EconomyError(400, i18n.t(lang, "error.unknown_item", item=item_key))
    item_name = label(item, lang)
    if healing.has_ongoing_battle(user_id):
        raise EconomyError(409, i18n.t(lang, "evs.error.in_battle"))
    owned = db.query_one("SELECT * FROM owned_pokemon WHERE id = ? AND user_id = ?",
                         (owned_id, user_id))
    if owned is None:
        raise EconomyError(404, i18n.t(lang, "error.pokemon_not_found"))

    # Raising the HP EV raises max HP - regenerate first so the bonus lands on
    # up-to-date HP (like medicine.use_out_of_battle).
    healing.apply_regen(user_id)
    owned = db.query_one("SELECT * FROM owned_pokemon WHERE id = ? AND user_id = ?",
                         (owned_id, user_id))
    species = db.query_one("SELECT * FROM species WHERE id = ?", (owned["species_id"],))
    name = owned.get("nickname") or battle.combatant_from_owned(owned).name
    per_stat, total_max = limits()
    old_evs = as_evs(owned.get("evs"))

    stock = _remaining(user_id, item_key)
    requested = min(max(1, int(count)), max(1, stock))
    new_evs, delta, used, error = compute_bulk_change(old_evs, item, per_stat, total_max, requested)
    if used == 0:
        raise EconomyError(400, i18n.t(lang, error, name=name, item=item_name,
                                       stat=i18n.t(lang, f"iv.{stat_of(item)}"),
                                       max=per_stat, total=total_max))

    ivs = battle._as_ivs(owned["ivs"])
    level = owned["level"]
    old_max = _max_hp(species, ivs, level, old_evs["hp"])
    new_max = _max_hp(species, ivs, level, new_evs["hp"])
    old_hp = owned["current_hp"]
    new_hp = _new_current_hp(None if old_hp is None else int(old_hp), old_max, new_max)

    with db.cursor() as cur:
        cur.execute("UPDATE inventory SET quantity = quantity - ? "
                    "WHERE item_key = ? AND user_id = ? AND quantity >= ?",
                    (used, item_key, user_id, used))
        if cur.rowcount != 1:
            raise EconomyError(409, i18n.t(lang, "error.item_not_in_bag", item=item_name))
        # Compare-and-set on the EVs read above, so a double click never
        # spends two items on one gain.
        if owned.get("evs") is None:
            cur.execute("UPDATE owned_pokemon SET evs = ?, current_hp = ? "
                        "WHERE id = ? AND user_id = ? AND evs IS NULL",
                        (json.dumps(new_evs), new_hp, owned_id, user_id))
        else:
            cur.execute("UPDATE owned_pokemon SET evs = ?, current_hp = ? "
                        "WHERE id = ? AND user_id = ? AND evs = ?",
                        (json.dumps(new_evs), new_hp, owned_id, user_id,
                         owned["evs"]))
        if cur.rowcount != 1:
            raise EconomyError(409, i18n.t(lang, "error.pokemon_changed"))

    stat = stat_of(item)
    return {
        "ok": True, "item_key": item_key, "owned_id": owned_id, "name": name,
        "stat": stat, "gained": delta, "used": used, "evs": new_evs, "total": total(new_evs),
        "max_per_stat": per_stat, "max_total": total_max,
        "stat_value": battle.stat_hp(species["base_hp"], ivs.get("hp", 15), level, new_evs["hp"],
                                      species_id=species.get("id"))
                      if stat == "hp" else
                      battle.stat_other(species[f"base_{stat}"], ivs.get(stat, 15), level,
                                        new_evs[stat]),
        "current_hp": new_hp, "max_hp": new_max,
        "remaining": _remaining(user_id, item_key),
        "message": _summary(lang, name, item, stat, delta, new_evs[stat], per_stat, used),
    }


def targets_for(user_id: str, item_key: str) -> list[dict[str, Any]]:
    """Own Pokemon the item would actually change (for the selection in the
    Bag). Team first, like medicine.targets_for."""
    item = get(item_key)
    if item is None:
        return []
    per_stat, total_max = limits()
    stat = stat_of(item)
    result = []
    for row in db.query(
        "SELECT * FROM owned_pokemon WHERE user_id = ? "
        "ORDER BY party_slot IS NULL, party_slot, level DESC, id",
        (user_id,),
    ):
        evs = as_evs(row.get("evs"))
        if isinstance(compute_change(evs, item, per_stat, total_max), str):
            continue
        combatant = battle.combatant_from_owned(row)
        result.append({
            "owned_id": row["id"], "name": row.get("nickname") or combatant.name,
            "level": row["level"], "in_party": row["party_slot"] is not None,
            "stat": stat, "ev": evs[stat], "max_per_stat": per_stat,
            "total": total(evs), "max_total": total_max,
        })
    return result


# -------------------------------------------------------- Battle yield

def yield_of(species: dict[str, Any]) -> dict[str, int]:
    """EVs a defeated member of the species gives (`species.ev_yield`, see
    seed.ev_yield_from_pokemon_doc). NULL = not fetched yet = nothing."""
    return as_evs(species.get("ev_yield"))


def battle_gain_enabled() -> bool:
    return bool(db.get_setting("ev_battle_gain_enabled",
                               db.DEFAULT_SETTINGS["ev_battle_gain_enabled"]))


def grant_battle_evs(
    log: list[str], team: list[battle.Combatant],
    knockouts: list[tuple[dict[str, Any], set[int] | list[int]]], lang: str = "en",
) -> dict[int, dict[str, int]]:
    """EVs for defeated opponents. `knockouts` = (species of the defeated
    opponent, indexes of the team members that were standing when it fell).
    The caller decides who counts - a member that fainted LATER in the fight
    keeps its EVs, so `fainted` is not checked here. Each gain is capped at the
    limits (per stat and in total). Updates the Combatant (EVs, max HP - a gained HP EV
    adds the HP on top of the current HP, like a level up) and writes the EVs
    to the DB; the current HP is written by the caller's `save_team`.
    Returns {owned_id: {stat: gained}} and logs one line per member (the
    summary at the end of the fight)."""
    if not knockouts or not battle_gain_enabled():
        return {}
    per_stat, total_max = limits()
    gained: dict[int, dict[str, int]] = {}
    for species, participants in knockouts:
        opponent_yield = yield_of(species)
        if not any(opponent_yield.values()):
            continue
        for idx in participants:
            if not 0 <= idx < len(team):
                continue
            member = team[idx]
            if member.owned_id is None:
                continue
            current = as_evs(member.evs)
            room = total_max - total(current)
            for stat in STAT_KEYS:
                delta = min(opponent_yield[stat], per_stat - current[stat], room)
                if delta <= 0:
                    continue
                current[stat] += delta
                room -= delta
                member.evs = current
                per_member = gained.setdefault(member.owned_id, {})
                per_member[stat] = per_member.get(stat, 0) + delta

    for member in team:
        per_member = gained.get(member.owned_id) if member.owned_id is not None else None
        if not per_member:
            continue
        old_max = member.max_hp
        new_max = battle.stat_hp(member.base_species["base_hp"], member.ivs.get("hp", 15),
                                 member.level, member.evs.get("hp", 0),
                                 species_id=member.base_species.get("id"))
        if new_max != old_max:
            if not member.fainted:
                member.current_hp = max(1, min(new_max, member.current_hp + (new_max - old_max)))
            member.max_hp = new_max
        db.execute("UPDATE owned_pokemon SET evs = ? WHERE id = ?",
                   (json.dumps(as_evs(member.evs)), member.owned_id))
        parts = ", ".join(
            i18n.t(lang, "evs.gain_part", amount=amount, stat=i18n.t(lang, f"iv.{stat}"))
            for stat, amount in per_member.items())
        log.append(i18n.t(lang, "battle.log.evs_gained", name=member.name, gains=parts))
    return gained
