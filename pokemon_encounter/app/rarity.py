"""Rarity tiers of a species.

Six tiers that control spawn weighting (see spawn._rarity_weight):

    common            species found early in the game: base form
                      (`evolves_from IS NULL`, or only a baby Pokemon - see
                      BABY_IDS) AND base stat total <= 450, except the starter
                      Pokemon
    rare              everything else non-legendary: evolutions, strong
                      base forms (base stat total > 450) and the starters
                      (STARTER_IDS)
    pseudo_legendary  the pseudo-legendary lines (all stages), fixed via their
                      National Dex numbers (PSEUDO_LEGENDARY_IDS)
    paradox           paradox Pokemon from Scarlet/Violet (base game + DLC),
                      fixed via their National Dex numbers (PARADOX_IDS)
    legendary         `is_legendary` from seeding OR one of the ultra beasts
                      (ULTRA_BEAST_IDS) - those count as legendary by specification
    mythical          `is_mythical` from seeding (overrides everything else)

The tier is ALWAYS calculated from already stored columns or the fixed-coded
Dex number sets - there is no separate column and thus no migration; existing
databases work without reseeding from PokeAPI.
"""

from __future__ import annotations

from typing import Any

from . import db

COMMON = "common"
RARE = "rare"
PSEUDO_LEGENDARY = "pseudo_legendary"
PARADOX = "paradox"
LEGENDARY = "legendary"
MYTHICAL = "mythical"

#: Order = common -> rare (settings, i18n and badges use it).
TIERS = (COMMON, RARE, PSEUDO_LEGENDARY, PARADOX, LEGENDARY, MYTHICAL)

#: Boundary for common/rare in base forms.
COMMON_BST_MAX = 450

#: Base forms of all starter Pokemon (gen 1-9, grass/fire/water). They would
#: count as common (base form, low BST) but are deliberately rare. Must match
#: `api.users.STARTER_DEX_IDS` (checked in tests/test_rarity.py); kept here to
#: avoid importing the API layer.
STARTER_IDS = frozenset({
    1, 4, 7,          # Bulbasaur, Charmander, Squirtle
    152, 155, 158,    # Chikorita, Cyndaquil, Totodile
    252, 255, 258,    # Treecko, Torchic, Mudkip
    387, 390, 393,    # Turtwig, Chimchar, Piplup
    495, 498, 501,    # Snivy, Tepig, Oshawott
    650, 653, 656,    # Chespin, Fennekin, Froakie
    722, 725, 728,    # Rowlet, Litten, Popplio
    810, 813, 816,    # Grookey, Scorbunny, Sobble
    906, 909, 912,    # Sprigatito, Fuecoco, Quaxly
})

#: All stages of pseudo-legendary lines (Dratini .. Frigibax).
PSEUDO_LEGENDARY_IDS = frozenset({
    147, 148, 149,        # Dratini
    246, 247, 248,        # Larvitar
    371, 372, 373,        # Bagon
    374, 375, 376,        # Beldum
    443, 444, 445,        # Gible
    633, 634, 635,        # Deino
    704, 705, 706,        # Goomy
    782, 783, 784,        # Jangmo-o
    885, 886, 887,        # Dreepy
    996, 997, 998,        # Frigibax
})

#: All paradox Pokemon (Scarlet/Violet base game + The Hidden Treasure of
#: Area Zero) - PokeAPI doesn't set is_legendary or is_mythical for them,
#: so like PSEUDO_LEGENDARY_IDS fixed via Dex number.
PARADOX_IDS = frozenset({
    984, 985, 986, 987, 988, 989,   # Great Tusk .. Sandy Shocks (Past)
    990, 991, 992, 993, 994, 995,   # Iron Treads .. Iron Thorns (Future)
    1005, 1006,                     # Roaring Moon, Iron Valiant
    1009, 1010,                     # Walking Wake, Iron Leaves
    1020, 1021, 1022, 1023,         # Gouging Fire, Raging Bolt, Iron Boulder, Iron Crown
})

#: Ultra beasts - count as "legendary" by specification even though PokeAPI
#: doesn't seed them as is_legendary/is_mythical.
ULTRA_BEAST_IDS = frozenset({
    793, 794, 795, 796, 797, 798, 799,   # Nihilego .. Guzzlord
    803, 804, 805, 806,                  # Poipole, Naganadel, Stakataka, Blacephalon
})

#: Baby Pokemon (PokeAPI `is_baby`) - they exist purely for breeding and were
#: retroactively inserted as `evolves_from_species` of Pokemon that already
#: existed in an earlier generation (e.g. Snorlax predates Munchlax by two
#: generations). Their adult form must NOT count as "is an evolution" for the
#: rarity rule - otherwise it's excluded from the wild spawn pool entirely
#: (see spawn._spawn_pool) and iconic wild Pokemon like Pikachu, Snorlax or
#: Chansey could never spawn. See `effective_evolves_from`.
BABY_IDS = frozenset({
    172,   # Pichu (-> Pikachu)
    173,   # Cleffa (-> Clefairy)
    174,   # Igglybuff (-> Jigglypuff)
    175,   # Togepi (-> Togetic)
    236,   # Tyrogue (-> Hitmonlee/Hitmonchan/Hitmontop)
    238,   # Smoochum (-> Jynx)
    239,   # Elekid (-> Electabuzz)
    240,   # Magby (-> Magmar)
    298,   # Azurill (-> Marill)
    360,   # Wynaut (-> Wobbuffet)
    406,   # Budew (-> Roselia)
    433,   # Chingling (-> Chimecho)
    438,   # Bonsly (-> Sudowoodo)
    439,   # Mime Jr. (-> Mr. Mime)
    440,   # Happiny (-> Chansey)
    446,   # Munchlax (-> Snorlax)
    458,   # Mantyke (-> Mantine)
    848,   # Toxel (-> Toxtricity)
})

_STAT_COLUMNS = ("base_hp", "base_atk", "base_def", "base_spa", "base_spd", "base_spe")


def base_stat_total(species: dict[str, Any]) -> int:
    return sum(int(species.get(column) or 0) for column in _STAT_COLUMNS)


def effective_evolves_from(species: dict[str, Any]) -> int | None:
    """`species["evolves_from"]`, but a baby Pokemon (BABY_IDS) as pre-evolution
    doesn't count - see BABY_IDS for why."""
    evolves_from = species.get("evolves_from")
    return None if evolves_from in BABY_IDS else evolves_from


def tier_for(species: dict[str, Any]) -> str:
    """Deterministische Stufe einer Spezies-Zeile."""
    species_id = int(species.get("id") or 0)
    if species.get("is_mythical"):
        return MYTHICAL
    if species.get("is_legendary") or species_id in ULTRA_BEAST_IDS:
        return LEGENDARY
    if species_id in PARADOX_IDS:
        return PARADOX
    if species_id in PSEUDO_LEGENDARY_IDS:
        return PSEUDO_LEGENDARY
    if species_id in STARTER_IDS:
        return RARE
    if effective_evolves_from(species) is None and base_stat_total(species) <= COMMON_BST_MAX:
        return COMMON
    return RARE


#: Tiers whose Pokémon arrive locked in `owned_pokemon` (see api/team.py:
#: `_release` and db._migrate_locked) - legendary and mythical, the two
#: rarest tiers, never come in unlocked by default.
AUTO_LOCK_TIERS = frozenset({LEGENDARY, MYTHICAL})


def auto_lock(species: dict[str, Any]) -> bool:
    """Whether a newly owned Pokémon of this species should start locked
    (can't be released without unlocking it first)."""
    return tier_for(species) in AUTO_LOCK_TIERS


def setting_key(tier: str) -> str:
    return f"rarity_weight_{tier}"


def out_of_order_tiers(weights: dict[str, float]) -> list[str]:
    """Stufen, deren Gewicht hoeher ist als das einer VORHERIGEN (eigentlich
    haeufigeren) Stufe in TIERS - z.B. paradox hoeher als pseudo_legendary.

    Da `spawn.tier_shares` jede Stufe nur relativ zur SUMME aller sechs Gewichte
    normalisiert (siehe admin.settings_group_hint.rarity), reisst so eine Stufe
    unbemerkt den Grossteil der Spawns an sich, sobald sie das hoechste Gewicht
    aller Stufen bis dahin ueberschreitet - selbst wenn ihr eigener Wert
    unveraendert und fuer sich genommen klein aussieht (z.B. paradox=2.5 bei
    common/rare/pseudo_legendary/legendary/mythical << 1). Nur eine Warnung,
    kein Validierungsfehler: manche Admins wollen eine Stufe bewusst bevorzugen
    (z.B. ein Paradox-Event)."""
    out_of_order: list[str] = []
    highest_so_far = 0.0
    for tier in TIERS:
        value = float(weights.get(tier, 0.0))
        if highest_so_far > 0 and value > highest_so_far:
            out_of_order.append(tier)
        highest_so_far = max(highest_so_far, value)
    return out_of_order


def tier_multipliers() -> dict[str, float]:
    """Einmal pro Spawn laden - `pick_species` gewichtet ueber den ganzen Pool."""
    return {
        tier: float(db.get_setting(setting_key(tier), db.DEFAULT_SETTINGS[setting_key(tier)]))
        for tier in TIERS
    }


def multiplier_for(species: dict[str, Any], multipliers: dict[str, float] | None = None) -> float:
    tier = tier_for(species)
    if multipliers is None:
        multipliers = tier_multipliers()
    return float(multipliers.get(tier, 1.0))


# --------------------------------------------------- Bonus fuer besiegte Ligen

# Zielgewichte, wenn WIRKLICH JEDE Region besiegt ist (siehe README, Abschnitt
# "Seltenheits-Stufen"). Ohne Bonus ergeben die Standard-Gewichte in
# db.DEFAULT_SETTINGS eine Verteilung von 83/10/3/2,5/1/0,5 % (Summe 100).
# Mit jeder besiegten Liga naehert sich jede Stufe GEOMETRISCH (gleicher
# Faktor pro einzelner Region, siehe region_multiplier) dieser Zielverteilung
# an, bis sie bei komplett besiegtem Spiel exakt erreicht ist. mythical wuchs
# dabei absichtlich am staerksten (x10, 0,5 -> 5 %, exakt die gewuenschte
# Vorgabe "bei allen Regionen 5 %"); die anderen Stufen wachsen schwaecher,
# damit das Fortschrittsgefuehl weder zu leicht noch zu hart ausfaellt.
REGION_FULL_CLEAR_TARGET: dict[str, float] = {
    COMMON: 58.5,
    RARE: 18.0,
    PSEUDO_LEGENDARY: 8.0,
    PARADOX: 6.5,
    LEGENDARY: 4.0,
    MYTHICAL: 5.0,
}


def region_growth_ratio(tier: str) -> float:
    """Zielgewicht / Standardgewicht einer Stufe - der Faktor, dem sich die
    Stufe bei komplett besiegten Regionen annaehert (siehe region_multiplier)."""
    base = float(db.DEFAULT_SETTINGS[setting_key(tier)])
    if base <= 0:
        return 1.0
    return REGION_FULL_CLEAR_TARGET[tier] / base


def region_multiplier(tier: str, regions_cleared: int, total_regions: int) -> float:
    """Faktor auf das Gewicht einer Stufe beim aktuellen Liga-Fortschritt:
    1.0 bei 0 besiegten Regionen, exakt `region_growth_ratio(tier)` wenn ALLE
    Regionen besiegt sind - dazwischen geometrisch interpoliert, damit jede
    einzelne besiegte Liga denselben relativen Zuwachs bringt ("incremental
    improvement for every region beaten")."""
    if total_regions <= 0:
        return 1.0
    progress = max(0.0, min(1.0, regions_cleared / total_regions))
    return region_growth_ratio(tier) ** progress
