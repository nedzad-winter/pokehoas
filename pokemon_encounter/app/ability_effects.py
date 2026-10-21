"""Ability handlers for the event hub (`battle_events.py`).

One function per `(event, effect)` of `data/abilities.json`. New abilities
need a catalog entry plus a handler here - `battle.py` only fires events.
Imported at the end of `battle.py` so the handlers are registered whenever
the engine is used.
"""

from __future__ import annotations

import random

from . import abilities
from . import battle as engine
from .battle_events import Ctx, Event, emit, on


def _ability_name(ctx: Ctx) -> str | None:
    return abilities.label(ctx.owner.ability, ctx.battle.lang if ctx.battle else "en")


def _who(ctx: Ctx, combatant) -> str:
    return engine.who(combatant, ctx.battle.lang if ctx.battle else "en")


# --------------------------------------------------------------- Modifiers

@on(Event.TYPE_EFFECT, "type_immune")
def _type_immune(ctx: Ctx) -> None:
    """Levitate: immune to one attack type, independent of own type."""
    if ctx.move_type == ctx.params.get("type"):
        ctx.value = 0.0


@on(Event.TYPE_EFFECT, "wonder_guard")
def _wonder_guard(ctx: Ctx) -> None:
    """Only a super-effective damaging move deals damage; everything else
    (including neutral/not-very-effective hits) fails. `ctx.move` is only
    passed in at damage-calculation call sites, never for a status move's
    own type check (e.g. an Electric status move against Ground), which
    Wonder Guard must not touch."""
    if ctx.move is not None and ctx.value < 2:
        ctx.value = 0.0


@on(Event.STAB, "adaptability")
def _adaptability(ctx: Ctx) -> None:
    """STAB 2x instead of 1.5x - only if the move gets STAB at all."""
    if ctx.value > 1.0:
        ctx.value = 2.0


@on(Event.EFFECTIVE_STAT, "guts")
def _guts(ctx: Ctx) -> None:
    """Attack x1.5 with ANY status, and burn no longer halves it."""
    if ctx.stat != "atk" or ctx.owner.status is None:
        return
    ctx.flags.add("ignore_burn")
    ctx.value *= float(ctx.params.get("multiplier", 1.5))


@on(Event.DAMAGE_MULT, "low_hp_stab_boost")
def _low_hp_type_boost(ctx: Ctx) -> None:
    """Blaze/Overgrow/Torrent/Swarm: matching type + below 1/3 HP."""
    if ctx.move is None or ctx.move["type"] != ctx.params.get("type"):
        return
    threshold = float(ctx.params.get("threshold", 1 / 3))
    if ctx.owner.current_hp > ctx.owner.max_hp * threshold:
        return
    ctx.value *= float(ctx.params.get("multiplier", 1.5))


# ------------------------------------------------------------------- Vetoes

@on(Event.BEFORE_STATUS, "status_immune")
def _status_immune(ctx: Ctx) -> None:
    """Immunity, Limber, Insomnia, Water Veil, Magma Armor."""
    if ctx.status not in (ctx.params.get("statuses") or []):
        return
    ctx.cancelled = True
    ctx.log("battle.log.status_immune_ability", who=_who(ctx, ctx.owner), ability=_ability_name(ctx))


# ------------------------------------------------------------ Notifications

@on(Event.DAMAGE_TAKEN, "contact_status")
def _contact_status(ctx: Ctx) -> None:
    """Static/Flame Body/Poison Point/Effect Spore: chance to inflict status
    on the attacker if it hits the holder with a physical move.

    The engine has no "makes contact" flag on moves - simplified here to
    "every physical hit that deals damage"."""
    attacker, defender, move = ctx.other, ctx.owner, ctx.move
    if ctx.dry_run or attacker is None or move is None:
        return
    if ctx.dealt <= 0 or move.get("category") != "physical":
        return
    if attacker.fainted or defender.fainted or attacker.status is not None:
        return
    statuses = ctx.params.get("statuses") or []
    if not statuses or random.random() * 100 >= float(ctx.params.get("chance", 30)):
        return
    status = random.choice(statuses)
    ctx.log("battle.log.contact_ability", ability=_ability_name(ctx), who=_who(ctx, defender))
    engine.inflict_status(ctx.battle, attacker, status)


@on(Event.END_OF_TURN, "cure_status")
def _shed_skin(ctx: Ctx) -> None:
    """Shed Skin: chance per turn to heal own status by itself."""
    c = ctx.owner
    if ctx.dry_run or c.fainted or c.status is None:
        return
    if random.random() * 100 >= float(ctx.params.get("chance", 30)):
        return
    cured = engine.status_verb(c.status, ctx.battle.lang)
    c.status, c.status_counter = None, 0
    ctx.log("battle.log.ability_cures_status",
            who=_who(ctx, c), ability=_ability_name(ctx), status=cured)


# ----------------------------------------------------------------- Switch-in

@on(Event.SWITCH_IN, "intimidate")
def _intimidate(ctx: Ctx) -> None:
    """Lowers the opposing Pokemon's Attack by one stage on entering the field."""
    foe = ctx.other
    if ctx.dry_run or foe is None or foe.fainted or ctx.owner.fainted:
        return
    ctx.log("battle.log.ability_activates", who=_who(ctx, ctx.owner), ability=_ability_name(ctx))
    engine.apply_stat_change(ctx.battle, foe, "atk", -1, source=ctx.owner, flags=("intimidate",))
    emit(Event.INTIMIDATED, foe, ctx.owner, battle=ctx.battle)


@on(Event.SWITCH_IN, "download")
def _download(ctx: Ctx) -> None:
    """+1 Attack if the foe's Defense is lower than its Special Defense,
    otherwise +1 Special Attack."""
    foe = ctx.other
    if ctx.dry_run or foe is None or foe.fainted or ctx.owner.fainted:
        return
    stat = "atk" if engine.effective_stat(foe, "def") < engine.effective_stat(foe, "spd") else "spa"
    ctx.log("battle.log.ability_activates", who=_who(ctx, ctx.owner), ability=_ability_name(ctx))
    engine.apply_stat_change(ctx.battle, ctx.owner, stat, 1, source=ctx.owner)


# -------------------------------------------------------------- Stat drops

def _block_stat_drop(ctx: Ctx) -> None:
    ctx.cancelled = True
    ctx.log("battle.log.ability_blocks_stat_drop", who=_who(ctx, ctx.owner),
            ability=_ability_name(ctx), stat=engine.stat_name(ctx.stat, ctx.battle.lang))


@on(Event.BEFORE_STAT_CHANGE, "stat_drop_immune")
def _stat_drop_immune(ctx: Ctx) -> None:
    """Clear Body/White Smoke/Full Metal Body (all stats), Hyper Cutter
    (Attack), Big Pecks (Defense): foes cannot lower these stats."""
    stats = ctx.params.get("stats")
    if ctx.value < 0 and (not stats or ctx.stat in stats):
        _block_stat_drop(ctx)


@on(Event.BEFORE_STAT_CHANGE, "intimidate_immune")
@on(Event.BEFORE_STAT_CHANGE, "inner_focus")
def _intimidate_immune(ctx: Ctx) -> None:
    """Own Tempo/Oblivious/Scrappy/Inner Focus: Intimidate has no effect."""
    if "intimidate" in ctx.flags:
        _block_stat_drop(ctx)


@on(Event.BEFORE_FLINCH, "inner_focus")
def _inner_focus(ctx: Ctx) -> None:
    ctx.cancelled = True


@on(Event.BEFORE_STAT_CHANGE, "guard_dog")
def _guard_dog(ctx: Ctx) -> None:
    """Intimidate raises its Attack instead of lowering it."""
    if "intimidate" not in ctx.flags:
        return
    ctx.cancelled = True
    ctx.log("battle.log.ability_activates", who=_who(ctx, ctx.owner), ability=_ability_name(ctx))
    engine.apply_stat_change(ctx.battle, ctx.owner, "atk", 1, source=ctx.owner)


@on(Event.STAT_LOWERED, "stat_drop_reaction")
def _stat_drop_reaction(ctx: Ctx) -> None:
    """Defiant/Competitive: a stat lowered by a foe sharply raises another one."""
    if ctx.owner.fainted:
        return
    ctx.log("battle.log.ability_activates", who=_who(ctx, ctx.owner), ability=_ability_name(ctx))
    engine.apply_stat_change(ctx.battle, ctx.owner, ctx.params["stat"],
                             int(ctx.params.get("stages", 2)), source=ctx.owner)


def _rattled_boost(ctx: Ctx) -> None:
    ctx.log("battle.log.ability_activates", who=_who(ctx, ctx.owner), ability=_ability_name(ctx))
    engine.apply_stat_change(ctx.battle, ctx.owner, "spe", 1, source=ctx.owner)


@on(Event.INTIMIDATED, "rattled")
def _rattled_intimidated(ctx: Ctx) -> None:
    if not ctx.dry_run and not ctx.owner.fainted:
        _rattled_boost(ctx)


@on(Event.DAMAGE_TAKEN, "rattled")
def _rattled_hit(ctx: Ctx) -> None:
    """+1 Speed when hit by a Bug, Ghost or Dark move."""
    if ctx.dry_run or ctx.move is None or ctx.dealt <= 0 or ctx.owner.fainted:
        return
    if ctx.move["type"] in (ctx.params.get("types") or []):
        _rattled_boost(ctx)


# ------------------------------------------------------- Absorbing a type

@on(Event.TYPE_EFFECT, "absorb")
@on(Event.TYPE_EFFECT, "flash_fire")
def _absorb_immune(ctx: Ctx) -> None:
    """Water Absorb & co.: moves of the absorbed type have no effect - what
    they do instead happens on MOVE_IMMUNITY."""
    if ctx.move_type == ctx.params.get("type"):
        ctx.value = 0.0


@on(Event.MOVE_IMMUNITY, "absorb")
def _absorb(ctx: Ctx) -> None:
    """Heals (Water/Volt Absorb, Earth Eater, Dry Skin) or raises a stat
    (Sap Sipper, Lightning Rod, Storm Drain, Motor Drive, Well-Baked Body)
    instead of taking the hit."""
    if ctx.dry_run or ctx.move_type != ctx.params.get("type"):
        return
    c = ctx.owner
    ctx.flags.add("absorbed")
    ctx.log("battle.log.ability_absorbs", who=_who(ctx, c), ability=_ability_name(ctx))
    heal = ctx.params.get("heal")
    if heal:
        healed = engine._heal(c, max(1, int(c.max_hp * float(heal))))
        if healed:
            ctx.log("battle.log.heals_self", who=_who(ctx, c), amount=healed)
    if ctx.params.get("stat"):
        engine.apply_stat_change(ctx.battle, c, ctx.params["stat"],
                                 int(ctx.params.get("stages", 1)), source=c)


@on(Event.DEFENSE_MULT, "absorb")
def _absorb_weakness(ctx: Ctx) -> None:
    """Dry Skin: takes more damage from one other type (Fire 1.25x)."""
    weak = ctx.params.get("weak_to")
    if weak and ctx.move is not None and ctx.move["type"] == weak["type"]:
        ctx.value *= float(weak["multiplier"])


@on(Event.MOVE_IMMUNITY, "flash_fire")
def _flash_fire(ctx: Ctx) -> None:
    """Absorbs Fire moves; from then on its own Fire moves deal 1.5x damage
    until it leaves the field (Combatant.volatile)."""
    if ctx.dry_run or ctx.move_type != ctx.params.get("type"):
        return
    ctx.flags.add("absorbed")
    ctx.log("battle.log.ability_absorbs", who=_who(ctx, ctx.owner), ability=_ability_name(ctx))
    if not ctx.owner.volatile.get("flash_fire"):
        ctx.owner.volatile["flash_fire"] = True
        ctx.log("battle.log.flash_fire", who=_who(ctx, ctx.owner))


@on(Event.DAMAGE_MULT, "flash_fire")
def _flash_fire_boost(ctx: Ctx) -> None:
    if (ctx.owner.volatile.get("flash_fire") and ctx.move is not None
            and ctx.move["type"] == ctx.params.get("type")):
        ctx.value *= float(ctx.params.get("multiplier", 1.5))


# ------------------------------------------------------------------ Damage

@on(Event.BEFORE_DAMAGE, "sturdy")
def _sturdy(ctx: Ctx) -> None:
    """At full HP, a hit that would knock it out leaves it with 1 HP."""
    c = ctx.owner
    if c.current_hp == c.max_hp and c.current_hp > 1 and ctx.value >= c.current_hp:
        ctx.value = c.current_hp - 1
        ctx.flags.add("endured")
