"""AbilityDex: lookup list of all abilities (catalog `data/abilities.json`).

Like the AttackDex the catalog itself is the same for everyone; two fields
are per user: `species` only lists species the user has already seen (so
the Dex never spoils undiscovered Pokémon, like the Pokédex grid) and `owned`
counts the user's own team/box Pokémon that have the ability.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from .. import abilities, db
from ..auth import get_current_user

router = APIRouter(prefix="/api", tags=["abilitydex"])


@router.get("/abilitydex")
def get_abilitydex(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    user_id = current_user["id"]
    species_by_ability: dict[str, list[dict[str, Any]]] = {}
    for row in db.query(
        """
        SELECT sa.ability_key, sa.species_id, sa.slot
        FROM species_ability sa
        JOIN dex_entry d ON d.species_id = sa.species_id AND d.user_id = ? AND d.seen_count > 0
        ORDER BY sa.species_id
        """,
        (user_id,),
    ):
        species_by_ability.setdefault(row["ability_key"], []).append(
            {"id": row["species_id"], "hidden": row["slot"] == abilities.HIDDEN_SLOT}
        )
    owned = {
        row["ability"]: row["n"]
        for row in db.query(
            "SELECT ability, COUNT(*) AS n FROM owned_pokemon "
            "WHERE user_id = ? AND ability IS NOT NULL GROUP BY ability",
            (user_id,),
        )
    }
    # Every catalog entry, plus keys that only exist in the species mapping or
    # on a Pokémon (an ability PokeAPI added after the catalog was built).
    keys = set(abilities.catalog()) | set(species_by_ability) | set(owned)
    entries = []
    for key in sorted(keys):
        entry = abilities.describe(key)
        entry["active"] = bool(abilities.effect_of(key))
        entry["species"] = species_by_ability.get(key, [])
        entry["owned"] = owned.get(key, 0)
        entries.append(entry)
    return {"abilities": entries}
