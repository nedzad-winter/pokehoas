"""Achievements per user.

Definitions are code or derived from the trainer catalog (one achievement per
gym leader / elite four member / champion), unlocked in the `achievement` table.
New trainers in data/trainers.json automatically get their achievement.

Many achievements pay out items the first time they are unlocked - a TM plus
balls per gym badge, a Master Ball per cleared region, the Shiny Charm for the
whole league. What exactly, and why, is in `rewards.py`; granting happens in
`on_trainer_defeated`, so a reward can never be farmed by rematching.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from . import db, i18n, rarity, rewards, roaming, trainers

LEAGUE_MASTER = "league_master"
SHINY_CHARM = "shiny_charm"

# Tier of an achievement, shown as a bronze / silver / gold trophy in the UI.
# Gym leaders are bronze, Elite Four silver, Champions and whole regions gold;
# the other groups are tiered per entry (see MILESTONES, DEX_TIER, LEVEL100_TIER).
TIER_BRONZE = "bronze"
TIER_SILVER = "silver"
TIER_GOLD = "gold"
TRAINER_TIER = {"gym_leader": TIER_BRONZE, "elite_four": TIER_SILVER, "champion": TIER_GOLD}

# Pokedex achievements (group "pokedex"). Their reward is a shiny perk, see
# `shiny_boost` and README "Shiny Pokemon".
DEX_SEEN_ALL = "dex_seen_all"
DEX_CAUGHT_ALL = "dex_caught_all"
DEX_LIVING = "dex_living"
DEX_KEYS = (DEX_SEEN_ALL, DEX_CAUGHT_ALL, DEX_LIVING)
DEX_TIER = {DEX_SEEN_ALL: TIER_SILVER, DEX_CAUGHT_ALL: TIER_GOLD, DEX_LIVING: TIER_GOLD}

# Level-100 milestones (group "level100"): the 1st and 10th OWN Pokemon (ever,
# see `pokemon_level100` - permanent, survives release/trade) to reach level
# 100 each pay out an Exp. Share (`EXP_SHARE`, held_items.py/shop.json).
LEVEL100_FIRST = "level100_first"
LEVEL100_TENTH = "level100_tenth"
LEVEL100_20TH = "level100_20th"
LEVEL100_35TH = "level100_35th"
LEVEL100_50TH = "level100_50th"
LEVEL100_101ST = "level100_101st"
LEVEL100_KEYS = (LEVEL100_FIRST, LEVEL100_TENTH, LEVEL100_20TH, LEVEL100_35TH, LEVEL100_50TH,
                 LEVEL100_101ST)
LEVEL100_THRESHOLD = {LEVEL100_FIRST: 1, LEVEL100_TENTH: 10, LEVEL100_20TH: 20,
                      LEVEL100_35TH: 35, LEVEL100_50TH: 50, LEVEL100_101ST: 101}
LEVEL100_TIER = {LEVEL100_FIRST: TIER_BRONZE, LEVEL100_TENTH: TIER_BRONZE,
                 LEVEL100_20TH: TIER_SILVER, LEVEL100_35TH: TIER_GOLD, LEVEL100_50TH: TIER_GOLD,
                 LEVEL100_101ST: TIER_GOLD}
# The 50th pays no item: it permanently gives EVERY team member the full EXP
# of a fight (`share_all_exp`, `battle.exp_shares`). The 101st neither: it
# unlocks Training (training.py, same threshold).
LEVEL100_NO_ITEM = (LEVEL100_50TH, LEVEL100_101ST)
EXP_SHARE = "exp_share"
# `battle.MAX_LEVEL` duplicated to avoid a circular import (battle.py calls
# into this module lazily to check level-100 after granting EXP - see
# team_code.py for the same MAX_LEVEL duplication and why).
LEVEL100 = 100

# Milestones counted from existing data (group "catching" / "daily", and the
# Pokedex percentages in group "pokedex"). Unlocked by `sync_milestones`; the
# reward is paid once, like every achievement. Balls are balanced against the
# gym badges (rewards.GYM_BALLS: at most 5 of a tier per gym) and the daily
# challenge (10-20 of each tier per quest): small early tiers, Ultra Balls
# later, a Master Ball only for the long hauls.
#   metric  - what is counted (see `milestone_progress`)
#   target  - the count to reach; `pct` targets are a percentage of all species
#   tier    - bronze / silver / gold trophy (see TIER_* above)
MILESTONES: dict[str, dict[str, Any]] = {
    "catch_10": {"group": "catching", "metric": "catches", "target": 10,
                 "tier": TIER_BRONZE, "items": {"poke_ball": 10}},
    "catch_100": {"group": "catching", "metric": "catches", "target": 100,
                  "tier": TIER_SILVER, "items": {"great_ball": 15}},
    "catch_500": {"group": "catching", "metric": "catches", "target": 500,
                  "tier": TIER_GOLD, "items": {"ultra_ball": 15}},
    "catch_1000": {"group": "catching", "metric": "catches", "target": 1000,
                   "tier": TIER_GOLD, "items": {"ultra_ball": 10, "master_ball": 1}},
    "roamer_first": {"group": "catching", "metric": "roamers", "target": 1,
                     "tier": TIER_BRONZE, "items": {"master_ball": 1}},
    "roamer_all": {"group": "catching", "metric": "roamers",
                   "target": len(roaming.ROAMING_IDS), "tier": TIER_GOLD,
                   "items": {"master_ball": 1}},
    "dex_caught_25": {"group": "pokedex", "metric": "dex_caught", "pct": 25,
                      "tier": TIER_BRONZE, "items": {"ultra_ball": 5, "rare_candy": 3}},
    "dex_caught_50": {"group": "pokedex", "metric": "dex_caught", "pct": 50,
                      "tier": TIER_SILVER, "items": {"ultra_ball": 10, "rare_candy": 5}},
    "dex_caught_75": {"group": "pokedex", "metric": "dex_caught", "pct": 75,
                      "tier": TIER_GOLD, "items": {"ultra_ball": 15, "rare_candy": 10}},
    "daily_streak_7": {"group": "daily", "metric": "streak", "target": 7,
                       "tier": TIER_BRONZE, "items": {"ultra_ball": 10, "rare_candy": 5}},
    "daily_streak_30": {"group": "daily", "metric": "streak", "target": 30,
                        "tier": TIER_SILVER, "items": {"ultra_ball": 25, "rare_candy": 15}},
    "daily_streak_100": {"group": "daily", "metric": "streak", "target": 100,
                         "tier": TIER_GOLD,
                         "items": {"ultra_ball": 30, "rare_candy": 30, "master_ball": 1}},
}

GROUP_BATTLES = "battles"
GROUP_POKEDEX = "pokedex"
GROUP_LEVEL100 = "level100"
GROUP_CATCHING = "catching"
GROUP_DAILY = "daily"
GROUP_GENERATIONS = "generations"

# Per-generation Pokedex achievements (group "generations"), two per
# generation, counted WITHOUT mythical Pokemon (`species.is_mythical`) - they
# are the reward of the second one instead:
#   gen_caught_<n> - every species of generation n caught at least once: pays
#                    GEN_CAUGHT_MONEY + GEN_CAUGHT_ITEMS once, and from then on
#                    that generation's wild spawns roll shiny with the
#                    `gen_caught_shiny_factor` (see `shiny_boost`).
#   gen_living_<n> - every species of generation n owned at the same time
#                    (team or box): every mythical Pokemon of generation n
#                    appears once in the player's queue
#                    (`spawn.create_event_encounters`), the player
#                    may switch single Pokemon to that generation's sprite style
#                    (`db.user_species_sprite_styles`, www/index.html SPRITE_STYLES).
# Unlocks are permanent like every achievement.
GEN_CAUGHT_PREFIX = "gen_caught_"
GEN_LIVING_PREFIX = "gen_living_"
GEN_CAUGHT_MONEY = 200_000
GEN_CAUGHT_ITEMS = {"ultra_ball": 40}


def trainer_key(trainer_id: str) -> str:
    return f"trainer_{trainer_id}"


def region_key(region: str) -> str:
    return f"region_unlock_{region}"


def gen_caught_key(generation: int) -> str:
    return f"{GEN_CAUGHT_PREFIX}{generation}"


def gen_living_key(generation: int) -> str:
    return f"{GEN_LIVING_PREFIX}{generation}"


def generations() -> list[int]:
    """Every generation that has a region (Kanto = 1 ... Paldea = 9)."""
    return list(range(1, len(trainers.regions()) + 1))


def generation_region_label(generation: int) -> str:
    for region in trainers.regions():
        if trainers.generation_for_region(region["key"]) == generation:
            return region["label"]
    return str(generation)


def event_mythicals(generation: int) -> list[dict[str, Any]]:
    """The mythical Pokemon `gen_living_<n>` spawns: every mythical base form
    of that generation (Melmetal evolves from Meltan and is left out, like
    every evolution in the wild pool)."""
    return db.query(
        "SELECT * FROM species WHERE generation = ? AND is_mythical = 1 "
        "AND evolves_from IS NULL ORDER BY id",
        (generation,),
    )


def unlock_generation(region_key: str, mode: str) -> int | None:
    """The generation that clearing this region's league adds to the wild
    spawn pool under `mode` (db.REGION_LOCK_MODES), or None if it adds none:
    'region' unlocks the region's own generation (Kanto/gen 1 is open from the
    start anyway), 'previous_region' the NEXT one (nothing after the last
    region), 'unlocked' has nothing left to unlock."""
    gen = trainers.generation_for_region(region_key)
    if gen is None or mode == "unlocked":
        return None
    if mode == "previous_region":
        return gen + 1 if gen < len(trainers.regions()) else None
    return gen if gen > 1 else None


def definitions(lang: str = "en", mode: str = db.DEFAULT_REGION_LOCK) -> list[dict[str, Any]]:
    """All achievements, with the items they pay out (`reward`, see rewards.py).
    `lang` affects the reward LABEL and the region achievements' description
    (which depends on the player's region lock `mode`); trainer titles and
    descriptions are the catalog's own English texts."""
    defs = [
        {
            "key": trainer_key(t["id"]),
            "group": GROUP_BATTLES,
            "category": t["kind"],
            "region": t["region"],
            "region_label": t["region_label"],
            "title": f"Defeated {trainers.title_for(t)} {t['name']}",
            "description": f"Defeat {trainers.title_for(t)} {t['name']} ({t['region_label']}).",
            "trainer_id": t["id"],
            "tier": TRAINER_TIER[t["kind"]],
            "reward": rewards.build(rewards.gym_items(t["id"]), lang),
        }
        for t in trainers.all_trainers()
    ]
    # One achievement per region that describes WHAT completing it does: it
    # always pays out a Master Ball and, depending on the player's region lock
    # mode, unlocks a generation in THEIR OWN wild spawn pool (spawn.pick_species,
    # see unlocked_generations below). A region that unlocks nothing (Kanto in
    # the default mode - gen 1 is open from the start anyway) still earns its
    # Master Ball like every other region.
    for region in trainers.regions():
        gen = trainers.generation_for_region(region["key"])
        unlocks = unlock_generation(region["key"], mode)
        description = i18n.t(
            lang,
            "achievements.region_desc_unlocks" if unlocks is not None
            else "achievements.region_desc",
            region=region["label"], gen=unlocks,
        )
        defs.append({
            "key": region_key(region["key"]),
            "group": GROUP_BATTLES,
            "category": "region",
            "region": region["key"],
            "region_label": region["label"],
            "title": f"{region['label']} League Cleared",
            "description": description,
            "generation": gen,
            "unlocks_generation": unlocks,
            "tier": TIER_GOLD,
            "reward": rewards.build(rewards.region_items(region["key"]), lang),
        })
    defs.append({
        "key": LEAGUE_MASTER,
        "group": GROUP_BATTLES,
        "category": "special",
        "region": None,
        "title": "League Champion",
        "description": "Defeat every Gym Leader, Elite Four member and Champion.",
        "tier": TIER_GOLD,
        "reward": rewards.build([{"key": SHINY_CHARM, "quantity": 1}], lang),
    })
    for key in DEX_KEYS:
        defs.append({
            "key": key,
            "group": GROUP_POKEDEX,
            "category": "pokedex",
            "region": None,
            "tier": DEX_TIER[key],
            "title": i18n.t(lang, f"achievements.{key}.title"),
            "description": i18n.t(lang, f"achievements.{key}.desc"),
            "reward": None,
        })
    for key in LEVEL100_KEYS:
        defs.append({
            "key": key,
            "group": GROUP_LEVEL100,
            "category": "level100",
            "region": None,
            "tier": LEVEL100_TIER[key],
            "title": i18n.t(lang, f"achievements.{key}.title"),
            "description": i18n.t(lang, f"achievements.{key}.desc"),
            "reward": (None if key in LEVEL100_NO_ITEM
                       else rewards.build([{"key": EXP_SHARE, "quantity": 1}], lang)),
        })
    for key, m in MILESTONES.items():
        defs.append({
            "key": key,
            "group": m["group"],
            "category": m["group"],
            "region": None,
            "tier": m["tier"],
            "title": i18n.t(lang, f"achievements.{key}.title"),
            "description": i18n.t(lang, f"achievements.{key}.desc"),
            "reward": rewards.build([{"key": k, "quantity": n} for k, n in m["items"].items()], lang),
        })
    defs += _generation_definitions(lang)
    return defs


def _generation_definitions(lang: str) -> list[dict[str, Any]]:
    """`gen_caught_<n>` / `gen_living_<n>` for every generation, see
    GEN_CAUGHT_PREFIX. The living one's reward carries no items, only a label
    naming the mythical Pokemon it spawns (`event_mythicals`)."""
    name_col = "name_de" if lang == "de" else "name_en"
    caught_reward = rewards.build(
        [{"key": k, "quantity": n} for k, n in GEN_CAUGHT_ITEMS.items()], lang,
        money=GEN_CAUGHT_MONEY)
    factor = float(db.get_setting("gen_caught_shiny_factor",
                                  db.DEFAULT_SETTINGS["gen_caught_shiny_factor"]))
    defs: list[dict[str, Any]] = []
    for gen in generations():
        region = generation_region_label(gen)
        params = {"gen": gen, "region": region, "factor": f"{factor:g}"}
        mythicals = [m.get(name_col) or m["name"] for m in event_mythicals(gen)]
        common = {"group": GROUP_GENERATIONS, "category": "generation", "region": None,
                  "generation": gen, "region_label": region}
        defs.append({
            **common,
            "key": gen_caught_key(gen),
            "tier": TIER_SILVER,
            "title": i18n.t(lang, "achievements.gen_caught.title", **params),
            "description": i18n.t(lang, "achievements.gen_caught.desc", **params),
            "reward": caught_reward,
        })
        defs.append({
            **common,
            "key": gen_living_key(gen),
            "tier": TIER_GOLD,
            "title": i18n.t(lang, "achievements.gen_living.title", **params),
            "description": i18n.t(lang, "achievements.gen_living.desc", **params),
            "reward": ({"items": [], "label": i18n.t(lang, "achievements.gen_living.reward",
                                                     names=", ".join(mythicals))}
                       if mythicals else None),
        })
    return defs


def dex_progress(user_id: str) -> dict[str, int]:
    """`total` species, how many this user has `seen` (trainer battles count,
    see trainer_battle.start), `caught` at least once, and currently `owned`
    (team or box) - the last one is the "living Pokedex"."""
    total = (db.query_one("SELECT COUNT(*) AS n FROM species") or {"n": 0})["n"]
    dex = db.query_one(
        "SELECT SUM(seen_count > 0 OR caught_count > 0) AS seen, "
        "SUM(caught_count > 0) AS caught FROM dex_entry WHERE user_id = ?",
        (user_id,),
    ) or {}
    owned = db.query_one(
        "SELECT COUNT(DISTINCT species_id) AS n FROM owned_pokemon WHERE user_id = ?",
        (user_id,),
    ) or {"n": 0}
    return {"total": int(total), "seen": int(dex.get("seen") or 0),
            "caught": int(dex.get("caught") or 0), "owned": int(owned["n"])}


def sync_pokedex(user_id: str, lang: str = "en") -> list[dict[str, Any]]:
    """Unlock the Pokedex achievements this user has earned by now and return
    the newly unlocked definitions - the per-generation ones included
    (`sync_generations`). Cheap once everything is unlocked. Unlocks are
    permanent: releasing a Pokemon later does not take the perk away."""
    new = sync_generations(user_id, lang)
    done = unlocked(user_id)
    if all(k in done for k in DEX_KEYS):
        return new
    progress = dex_progress(user_id)
    if progress["total"] <= 0:
        return new
    reached = {
        DEX_SEEN_ALL: progress["seen"], DEX_CAUGHT_ALL: progress["caught"],
        DEX_LIVING: progress["owned"],
    }
    by_key = {d["key"]: d for d in definitions(lang)}
    for key in DEX_KEYS:
        if key not in done and reached[key] >= progress["total"]:
            _unlock_with_reward(user_id, key, by_key, new)
    return new


def generation_progress(user_id: str) -> dict[int, dict[str, int]]:
    """Per generation: `total` species WITHOUT mythical Pokemon, how many of
    them this user has `caught` at least once and currently `owns` (team or
    box). Generations without any species have total 0."""
    rows = db.query(
        "SELECT s.generation AS gen, COUNT(*) AS total, "
        "COALESCE(SUM(COALESCE(d.caught_count, 0) > 0), 0) AS caught, "
        "COALESCE(SUM(s.id IN (SELECT species_id FROM owned_pokemon WHERE user_id = ?)), 0) AS owned "
        "FROM species s LEFT JOIN dex_entry d ON d.species_id = s.id AND d.user_id = ? "
        "WHERE s.is_mythical = 0 GROUP BY s.generation",
        (user_id, user_id),
    )
    by_gen = {int(r["gen"]): r for r in rows}
    return {
        gen: {key: int((by_gen.get(gen) or {}).get(key) or 0) for key in ("total", "caught", "owned")}
        for gen in generations()
    }


def sync_generations(user_id: str, lang: str = "en") -> list[dict[str, Any]]:
    """Unlock the per-generation achievements (GEN_CAUGHT_PREFIX) this user
    has earned and return the newly unlocked definitions. A newly unlocked
    living one spawns that generation's mythical Pokemon into the user's queue
    right away. Cheap once every generation is done."""
    done = unlocked(user_id)
    gens = [g for g in generations()
            if gen_caught_key(g) not in done or gen_living_key(g) not in done]
    if not gens:
        return []
    progress = generation_progress(user_id)
    by_key: dict[str, dict[str, Any]] | None = None
    new: list[dict[str, Any]] = []
    for gen in gens:
        p = progress[gen]
        if p["total"] <= 0:
            continue
        for key, reached in ((gen_caught_key(gen), p["caught"]), (gen_living_key(gen), p["owned"])):
            if key in done or reached < p["total"]:
                continue
            if by_key is None:
                by_key = {d["key"]: d for d in definitions(lang)}
            if _unlock_with_reward(user_id, key, by_key, new) and key == gen_living_key(gen):
                from . import spawn  # spawn imports this module
                spawn.create_event_encounters(user_id, gen, event_mythicals(gen))
    return new


def caught_generations(user_id: str | None) -> set[int]:
    """Generations whose `gen_caught_<n>` this user has unlocked."""
    if user_id is None:
        return set()
    return {g for g in generations() if gen_caught_key(g) in unlocked(user_id)}


def living_generations(user_id: str | None) -> set[int]:
    """Generations whose `gen_living_<n>` this user has unlocked - their sprite
    style is available."""
    if user_id is None:
        return set()
    return {g for g in generations() if gen_living_key(g) in unlocked(user_id)}


def daily_streak(user_id: str) -> int:
    """Longest run of consecutive local days on which this user won at least
    one daily challenge quest (`daily_challenge.status = 'won'`). The longest
    run EVER counts, so a streak that was reached once stays unlocked."""
    days = sorted(
        datetime.fromisoformat(row["day"]).date().toordinal()
        for row in db.query(
            "SELECT DISTINCT day FROM daily_challenge WHERE user_id = ? AND status = 'won'",
            (user_id,))
    )
    best = run = 0
    for i, day in enumerate(days):
        run = run + 1 if i and day == days[i - 1] + 1 else 1
        best = max(best, run)
    return best


def milestone_progress(user_id: str) -> dict[str, int]:
    """Current value of every metric used by `MILESTONES`: `catches` (all
    catches ever, the sum of the Pokedex catch counters - trades, eggs and
    evolutions count like in the profile), `dex_caught` (species caught at
    least once), `roamers` (roaming legendaries caught, see roaming.py) and
    `streak` (`daily_streak`)."""
    row = db.query_one(
        "SELECT COALESCE(SUM(caught_count), 0) AS catches FROM dex_entry WHERE user_id = ?",
        (user_id,)) or {"catches": 0}
    roamers = db.query_one(
        "SELECT COUNT(*) AS n FROM dex_entry WHERE user_id = ? AND caught_count > 0 "
        f"AND species_id IN ({','.join('?' * len(roaming.ROAMING_IDS))})",
        (user_id, *sorted(roaming.ROAMING_IDS))) or {"n": 0}
    return {"catches": int(row["catches"]), "dex_caught": dex_progress(user_id)["caught"],
            "roamers": int(roamers["n"]), "streak": daily_streak(user_id)}


def milestone_target(key: str, dex_total: int) -> int:
    """The count a milestone needs; percentage milestones round up."""
    m = MILESTONES[key]
    if "pct" in m:
        return max(1, -(-dex_total * m["pct"] // 100))
    return int(m["target"])


def sync_milestones(user_id: str, lang: str = "en", metrics: tuple[str, ...] | None = None
                    ) -> list[dict[str, Any]]:
    """Unlock the `MILESTONES` this user has reached and return the newly
    unlocked definitions. `metrics` limits the check to those metrics (e.g.
    only `streak` after a daily quest); None checks all. Cheap once everything
    in scope is unlocked."""
    done = unlocked(user_id)
    keys = [k for k, m in MILESTONES.items()
            if k not in done and (metrics is None or m["metric"] in metrics)]
    if not keys:
        return []
    progress = milestone_progress(user_id)
    total = dex_progress(user_id)["total"]
    by_key = {d["key"]: d for d in definitions(lang)}
    new: list[dict[str, Any]] = []
    for key in keys:
        m = MILESTONES[key]
        if "pct" in m and total <= 0:
            continue
        if progress[m["metric"]] >= milestone_target(key, total):
            _unlock_with_reward(user_id, key, by_key, new)
    return new


def shiny_boost(user_id: str | None) -> dict[str, Any]:
    """The Pokedex perks that change this user's shiny odds:

    * `charm_factor` - extra factor on the Shiny Charm's multiplier (only
      effective while the charm is owned): x`shiny_charm_seen_factor` once every
      Pokemon is seen, x`shiny_charm_caught_factor` once every one is caught
      (the caught perk replaces the seen one, they don't stack).
    * `living_rate` - with the living Pokedex a fixed shiny chance per spawn
      (`living_dex_shiny_rate`, 1 in 100), charm or not; None without it.
    * `gen_factors` - {generation: factor} for every generation whose
      `gen_caught_<n>` is unlocked: spawns of that generation multiply their
      shiny chance by `gen_caught_shiny_factor` - on top of the charm (it
      multiplies, see spawn.roll_shiny), but also without it."""
    boost: dict[str, Any] = {"charm_factor": 1.0, "living_rate": None, "gen_factors": {}}
    if user_id is None:
        return boost
    sync_pokedex(user_id)
    done = unlocked(user_id)
    factor = float(db.get_setting("gen_caught_shiny_factor",
                                  db.DEFAULT_SETTINGS["gen_caught_shiny_factor"]))
    boost["gen_factors"] = {g: factor for g in generations() if gen_caught_key(g) in done}
    if DEX_CAUGHT_ALL in done:
        boost["charm_factor"] = float(db.get_setting("shiny_charm_caught_factor", 2.0))
    elif DEX_SEEN_ALL in done:
        boost["charm_factor"] = float(db.get_setting("shiny_charm_seen_factor", 1.1))
    if DEX_LIVING in done:
        boost["living_rate"] = float(db.get_setting("living_dex_shiny_rate", 0.01))
    return boost


def unlocked(user_id: str) -> dict[str, str]:
    return {
        row["key"]: row["unlocked_at"]
        for row in db.query("SELECT key, unlocked_at FROM achievement WHERE user_id = ?", (user_id,))
    }


def unlock(user_id: str, key: str) -> bool:
    """True, wenn der Erfolg gerade neu freigeschaltet wurde."""
    with db.cursor() as cur:
        cur.execute(
            "INSERT OR IGNORE INTO achievement (user_id, key, unlocked_at) VALUES (?,?,?)",
            (user_id, key, datetime.now().astimezone().isoformat()),
        )
        return cur.rowcount == 1


def _unlock_with_reward(
    user_id: str, key: str, by_key: dict[str, dict[str, Any]], new: list[dict[str, Any]]
) -> bool:
    """Unlock an achievement and, if that actually happened for the first time,
    hand out its items (see rewards.py)."""
    definition = by_key.get(key)
    if definition is None or not unlock(user_id, key):
        return False
    reward = definition.get("reward")
    if reward:
        rewards.grant(user_id, reward["items"], reward.get("money", 0))
    new.append(definition)
    return True


def level100_count(user_id: str) -> int:
    """How many of this user's Pokemon have EVER reached level 100 - counts
    `pokemon_level100`, which (unlike `owned_pokemon`) is never deleted on
    release or trade, so the milestone stays permanent."""
    row = db.query_one("SELECT COUNT(*) AS n FROM pokemon_level100 WHERE user_id = ?", (user_id,))
    return int(row["n"]) if row else 0


def share_all_exp(user_id: str | None) -> bool:
    """True once the user unlocked the 50th level-100 achievement: every team
    member then gets the full EXP of a fight, like with an Exp. Share."""
    return user_id is not None and LEVEL100_50TH in unlocked(user_id)


def check_level100(
    user_id: str, exp_details: list[dict[str, Any]] | None, lang: str = "en"
) -> list[dict[str, Any]]:
    """Call after any `battle.grant_exp` with `details=...`: records every team
    member that battle just brought to level 100 (`pokemon_level100`, once per
    Pokemon - `INSERT OR IGNORE`) and unlocks the level-100 milestones this
    newly qualifies for (`LEVEL100_KEYS`). Cheap no-op call when
    nobody leveled to 100."""
    reached = [e["owned_id"] for e in exp_details or []
               if e.get("level_to") == LEVEL100 and e.get("owned_id") is not None]
    if not reached:
        return []
    now = datetime.now().astimezone().isoformat()
    with db.cursor() as cur:
        cur.executemany(
            "INSERT OR IGNORE INTO pokemon_level100 (user_id, owned_id, reached_at) VALUES (?,?,?)",
            [(user_id, owned_id, now) for owned_id in reached],
        )
    return sync_level100(user_id, lang)


def sync_level100(user_id: str, lang: str = "en") -> list[dict[str, Any]]:
    """Unlock every level-100 milestone the current count qualifies for -
    also catches up milestones added later (e.g. the 101st) for players who
    were already past their threshold. Returns the NEWLY unlocked ones."""
    count = level100_count(user_id)
    by_key = {d["key"]: d for d in definitions(lang)}
    new: list[dict[str, Any]] = []
    for key in LEVEL100_KEYS:
        if count >= LEVEL100_THRESHOLD[key]:
            _unlock_with_reward(user_id, key, by_key, new)
    return new


def on_trainer_defeated(user_id: str, trainer_id: str, lang: str = "en") -> list[dict[str, Any]]:
    """Schaltet den Trainer-Erfolg frei, ggf. den Regions-Erfolg (= Master Ball,
    und je nach Regions-Sperr-Modus des Nutzers eine weitere Generation im
    Spawn-Pool, siehe unlocked_generations)
    und ggf. "Liga-Meister". Verbucht die Item-Belohnungen jedes neuen Erfolgs
    (rewards.py) und gibt die NEU freigeschalteten Erfolge zurueck."""
    by_key = {d["key"]: d for d in definitions(lang)}
    new: list[dict[str, Any]] = []
    _unlock_with_reward(user_id, trainer_key(trainer_id), by_key, new)

    trainer = trainers.get(trainer_id)
    if trainer is not None and region_cleared(user_id, trainer["region"]):
        _unlock_with_reward(user_id, region_key(trainer["region"]), by_key, new)

    done = unlocked(user_id)
    required = [d["key"] for d in by_key.values() if d.get("trainer_id")]
    if all(k in done for k in required):
        _unlock_with_reward(user_id, LEAGUE_MASTER, by_key, new)
    return new


def region_cleared(user_id: str, region_key: str) -> bool:
    """True, wenn dieser Nutzer ALLE Trainer (Arenaleiter, Top Vier, Champ)
    einer Region besiegt hat."""
    ids = [t["id"] for t in trainers.all_trainers() if t["region"] == region_key]
    if not ids:
        return False
    done = unlocked(user_id)
    return all(trainer_key(tid) in done for tid in ids)


def regions_cleared(user_id: str) -> list[str]:
    """Alle Regionen, deren Liga dieser Nutzer komplett besiegt hat
    (inklusive Kanto - dort gibt es zwar keinen Regions-Erfolg, die Liga
    zaehlt fuer den Seltenheits-Bonus aber genauso)."""
    done = unlocked(user_id)
    cleared = []
    for region in trainers.regions():
        ids = [t["id"] for t in trainers.all_trainers() if t["region"] == region["key"]]
        if ids and all(trainer_key(tid) in done for tid in ids):
            cleared.append(region["key"])
    return cleared


def rarity_bonus(user_id: str | None = None) -> dict[str, Any]:
    """Seltenheits-Bonus EINES Nutzers (siehe spawn.pick_species).

    Jede komplett besiegte Liga macht seltene Spawns wahrscheinlicher: jede
    Stufe naehert sich geometrisch ihrem Zielgewicht bei komplett besiegtem
    Spiel an (siehe rarity.region_multiplier / REGION_FULL_CLEAR_TARGET) -
    mythical waechst dabei am staerksten. Gezaehlt werden ausschliesslich die
    Regionen, die DIESER Nutzer selbst besiegt hat: die Warteschlange ist pro
    Nutzer (siehe db.py), wer selbst nichts geschafft hat, profitiert also
    nicht vom Fortschritt der anderen.

    Ohne `user_id` (nur noch alte, haushaltsweit geteilte Encounter und
    Admin-Spawns) zaehlt weiterhin der berechtigte Nutzer (db.eligible_users)
    mit den meisten besiegten Regionen."""
    total_regions = len(trainers.regions())
    if user_id is not None:
        best = len(regions_cleared(user_id))
    else:
        best = max((len(regions_cleared(user["id"])) for user in db.eligible_users()), default=0)
    return {
        "regions": best,
        "total_regions": total_regions,
        # Aktueller Faktor je Stufe beim jetzigen Fortschritt ...
        "factors": {
            tier: rarity.region_multiplier(tier, best, total_regions) for tier in rarity.TIERS
        },
        # ... und der maximale Faktor bei komplett besiegtem Spiel, fuer die
        # "noch kein Bonus"-Anzeige (siehe achievements.rarity_bonus_none).
        "max_factors": {tier: rarity.region_growth_ratio(tier) for tier in rarity.TIERS},
    }


def unlocked_generations(user_id: str | None = None) -> set[int]:
    """Welche Generationen im wilden Spawn-Pool DIESES Nutzers auftauchen
    duerfen (siehe spawn.pick_species). Das haengt vom Regions-Sperr-Modus des
    Nutzers ab (db.REGION_LOCK_MODES, Standard 'region'):

    * 'unlocked': alle Generationen von Anfang an.
    * 'region': Generation 1 (Kanto) ist offen, jede weitere erst, wenn der
      Nutzer die komplette Liga der entsprechenden Region selbst besiegt hat.
    * 'previous_region': Generation 1 ist offen, jede weitere erst, wenn der
      Nutzer die Liga der VORHERIGEN Region besiegt hat (Kanto -> Gen 2, ...).

    Warteschlange und Freischaltung sind pro Nutzer (siehe db.py), niemand
    profitiert vom Fortschritt der anderen.

    Ohne `user_id` (nur noch alte, haushaltsweit geteilte Encounter und
    Admin-Spawns) bleibt es bei der Vereinigung ueber alle Nutzer, jeweils
    nach dem Modus des einzelnen Nutzers."""
    ids = [user_id] if user_id is not None else [user["id"] for user in db.list_users()]
    generations = {1}
    for uid in ids:
        generations |= _generations_for_user(uid)
    return generations


def elite_four_defeated(user_id: str) -> int:
    """Count the number of Elite Four trainers this user has defeated."""
    done = unlocked(user_id)
    return sum(
        1 for t in trainers.all_trainers()
        if t["kind"] == "elite_four" and trainer_key(t["id"]) in done
    )


def elite_four_and_champion_defeated(user_id: str) -> int:
    """Count the number of Elite Four members AND Champions this user has
    defeated (see `healing.cooldown_minutes`)."""
    done = unlocked(user_id)
    return sum(
        1 for t in trainers.all_trainers()
        if t["kind"] in ("elite_four", "champion") and trainer_key(t["id"]) in done
    )


def _generations_for_user(user_id: str) -> set[int]:
    mode = db.user_region_lock(user_id)
    total = len(trainers.regions())
    if mode == "unlocked":
        return set(range(1, total + 1))
    generations = {1}
    for region in trainers.regions():
        unlocks = unlock_generation(region["key"], mode)
        if unlocks is not None and region_cleared(user_id, region["key"]):
            generations.add(unlocks)
    return generations


def perks(user_id: str) -> dict[str, Any]:
    """The permanent non-item bonuses this user's achievements unlocked, for the
    achievements overview (shiny and rarity bonus are reported separately):

    * `regen_bonus` / `cooldown_reduction` - healing perks from beaten Elite Four
      members and Champions (healing.regen_percent / healing.cooldown_minutes).
    * `share_all_exp` - LEVEL100_50TH, see `share_all_exp`.
    * `training` - LEVEL100_101ST unlocks Training (training.py).
    * `generations` - generations in the wild spawn pool (`unlocked_generations`).
    * `sprite_styles` - generations whose sprite style is available (`living_generations`)."""
    from . import healing  # Local: healing imports this module lazily as well

    done = unlocked(user_id)
    return {
        "regen_bonus": healing.ELITE_FOUR_REGEN_BONUS * elite_four_defeated(user_id),
        "cooldown_reduction": (healing.ELITE_FOUR_CHAMPION_COOLDOWN_REDUCTION_MINUTES
                               * elite_four_and_champion_defeated(user_id)),
        "share_all_exp": LEVEL100_50TH in done,
        "training": LEVEL100_101ST in done,
        "generations": sorted(unlocked_generations(user_id)),
        "sprite_styles": sorted(living_generations(user_id)),
    }


def overview(user_id: str) -> dict[str, Any]:
    lang = db.user_language(user_id)
    sync_pokedex(user_id, lang)
    sync_milestones(user_id, lang)
    sync_level100(user_id, lang)
    done = unlocked(user_id)
    progress = dex_progress(user_id)
    milestones = milestone_progress(user_id)
    dex_current = {DEX_SEEN_ALL: progress["seen"], DEX_CAUGHT_ALL: progress["caught"],
                   DEX_LIVING: progress["owned"]}
    level100_current = level100_count(user_id)
    gen_progress = generation_progress(user_id)
    gen_current = {}
    for gen, p in gen_progress.items():
        gen_current[gen_caught_key(gen)] = {"current": p["caught"], "total": p["total"]}
        gen_current[gen_living_key(gen)] = {"current": p["owned"], "total": p["total"]}
    items = [{**d, "unlocked": d["key"] in done, "unlocked_at": done.get(d["key"]),
              **({"progress": {"current": dex_current[d["key"]], "total": progress["total"]}}
                 if d["key"] in dex_current else {}),
              **({"progress": {"current": min(level100_current, LEVEL100_THRESHOLD[d["key"]]),
                               "total": LEVEL100_THRESHOLD[d["key"]]}}
                 if d["key"] in LEVEL100_THRESHOLD else {}),
              **({"progress": {"current": min(milestones[MILESTONES[d["key"]]["metric"]],
                                              milestone_target(d["key"], progress["total"])),
                               "total": milestone_target(d["key"], progress["total"])}}
                 if d["key"] in MILESTONES else {}),
              **({"progress": gen_current[d["key"]]} if d["key"] in gen_current else {})}
             for d in definitions(db.user_language(user_id), db.user_region_lock(user_id))]
    boost = shiny_boost(user_id)
    return {
        "shiny_boost": {"charm_factor": boost["charm_factor"],
                        "living_rate": boost["living_rate"],
                        "gen_factors": boost["gen_factors"]},
        "region_lock": db.user_region_lock(user_id),
        "achievements": items,
        "unlocked_count": sum(1 for it in items if it["unlocked"]),
        "total": len(items),
        "rarity_bonus": {**rarity_bonus(user_id), "own_regions": len(regions_cleared(user_id))},
        "perks": perks(user_id),
        "shiny_charm": db.query_one(
            "SELECT 1 FROM inventory WHERE user_id = ? AND item_key = ? AND quantity > 0",
            (user_id, SHINY_CHARM),
        ) is not None,
    }
