"""Auto rules: per-player IF/DO rules that handle encounters without input.

Every player keeps an ordered list of rules (table `auto_rule`). A rule has
conditions (IF - all must hold; a list condition matches any of its values)
and actions (DO). For an encounter the FIRST enabled rule whose conditions
match decides; without a match the encounter simply stays in the queue.

Rules run
* right after a spawn (`run_on_spawn`, called from spawn.process_events) - the
  "offline" part: the household switch `auto_battle_enabled` and the player's
  own switch (`app_user.auto_rules_enabled`) must both be on, and
* on demand from the queue (`run_for_user`, "Run rules" button).

Modes (`Actions.mode`):
* defeat - battle until the wild Pokemon faints (EXP, EVs, money).
* catch  - weaken it with non-lethal moves, then throw balls in battle; like a
           manual catch, a success gives EXP + catch rewards, no win money/EVs.
* throw  - throw balls from the queue without a battle (Quick Ball first turn).
* flee   - let it go.  * skip - leave it in the queue (stops evaluation).

Safety guards: a shiny, legendary or mythical Pokemon is never let go (mode
flee or `on_fail: flee`) and a Master Ball is never thrown unless the rule
explicitly allows it (`allow_flee_special` / `allow_master_ball`). Auto-buy
respects a money reserve per rule and the player's daily cap.
"""

from __future__ import annotations

import json
import logging
import math
import threading
from datetime import datetime
from functools import cached_property
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import (achievements, battle, battle_history, breeding, db, economy, encounter_state,
               healing, i18n, rarity, roaming, spawn)
from . import moves as moves_mod

log = logging.getLogger(__name__)

MODES = ("defeat", "catch", "throw", "flee", "skip")
TEAM_STRATEGIES = ("current", "lineup", "type_matchup", "lowest_level")
BALL_STRATEGIES = ("specific", "priority", "cheapest_chance", "best")
ON_FAIL = ("leave", "flee", "defeat")
EVO_STAGES = ("base", "middle", "final")
ALL_TYPES = tuple(sorted(battle.TYPE_CHART))
STAT_KEYS = ("hp", "atk", "def", "spa", "spd", "spe")
IV_MAX_TOTAL = 31 * len(STAT_KEYS)
MAX_GENERATION = 9
MAX_RULES = 50
MAX_TURNS = 60
LOG_KEEP = 200
# A move counts as "safe" while weakening when this multiple of its expected
# damage still leaves the wild Pokemon standing (covers damage rolls and most
# critical hits).
KO_SAFETY = 1.6
# Outcomes that ended a battle/throw - written to the battle history.
HISTORY_OUTCOME = {"caught": "caught", "defeated": "won", "lost": "lost", "left": "ran",
                   "fled": "ran", "timeout": "timeout"}


class AutoRuleError(Exception):
    """Business logic error with HTTP status - routes convert to HTTPException."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# ------------------------------------------------------------------ Schema

def _check_values(values: list[Any], allowed: Any, what: str) -> list[Any]:
    bad = [v for v in values if v not in allowed]
    if bad:
        raise ValueError(f"unknown {what}: {', '.join(map(str, bad))}")
    return list(dict.fromkeys(values))  # de-duplicate, keep order


class Conditions(BaseModel):
    """IF part. `None` / empty list = "don't care"."""
    # Stored rules may come from an older/newer schema - ignore unknown keys.
    model_config = ConfigDict(extra="ignore")

    ivs_maxed: bool | None = None            # species' IV floor is fully unlocked
    level_min: int | None = Field(None, ge=1, le=battle.MAX_LEVEL)
    level_max: int | None = Field(None, ge=1, le=battle.MAX_LEVEL)
    generations: list[int] = []
    rarity: list[str] = []
    types: list[str] = []
    types_mode: Literal["any", "all"] = "any"
    caught_before: bool | None = None        # Pokedex: species caught at least once
    caught_count_max: int | None = Field(None, ge=0)
    shiny: bool | None = None
    species_include: list[int] = []
    species_exclude: list[int] = []
    evo_stage: list[Literal["base", "middle", "final"]] = []
    iv_percent_min: int | None = Field(None, ge=0, le=100)
    iv_better_than_best: bool | None = None  # IV total beats every owned one of the species
    catch_chance_min: int | None = Field(None, ge=1, le=100)
    catch_chance_ball: str = "poke_ball"
    hidden_ability: bool | None = None
    natures: list[str] = []
    sources: list[str] = []
    biomes: list[str] = []
    team_hp_min: int | None = Field(None, ge=0, le=100)
    level_advantage_min: int | None = Field(None, ge=-100, le=100)
    type_advantage: bool | None = None
    money_min: int | None = Field(None, ge=0)
    ball_stock_min: int | None = Field(None, ge=0)
    free_team_slot: bool | None = None

    @field_validator("generations")
    @classmethod
    def _generations(cls, v: list[int]) -> list[int]:
        return _check_values(v, range(1, MAX_GENERATION + 1), "generation")

    @field_validator("rarity")
    @classmethod
    def _rarity(cls, v: list[str]) -> list[str]:
        return _check_values(v, rarity.TIERS, "rarity tier")

    @field_validator("types")
    @classmethod
    def _types(cls, v: list[str]) -> list[str]:
        return _check_values(v, ALL_TYPES, "type")

    @field_validator("natures")
    @classmethod
    def _natures(cls, v: list[str]) -> list[str]:
        return _check_values(v, spawn.NATURES, "nature")

    @field_validator("catch_chance_ball")
    @classmethod
    def _ball(cls, v: str) -> str:
        _check_values([v], battle.BALLS, "ball")
        return v


class Actions(BaseModel):
    """DO part."""
    model_config = ConfigDict(extra="ignore")

    mode: Literal["defeat", "catch", "throw", "flee", "skip"] = "defeat"
    # Which Pokemon fight (defeat/catch): the party in its order, a fixed
    # lineup (team or box, up to 6), best expected damage first, or lowest
    # level first (training).
    team: Literal["current", "lineup", "type_matchup", "lowest_level"] = "current"
    lineup: list[int] = Field(default_factory=list, max_length=breeding.PARTY_SIZE)
    heal_below: int | None = Field(None, ge=1, le=100)  # auto heal first below N % team HP
    ball: Literal["specific", "priority", "cheapest_chance", "best"] = "cheapest_chance"
    ball_key: str = "poke_ball"
    ball_priority: list[str] = []
    ball_exclude: list[str] = []
    chance_min: int = Field(60, ge=1, le=100)  # throw once the ball reaches this chance
    max_balls: int = Field(3, ge=1, le=20)
    on_fail: Literal["leave", "flee", "defeat"] = "leave"
    buy_ball: str | None = None
    buy_below: int = Field(5, ge=1, le=economy.MAX_PURCHASE_QUANTITY)
    buy_up_to: int = Field(20, ge=1, le=economy.MAX_PURCHASE_QUANTITY)
    money_reserve: int = Field(0, ge=0)
    to_box: bool = False
    notify: bool = False
    allow_flee_special: bool = False
    allow_master_ball: bool = False

    @field_validator("ball_key")
    @classmethod
    def _ball_key(cls, v: str) -> str:
        _check_values([v], battle.BALLS, "ball")
        return v

    @field_validator("ball_priority", "ball_exclude")
    @classmethod
    def _balls(cls, v: list[str]) -> list[str]:
        return _check_values(v, battle.BALLS, "ball")

    @field_validator("buy_ball")
    @classmethod
    def _buy_ball(cls, v: str | None) -> str | None:
        if v is None:
            return v
        item = economy.catalog_by_key().get(v)
        if v not in battle.BALLS or item is None or not economy.is_buyable(item):
            raise ValueError(f"ball can't be bought: {v}")
        return v


class RuleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=60)
    enabled: bool = True
    conditions: Conditions = Field(default_factory=Conditions)
    actions: Actions = Field(default_factory=Actions)


# ------------------------------------------------------------- Rule storage

def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _row_to_rule(row: dict[str, Any]) -> dict[str, Any]:
    def load(value: str, model: type[BaseModel]) -> dict[str, Any]:
        try:
            return model.model_validate(json.loads(value or "{}")).model_dump()
        except (ValueError, TypeError):
            # Broken JSON or a value an update made invalid: fall back to the
            # defaults instead of breaking the whole rule list.
            log.warning("Auto rule %s: invalid %s, using defaults", row["id"], model.__name__)
            return model().model_dump()

    return {
        "id": row["id"], "position": row["position"], "name": row["name"],
        "enabled": bool(row["enabled"]),
        "conditions": load(row["conditions"], Conditions),
        "actions": load(row["actions"], Actions),
    }


def list_rules(user_id: str) -> list[dict[str, Any]]:
    rows = db.query("SELECT * FROM auto_rule WHERE user_id = ? ORDER BY position, id", (user_id,))
    return [_row_to_rule(row) for row in rows]


def _get_row(user_id: str, rule_id: int) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM auto_rule WHERE id = ? AND user_id = ?", (rule_id, user_id))
    if row is None:
        raise AutoRuleError(404, i18n.t(db.user_language(user_id), "auto_rules.error.not_found"))
    return row


def _dump(model: BaseModel) -> str:
    # Only what differs from the defaults - keeps stored rules small and lets
    # changed defaults reach rules that never touched them.
    return json.dumps(model.model_dump(exclude_defaults=True))


def create_rule(user_id: str, rule: RuleIn, top: bool = False) -> dict[str, Any]:
    count = db.query_one("SELECT COUNT(*) AS n FROM auto_rule WHERE user_id = ?", (user_id,))["n"]
    if count >= MAX_RULES:
        raise AutoRuleError(409, i18n.t(db.user_language(user_id), "auto_rules.error.too_many",
                                        max=MAX_RULES))
    if top:
        db.execute("UPDATE auto_rule SET position = position + 1 WHERE user_id = ?", (user_id,))
        position = 0
    else:
        last = db.query_one("SELECT MAX(position) AS p FROM auto_rule WHERE user_id = ?", (user_id,))
        position = (last["p"] + 1) if last and last["p"] is not None else 0
    rule_id = db.execute(
        "INSERT INTO auto_rule (user_id, position, name, enabled, conditions, actions, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (user_id, position, rule.name.strip(), int(rule.enabled), _dump(rule.conditions),
         _dump(rule.actions), _now()),
    )
    return _row_to_rule(_get_row(user_id, rule_id))


def update_rule(user_id: str, rule_id: int, rule: RuleIn) -> dict[str, Any]:
    _get_row(user_id, rule_id)
    db.execute(
        "UPDATE auto_rule SET name = ?, enabled = ?, conditions = ?, actions = ? "
        "WHERE id = ? AND user_id = ?",
        (rule.name.strip(), int(rule.enabled), _dump(rule.conditions), _dump(rule.actions),
         rule_id, user_id),
    )
    return _row_to_rule(_get_row(user_id, rule_id))


def delete_rule(user_id: str, rule_id: int) -> None:
    _get_row(user_id, rule_id)
    db.execute("DELETE FROM auto_rule WHERE id = ? AND user_id = ?", (rule_id, user_id))
    _renumber(user_id, [rule["id"] for rule in list_rules(user_id)])


def _renumber(user_id: str, ids: list[int]) -> None:
    with db.cursor() as cur:
        cur.executemany("UPDATE auto_rule SET position = ? WHERE id = ? AND user_id = ?",
                        [(pos, rule_id, user_id) for pos, rule_id in enumerate(ids)])


def reorder(user_id: str, ids: list[int]) -> list[dict[str, Any]]:
    """New order; ids that aren't listed keep their relative order at the end."""
    existing = [rule["id"] for rule in list_rules(user_id)]
    ordered = [i for i in dict.fromkeys(ids) if i in existing]
    _renumber(user_id, ordered + [i for i in existing if i not in ordered])
    return list_rules(user_id)


# Starter templates; `name` is an i18n key, `top` puts the rule first.
TEMPLATES: dict[str, dict[str, Any]] = {
    "shiny_hunter": {
        "name": "auto_rules.template.shiny_hunter", "top": True,
        "conditions": {"shiny": True},
        "actions": {"mode": "catch", "ball": "best", "chance_min": 90, "max_balls": 10,
                    "heal_below": 60, "on_fail": "leave", "notify": True},
    },
    "catch_new": {
        "name": "auto_rules.template.catch_new",
        "conditions": {"caught_before": False},
        "actions": {"mode": "catch", "ball": "cheapest_chance", "chance_min": 60, "max_balls": 5,
                    "heal_below": 50, "on_fail": "leave",
                    "buy_ball": "poke_ball", "buy_below": 5, "buy_up_to": 20,
                    "money_reserve": 1000},
    },
    "train_commons": {
        "name": "auto_rules.template.train_commons",
        "conditions": {"caught_before": True, "rarity": ["common"], "ivs_maxed": True},
        "actions": {"mode": "defeat", "team": "lowest_level", "heal_below": 50},
    },
}


def create_from_template(user_id: str, key: str) -> dict[str, Any]:
    template = TEMPLATES.get(key)
    lang = db.user_language(user_id)
    if template is None:
        raise AutoRuleError(404, i18n.t(lang, "auto_rules.error.unknown_template"))
    rule = RuleIn(name=i18n.t(lang, template["name"]), conditions=template["conditions"],
                  actions=template["actions"])
    return create_rule(user_id, rule, top=template.get("top", False))


# ----------------------------------------------------------- Player settings

def user_settings(user_id: str) -> dict[str, Any]:
    user = db.get_user(user_id) or {}
    cap = int(user.get("auto_rules_daily_cap") or 0)
    return {
        "enabled": bool(user.get("auto_rules_enabled")),
        "daily_cap": cap,
        "spent_today": spent_today(user_id),
        "household_enabled": bool(db.get_setting("auto_battle_enabled", False)),
    }


def set_user_settings(user_id: str, enabled: bool | None = None,
                      daily_cap: int | None = None) -> dict[str, Any]:
    if enabled is not None:
        db.execute("UPDATE app_user SET auto_rules_enabled = ? WHERE id = ?",
                   (int(bool(enabled)), user_id))
    if daily_cap is not None:
        db.execute("UPDATE app_user SET auto_rules_daily_cap = ? WHERE id = ?",
                   (max(0, int(daily_cap)), user_id))
    return user_settings(user_id)


def spent_today(user_id: str) -> int:
    today = datetime.now().astimezone().date().isoformat()
    row = db.query_one(
        "SELECT COALESCE(SUM(spent), 0) AS s FROM auto_rule_log "
        "WHERE user_id = ? AND created_at LIKE ?",
        (user_id, today + "%"),
    )
    return int(row["s"]) if row else 0


def recent_log(user_id: str, limit: int = 30) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT l.*, s.name_en, s.name_de, s.sprite_url FROM auto_rule_log l "
        "LEFT JOIN species s ON s.id = l.species_id "
        "WHERE l.user_id = ? ORDER BY l.id DESC LIMIT ?",
        (user_id, limit),
    )
    for row in rows:
        row["detail"] = json.loads(row["detail"] or "{}")
    return rows


# ------------------------------------------------------------------- Facts

class Facts:
    """Everything a condition can ask about one encounter, computed lazily."""

    def __init__(self, encounter: dict[str, Any], user_id: str) -> None:
        self.encounter = encounter  # already from the user's perspective (for_user)
        self.user_id = user_id

    @cached_property
    def wild(self) -> battle.Combatant:
        return battle.combatant_from_encounter(self.encounter)

    @property
    def species(self) -> dict[str, Any]:
        return self.wild.species

    @cached_property
    def dex(self) -> dict[str, Any]:
        return db.query_one(
            "SELECT * FROM dex_entry WHERE species_id = ? AND user_id = ?",
            (self.encounter["species_id"], self.user_id),
        ) or {}

    @property
    def caught_count(self) -> int:
        return int(self.dex.get("caught_count") or 0)

    @cached_property
    def iv_total(self) -> int:
        ivs = battle._as_ivs(self.encounter["ivs"])
        return sum(int(ivs.get(key, 0)) for key in STAT_KEYS)

    @cached_property
    def best_owned_iv_total(self) -> int | None:
        rows = db.query(
            "SELECT ivs FROM owned_pokemon WHERE user_id = ? AND species_id = ?",
            (self.user_id, self.encounter["species_id"]),
        )
        totals = [sum(int(battle._as_ivs(r["ivs"]).get(k, 0)) for k in STAT_KEYS) for r in rows]
        return max(totals) if totals else None

    @cached_property
    def evo_stages(self) -> set[str]:
        has_parent = self.species.get("evolves_from") is not None
        has_child = db.query_one("SELECT 1 FROM species WHERE evolves_from = ? LIMIT 1",
                                 (self.species["id"],)) is not None
        stages = set()
        if not has_parent:
            stages.add("base")
        if not has_child:
            stages.add("final")
        if has_parent and has_child:
            stages.add("middle")
        return stages

    @cached_property
    def hidden_ability(self) -> bool:
        ability = self.encounter.get("ability")
        if not ability:
            return False
        return db.query_one(
            "SELECT 1 FROM species_ability WHERE species_id = ? AND ability_key = ? AND slot = 2",
            (self.encounter["species_id"], ability),
        ) is not None

    @cached_property
    def party_rows(self) -> list[dict[str, Any]]:
        return battle.party(self.user_id)

    @cached_property
    def party(self) -> list[battle.Combatant]:
        return [battle.combatant_from_owned(row) for row in self.party_rows]

    @property
    def team_hp_percent(self) -> float:
        total = sum(member.max_hp for member in self.party)
        if not total:
            return 0.0
        return 100 * sum(max(0, member.current_hp) for member in self.party) / total

    @cached_property
    def inventory(self) -> dict[str, int]:
        return _stock(self.user_id)

    @property
    def is_special(self) -> bool:
        """Never let these go without explicit permission (safety guard)."""
        return bool(self.encounter.get("shiny") or self.species.get("is_legendary")
                    or self.species.get("is_mythical"))

    def catch_chance(self, ball_key: str) -> float:
        return battle.catch_probability(self.wild, ball_key, context=_queue_context(
            self.encounter, self.wild, self.user_id))


def _queue_context(row: dict[str, Any], wild: battle.Combatant, user_id: str,
                   first_turn: bool | None = None) -> battle.CatchContext:
    """Same context as a throw from the queue (see api/encounters.py)."""
    if first_turn is None:
        first_turn = row.get("current_hp") is None
    return battle.catch_context(wild, user_id, turn=0, first_turn=first_turn,
                                biome=row.get("biome"))


def _stock(user_id: str) -> dict[str, int]:
    return {row["item_key"]: row["quantity"] for row in db.query(
        "SELECT item_key, quantity FROM inventory WHERE user_id = ?", (user_id,))}


def _has_super_effective_move(member: battle.Combatant, wild: battle.Combatant) -> bool:
    for key in battle.available_moves(member):
        move = moves_mod.get(key)
        if move["power"] is not None and battle.type_effectiveness(move["type"], wild.types) > 1:
            return True
    return False


def matches(cond: Conditions, facts: Facts) -> bool:
    """All set conditions must hold. Cheap checks first."""
    enc, species = facts.encounter, facts.species
    level = enc["level"]
    if cond.level_min is not None and level < cond.level_min:
        return False
    if cond.level_max is not None and level > cond.level_max:
        return False
    if cond.shiny is not None and bool(enc.get("shiny")) != cond.shiny:
        return False
    if cond.species_include and enc["species_id"] not in cond.species_include:
        return False
    if enc["species_id"] in cond.species_exclude:
        return False
    if cond.generations and species.get("generation") not in cond.generations:
        return False
    if cond.rarity and rarity.tier_for(species) not in cond.rarity:
        return False
    if cond.types:
        own = set(facts.wild.types)
        wanted = set(cond.types)
        if cond.types_mode == "all" and not wanted <= own:
            return False
        if cond.types_mode == "any" and not wanted & own:
            return False
    if cond.natures and enc.get("nature") not in cond.natures:
        return False
    if cond.sources and enc.get("source_entity") not in cond.sources:
        return False
    if cond.biomes and enc.get("biome") not in cond.biomes:
        return False
    if cond.caught_before is not None and (facts.caught_count > 0) != cond.caught_before:
        return False
    if cond.caught_count_max is not None and facts.caught_count > cond.caught_count_max:
        return False
    if cond.ivs_maxed is not None and economy.species_ivs_maxed(species) != cond.ivs_maxed:
        return False
    if cond.iv_percent_min is not None and 100 * facts.iv_total / IV_MAX_TOTAL < cond.iv_percent_min:
        return False
    if cond.iv_better_than_best is not None:
        best = facts.best_owned_iv_total
        better = best is None or facts.iv_total > best
        if better != cond.iv_better_than_best:
            return False
    if cond.evo_stage and not facts.evo_stages & set(cond.evo_stage):
        return False
    if cond.hidden_ability is not None and facts.hidden_ability != cond.hidden_ability:
        return False
    if cond.money_min is not None and economy.balance(facts.user_id) < cond.money_min:
        return False
    if cond.ball_stock_min is not None and \
            sum(facts.inventory.get(key, 0) for key in battle.BALLS) < cond.ball_stock_min:
        return False
    if cond.free_team_slot is not None and \
            (breeding.free_party_slot(facts.user_id) is not None) != cond.free_team_slot:
        return False
    if cond.team_hp_min is not None and facts.team_hp_percent < cond.team_hp_min:
        return False
    if cond.level_advantage_min is not None:
        if not facts.party:
            return False
        if max(member.level for member in facts.party) - level < cond.level_advantage_min:
            return False
    if cond.type_advantage is not None:
        has = any(_has_super_effective_move(member, facts.wild)
                  for member in facts.party if not member.fainted)
        if has != cond.type_advantage:
            return False
    if cond.catch_chance_min is not None and \
            100 * facts.catch_chance(cond.catch_chance_ball) < cond.catch_chance_min:
        return False
    return True


def match_rule(user_id: str, encounter: dict[str, Any],
               rules: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """First enabled rule whose conditions hold for this encounter."""
    facts = Facts(encounter, user_id)
    for rule in list_rules(user_id) if rules is None else rules:
        if rule["enabled"] and matches(Conditions.model_validate(rule["conditions"]), facts):
            return rule
    return None


# --------------------------------------------------------------- Ball choice

def _ball_price(key: str) -> float:
    item = economy.catalog_by_key().get(key)
    if item is None or not economy.is_buyable(item):
        return math.inf  # e.g. the Master Ball: always the last resort
    return economy.unit_price(item)


def usable_balls(actions: Actions, stock: dict[str, int]) -> list[str]:
    keys = [key for key in battle.BALLS
            if stock.get(key, 0) > 0 and key not in actions.ball_exclude]
    if not actions.allow_master_ball:
        keys = [key for key in keys if key != "master_ball"]
    return keys


def choose_ball(actions: Actions, wild: battle.Combatant, ctx: battle.CatchContext,
                stock: dict[str, int]) -> tuple[str, float] | None:
    """(ball, catch probability) by the rule's ball strategy, or None if no
    allowed ball is in the bag."""
    keys = usable_balls(actions, stock)
    if not keys:
        return None
    if actions.ball == "specific":
        keys = [actions.ball_key] if actions.ball_key in keys else []
    elif actions.ball == "priority":
        keys = [key for key in actions.ball_priority if key in keys][:1]
    if not keys:
        return None
    chances = {key: battle.catch_probability(wild, key, context=ctx) for key in keys}
    if actions.ball == "cheapest_chance":
        good = [key for key in keys if chances[key] * 100 >= actions.chance_min]
        if good:
            key = min(good, key=lambda k: (_ball_price(k), -chances[k]))
            return key, chances[key]
    key = max(keys, key=lambda k: (chances[k], -_ball_price(k)))
    return key, chances[key]


# ------------------------------------------------------------- Preparation

def _auto_buy(user_id: str, actions: Actions, result: dict[str, Any]) -> None:
    """Top up `buy_ball` when fewer than `buy_below` are left - up to
    `buy_up_to`, keeping `money_reserve` and the player's daily cap."""
    ball = actions.buy_ball
    if not ball:
        return
    have = _stock(user_id).get(ball, 0)
    if have >= actions.buy_below:
        return
    price = _ball_price(ball)
    if not math.isfinite(price) or price <= 0:
        return
    budget = economy.balance(user_id) - actions.money_reserve
    cap = int((db.get_user(user_id) or {}).get("auto_rules_daily_cap") or 0)
    if cap:
        budget = min(budget, cap - spent_today(user_id) - result["spent"])
    count = int(min(actions.buy_up_to - have, budget // price, economy.MAX_PURCHASE_QUANTITY))
    if count < 1:
        return
    try:
        bought = economy.purchase(user_id, ball, count)
    except economy.EconomyError:
        return
    result["bought"][ball] = result["bought"].get(ball, 0) + count
    result["spent"] += bought["total"]
    result["log"].append(i18n.t(result["lang"], "auto_rules.log.bought", count=count,
                                ball=battle.ball_label(ball, result["lang"]), cost=bought["total"]))


def _maybe_heal(user_id: str, actions: Actions, facts: Facts, result: dict[str, Any]) -> None:
    from . import autoheal  # autoheal -> medicine -> ... keep the import graph flat

    if actions.heal_below is None or not facts.party:
        return
    if facts.team_hp_percent >= actions.heal_below:
        return
    try:
        healed = autoheal.run(user_id, from_rule=True)
    except economy.EconomyError:
        return
    if healed["healed"]:
        result["healed"] = healed["message"]
        result["log"].append(healed["message"])


def _team_rows(user_id: str, actions: Actions, wild: battle.Combatant) -> list[dict[str, Any]]:
    """Fighters in send-out order (see Actions.team)."""
    party = battle.party(user_id)
    if actions.team == "lineup" and actions.lineup:
        rows = [db.query_one("SELECT * FROM owned_pokemon WHERE id = ? AND user_id = ?",
                             (owned_id, user_id)) for owned_id in actions.lineup]
        rows = [row for row in rows if row is not None]
        return rows[:breeding.PARTY_SIZE] if rows else party
    if actions.team == "lowest_level":
        return sorted(party, key=lambda row: row["level"])
    if actions.team == "type_matchup":
        def best_damage(row: dict[str, Any]) -> float:
            member = battle.combatant_from_owned(row)
            if member.fainted:
                return -1.0
            return max((battle.expected_damage(member, wild, key)
                        for key in battle.available_moves(member)
                        if moves_mod.get(key)["power"] is not None), default=0.0)
        return sorted(party, key=best_damage, reverse=True)
    return party


def _weakening_move(attacker: battle.Combatant, wild: battle.Combatant) -> str | None:
    """Strongest damaging move that safely leaves the wild Pokemon standing."""
    best, best_damage = None, 0.0
    for key in battle.available_moves(attacker):
        move = moves_mod.get(key)
        if move["power"] is None:
            continue
        damage = battle.expected_damage(attacker, wild, key)
        if damage <= 0 or damage * KO_SAFETY >= wild.current_hp:
            continue
        if damage > best_damage:
            best, best_damage = key, damage
    return best


# --------------------------------------------------------------- Execution

def _requeue(encounter_id: int, user_id: str, wild: battle.Combatant) -> None:
    """Back into the queue with the wild Pokemon's HP/level preserved (like
    a lost or fled manual battle)."""
    encounter_state.save_progress(encounter_id, user_id, current_hp=wild.current_hp,
                                  level=wild.level)
    db.execute("UPDATE encounter SET status = 'waiting', claimed_by = NULL WHERE id = ?",
               (encounter_id,))


def _give_up(encounter_id: int, user_id: str, wild: battle.Combatant, actions: Actions,
             facts: Facts, result: dict[str, Any], touched: bool = True) -> None:
    """Catch failed and `on_fail` is leave/flee (defeat is handled by the
    caller). Guard: a special Pokemon is never let go without permission.
    `touched` False: nothing happened to it yet - leave its state alone."""
    if actions.on_fail == "flee" and (actions.allow_flee_special or not facts.is_special):
        encounter_state.finish(encounter_id, user_id, "fled")
        result["outcome"] = "fled"
        result["log"].append(i18n.t(result["lang"], "auto_rules.log.let_go", name=wild.name))
        return
    if touched:
        _requeue(encounter_id, user_id, wild)
    result["outcome"] = "left"
    result["log"].append(i18n.t(result["lang"], "auto_rules.log.left", name=wild.name))


def _escape(encounter_id: int, user_id: str, wild: battle.Combatant, result: dict[str, Any]) -> None:
    """A roaming legendary ran off (see roaming.py) - this player's attempt is over."""
    roaming.escape(encounter_id, user_id, wild)
    result["outcome"] = "fled"
    result["log"].append(i18n.t(result["lang"], "auto_rules.log.escaped", name=wild.name))


def _throw(user_id: str, ball: str, wild: battle.Combatant, ctx: battle.CatchContext,
           log_lines: list[str], lang: str, result: dict[str, Any]) -> battle.CatchResult | None:
    from .api import encounters as encounters_api  # local: avoid circular import

    if not encounters_api.consume_ball(ball, user_id):
        return None
    result["balls_used"][ball] = result["balls_used"].get(ball, 0) + 1
    log_lines.append(i18n.t(lang, "battle.log.throw_ball", ball=battle.ball_label(ball, lang)))
    res = battle.throw_ball(wild, user_id, ball, context=ctx)
    if res.critical:
        log_lines.append(i18n.t(lang, "battle.log.critical_catch"))
    if res.caught:
        log_lines.append(i18n.t(lang, "battle.log.caught", name=wild.name))
    elif res.reason:
        log_lines.append(res.reason)
    else:
        log_lines.append(i18n.t(lang, "battle.log.broke_free", shakes=res.shakes))
    return res


def _finish_catch(row: dict[str, Any], wild: battle.Combatant, user_id: str, ball: str,
                  actions: Actions, result: dict[str, Any]) -> None:
    from .api import encounters as encounters_api

    catch = encounters_api.finalize_catch(row, wild, user_id, ball)
    if actions.to_box:
        db.execute("UPDATE owned_pokemon SET party_slot = NULL WHERE id = ?", (catch["owned_id"],))
    result["outcome"] = "caught"
    result["owned_id"] = catch["owned_id"]
    for key in ("money_earned", "money", "iv_gains", "iv_floor"):
        if key in catch:
            result[key] = catch[key]
    result["achievements"] += catch.get("achievements", [])


def _run_defeat(row: dict[str, Any], user_id: str, team_rows: list[dict[str, Any]],
                result: dict[str, Any]) -> None:
    outcome = battle.auto_battle(row, user_id, owned=team_rows)
    result["fought"] = True
    result["log"] += outcome.get("log", [])
    result["exp_details"] = outcome.get("exp_details") or []
    result["wild_hp"] = outcome.get("wild_hp")
    if outcome["outcome"] == "won":
        encounter_state.finish(row["id"], user_id, "defeated", current_hp=0,
                               level=outcome.get("wild_level", row["level"]))
        result["outcome"] = "defeated"
        for key in ("money_earned", "money"):
            if key in outcome:
                result[key] = outcome[key]
        return
    if outcome["outcome"] == "no_party":
        result["outcome"] = "no_party"
        result["fought"] = False
        return
    if outcome["outcome"] == "fled":
        wild = battle.combatant_from_encounter(
            dict(row, current_hp=outcome.get("wild_hp"), level=outcome.get("wild_level", row["level"])))
        _escape(row["id"], user_id, wild, result)
        return
    if outcome["outcome"] == "lost":
        # Active Pokemon fainted: not like the mainline games - the fight is
        # over right there and the wild Pokemon flees (see battle.py), same
        # queue handling as a roamer escaping. This player's attempt is done.
        encounter_state.finish(row["id"], user_id, "fled", current_hp=outcome.get("wild_hp"),
                               level=outcome.get("wild_level", row["level"]))
        result["outcome"] = "lost"
        return
    encounter_state.save_progress(row["id"], user_id, current_hp=outcome.get("wild_hp"),
                                  level=outcome.get("wild_level", row["level"]))
    db.execute("UPDATE encounter SET status = 'waiting', claimed_by = NULL WHERE id = ?",
               (row["id"],))
    result["outcome"] = outcome["outcome"]  # timeout


def _run_catch(row: dict[str, Any], user_id: str, actions: Actions, facts: Facts,
               team_rows: list[dict[str, Any]], result: dict[str, Any]) -> None:
    """Weaken with safe moves, throw once the chosen ball reaches `chance_min`
    (or nothing safe is left). A miss costs the turn - the wild Pokemon
    attacks, like in a manual battle."""
    lang = result["lang"]
    wild = battle.combatant_from_encounter(row)
    team = [battle.combatant_from_owned(owned) for owned in team_rows]
    alive = [idx for idx, member in enumerate(team) if not member.fainted]
    if not alive:
        result["outcome"] = "no_party"
        return
    b = battle.Battle(wild=wild, team=team, active=alive[0], lang=lang)
    battle.start_log(b)
    result["fought"] = True
    balls_left = actions.max_balls
    attacking = False  # on_fail "defeat": stop catching, battle it down
    caught_with: str | None = None
    while b.outcome is None and b.turn < MAX_TURNS:
        if b.needs_switch:
            battle.switch_to(b, b.alive_indexes()[0], forced=True)
            continue
        if attacking:
            if battle.can_mega_evolve(b, b.active_mon):
                battle.mega_evolve(b, b.active_mon)
            battle.run_turn(b, battle.ai_choose_move(b.active_mon, wild))
            continue
        ctx = battle.catch_context(
            wild, user_id, turn=b.turn,
            first_turn=b.turn == 0 and row.get("current_hp") is None,
            player_level=b.active_mon.level, biome=row.get("biome"),
        )
        choice = None if wild.leveled_in_battle or balls_left <= 0 \
            else choose_ball(actions, wild, ctx, _stock(user_id))
        if choice is None:
            b.log.append(i18n.t(lang, "auto_rules.log.no_ball" if balls_left > 0
                                else "auto_rules.log.out_of_throws"))
            if actions.on_fail == "defeat":
                attacking = True
                continue
            break
        ball, chance = choice
        move = _weakening_move(b.active_mon, wild)
        if move is not None and chance * 100 < actions.chance_min:
            battle.run_turn(b, move)
            continue
        balls_left -= 1
        res = _throw(user_id, ball, wild, ctx, b.log, lang, result)
        if res is None:
            continue  # stock changed underneath - re-evaluate next round
        if res.caught:
            caught_with = ball
            b.outcome = "caught"
            break
        battle.run_turn(b, None)

    if b.outcome == "caught":
        # Like a manual catch: as much EXP as a win once a move was used.
        if b.fought:
            battle.award_battle_exp(b, user_id)
            for ach in achievements.check_level100(user_id, b.exp_details, lang):
                b.log.append(i18n.t(lang, "battle.log.achievement_unlocked", title=ach["title"]))
                result["achievements"].append(ach["title"])
        battle.save_team(team)
        _finish_catch(row, wild, user_id, caught_with, actions, result)
    elif b.outcome == "won":
        won = battle.on_battle_won(user_id, b, row["id"])
        battle.save_team(team)
        encounter_state.finish(row["id"], user_id, "defeated", current_hp=0, level=wild.level)
        result["outcome"] = "defeated"
        result.update(won.get("reward") or {})
        result["achievements"] += [a["title"] for a in won.get("achievements", [])]
    elif b.outcome == "lost":
        battle.save_team(team)
        _requeue(row["id"], user_id, wild)
        result["outcome"] = "lost"
    elif b.outcome == "fled":
        battle.save_team(team)
        _escape(row["id"], user_id, wild, result)
    else:
        battle.save_team(team)
        if b.turn >= MAX_TURNS:
            _requeue(row["id"], user_id, wild)
            result["outcome"] = "timeout"
        else:
            _give_up(row["id"], user_id, wild, actions, facts, result)
    result["log"] += b.log
    result["exp_details"] = b.exp_details


def _run_throw(row: dict[str, Any], user_id: str, actions: Actions, facts: Facts,
               result: dict[str, Any]) -> None:
    """Balls straight from the queue - no battle, so a miss costs nothing but
    the ball (same as the queue's throw button)."""
    lang = result["lang"]
    wild = battle.combatant_from_encounter(row)
    first_turn = row.get("current_hp") is None
    for _ in range(actions.max_balls):
        ctx = _queue_context(row, wild, user_id, first_turn=first_turn)
        choice = choose_ball(actions, wild, ctx, _stock(user_id))
        if choice is None:
            result["log"].append(i18n.t(lang, "auto_rules.log.no_ball"))
            break
        res = _throw(user_id, choice[0], wild, ctx, result["log"], lang, result)
        if res is None:
            break
        result["fought"] = True
        first_turn = False
        if res.caught:
            _finish_catch(row, wild, user_id, choice[0], actions, result)
            return
        if wild.roams:
            _escape(row["id"], user_id, wild, result)
            return
    if result["balls_used"]:
        encounter_state.save_progress(row["id"], user_id, current_hp=wild.current_hp)
    if actions.on_fail == "defeat":
        team_rows = _team_rows(user_id, actions, wild)
        if any(not battle.combatant_from_owned(r).fainted for r in team_rows):
            row = encounter_state.for_user(row, user_id)
            _run_defeat(row, user_id, team_rows, result)
            return
    _give_up(row["id"], user_id, wild, actions, facts, result,
             touched=bool(result["balls_used"]))


def _notify(user_id: str, result: dict[str, Any]) -> None:
    user = db.get_user(user_id) or {}
    lang = result["lang"]
    queue_notification({
        "title": i18n.t(lang, "auto_rules.notify.title"),
        "message": i18n.t(
            lang, "auto_rules.notify.message",
            player=user.get("display_name") or user.get("name") or user_id,
            rule=result["rule_name"], name=result["name"], level=result["level"],
            outcome=i18n.t(lang, f"auto_rules.outcome.{result['outcome']}"),
        ),
        "notification_id": f"pokehoas_auto_{user_id}_{result['encounter_id']}",
    })


def _record(user_id: str, rule: dict[str, Any] | None, result: dict[str, Any]) -> None:
    now = _now()
    if result["fought"] and result["outcome"] in HISTORY_OUTCOME:
        battle_history.record(
            user_id, battle_history.KIND_AUTO, HISTORY_OUTCOME[result["outcome"]],
            encounter_id=result["encounter_id"], opponent=result["name"],
            money_earned=result.get("money_earned"), caught=result["outcome"] == "caught",
            exp_details=result["exp_details"], log=result["log"], created_at=now,
            rule_name=result["rule_name"],
        )
    if rule is None:
        return  # the queue's plain auto-battle button: battle history only
    db.execute(
        "INSERT INTO auto_rule_log (user_id, encounter_id, rule_id, rule_name, species_id, level, "
        "mode, outcome, spent, detail, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (user_id, result["encounter_id"], rule["id"], rule["name"], result["species_id"],
         result["level"], result["mode"], result["outcome"], result["spent"],
         json.dumps({"balls_used": result["balls_used"], "bought": result["bought"],
                     "money_earned": result.get("money_earned"), "healed": result["healed"]}),
         now),
    )
    db.execute(
        "DELETE FROM auto_rule_log WHERE user_id = ? AND id NOT IN "
        "(SELECT id FROM auto_rule_log WHERE user_id = ? ORDER BY id DESC LIMIT ?)",
        (user_id, user_id, LOG_KEEP),
    )


def execute(user_id: str, encounter: dict[str, Any], actions: Actions,
            rule: dict[str, Any] | None = None) -> dict[str, Any]:
    """Carry out `actions` on one open encounter of `user_id`. `encounter` is
    the raw row; `rule` None = the queue's plain auto-battle button."""
    lang = db.user_language(user_id)
    row = encounter_state.for_user(encounter, user_id)
    facts = Facts(row, user_id)
    result: dict[str, Any] = {
        "encounter_id": row["id"], "species_id": row["species_id"], "name": facts.wild.name,
        "opponent": facts.wild.name,
        "level": row["level"], "shiny": bool(row.get("shiny")),
        "rule_id": rule["id"] if rule else None, "rule_name": rule["name"] if rule else None,
        "mode": actions.mode, "outcome": "skipped", "fought": False, "owned_id": None,
        "balls_used": {}, "bought": {}, "spent": 0, "healed": None,
        "exp_details": [], "achievements": [], "log": [], "lang": lang,
    }
    if rule is not None:
        result["log"].append(i18n.t(lang, "auto_rules.log.rule", rule=rule["name"],
                                    name=facts.wild.name, level=row["level"]))

    if actions.mode == "skip":
        result.pop("lang")
        return result
    mode = actions.mode
    if mode in ("catch", "throw"):
        _auto_buy(user_id, actions, result)
        ctx = _queue_context(row, facts.wild, user_id)
        if choose_ball(actions, facts.wild, ctx, _stock(user_id)) is None:
            # No allowed ball in the bag: don't start a pointless battle.
            result["log"].append(i18n.t(lang, "auto_rules.log.no_ball"))
            if actions.on_fail == "defeat":
                mode = "defeat"
            else:
                result["outcome"] = "no_ball"
    if result["outcome"] == "no_ball":
        pass
    elif mode == "flee":
        _give_up(row["id"], user_id, facts.wild,
                 actions.model_copy(update={"on_fail": "flee"}), facts, result, touched=False)
    else:
        if mode in ("defeat", "catch"):
            _maybe_heal(user_id, actions, facts, result)
            team_rows = _team_rows(user_id, actions, facts.wild)
            if not any(not battle.combatant_from_owned(r).fainted for r in team_rows):
                result["outcome"] = "no_party"
            elif mode == "defeat":
                _run_defeat(row, user_id, team_rows, result)
            else:
                _run_catch(row, user_id, actions, facts, team_rows, result)
        else:
            _run_throw(row, user_id, actions, facts, result)

    if result["outcome"] != "no_party" or result["spent"]:
        _record(user_id, rule, result)
    if actions.notify and result["outcome"] in HISTORY_OUTCOME:
        _notify(user_id, result)
    result["message"] = i18n.t(lang, "auto_rules.result", name=result["name"],
                               level=result["level"],
                               outcome=i18n.t(lang, f"auto_rules.outcome.{result['outcome']}"))
    result.pop("lang")
    return result


# -------------------------------------------------------------- Entry points

def household_enabled() -> bool:
    return bool(db.get_setting("auto_battle_enabled", False))


def _open_rows(user_id: str) -> list[dict[str, Any]]:
    """The user's waiting encounters, oldest first (same filter as the queue)."""
    return db.query(
        "SELECT e.* FROM encounter e "
        "LEFT JOIN encounter_user eu ON eu.encounter_id = e.id AND eu.user_id = ? "
        "WHERE e.status = 'waiting' AND eu.outcome IS NULL "
        "AND (e.user_id IS NULL OR e.user_id = ?) ORDER BY e.created_at, e.id",
        (user_id, user_id),
    )


def apply_rules(user_id: str, encounter: dict[str, Any],
                rules: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """Match and execute; None when no rule matched."""
    rule = match_rule(user_id, encounter_state.for_user(encounter, user_id), rules)
    if rule is None:
        return None
    return execute(user_id, encounter, Actions.model_validate(rule["actions"]), rule)


def run_for_user(user_id: str) -> dict[str, Any]:
    """"Run rules" button: every waiting encounter of the player."""
    lang = db.user_language(user_id)
    if not household_enabled():
        raise AutoRuleError(409, i18n.t(lang, "error.auto_battle_disabled"))
    if healing.has_ongoing_battle(user_id):
        raise AutoRuleError(409, i18n.t(lang, "auto_rules.error.in_battle"))
    rules = list_rules(user_id)
    results = []
    unmatched = 0
    for row in _open_rows(user_id):
        result = apply_rules(user_id, row, rules)
        if result is None:
            unmatched += 1
        else:
            results.append(result)
    # Handled = it left the queue (caught, defeated, let go).
    handled = sum(1 for r in results if r["outcome"] in ("caught", "defeated", "fled"))
    return {"results": results, "handled": handled, "unmatched": unmatched,
            "money": economy.balance(user_id),
            "message": i18n.t(lang, "auto_rules.run_summary", handled=handled,
                              total=len(results) + unmatched)}


def run_on_spawn(created: list[tuple[str, int]]) -> None:
    """Called after a spawn round with (user_id, encounter_id) pairs. Never
    raises - a broken rule must not break spawning."""
    if not created or not household_enabled():
        return
    for user_id, encounter_id in created:
        try:
            user = db.get_user(user_id) or {}
            if not user.get("auto_rules_enabled") or healing.has_ongoing_battle(user_id):
                continue
            row = db.query_one("SELECT * FROM encounter WHERE id = ? AND status = 'waiting'",
                               (encounter_id,))
            if row is not None:
                apply_rules(user_id, row)
        except Exception:  # noqa: BLE001
            log.exception("Auto rules failed for encounter %s of %s", encounter_id, user_id)


def preview(user_id: str) -> list[dict[str, Any]]:
    """Dry run: which rule would take each waiting encounter, and with which
    ball (at the encounter's current HP)."""
    rules = list_rules(user_id)
    out = []
    for raw in _open_rows(user_id):
        row = encounter_state.for_user(raw, user_id)
        facts = Facts(row, user_id)
        rule = match_rule(user_id, row, rules)
        entry: dict[str, Any] = {
            "encounter_id": row["id"], "species_id": row["species_id"],
            "name_en": facts.species.get("name_en"), "name_de": facts.species.get("name_de"),
            "sprite_url": facts.species.get("sprite_url"), "level": row["level"],
            "shiny": bool(row.get("shiny")), "rule_id": None, "rule_name": None, "mode": None,
            "ball": None, "chance": None,
        }
        if rule is not None:
            actions = Actions.model_validate(rule["actions"])
            entry.update(rule_id=rule["id"], rule_name=rule["name"], mode=actions.mode)
            if actions.mode in ("catch", "throw"):
                choice = choose_ball(actions, facts.wild,
                                     _queue_context(row, facts.wild, user_id), facts.inventory)
                if choice is not None:
                    entry.update(ball=choice[0], chance=round(choice[1], 4))
        out.append(entry)
    return out


def catalog() -> dict[str, Any]:
    """Option lists for the rule editor."""
    return {
        "modes": MODES, "team_strategies": TEAM_STRATEGIES, "ball_strategies": BALL_STRATEGIES,
        "on_fail": ON_FAIL, "evo_stages": EVO_STAGES, "types": ALL_TYPES,
        "rarity": rarity.TIERS, "natures": spawn.NATURES,
        "generations": list(range(1, MAX_GENERATION + 1)),
        "balls": [{"key": key, "label": ball["label"], "label_de": ball.get("label_de"),
                   "buyable": math.isfinite(_ball_price(key))}
                  for key, ball in battle.BALLS.items()],
        "biomes": sorted(spawn.biome_data().get("biomes") or {}),
        "templates": list(TEMPLATES),
        "max_rules": MAX_RULES,
    }


# ------------------------------------------------------------ Notifications
# "Notify me" messages wait here until the poller sends them to Home Assistant
# (persistent_notification.create, see poller.py) - rules also run from sync
# request handlers, which can't await the HA client.

_notifications: list[dict[str, Any]] = []
_notifications_lock = threading.Lock()


def queue_notification(note: dict[str, Any]) -> None:
    with _notifications_lock:
        _notifications.append(note)
        del _notifications[:-50]  # bounded: HA may be unreachable for a while


def drain_notifications() -> list[dict[str, Any]]:
    with _notifications_lock:
        notes = list(_notifications)
        _notifications.clear()
    return notes
