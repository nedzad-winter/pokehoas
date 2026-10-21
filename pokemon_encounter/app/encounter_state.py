"""Progress of an encounter per user (table `encounter_user`).

The queue is per user (`encounter.user_id`, see db.py): a trigger event creates
one encounter per player, so normally exactly one row - that of the owner -
belongs to an encounter, and it leaves the queue as soon as the owner is done.

Old encounters from before that split (`encounter.user_id IS NULL`) stay
household-wide: every eligible player (`db.eligible_users`) can attempt the same
encounter, each time from the original state (full HP, original level). What a
user has already done to the Pokemon is in their own row - no longer in
`encounter.current_hp`.

* Open row (`outcome IS NULL`): user has battled/thrown, but not finished yet -
  HP and level are preserved for their next attempt (loss/flee/miss).
* Completed row: the encounter disappears from THEIR queue.
* If all pending users are done, the encounter leaves the queue completely
  (`settle`); otherwise it's freed again so the next can go. Retention
  (`spawn.expire_stale_encounters`) ends it regardless.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from . import db

# 'released' is reserved for filtering from outside (admin/retention); game
# endpoints use the first three.
OUTCOMES = ("caught", "defeated", "fled", "released")

# Order for the overall outcome of an encounter once all are done.
_RANKED_OUTCOMES = ("caught", "defeated", "fled", "released")


def _now() -> str:
    return datetime.now().astimezone().isoformat()


# ------------------------------------------------------------------ Read

def progress(encounter_id: int, user_id: str) -> dict[str, Any] | None:
    return db.query_one(
        "SELECT * FROM encounter_user WHERE encounter_id = ? AND user_id = ?",
        (encounter_id, user_id),
    )


def for_user(encounter: dict[str, Any], user_id: str) -> dict[str, Any]:
    """Encounter row from a user's perspective: HP and level from their own
    progress, otherwise the encounter's original state.

    Ohne eigene Zeile bleibt die Encounter-Zeile unveraendert - so sehen alte
    Datenbanken (in denen KP noch am Encounter hingen) weiterhin denselben
    Zustand wie frueher.
    """
    row = dict(encounter)
    own = progress(encounter["id"], user_id)
    if own is None:
        return row
    row["current_hp"] = own["current_hp"]
    row["level"] = own["level"] if own["level"] is not None else encounter["level"]
    return row


def is_done(encounter_id: int, user_id: str) -> bool:
    """Hat dieser Nutzer seinen Versuch schon hinter sich?"""
    own = progress(encounter_id, user_id)
    return bool(own and own["outcome"])


def outcomes(encounter_id: int) -> dict[str, str]:
    return {
        row["user_id"]: row["outcome"]
        for row in db.query(
            "SELECT user_id, outcome FROM encounter_user "
            "WHERE encounter_id = ? AND outcome IS NOT NULL",
            (encounter_id,),
        )
    }


def belongs_to(encounter: dict[str, Any], user_id: str) -> bool:
    """Darf dieser Nutzer den Encounter ueberhaupt sehen? Eigene Encounter nur
    der Besitzer, alte geteilte (user_id NULL) weiterhin jeder."""
    owner = encounter.get("user_id")
    return owner is None or owner == user_id


def pending_user_ids(encounter_id: int, encounter: dict[str, Any] | None = None) -> list[str]:
    """Nutzer, die diesen Encounter noch vor sich haben: beim eigenen Encounter
    nur sein Besitzer, beim alten geteilten alle Berechtigten
    (`db.eligible_users`)."""
    if encounter is None:
        encounter = db.query_one("SELECT * FROM encounter WHERE id = ?", (encounter_id,))
    done = outcomes(encounter_id)
    owner = (encounter or {}).get("user_id")
    if owner is not None:
        return [] if owner in done else [owner]
    return [user["id"] for user in db.eligible_users() if user["id"] not in done]


# ---------------------------------------------------------------- Schreiben

def save_progress(
    encounter_id: int, user_id: str, current_hp: int | None = None,
    level: int | None = None, outcome: str | None = None,
) -> None:
    """Legt die Zeile an oder aktualisiert sie. `None` heisst jeweils
    "unveraendert lassen" - ein Ausgang wird nie wieder ueberschrieben."""
    db.execute(
        """
        INSERT INTO encounter_user
            (encounter_id, user_id, outcome, current_hp, level, updated_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(encounter_id, user_id) DO UPDATE SET
            outcome = COALESCE(encounter_user.outcome, excluded.outcome),
            current_hp = COALESCE(excluded.current_hp, encounter_user.current_hp),
            level = COALESCE(excluded.level, encounter_user.level),
            updated_at = excluded.updated_at
        """,
        (encounter_id, user_id, outcome, current_hp, level, _now()),
    )


def finish(
    encounter_id: int, user_id: str, outcome: str, current_hp: int | None = None,
    level: int | None = None,
) -> bool:
    """Schliesst den Versuch EINES Nutzers ab und raeumt den Encounter auf.

    Gibt zurueck, ob der Encounter damit komplett erledigt ist."""
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome: {outcome}")
    save_progress(encounter_id, user_id, current_hp=current_hp, level=level, outcome=outcome)
    return settle(encounter_id)


def settle(encounter_id: int) -> bool:
    """Warteschlangen-Status nach einem beendeten Versuch nachziehen.

    Beim eigenen Encounter ist mit dem Besitzer alles durch. Bei einem alten
    geteilten gilt: noch jemand offen -> der Encounter geht frei zurueck in die
    Warteschlange (Anspruch aufgeben, damit der Naechste kaempfen kann).
    Niemand mehr offen -> er verlaesst sie mit dem "besten" Ausgang, den
    jemand erzielt hat (gefangen > besiegt > geflohen).
    """
    row = db.query_one("SELECT * FROM encounter WHERE id = ?", (encounter_id,))
    if row is None:
        return True
    if row["status"] not in ("waiting", "active"):
        return True  # Retention oder ein frueherer settle war schneller

    if pending_user_ids(encounter_id, row):
        db.execute(
            "UPDATE encounter SET status = 'waiting', claimed_by = NULL "
            "WHERE id = ? AND status IN ('waiting','active')",
            (encounter_id,),
        )
        return False

    done = outcomes(encounter_id)
    final = next((o for o in _RANKED_OUTCOMES if o in done.values()), "fled")
    winner = next((uid for uid, o in done.items() if o == final), None)
    db.execute(
        "UPDATE encounter SET status = ?, resolved_at = ?, claimed_by = COALESCE(?, claimed_by) "
        "WHERE id = ?",
        (final, _now(), winner, encounter_id),
    )
    return True
