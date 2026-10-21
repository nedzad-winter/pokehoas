"""Spawn engine.

Rules from project planning:
* Each entity selected by the user can ITSELF trigger a spawn. The trigger
  type additionally determines type probability - a light switch thus triggers
  a normal encounter, just with increased chance of an Electric Pokemon.
* The queue is PER USER: one trigger event creates one encounter per registered
  player, each with its own species/level/shiny roll based on THAT player's
  unlocked regions and rarity bonus (see process_events).
* Home-internal triggers draw from the FULL Pokemon pool. Biomes only apply to
  geolocation triggers (phone's zone).
* Rarity comes from the rarity tier of the species (rarity.py, five tiers each
  with an adjustable multiplier) times the catch rate (catch_rate) as fine
  gradation within a tier.
"""

from __future__ import annotations

import json
import logging
import math
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from . import achievements, battle, db, economy, rarity, roaming

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"
_biome_data: dict[str, Any] | None = None

STAT_KEYS = ["hp", "atk", "def", "spa", "spd", "spe"]

# Event spawns (create_event_encounters): a mythical Pokemon must not flee after
# the usual week, and must not use up the day's spawns.
EVENT_SOURCE_KIND = "event"
EVENT_SOURCE_PREFIX = "event:gen_living_"
EVENT_RETENTION_DAYS = 365

NATURES = [
    "hardy", "lonely", "brave", "adamant", "naughty", "bold", "docile",
    "relaxed", "impish", "lax", "timid", "hasty", "serious", "jolly",
    "naive", "modest", "mild", "quiet", "bashful", "rash", "calm",
    "gentle", "sassy", "careful", "quirky",
]


def biome_data() -> dict[str, Any]:
    global _biome_data
    if _biome_data is None:
        _biome_data = json.loads((DATA_DIR / "biomes.json").read_text(encoding="utf-8"))
    return _biome_data


def default_type_weights_for(entity_id: str, device_class: str | None = None) -> dict[str, float]:
    """Suggested values when the user adds a new entity."""
    hints = biome_data().get("trigger_type_hints", {})
    domain = entity_id.split(".", 1)[0]
    if device_class:
        specific = hints.get(f"{domain}.{device_class}")
        if specific is not None:
            return dict(specific)
    return dict(hints.get(domain, {}))


# ------------------------------------------------------------- Weighting

def _maxed_ivs_spawn_factor() -> float:
    return float(db.get_setting(
        "maxed_ivs_spawn_weight_factor", db.DEFAULT_SETTINGS["maxed_ivs_spawn_weight_factor"]
    ))


def _catch_factor(species: dict[str, Any]) -> float:
    return 0.6 + 0.4 * float(species["catch_rate"]) / 255.0


def _tier_norms(species_list: list[dict[str, Any]]) -> dict[str, float]:
    """Per tier: 1 / sum of the catch factors of the tier's species in the pool.

    The tier weights are meant as the tier's SHARE of all spawns. Without this
    normalisation every species carries the full tier weight, so a tier with
    321 species (common) would beat one with 10 (pseudo-legendary) by far more
    than its weight says. Scaling each species by this norm makes the tier's
    weights sum to exactly its tier weight."""
    sums: dict[str, float] = {}
    for species in species_list:
        tier = rarity.tier_for(species)
        sums[tier] = sums.get(tier, 0.0) + _catch_factor(species)
    return {tier: 1.0 / total for tier, total in sums.items() if total > 0}


def _rarity_weight(
    species: dict[str, Any],
    multipliers: dict[str, float] | None = None,
    maxed_ivs_factor: float | None = None,
    tier_norms: dict[str, float] | None = None,
) -> float:
    """Spawn weight = tier multiplier x catch rate fine gradation.

    With `tier_norms` (see `_tier_norms`) the weight is additionally divided by
    the tier's pool size, so the tier multiplier becomes the tier's share of
    the pool; without it the weight is the raw per-species value.

    The tier (common/rare/pseudo_legendary/legendary/mythical) comes from
    `rarity.tier_for`, its multiplier from settings. Catch rate only scales
    WITHIN a tier (factor 0.6 at catch rate 3 to 1.0 at 255) - this narrow
    range is intentional: otherwise a mythical with high catch rate could become
    more common than a legendary with low catch rate.

    A species whose IV floor is already maxed (`economy.species_ivs_maxed`)
    gets this weight multiplied by `maxed_ivs_spawn_weight_factor` - catching
    it again can no longer improve its own future spawns, so it should surface
    less often once the player has already caught it enough times.

    `multipliers` and `maxed_ivs_factor` can be preloaded (see
    rarity.tier_multipliers / `_maxed_ivs_spawn_factor`) so the settings table
    doesn't get read per species.
    """
    weight = rarity.multiplier_for(species, multipliers) * _catch_factor(species)
    if tier_norms is not None:
        weight *= tier_norms.get(rarity.tier_for(species), 1.0)
    if economy.species_ivs_maxed(species):
        factor = _maxed_ivs_spawn_factor() if maxed_ivs_factor is None else maxed_ivs_factor
        weight *= factor
    return max(weight, 1e-6)


def _boosted_tier_multipliers(user_id: str | None = None) -> dict[str, float]:
    """Tier multipliers including league rewards: each completely defeated
    region boosts each tier tier-specifically towards its target weight when
    the game is completely beaten (see achievements.rarity_bonus, rarity.
    region_multiplier). This makes rarer species not only relatively, but also
    absolutely more common the further THAT player progresses - mythical most
    strongly."""
    multipliers = rarity.tier_multipliers()
    factors = achievements.rarity_bonus(user_id)["factors"]
    return {tier: value * factors.get(tier, 1.0) for tier, value in multipliers.items()}


def _type_multiplier(species: dict[str, Any], type_weights: dict[str, float]) -> float:
    if not type_weights:
        return 1.0
    multiplier = 1.0
    for slot in ("type1", "type2"):
        poke_type = species.get(slot)
        if poke_type and poke_type in type_weights:
            multiplier *= float(type_weights[poke_type])
    return multiplier


def _weighted_choice(candidates: list[dict[str, Any]], weights: list[float]) -> dict[str, Any]:
    total = sum(weights)
    if total <= 0:
        return random.choice(candidates)
    roll = random.random() * total
    running = 0.0
    for candidate, weight in zip(candidates, weights):
        running += weight
        if roll <= running:
            return candidate
    return candidates[-1]


def _spawn_pool(user_id: str | None) -> list[dict[str, Any]]:
    """Wild-spawnable species of this user: base forms of the unlocked generations.

    A species that only evolves from a baby Pokemon (rarity.BABY_IDS) counts as
    a base form too - babies exist purely for breeding and must not gate their
    adult form out of the wild pool (e.g. Snorlax, listed as evolving from
    Munchlax; see rarity.effective_evolves_from)."""
    placeholders = ",".join("?" for _ in rarity.BABY_IDS)
    species_list = db.query(
        f"SELECT * FROM species WHERE evolves_from IS NULL OR evolves_from IN ({placeholders})",
        tuple(rarity.BABY_IDS),
    )
    unlocked_gens = achievements.unlocked_generations(user_id)
    return [s for s in species_list if s["generation"] in unlocked_gens]


def tier_shares(user_id: str | None = None) -> dict[str, Any]:
    """Share of each rarity tier among this user's spawns (percent, sums to 100).

    Same weighting as `pick_species` - unlocked generations, the user's league
    bonus, tier normalisation, maxed-IV factor - but without the trigger's type
    bonus and biome, which differ per spawn. `base_percent` is the share
    without league bonus, to show what the bonus changes. A tier that has no
    species in the pool yet (e.g. its generation is still locked) gets 0."""
    pool = _spawn_pool(user_id)
    norms = _tier_norms(pool)
    maxed_ivs_factor = _maxed_ivs_spawn_factor()
    plain = rarity.tier_multipliers()
    boosted = _boosted_tier_multipliers(user_id)

    def shares(multipliers: dict[str, float]) -> dict[str, float]:
        totals = dict.fromkeys(rarity.TIERS, 0.0)
        for species in pool:
            totals[rarity.tier_for(species)] += _rarity_weight(
                species, multipliers, maxed_ivs_factor, norms)
        grand = sum(totals.values())
        return {tier: (100.0 * value / grand if grand else 0.0) for tier, value in totals.items()}

    base, current = shares(plain), shares(boosted)
    bonus = achievements.rarity_bonus(user_id)
    return {
        "regions": bonus["regions"],
        "total_regions": bonus["total_regions"],
        "tiers": [
            {"tier": tier, "base_percent": round(base[tier], 3),
             "percent": round(current[tier], 3), "in_pool": tier in norms}
            for tier in rarity.TIERS
        ],
    }


def pick_species(
    type_weights: dict[str, float] | None = None,
    biome: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any] | None:
    """Waehlt eine Spezies fuer die Warteschlange von `user_id`.

    `type_weights` kommt vom ausloesenden Trigger (z.B. Lichtschalter -> Elektro).
    `biome` wird nur bei Geolocation-Triggern gesetzt und legt eigene
    Typ-Gewichte plus eine Liste bevorzugter Spezies obendrauf.

    Nur Basis-Formen (keine Entwicklungen) sind wild fangbar - Entwicklungen
    gibt es ausschliesslich durch Level-Aufstieg bzw. Entwicklungs-Items.

    Die Seltenheits-Stufe (rarity.py) gewichtet den Pool zusaetzlich: haeufige
    Arten deutlich oefter als Entwicklungen/starke Arten, Pseudo-Legendaere,
    Legendaere und Mysterioese nur sehr selten. Typ-Bonus und Biom wirken
    multiplikativ obendrauf. Jede von DIESEM Nutzer besiegte Liga hebt
    zusaetzlich alle Stufen ausser "common" (siehe _boosted_tier_multipliers).

    Welche Generationen im Pool sind, haengt vom Regions-Sperr-Modus dieses
    Nutzers ab (achievements.unlocked_generations): alles von Anfang an,
    Generation 1 plus jede Generation, deren Region er selbst besiegt hat,
    oder Generation 1 plus die Generation NACH jeder besiegten Region. Ohne
    `user_id` gilt weiterhin der haushaltsweite Pool (alte geteilte
    Encounter, Admin-Spawns).
    """
    species_list = _spawn_pool(user_id)
    if not species_list:
        return None

    weights_from_type = dict(type_weights or {})
    featured: set[str] = set()

    if biome:
        config = biome_data()["biomes"].get(biome)
        if config:
            for poke_type, value in config.get("type_weights", {}).items():
                # Biom und Trigger multiplizieren sich.
                weights_from_type[poke_type] = weights_from_type.get(poke_type, 1.0) * value
            featured = set(config.get("featured", []))

    multipliers = _boosted_tier_multipliers(user_id)
    maxed_ivs_factor = _maxed_ivs_spawn_factor()
    tier_norms = _tier_norms(species_list)
    weights = []
    for species in species_list:
        weight = (_rarity_weight(species, multipliers, maxed_ivs_factor, tier_norms)
                  * _type_multiplier(species, weights_from_type))
        if species["name"] in featured:
            weight *= 6.0
        weights.append(weight)

    return _weighted_choice(species_list, weights)


# ----------------------------------------------------------- Encounter bauen

def roll_ivs(species_id: int | None = None) -> dict[str, int]:
    """Zufaellige IVs 0-31. Mit `species_id` wird jeder Wert nur auf die
    IV-Untergrenze der Art angehoben (`species.min_ivs`, siehe economy.py) -
    die Zufallsverteilung darueber bleibt unberuehrt."""
    ivs = {key: random.randint(0, 31) for key in STAT_KEYS}
    if species_id is None:
        return ivs
    floor = economy.species_min_ivs(species_id)
    return {key: max(value, floor[key]) for key, value in ivs.items()}


# Genderless species (PokeAPI gender_rate -1, checked 2026-10-04) are stored with
# gender NULL: they have no male/female in the games, so they never roll one and
# can only breed with Ditto. Magnemite/Voltorb-style Pokemon and many legendaries
# (Zapdos, Mewtwo, Lugia, ...) are in here.
GENDERLESS_SPECIES = frozenset({
    # Gen 1: magnemite, magneton, voltorb, electrode, staryu, starmie, ditto, porygon, articuno, zapdos, moltres, mewtwo, mew
    81, 82, 100, 101, 120, 121, 132, 137, 144, 145, 146, 150, 151,
    # Gen 2: unown, porygon2, raikou, entei, suicune, lugia, ho-oh, celebi
    201, 233, 243, 244, 245, 249, 250, 251,
    # Gen 3: shedinja, lunatone, solrock, baltoy, claydol, beldum, metang, metagross, regirock, regice, registeel, kyogre, groudon, rayquaza, jirachi, deoxys
    292, 337, 338, 343, 344, 374, 375, 376, 377, 378, 379, 382, 383, 384, 385, 386,
    # Gen 4: bronzor, bronzong, magnezone, porygon-z, rotom, uxie, mesprit, azelf, dialga, palkia, regigigas, giratina, phione, manaphy, darkrai, shaymin, arceus
    436, 437, 462, 474, 479, 480, 481, 482, 483, 484, 486, 487, 489, 490, 491, 492, 493,
    # Gen 5: victini, klink, klang, klinklang, cryogonal, golett, golurk, cobalion, terrakion, virizion, reshiram, zekrom, kyurem, keldeo, meloetta, genesect
    494, 599, 600, 601, 615, 622, 623, 638, 639, 640, 643, 644, 646, 647, 648, 649,
    # Gen 6: carbink, xerneas, yveltal, zygarde, diancie, hoopa, volcanion
    703, 716, 717, 718, 719, 720, 721,
    # Gen 7: type-null, silvally, minior, dhelmise, tapu-koko, tapu-lele, tapu-bulu, tapu-fini, cosmog, cosmoem, solgaleo, lunala, nihilego, buzzwole, pheromosa, xurkitree, celesteela, kartana, guzzlord, necrozma, magearna, marshadow, poipole, naganadel, stakataka, blacephalon, zeraora, meltan, melmetal
    772, 773, 774, 781, 785, 786, 787, 788, 789, 790, 791, 792, 793, 794, 795, 796, 797, 798,
    799, 800, 801, 802, 803, 804, 805, 806, 807, 808, 809,
    # Gen 8: sinistea, polteageist, falinks, dracozolt, arctozolt, dracovish, arctovish, zacian, zamazenta, eternatus, zarude, regieleki, regidrago, glastrier, spectrier, calyrex
    854, 855, 870, 880, 881, 882, 883, 888, 889, 890, 893, 894, 895, 896, 897, 898,
    # Gen 9: tandemaus, maushold, great-tusk, scream-tail, brute-bonnet, flutter-mane, slither-wing, sandy-shocks, iron-treads, iron-bundle, iron-hands, iron-jugulis, iron-moth, iron-thorns, gimmighoul, gholdengo, wo-chien, chien-pao, ting-lu, chi-yu, roaring-moon, iron-valiant, koraidon, miraidon, walking-wake, iron-leaves, poltchageist, sinistcha, gouging-fire, raging-bolt, iron-boulder, iron-crown, pecharunt
    924, 925, 984, 985, 986, 987, 988, 989, 990, 991, 992, 993, 994, 995, 999, 1000, 1001, 1002,
    1003, 1004, 1005, 1006, 1007, 1008, 1009, 1010, 1012, 1013, 1020, 1021, 1022, 1023, 1025,
})


def roll_gender(species_id: int) -> str | None:
    """Random male/female, or None for a genderless species (GENDERLESS_SPECIES)."""
    if species_id in GENDERLESS_SPECIES:
        return None
    return random.choice(["male", "female"])


def roll_level() -> int:
    low = min(int(db.get_setting("wild_level_min", 2)), battle.MAX_LEVEL)
    high = min(int(db.get_setting("wild_level_max", 30)), battle.MAX_LEVEL)
    if high <= low:
        return max(1, low)
    # Leichte Neigung zu niedrigeren Leveln.
    return max(1, min(high, int(low + (high - low) * random.random() ** 1.4)))


def shiny_charm_owned(user_id: str | None = None) -> bool:
    """Besitzt dieser Nutzer den Schillerpin? Jeder spawnt in seine eigene
    Warteschlange, der Pin wirkt also nur auf die eigenen Spawns. Ohne
    `user_id` (alte geteilte Encounter, Admin-Spawns) zaehlt weiterhin, ob
    irgendwer im Haushalt ihn besitzt."""
    if user_id is not None:
        return db.query_one(
            "SELECT 1 FROM inventory WHERE item_key = 'shiny_charm' AND user_id = ? "
            "AND quantity > 0 LIMIT 1",
            (user_id,),
        ) is not None
    return db.query_one(
        "SELECT 1 FROM inventory WHERE item_key = 'shiny_charm' AND quantity > 0 LIMIT 1"
    ) is not None


def roll_shiny(user_id: str | None = None, generation: int | None = None) -> bool:
    """`generation` = the species' generation: once the user caught every
    species of it, its factor (achievements.shiny_boost `gen_factors`)
    multiplies the rate - with or without the charm."""
    rate = float(db.get_setting("shiny_rate", 1 / 4096))
    boost = achievements.shiny_boost(user_id)
    if shiny_charm_owned(user_id):
        # Pokedex perks make the charm stronger (achievements.shiny_boost).
        rate *= float(db.get_setting("shiny_charm_multiplier", 2)) * boost["charm_factor"]
    rate *= boost["gen_factors"].get(generation, 1.0)
    if boost["living_rate"] is not None:
        rate = max(rate, boost["living_rate"])
    return random.random() < rate


def roll_ability(species_id: int) -> str | None:
    """Wuerfelt die EINE Faehigkeit eines neuen Individuums aus dem Pool in
    `species_ability` (siehe abilities.py). Faellt sauber auf `None` zurueck,
    wenn fuer diese Spezies noch keine Faehigkeiten geseedet sind (Hintergrund-
    Seeding laeuft noch) - kein Grund, einen Spawn/Fang/Starter abzulehnen,
    die Kampf-Engine behandelt `ability=None` ueberall als "keine Wirkung"."""
    rows = db.query(
        "SELECT ability_key, slot FROM species_ability WHERE species_id = ?", (species_id,)
    )
    if not rows:
        return None
    hidden = [r["ability_key"] for r in rows if r["slot"] == 2]
    regular = [r["ability_key"] for r in rows if r["slot"] != 2]
    chance = float(db.get_setting("hidden_ability_chance", 0.05))
    if hidden and random.random() < chance:
        return random.choice(hidden)
    if regular:
        return random.choice(regular)
    return random.choice(hidden) if hidden else None


def _apply_roamer_wound(encounter_id: int, user_id: str, species: dict[str, Any]) -> None:
    """A roaming legendary that ran off from this player earlier comes back
    with the damage it had (see roaming.py) - the encounter starts from that HP."""
    fraction = roaming.take_wound(user_id, species["id"])
    if fraction is None:
        return
    row = db.query_one("SELECT level, ivs FROM encounter WHERE id = ?", (encounter_id,))
    max_hp = battle.stat_hp(species["base_hp"], json.loads(row["ivs"]).get("hp", 15), row["level"],
                            species_id=species["id"])
    db.execute("UPDATE encounter SET current_hp = ? WHERE id = ?",
               (max(1, round(max_hp * fraction)), encounter_id))


def create_encounter(
    species: dict[str, Any],
    source_entity: str | None,
    source_kind: str = "home",
    biome: str | None = None,
    now: datetime | None = None,
    user_id: str | None = None,
    retention_days: int | None = None,
) -> int:
    """Legt einen Encounter an - mit `user_id` in dessen eigener
    Warteschlange, ohne (Admin-Spawn) haushaltsweit geteilt.
    `retention_days` overrides the `retention_days` setting (event spawns)."""
    now = now or datetime.now().astimezone()
    if retention_days is None:
        retention_days = int(db.get_setting("retention_days", 7))
    expires_at = now + timedelta(days=retention_days)
    generation = species.get("generation")

    encounter_id = db.execute(
        """
        INSERT INTO encounter (
            user_id, species_id, level, shiny, ivs, nature, gender, ability,
            source_entity, source_kind, biome, status, created_at, expires_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?, 'waiting', ?, ?)
        """,
        (
            user_id,
            species["id"], roll_level(), int(roll_shiny(user_id, generation)),
            json.dumps(roll_ivs(species["id"])),
            random.choice(NATURES), roll_gender(species["id"]),
            roll_ability(species["id"]),
            source_entity, source_kind, biome,
            now.isoformat(), expires_at.isoformat(),
        ),
    )

    if user_id is not None:
        _apply_roamer_wound(encounter_id, user_id, species)

    # "Gesehen" zaehlt fuer den Besitzer der Warteschlange - ein geteilter
    # Encounter (user_id NULL) dagegen fuer jeden registrierten Nutzer.
    seen_by = [{"id": user_id}] if user_id is not None else db.list_users()
    for user in seen_by:
        db.execute(
            """
            INSERT INTO dex_entry (species_id, user_id, seen_count, first_seen_at)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(species_id, user_id) DO UPDATE SET seen_count = seen_count + 1
            """,
            (species["id"], user["id"], now.isoformat()),
        )
    return encounter_id


def create_event_encounters(user_id: str, generation: int,
                            species_list: list[dict[str, Any]]) -> list[int]:
    """Reward of the living Pokedex of `generation` (achievements.sync_generations):
    each species (its mythical Pokemon) appears once in the user's queue.
    Event spawns stay EVENT_RETENTION_DAYS and count neither for the daily
    cap nor for the global cooldown (`source_kind = 'event'`)."""
    return [
        create_encounter(species, source_entity=f"{EVENT_SOURCE_PREFIX}{generation}",
                         source_kind=EVENT_SOURCE_KIND, user_id=user_id,
                         retention_days=EVENT_RETENTION_DAYS)
        for species in species_list
    ]


def event_generation(source_entity: str | None) -> int | None:
    """Generation of an event spawn's `source_entity` ("event:gen_living_3" -> 3)."""
    if not source_entity or not source_entity.startswith(EVENT_SOURCE_PREFIX):
        return None
    suffix = source_entity[len(EVENT_SOURCE_PREFIX):]
    return int(suffix) if suffix.isdigit() else None


# ------------------------------------------------------------ Anzeigenamen

def source_labels() -> dict[str, str]:
    """`entity_id` -> Anzeigename aller konfigurierten Trigger. Die Tabelle ist
    klein, deshalb einmal pro Anfrage komplett laden statt je Encounter."""
    return {
        row["entity_id"]: row["friendly_name"] or row["entity_id"]
        for row in db.query("SELECT entity_id, friendly_name FROM trigger_entity")
    }


def source_label(source_entity: str | None, labels: dict[str, str] | None = None) -> str | None:
    """Anzeigename des Ausloesers fuer die Warteschlange: der friendly_name des
    Geraets aus Home Assistant statt der Entity-ID. Faellt auf die ID zurueck,
    wenn es keinen Trigger dazu gibt (Admin-Spawn, geloeschte Entity)."""
    if not source_entity:
        return None
    if labels is None:
        row = db.query_one(
            "SELECT friendly_name FROM trigger_entity WHERE entity_id = ?", (source_entity,)
        )
        return (row["friendly_name"] if row else None) or source_entity
    return labels.get(source_entity, source_entity)


# ------------------------------------------------------------ Event-Verarbeitung

def _entity_cooldown_blocked(trigger: dict[str, Any], now: datetime) -> bool:
    """Eigener Cooldown der Entity - der gilt haushaltsweit, das Geraet loest
    ja fuer alle zugleich aus."""
    entity_cooldown = int(trigger.get("cooldown_minutes") or 0)
    if entity_cooldown > 0 and trigger.get("last_spawn_at"):
        last = datetime.fromisoformat(trigger["last_spawn_at"])
        if now - last < timedelta(minutes=entity_cooldown):
            return True
    return False


def _cooldown_blocked(user_id: str, now: datetime) -> bool:
    """Globaler Cooldown - er zaehlt pro Nutzer, weil jeder seine eigene
    Warteschlange hat (geteilte Alt-Encounter zaehlen fuer jeden mit)."""
    global_cooldown = int(db.get_setting("global_cooldown_minutes", 30))
    if global_cooldown <= 0:
        return False
    last = db.query_one(
        "SELECT created_at FROM encounter WHERE (user_id = ? OR user_id IS NULL) "
        "AND source_kind != ? ORDER BY id DESC LIMIT 1",
        (user_id, EVENT_SOURCE_KIND),
    )
    if not last:
        return False
    return now - datetime.fromisoformat(last["created_at"]) < timedelta(minutes=global_cooldown)


def _daily_cap_reached(user_id: str, now: datetime) -> bool:
    """Tageslimit - ebenfalls pro Nutzer (ein Event erzeugt je Nutzer eine
    Zeile, gezaehlt wird also die eigene Warteschlange)."""
    cap = int(db.get_setting("max_spawns_per_day", 15))
    if cap <= 0:
        return False
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM encounter "
        "WHERE created_at >= ? AND (user_id = ? OR user_id IS NULL) AND source_kind != ?",
        (day_start.isoformat(), user_id, EVENT_SOURCE_KIND),
    )
    return bool(row and row["n"] >= cap)


def spawn_users() -> list[dict[str, Any]]:
    """Fuer wen wird gespawnt? Fuer jeden registrierten Nutzer - wer die Web-App
    noch nie geoeffnet hat, existiert nicht und bekommt daher auch keine
    Warteschlange (siehe app/auth.py: Nutzer entstehen beim ersten Aufruf)."""
    return db.list_users()


def process_events(
    events: Iterable[dict[str, Any]],
    triggers: list[dict[str, Any]],
    now: datetime | None = None,
    force: bool = False,
) -> int:
    """Verarbeitet Zustandswechsel und erzeugt daraus Encounter.

    Ob ein Event ueberhaupt spawnt (Trigger-Zustand, Entity-Cooldown,
    `spawn_chance`), wird EINMAL je Event entschieden - das Geraet loest fuer
    den ganzen Haushalt aus. Danach bekommt jeder Nutzer seinen eigenen
    Encounter mit eigenem Spezies-Wurf, damit niemand von den freigeschalteten
    Regionen der anderen profitiert. Gibt die Zahl der angelegten Encounter
    zurueck (bei N Nutzern also bis zu N je Event).

    `force=True` (admin "trigger now" button, see api/config_routes.py
    fire_trigger) ueberspringt nur den Entity-Cooldown und den Zufalls-Wurf
    ueber `spawn_chance` - beides Geraete-Eigenschaften, die ein manuelles
    Ausloesen gerade umgehen soll. Die Pro-Nutzer-Schutzmechanismen
    (Tageslimit, globaler Cooldown) bleiben unveraendert bestehen, damit ein
    Test-Spawn nicht die normalen Spiel-Grenzen aushebelt.
    """
    now = now or datetime.now().astimezone()
    by_entity = {trigger["entity_id"]: trigger for trigger in triggers}
    users = spawn_users()
    created = 0
    if not users:
        return 0
    # (user_id, encounter_id) of this round - the players' auto rules get them
    # once all events are processed (see auto_rules.run_on_spawn).
    spawned_ids: list[tuple[str, int]] = []

    for event in events:
        trigger = by_entity.get(event["entity_id"])
        if trigger is None:
            continue

        wanted_states = json.loads(trigger.get("trigger_states") or '["on"]')
        if wanted_states and event.get("state") not in wanted_states:
            continue

        if not force and _entity_cooldown_blocked(trigger, now):
            continue

        if not force and random.random() > float(trigger.get("spawn_chance") or 0.25):
            continue

        type_weights = json.loads(trigger.get("type_weights") or "{}")
        biome = _biome_for_trigger(trigger)
        spawned = 0
        for user in users:
            if _daily_cap_reached(user["id"], now):
                log.debug("Tageslimit fuer %s erreicht", user["id"])
                continue
            if _cooldown_blocked(user["id"], now):
                continue
            species = pick_species(type_weights=type_weights, biome=biome, user_id=user["id"])
            if species is None:
                log.warning("Kein Pokédex vorhanden - Spawn uebersprungen")
                return created
            encounter_id = create_encounter(
                species,
                source_entity=trigger["entity_id"],
                source_kind="geo" if biome else "home",
                biome=biome,
                now=now,
                user_id=user["id"],
            )
            spawned_ids.append((user["id"], encounter_id))
            spawned += 1

        if not spawned:
            continue
        db.execute(
            "UPDATE trigger_entity SET last_spawn_at = ? WHERE entity_id = ?",
            (now.isoformat(), trigger["entity_id"]),
        )
        # Damit der naechste Cooldown-Check im selben Durchlauf greift.
        trigger["last_spawn_at"] = now.isoformat()
        created += spawned

    from . import auto_rules  # auto_rules imports spawn
    auto_rules.run_on_spawn(spawned_ids)
    return created


def _biome_for_trigger(trigger: dict[str, Any]) -> str | None:
    """Biome greifen nur bei Geolocation-Triggern (zone.* / person.*)."""
    entity_id = trigger["entity_id"]
    if not entity_id.startswith(("zone.", "person.", "device_tracker.")):
        return None
    row = db.query_one(
        "SELECT biome FROM zone_biome WHERE zone_entity_id = ? AND enabled = 1",
        (entity_id,),
    )
    return row["biome"] if row else None


# ----------------------------------------------------------------- Retention

def expire_stale_encounters(now: datetime | None = None) -> int:
    """Wartende Encounter, deren Retention abgelaufen ist, fliehen."""
    now = now or datetime.now().astimezone()
    rows = db.query(
        "SELECT id FROM encounter WHERE status = 'waiting' AND expires_at <= ?",
        (now.isoformat(),),
    )
    for row in rows:
        db.execute(
            "UPDATE encounter SET status = 'fled', resolved_at = ? WHERE id = ?",
            (now.isoformat(), row["id"]),
        )
    return len(rows)
