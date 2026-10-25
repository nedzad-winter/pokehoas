"""Nutzerprofil und Starter-Auswahl.

Ein frisch erkannter Home-Assistant-Nutzer (siehe `auth.py`) hat noch kein
Team. Bevor er am eigentlichen Spiel teilnimmt, waehlt er einmalig einen
Starter aus - wie in den Hauptspielen, aber generationsuebergreifend: zur
Auswahl stehen alle Gras-/Feuer-/Wasserstarter jeder bisherigen Generation
(1-9), nicht nur einer.
"""

from __future__ import annotations

import json
import random
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import achievements, battle, db, god_mode, i18n, profiles, spawn
from ..admin import current_user_is_admin
from .. import economy
from ..auth import get_current_user

router = APIRouter(prefix="/api", tags=["users"])

# National-Dex-Nummern aller Starter, generationsweise - jeweils nur die
# Basisform (nicht die Zwischen-/Endentwicklung), wie beim Start eines
# Hauptspiels. Neue Generation: hier ergaenzen. Reihenfolge je Trio:
# Pflanze, Feuer, Wasser.
STARTER_GENERATIONS: list[tuple[int, list[int]]] = [
    (1, [1, 4, 7]),        # Bulbasaur, Charmander, Squirtle
    (2, [152, 155, 158]),  # Chikorita, Cyndaquil, Totodile
    (3, [252, 255, 258]),  # Treecko, Torchic, Mudkip
    (4, [387, 390, 393]),  # Turtwig, Chimchar, Piplup
    (5, [495, 498, 501]),  # Snivy, Tepig, Oshawott
    (6, [650, 653, 656]),  # Chespin, Fennekin, Froakie
    (7, [722, 725, 728]),  # Rowlet, Litten, Popplio
    (8, [810, 813, 816]),  # Grookey, Scorbunny, Sobble
    (9, [906, 909, 912]),  # Sprigatito, Fuecoco, Quaxly
]
STARTER_DEX_IDS = [dex_id for _, ids in STARTER_GENERATIONS for dex_id in ids]

# Willkommens-Encounter fuer frisch gestartete Spieler: drei bekannte
# Kanto-Fruehroute-Pokemon warten direkt in der eigenen Warteschlange, damit
# es vor dem ersten echten Trigger-Event schon etwas zu fangen gibt.
# National-Dex-Nummern: Taubsi (16), Ratfratz (19), Raupi (10). Annahme (nicht
# spezifiziert): Level 2, wie ein frueher Wildfang auf der ersten Route.
WELCOME_ENCOUNTER_DEX_IDS = [16, 19, 10]
WELCOME_ENCOUNTER_LEVEL = 2


def _spawn_welcome_encounters(user_id: str) -> None:
    """Legt die drei Willkommens-Encounter in der eigenen Warteschlange an.

    Fehlt eine Spezies noch (Pokedex-Hintergrund-Seeding nicht fertig), wird
    sie uebersprungen statt die Starter-Auswahl zu blockieren - analog zum
    generellen Umgang mit unvollstaendig geseedeten Daten (s. list_starters).
    """
    for dex_id in WELCOME_ENCOUNTER_DEX_IDS:
        species = db.query_one("SELECT * FROM species WHERE id = ?", (dex_id,))
        if species is None:
            continue
        encounter_id = spawn.create_encounter(
            species, source_entity="welcome", source_kind="home", user_id=user_id
        )
        db.execute("UPDATE encounter SET level = ? WHERE id = ?",
                   (WELCOME_ENCOUNTER_LEVEL, encounter_id))


class StarterChoice(BaseModel):
    species_id: int
    # Region lock mode chosen together with the starter ("starting fresh");
    # omitted = keep the user's current mode (default 'region').
    region_lock: str | None = None


class RegionLockChoice(BaseModel):
    region_lock: str


class BattleStyleChoice(BaseModel):
    battle_style: str


class GymDifficultyChoice(BaseModel):
    gym_difficulty: str


class AnimationSpeedChoice(BaseModel):
    animation_speed: str


class ShowTeamIvsChoice(BaseModel):
    show_team_ivs: bool


class BoxItemReturnChoice(BaseModel):
    box_item_return: bool


class AutoLockFirstCatchChoice(BaseModel):
    auto_lock_first_catch: bool


class AutoLockShinyChoice(BaseModel):
    auto_lock_shiny: bool


class AutoLockLivingDexChoice(BaseModel):
    auto_lock_living_dex: bool


class SpriteStyleChoice(BaseModel):
    # None = the normal sprites of that species; otherwise a generation whose
    # living Pokedex achievement the player has (achievements.living_generations).
    sprite_style: int | None = None


class AutoHealChoice(BaseModel):
    # None = keep the current value.
    auto_heal: bool | None = None
    auto_buy_potions: bool | None = None


class LanguageChoice(BaseModel):
    language: str


@router.get("/users/me")
def me(
    current_user: dict = Depends(get_current_user),
    is_admin: bool = Depends(current_user_is_admin),
) -> dict[str, Any]:
    god_mode.clear_unless_admin(current_user, is_admin)
    return {
        # Steuert nur die Sichtbarkeit des Admin-Tabs; geprueft wird serverseitig.
        "is_admin": is_admin,
        # Shown as a badge in the header (see app/god_mode.py).
        "god_mode": bool(current_user.get("god_mode")),
        "id": current_user["id"],
        "name": current_user["name"],
        "display_name": current_user["display_name"],
        "starter_species_id": current_user["starter_species_id"],
        "has_starter": current_user["starter_species_id"] is not None,
        "created_at": current_user["created_at"],
        "language": current_user["language"],
        "region_lock": db.user_region_lock(current_user["id"]),
        "region_lock_modes": list(db.REGION_LOCK_MODES),
        "battle_style": db.user_battle_style(current_user["id"]),
        "battle_styles": list(db.BATTLE_STYLES),
        "gym_difficulty": db.user_gym_difficulty(current_user["id"]),
        "gym_difficulties": list(db.GYM_DIFFICULTY_MODES),
        "animation_speed": db.user_animation_speed(current_user["id"]),
        "animation_speeds": list(db.ANIMATION_SPEEDS),
        "show_team_ivs": db.user_show_team_ivs(current_user["id"]),
        "box_item_return": db.user_box_item_return(current_user["id"]),
        "auto_lock_first_catch": db.user_auto_lock_first_catch(current_user["id"]),
        "auto_lock_shiny": db.user_auto_lock_shiny(current_user["id"]),
        "auto_lock_living_dex": db.user_auto_lock_living_dex(current_user["id"]),
        **_sprite_style(current_user["id"]),
        **db.user_auto_heal(current_user["id"]),
        "available_languages": i18n.available_languages(),
    }


@router.get("/users")
def list_household(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    """Haushaltsmitglieder fuer die Profil-Auswahl.

    Basis ist `db.eligible_users` (aktive Mitspieler, keine Testkonten - die
    gleiche Menge, die auch bei den alten geteilten Encountern mitzaehlt). Der
    eigene Nutzer steht immer drin, auch wenn er die Kriterien gerade nicht
    erfuellt (frisch angelegtes Konto).
    """
    users = [
        {"id": u["id"], "name": u["name"], "display_name": u["display_name"] or u["name"]}
        for u in db.eligible_users()
    ]
    if not any(u["id"] == current_user["id"] for u in users):
        users.insert(0, {
            "id": current_user["id"], "name": current_user["name"],
            "display_name": current_user["display_name"] or current_user["name"],
        })
    return {"users": users, "me": current_user["id"]}


@router.get("/users/{user_id}/profile")
def profile(
    user_id: str, current_user: dict = Depends(get_current_user)
) -> dict[str, Any]:
    """Profil eines Haushaltsmitglieds - immer nur lesend.

    `user_id = "me"` meint den angemeldeten Nutzer. Fremde Profile sind
    bewusst fuer alle sichtbar (ein Haushalt); es gibt hier keinerlei
    Schreibpfad.
    """
    lang = current_user["language"]
    if user_id == "me":
        user_id = current_user["id"]
    user = db.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail=i18n.t(lang, "error.user_not_found"))
    return profiles.build(user, lang, viewer_id=current_user["id"])


@router.put("/users/me/language")
def set_language(
    payload: LanguageChoice, current_user: dict = Depends(get_current_user)
) -> dict[str, Any]:
    if payload.language not in i18n.available_languages():
        raise HTTPException(status_code=400, detail=f"Unknown language: {payload.language}")
    user = db.set_user_language(current_user["id"], payload.language)
    return {"language": user["language"]}


@router.put("/users/me/region-lock")
def set_region_lock(
    payload: RegionLockChoice, current_user: dict = Depends(get_current_user)
) -> dict[str, Any]:
    if payload.region_lock not in db.REGION_LOCK_MODES:
        raise HTTPException(
            status_code=400,
            detail=i18n.t(current_user["language"], "error.invalid_region_lock"),
        )
    user = db.set_user_region_lock(current_user["id"], payload.region_lock)
    return {"region_lock": user["region_lock"]}


@router.put("/users/me/battle-style")
def set_battle_style(
    payload: BattleStyleChoice, current_user: dict = Depends(get_current_user)
) -> dict[str, Any]:
    if payload.battle_style not in db.BATTLE_STYLES:
        raise HTTPException(
            status_code=400,
            detail=i18n.t(current_user["language"], "error.invalid_battle_style"),
        )
    user = db.set_user_battle_style(current_user["id"], payload.battle_style)
    return {"battle_style": user["battle_style"]}


@router.put("/users/me/gym-difficulty")
def set_gym_difficulty(
    payload: GymDifficultyChoice, current_user: dict = Depends(get_current_user)
) -> dict[str, Any]:
    if payload.gym_difficulty not in db.GYM_DIFFICULTY_MODES:
        raise HTTPException(
            status_code=400,
            detail=i18n.t(current_user["language"], "error.invalid_gym_difficulty"),
        )
    user = db.set_user_gym_difficulty(current_user["id"], payload.gym_difficulty)
    return {"gym_difficulty": user["gym_difficulty"]}


@router.put("/users/me/animation-speed")
def set_animation_speed(
    payload: AnimationSpeedChoice, current_user: dict = Depends(get_current_user)
) -> dict[str, Any]:
    if payload.animation_speed not in db.ANIMATION_SPEEDS:
        raise HTTPException(
            status_code=400,
            detail=i18n.t(current_user["language"], "error.invalid_animation_speed"),
        )
    user = db.set_user_animation_speed(current_user["id"], payload.animation_speed)
    return {"animation_speed": user["animation_speed"]}


@router.put("/users/me/show-team-ivs")
def set_show_team_ivs(
    payload: ShowTeamIvsChoice, current_user: dict = Depends(get_current_user)
) -> dict[str, Any]:
    user = db.set_user_show_team_ivs(current_user["id"], payload.show_team_ivs)
    return {"show_team_ivs": bool(user["show_team_ivs"])}


def _sprite_style(user_id: str) -> dict[str, Any]:
    """The generations the player may pick and their per-species choices. A
    stored style whose achievement is gone (save data wiped) is ignored."""
    # God mode (app/god_mode.py): every generation's style, no achievement needed.
    styles = (achievements.generations() if god_mode.active(user_id)
              else sorted(achievements.living_generations(user_id)))
    chosen = {sid: g for sid, g in db.user_species_sprite_styles(user_id).items() if g in styles}
    return {"sprite_styles": styles, "species_sprite_styles": chosen}


@router.put("/users/me/sprite-style/{species_id}")
def set_sprite_style(
    species_id: int, payload: SpriteStyleChoice, current_user: dict = Depends(get_current_user)
) -> dict[str, Any]:
    if (payload.sprite_style is not None
            and payload.sprite_style not in _sprite_style(current_user["id"])["sprite_styles"]):
        raise HTTPException(
            status_code=400,
            detail=i18n.t(current_user["language"], "error.sprite_style_locked"),
        )
    db.set_species_sprite_style(current_user["id"], species_id, payload.sprite_style)
    return _sprite_style(current_user["id"])


@router.put("/users/me/box-item-return")
def set_box_item_return(
    payload: BoxItemReturnChoice, current_user: dict = Depends(get_current_user)
) -> dict[str, Any]:
    user = db.set_user_box_item_return(current_user["id"], payload.box_item_return)
    return {"box_item_return": bool(user["box_item_return"])}


@router.put("/users/me/auto-lock-first-catch")
def set_auto_lock_first_catch(
    payload: AutoLockFirstCatchChoice, current_user: dict = Depends(get_current_user)
) -> dict[str, Any]:
    user = db.set_user_auto_lock_first_catch(current_user["id"], payload.auto_lock_first_catch)
    return {"auto_lock_first_catch": bool(user["auto_lock_first_catch"])}


@router.put("/users/me/auto-lock-shiny")
def set_auto_lock_shiny(
    payload: AutoLockShinyChoice, current_user: dict = Depends(get_current_user)
) -> dict[str, Any]:
    user = db.set_user_auto_lock_shiny(current_user["id"], payload.auto_lock_shiny)
    return {"auto_lock_shiny": bool(user["auto_lock_shiny"])}


@router.put("/users/me/auto-lock-living-dex")
def set_auto_lock_living_dex(
    payload: AutoLockLivingDexChoice, current_user: dict = Depends(get_current_user)
) -> dict[str, Any]:
    user = db.set_user_auto_lock_living_dex(current_user["id"], payload.auto_lock_living_dex)
    return {"auto_lock_living_dex": bool(user["auto_lock_living_dex"])}


@router.put("/users/me/auto-heal")
def set_auto_heal(
    payload: AutoHealChoice, current_user: dict = Depends(get_current_user)
) -> dict[str, Any]:
    """Auto heal on/off; auto buy can only be on together with auto heal
    (turning auto heal off switches auto buy off as well)."""
    try:
        return db.set_user_auto_heal(current_user["id"], payload.auto_heal, payload.auto_buy_potions)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=i18n.t(current_user["language"], "autoheal.error.buy_needs_heal"),
        ) from exc


@router.get("/starters")
def list_starters() -> dict[str, Any]:
    species_by_id = {
        row["id"]: row
        for row in db.query(
            f"SELECT * FROM species WHERE id IN ({','.join('?' * len(STARTER_DEX_IDS))})",
            tuple(STARTER_DEX_IDS),
        )
    }
    generations = []
    for generation, dex_ids in STARTER_GENERATIONS:
        options = [species_by_id[i] for i in dex_ids if i in species_by_id]
        generations.append({"generation": generation, "starters": options})

    return {
        "generations": generations,
        # Solange der Pokédex noch im Hintergrund geseedet wird, fehlen
        # anfangs Eintraege - das Frontend kann daran erkennen, dass es
        # gleich nochmal nachfragen sollte.
        "seeding": len(species_by_id) < len(STARTER_DEX_IDS),
    }


@router.post("/users/me/starter")
def choose_starter(
    payload: StarterChoice, current_user: dict = Depends(get_current_user)
) -> dict[str, Any]:
    lang = current_user["language"]
    if current_user["starter_species_id"] is not None:
        raise HTTPException(status_code=409, detail=i18n.t(lang, "error.starter_already_chosen"))

    if payload.species_id not in STARTER_DEX_IDS:
        raise HTTPException(status_code=400, detail=i18n.t(lang, "error.invalid_starter"))

    if payload.region_lock is not None and payload.region_lock not in db.REGION_LOCK_MODES:
        raise HTTPException(status_code=400, detail=i18n.t(lang, "error.invalid_region_lock"))

    species = db.query_one("SELECT * FROM species WHERE id = ?", (payload.species_id,))
    if species is None:
        raise HTTPException(status_code=503, detail=i18n.t(lang, "error.pokedex_loading"))

    user_id = current_user["id"]
    if payload.region_lock is not None:
        db.set_user_region_lock(user_id, payload.region_lock)
    now = datetime.now().astimezone().isoformat()
    level = int(db.get_setting("starter_level", 5))
    ivs = spawn.roll_ivs(species["id"])
    current_hp = battle.stat_hp(species["base_hp"], ivs["hp"], level, species_id=species["id"])
    ability = spawn.roll_ability(species["id"])

    owned_id = db.execute(
        """
        INSERT INTO owned_pokemon (
            user_id, species_id, level, exp, shiny, ivs, nature, gender, ability,
            current_hp, party_slot, caught_at, caught_from
        ) VALUES (?,?,?,?,0,?,?,?,?,?,1,?,NULL)
        """,
        (
            user_id, species["id"], level,
            battle.exp_for_level(level, species.get("growth_rate", "medium_fast")),
            json.dumps(ivs), random.choice(spawn.NATURES),
            spawn.roll_gender(species["id"]), ability, current_hp, now,
        ),
    )

    db.execute(
        """
        INSERT INTO dex_entry (species_id, user_id, seen_count, caught_count, shiny_caught,
                                first_seen_at, first_caught_at)
        VALUES (?, ?, 1, 1, 0, ?, ?)
        ON CONFLICT(species_id, user_id) DO UPDATE SET
            caught_count = caught_count + 1,
            first_caught_at = COALESCE(dex_entry.first_caught_at, excluded.first_caught_at)
        """,
        (species["id"], user_id, now, now),
    )

    starter_inventory = db.get_setting("starter_inventory")
    if not isinstance(starter_inventory, dict):
        starter_inventory = db.DEFAULT_SETTINGS["starter_inventory"]
    for item_key, quantity in starter_inventory.items():
        db.execute(
            """
            INSERT INTO inventory (item_key, user_id, quantity) VALUES (?,?,?)
            ON CONFLICT(item_key, user_id) DO UPDATE SET quantity = quantity + excluded.quantity
            """,
            (item_key, user_id, quantity),
        )
    economy.grant_starting_money(user_id)

    db.execute(
        "UPDATE app_user SET starter_species_id = ? WHERE id = ?",
        (species["id"], user_id),
    )
    _spawn_welcome_encounters(user_id)

    owned = db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (owned_id,))
    owned["species"] = species
    owned["ivs"] = ivs
    return {"ok": True, "owned": owned}
