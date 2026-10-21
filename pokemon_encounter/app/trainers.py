"""Katalog der Arenaleiter, Top Vier und Champs (`data/trainers.json`)."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from . import db, i18n

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"

# Sprachunabhaengiger Schluessel je Trainer - siehe `title_for` unten, das
# daraus erst pro Anfrage (mit der Sprache des Betrachters) einen Anzeige-
# text macht. Titel werden also NICHT mehr fest in `all_trainers()` gebacken.
# A trainer's optional `title` field picks one of these instead of its kind's:
# Galar has no Elite Four, its Champion Cup rounds (kind elite_four, see
# `stage` in trainers.json) are fought by rivals and gym leaders.
KIND_TITLE_KEYS = {
    "gym_leader": ("trainer.title.gym_leader_m", "trainer.title.gym_leader_f"),
    "elite_four": ("trainer.title.elite_four", "trainer.title.elite_four"),
    "champion": ("trainer.title.champion", "trainer.title.champion"),
    "training": ("trainer.title.training", "trainer.title.training"),
    "rival": ("trainer.title.rival_m", "trainer.title.rival_f"),
}
KIND_IVS = {"gym_leader": 20, "elite_four": 26, "champion": 31}

# Portraits come from Pokémon Showdown's trainer-avatar sprites (same CDN
# pattern as the PokeAPI-sprites URLs used for species/items elsewhere), keyed
# by a slug derived from the trainer's English name. Trainers whose slug isn't
# just the lowercased name (missing avatar, alternate-game variant, or - for
# the one duo gym leader - no combined sprite at all) are listed here instead.
_SPRITE_OVERRIDES: dict[str, str] = {
    "kanto_lorelei": "lorelei-gen1",
    "kanto_agatha": "agatha-gen1",
    "hoenn_phoebe": "phoebe-gen3",
    "hoenn_drake": "drake-gen3",
    # Showdown has no combined sprite for the Tate & Liza duo; Tate stands in.
    "hoenn_tate_liza": "tate",
    "alola_kukui": "kukui",
}
SPRITE_BASE_URL = "https://play.pokemonshowdown.com/sprites/trainers"


def sprite_url_for(trainer: dict[str, Any]) -> str:
    """Portrait URL for a trainer, see `_SPRITE_OVERRIDES` above."""
    slug = _SPRITE_OVERRIDES.get(trainer["id"])
    if slug is None:
        slug = re.sub(r"[^a-z0-9]", "", trainer["name"].replace("&", "and").lower())
    return f"{SPRITE_BASE_URL}/{slug}.png"


def title_for(trainer: dict[str, Any], lang: str = "en") -> str:
    key = KIND_TITLE_KEYS[trainer.get("title") or trainer["kind"]][1 if trainer.get("f") else 0]
    return i18n.t(lang, key)

_raw: dict[str, Any] | None = None
_trainers: list[dict[str, Any]] | None = None


def _load() -> dict[str, Any]:
    global _raw
    if _raw is None:
        _raw = json.loads((DATA_DIR / "trainers.json").read_text(encoding="utf-8"))
    return _raw


def all_trainers() -> list[dict[str, Any]]:
    global _trainers
    if _trainers is None:
        raw = _load()
        regions = {r["key"]: r["label"] for r in raw["regions"]}
        _trainers = [
            {
                **t,
                "region_label": regions.get(t["region"], t["region"]),
                "max_level": max(level for _, level in t["team"]),
                "iv": int(t.get("iv", KIND_IVS[t["kind"]])),
                "sprite_url": sprite_url_for(t),
            }
            for t in raw["trainers"]
        ]
    return _trainers


def regions() -> list[dict[str, str]]:
    return _load()["regions"]


def generation_for_region(region_key: str) -> int | None:
    """Regionen stehen 1:1 fuer die Generationsgrenzen aus seed.py
    (kanto=1, johto=2, ... paldea=9) - siehe achievements.unlocked_generations,
    das genau diese Zuordnung fuer die Spawn-Pool-Freischaltung braucht (je
    nach Regions-Sperr-Modus des Nutzers, siehe achievements.unlock_generation)."""
    for idx, region in enumerate(regions(), start=1):
        if region["key"] == region_key:
            return idx
    return None


def get(trainer_id: str) -> dict[str, Any] | None:
    return next((t for t in all_trainers() if t["id"] == trainer_id), None)


def prerequisite(trainer_id: str) -> dict[str, Any] | None:
    """The trainer that must be beaten before `trainer_id` can be challenged:
    the one right before it in its region's catalog order (gym 1, 2, ... then
    Elite Four / Champion Cup, then the champion). None for the first trainer
    of a region and for anything outside the catalog (e.g. Training)."""
    trainer = get(trainer_id)
    if trainer is None:
        return None
    previous = None
    for t in all_trainers():
        if t["region"] != trainer["region"]:
            continue
        if t["id"] == trainer_id:
            return previous
        previous = t
    return None


def species_index() -> dict[str, dict[str, Any]]:
    return {row["name"]: row for row in db.query("SELECT * FROM species ORDER BY id")}


def _resolve(name: str, index: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """PokeAPI-Namen tragen teils einen Formzusatz (aegislash-shield,
    mimikyu-disguised) - dann die erste Form dieser Art nehmen."""
    if name in index:
        return index[name]
    prefix = name + "-"
    return next((row for key, row in index.items() if key.startswith(prefix)), None)


def resolve_team(
    trainer: dict[str, Any], index: dict[str, dict[str, Any]] | None = None
) -> list[tuple[dict[str, Any], int]] | None:
    """[(species-Zeile, Level), ...] oder None, solange eine Art (noch) nicht
    im Pokédex ist (z. B. waehrend des ersten Seedings)."""
    index = index if index is not None else species_index()
    team = []
    for name, level in trainer["team"]:
        species = _resolve(name, index)
        if species is None:
            log.debug("Trainer %s: Art %s nicht im Pokédex", trainer["id"], name)
            return None
        team.append((species, int(level)))
    return team
