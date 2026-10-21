"""One-off gifts to a named player, granted once on start-up.

Each gift is recorded in `seed_meta` under its key, so it is granted exactly
once - also across restarts. A gift whose player or species doesn't exist yet
(fresh database, Pokédex still seeding) stays pending and is retried on the
next start-up instead of being marked done.

The Pokémon goes into the first free team slot (eggs count, see
breeding.free_party_slot). A full team puts it in the box, like the admin's
direct give (api/admin.py `give_pokemon`) does.
"""

from __future__ import annotations

import json
import logging
import random
from datetime import datetime
from typing import Any

from . import battle, breeding, db, rarity, spawn

log = logging.getLogger(__name__)

# Entries: {"key": ..., "user_id": ..., "species_id": ..., "level": ...}
GIFTS: tuple[dict[str, Any], ...] = ()


def _grant(user_id: str, species: dict[str, Any], level: int) -> int:
    ivs = spawn.roll_ivs(species["id"])
    growth = species.get("growth_rate") or "medium_fast"
    now = datetime.now().astimezone().isoformat()
    owned_id = db.execute(
        """
        INSERT INTO owned_pokemon (
            user_id, species_id, level, exp, shiny, ivs, nature, gender, ability,
            current_hp, party_slot, caught_at, caught_from, locked
        ) VALUES (?,?,?,?,0,?,?,?,?,?,?,?,NULL,?)
        """,
        (
            user_id, species["id"], level, battle.exp_for_level(level, growth),
            json.dumps(ivs), random.choice(spawn.NATURES),
            spawn.roll_gender(species["id"]), spawn.roll_ability(species["id"]),
            battle.stat_hp(species["base_hp"], ivs["hp"], level, species_id=species["id"]),
            breeding.free_party_slot(user_id),
            now, int(rarity.auto_lock(species)),
        ),
    )
    db.execute(
        """
        INSERT INTO dex_entry (species_id, user_id, seen_count, caught_count, shiny_caught,
                                first_seen_at, first_caught_at)
        VALUES (?, ?, 1, 1, 0, ?, ?)
        ON CONFLICT(species_id, user_id) DO UPDATE SET
            seen_count = MAX(dex_entry.seen_count, 1),
            caught_count = dex_entry.caught_count + 1,
            first_seen_at = COALESCE(dex_entry.first_seen_at, excluded.first_seen_at),
            first_caught_at = COALESCE(dex_entry.first_caught_at, excluded.first_caught_at)
        """,
        (species["id"], user_id, now, now),
    )
    return owned_id


def grant_pending(gifts: tuple[dict[str, Any], ...] = GIFTS) -> int:
    """Grants every gift that isn't recorded as done yet. Returns how many were granted."""
    granted = 0
    for gift in gifts:
        if db.query_one("SELECT 1 FROM seed_meta WHERE key = ?", (gift["key"],)):
            continue
        species = db.query_one("SELECT * FROM species WHERE id = ?", (gift["species_id"],))
        player = db.query_one("SELECT 1 FROM app_user WHERE id = ?", (gift["user_id"],))
        if species is None or player is None:
            log.info("Gift %s waits for its species or player", gift["key"])
            continue
        _grant(gift["user_id"], species, gift["level"])
        db.execute("INSERT INTO seed_meta (key, value) VALUES (?, ?)", (gift["key"], "granted"))
        log.info("Gift %s granted", gift["key"])
        granted += 1
    return granted
