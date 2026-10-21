"""Event hub of the battle engine: named moments at which `battle.py` asks the
abilities of the involved combatants what should happen.

Three kinds of events:
* notify - side effects only (`SWITCH_IN`, `DAMAGE_TAKEN`, `END_OF_TURN`, ...)
* modify - a value is passed through the handlers (`ctx.value`), e.g. a
  damage or stat multiplier (`STAB`, `DAMAGE_MULT`, `DEFENSE_MULT`, ...)
* veto   - a handler may block the action (`ctx.cancelled`, `BEFORE_STATUS`,
  `BEFORE_STAT_CHANGE`, `BEFORE_FLINCH`)

Handlers are registered per `(event, effect)` with `@on(...)`, where `effect`
is the `effect` field of the catalog entry (`data/abilities.json`). The
catalog stays data-driven: parameters like `chance`, `statuses` or
`multiplier` arrive in the handler as `ctx.params`. An ability without a
handler for an event (catalog entry without `effect`, or no entry at all) is
a pure no-op - see the module docstring of `abilities.py`. The handlers themselves live in
`ability_effects.py`, which `battle.py` imports so they are registered.

`ctx.battle` is None for pure calculations without a running battle (AI
damage estimate `expected_damage`, `effective_stat`, ...) - handlers must then
neither write to the log nor roll dice; `ctx.log` already ignores the call.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Callable

from . import abilities, i18n

if TYPE_CHECKING:
    from .battle import Battle, Combatant


class Event(StrEnum):
    # modify
    STAB = "stab"                      # owner = attacker, value = STAB multiplier
    DAMAGE_MULT = "damage_mult"        # owner = attacker, value = damage multiplier
    DEFENSE_MULT = "defense_mult"      # owner = defender, other = attacker, value = damage multiplier
    EFFECTIVE_STAT = "effective_stat"  # owner = stat holder, value = multiplier
    TYPE_EFFECT = "type_effect"        # owner = defender, value = type effectiveness
    BEFORE_DAMAGE = "before_damage"    # owner = defender, value = damage about to be dealt
    # veto
    BEFORE_STATUS = "before_status"    # owner = target of the status
    BEFORE_STAT_CHANGE = "before_stat_change"  # owner = target, other = foe lowering it, value = stages
    BEFORE_FLINCH = "before_flinch"    # owner = Pokemon about to flinch
    # notify
    SWITCH_IN = "switch_in"            # owner = Pokemon entering the field, other = foe
    MOVE_IMMUNITY = "move_immunity"    # owner = defender a move had no effect on, other = attacker
    DAMAGE_TAKEN = "damage_taken"      # owner = hit Pokemon, other = attacker
    STAT_LOWERED = "stat_lowered"      # owner = Pokemon whose stat a foe lowered, other = that foe
    INTIMIDATED = "intimidated"        # owner = target of Intimidate, other = intimidator
    END_OF_TURN = "end_of_turn"        # owner = each active Pokemon


@dataclass
class Ctx:
    owner: Combatant
    other: Combatant | None = None
    battle: Battle | None = None
    move: dict[str, Any] | None = None
    value: float = 1.0
    status: str | None = None
    stat: str | None = None
    move_type: str | None = None
    effectiveness: float = 1.0
    dealt: int = 0
    cancelled: bool = False
    # Markers between engine and handlers - set by a handler for the engine
    # (e.g. "ignore_burn" for Guts, "absorbed", "endured") or passed in by the
    # engine (e.g. "intimidate" on BEFORE_STAT_CHANGE).
    flags: set[str] = field(default_factory=set)
    # Catalog entry of the source currently being evaluated.
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def dry_run(self) -> bool:
        """No running battle: calculate only - no log, no dice."""
        return self.battle is None

    def log(self, key: str, **params: Any) -> None:
        if self.battle is not None:
            self.battle.log.append(i18n.t(self.battle.lang, key, **params))


Handler = Callable[[Ctx], None]

_HANDLERS: dict[tuple[Event, str], Handler] = {}

# Nesting depth of `emit` - a handler may trigger further events (e.g. contact
# status -> inflict_status -> BEFORE_STATUS); the limit stops two abilities from
# triggering each other endlessly. ContextVar, because FastAPI runs sync
# routes in a thread pool.
_depth: ContextVar[int] = ContextVar("battle_event_depth", default=0)
MAX_DEPTH = 8


def on(event: Event, effect: str) -> Callable[[Handler], Handler]:
    """Registers `fn` as handler for `event` on sources with `effect`."""
    def deco(fn: Handler) -> Handler:
        _HANDLERS[(event, effect)] = fn
        return fn
    return deco


def _sources(owner: Combatant) -> list[dict[str, Any]]:
    """Catalog entries whose handlers apply to `owner` - currently only the
    ability; held items or weather can be added here later."""
    ability = abilities.get(owner.ability)
    return [ability] if ability else []


def emit(
    event: Event,
    owner: Combatant,
    other: Combatant | None = None,
    *,
    battle: Battle | None = None,
    **fields: Any,
) -> Ctx:
    """Runs all handlers of `owner` for `event` and returns the context -
    the caller reads `value`, `cancelled` or `flags` from it."""
    ctx = Ctx(owner=owner, other=other, battle=battle, **fields)
    depth = _depth.get()
    if depth >= MAX_DEPTH:
        return ctx
    token = _depth.set(depth + 1)
    try:
        for source in _sources(owner):
            handler = _HANDLERS.get((event, source.get("effect")))
            if handler is None:
                continue
            ctx.params = source
            handler(ctx)
            if ctx.cancelled:
                break
    finally:
        _depth.reset(token)
    return ctx
