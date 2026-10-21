"""Battle history: write and read from `battle_log`.

`battle_log` existed before (to make auto-battles trackable), but only stored
the outcome + log text. For the history view, enemy, money, catch flag and the
EXP breakdown are added via migration (`db._migrate_battle_history`). Old rows
don't have these fields - `recent()` still returns them, just without breakdown.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from . import db

KIND_WILD = "wild"        # manual battle against a wild Pokémon
KIND_AUTO = "auto"        # auto-battle against a wild Pokémon
KIND_TRAINER = "trainer"  # gym/league battle against a trainer
KIND_PVP = "pvp"          # battle against the AI copy of another player's team (pvp.py)

DEFAULT_LIMIT = 20
HISTORY_KEEP = 50  # per user; older rows are pruned on every write


def record(
    user_id: str,
    kind: str,
    outcome: str | None,
    *,
    encounter_id: int | None = None,
    opponent: str | None = None,
    money_earned: int | None = None,
    caught: bool = False,
    exp_details: list[dict[str, Any]] | None = None,
    log: list[str] | None = None,
    created_at: str | None = None,
    rule_name: str | None = None,
) -> int:
    """Write exactly one history row. The only write path to battle_log.
    `rule_name`: the auto rule that fought the battle (see auto_rules.py)."""
    row_id = db.execute(
        """
        INSERT INTO battle_log
            (encounter_id, user_id, created_at, auto, outcome, detail,
             kind, opponent, money_earned, caught, exp_breakdown, rule_name)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            encounter_id, user_id,
            created_at or datetime.now().astimezone().isoformat(),
            1 if kind == KIND_AUTO else 0,
            outcome, json.dumps(log or []),
            kind, opponent, money_earned, int(bool(caught)),
            json.dumps(exp_details or []), rule_name,
        ),
    )
    db.execute(
        "DELETE FROM battle_log WHERE user_id = ? AND id NOT IN "
        "(SELECT id FROM battle_log WHERE user_id = ? ORDER BY id DESC LIMIT ?)",
        (user_id, user_id, HISTORY_KEEP),
    )
    return row_id


def _loads(value: Any, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def count(user_id: str) -> int:
    """Total number of history rows for the user, for the frontend's pager."""
    row = db.query_one("SELECT COUNT(*) AS n FROM battle_log WHERE user_id = ?", (user_id,))
    return row["n"] if row else 0


def recent(user_id: str, limit: int = DEFAULT_LIMIT, offset: int = 0) -> list[dict[str, Any]]:
    """A page of the user's battles, newest first."""
    rows = db.query(
        "SELECT * FROM battle_log WHERE user_id = ? ORDER BY id DESC LIMIT ? OFFSET ?",
        (user_id, limit, offset),
    )
    entries = []
    for row in rows:
        breakdown = _loads(row.get("exp_breakdown"), [])
        entries.append({
            "id": row["id"],
            "created_at": row["created_at"],
            # Old rows don't know `kind` yet - derive it from the `auto` flag,
            # which has existed from the beginning.
            "kind": row.get("kind") or (KIND_AUTO if row["auto"] else KIND_WILD),
            "outcome": row["outcome"],
            "opponent": row.get("opponent"),
            "money_earned": row.get("money_earned"),
            "caught": bool(row.get("caught")),
            "rule_name": row.get("rule_name"),
            "exp": breakdown if isinstance(breakdown, list) else [],
            # True only for rows from before the migration: then the breakdown is missing,
            # rather than there being nothing to distribute.
            "legacy": row.get("kind") is None,
        })
    return entries
