"""User profile: the summary behind the name in the header.

Everything here is read-only and aggregates only what already exists
(team, achievements, Pokédex, balance). Therefore viewing another user's
profile is safe - there is no write path.
"""

from __future__ import annotations

from typing import Any

from . import achievements, db, economy, pvp, spawn, trainers


def _shiny_sprite_url(sprite_url: str | None) -> str | None:
    """Derive shiny sprite from normal one - same rule as in
    api/team.py; the species table only holds the normal URL."""
    if not sprite_url:
        return None
    return sprite_url.replace("/sprites/pokemon/", "/sprites/pokemon/shiny/")


def _team(user_id: str) -> list[dict[str, Any]]:
    rows = db.query(
        """
        SELECT o.id, o.level, o.shiny, o.party_slot, o.current_hp,
               s.id AS species_id, s.name, s.name_en, s.name_de, s.sprite_url
        FROM owned_pokemon o JOIN species s ON s.id = o.species_id
        WHERE o.user_id = ? AND o.party_slot IS NOT NULL
        ORDER BY o.party_slot
        """,
        (user_id,),
    )
    for row in rows:
        row["shiny"] = bool(row["shiny"])
        row["shiny_sprite_url"] = _shiny_sprite_url(row["sprite_url"])
    return rows


def _dex(user_id: str) -> dict[str, int]:
    row = db.query_one(
        """
        SELECT (SELECT COUNT(*) FROM species) AS total,
               COUNT(*) AS seen,
               SUM(CASE WHEN caught_count > 0 THEN 1 ELSE 0 END) AS caught,
               SUM(caught_count) AS catches,
               SUM(CASE WHEN shiny_caught > 0 THEN 1 ELSE 0 END) AS shiny
        FROM dex_entry WHERE user_id = ? AND seen_count > 0
        """,
        (user_id,),
    ) or {}
    return {key: int(row.get(key) or 0)
            for key in ("total", "seen", "caught", "catches", "shiny")}


def _generations(user_id: str) -> list[dict[str, int]]:
    """Per-generation overview for the profile table: species total, how many
    of them this user has seen and caught (distinct species, like
    `pokedex.seen` / `pokedex.caught`), and how many are currently owned (team
    or box - the "living Pokedex", see `achievements.dex_progress`)."""
    rows = db.query(
        """
        SELECT s.generation AS generation,
               COUNT(*) AS total,
               SUM(CASE WHEN de.seen_count > 0 THEN 1 ELSE 0 END) AS seen,
               SUM(CASE WHEN de.caught_count > 0 THEN 1 ELSE 0 END) AS caught,
               SUM(CASE WHEN op.species_id IS NOT NULL THEN 1 ELSE 0 END) AS living
        FROM species s
        LEFT JOIN dex_entry de ON de.species_id = s.id AND de.user_id = ?
        LEFT JOIN (SELECT DISTINCT species_id FROM owned_pokemon WHERE user_id = ?) op
               ON op.species_id = s.id
        GROUP BY s.generation
        ORDER BY s.generation
        """,
        (user_id, user_id),
    )
    return [
        {"generation": row["generation"], "total": row["total"],
         "seen": int(row["seen"] or 0), "caught": int(row["caught"] or 0),
         "living": int(row["living"] or 0)}
        for row in rows
    ]


def _league(user_id: str, lang: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(Badges per region, champion titles). Badges = defeated gym leaders."""
    done = achievements.unlocked(user_id)
    regions: list[dict[str, Any]] = []
    champion_titles: list[dict[str, Any]] = []
    for region in trainers.regions():
        gyms = [t for t in trainers.all_trainers()
                if t["region"] == region["key"] and t["kind"] == "gym_leader"]
        badges = [
            {
                "trainer_id": t["id"],
                "name": t["name"],
                "title": trainers.title_for(t, lang),
                "earned": achievements.trainer_key(t["id"]) in done,
                "earned_at": done.get(achievements.trainer_key(t["id"])),
            }
            for t in gyms
        ]
        regions.append({
            "key": region["key"],
            "label": region["label"],
            "badges": badges,
            "earned": sum(1 for b in badges if b["earned"]),
            "total": len(badges),
            "cleared": achievements.region_cleared(user_id, region["key"]),
        })
        # "Champion" means: completely cleared the league of this region.
        if achievements.region_cleared(user_id, region["key"]) and gyms:
            champion_titles.append({"region": region["key"], "label": region["label"]})
    return regions, champion_titles


def build(user: dict[str, Any], lang: str = "en", viewer_id: str | None = None) -> dict[str, Any]:
    """`viewer_id`: who is viewing? Only for the viewer's PvP record
    against this user - this doesn't change the profile itself."""
    user_id = user["id"]
    team = _team(user_id)
    regions, champion_titles = _league(user_id, lang)
    ach = achievements.overview(user_id)
    dex = _dex(user_id)
    owned = db.query_one(
        "SELECT COUNT(*) AS n FROM owned_pokemon WHERE user_id = ?", (user_id,)
    ) or {"n": 0}
    return {
        "user": {
            "id": user_id,
            "name": user["name"],
            "display_name": user["display_name"] or user["name"],
            "created_at": user["created_at"],
        },
        "money": economy.balance(user_id),
        "team": team,
        "owned_count": int(owned["n"] or 0),
        # `catches` counts every catch (including duplicate species),
        # `caught` counts only species - both are useful.
        "caught_count": dex["catches"],
        "pokedex": {"seen": dex["seen"], "caught": dex["caught"],
                    "total": dex["total"], "shiny": dex["shiny"]},
        "generations": _generations(user_id),
        # Battle record of the viewer against this user (see pvp.py) -
        # nothing to show on own profile.
        "pvp": {
            "can_challenge": viewer_id is not None and viewer_id != user_id,
            "team_size": len(team),
            "record": pvp.record_of(viewer_id, user_id) if viewer_id and viewer_id != user_id
                      else {"wins": 0, "losses": 0, "last_at": None},
        },
        # Share of each rarity tier among THIS user's spawns (see spawn.tier_shares).
        "spawn_rates": spawn.tier_shares(user_id),
        "regions": regions,
        "champion_titles": champion_titles,
        "league_master": achievements.LEAGUE_MASTER in achievements.unlocked(user_id),
        "achievements": {
            "unlocked": ach["unlocked_count"],
            "total": ach["total"],
            "items": [a for a in ach["achievements"] if a["unlocked"]],
        },
    }
