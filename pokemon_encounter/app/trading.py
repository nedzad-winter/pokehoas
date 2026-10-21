"""Pokemon trades between two household members.

A trade is a three-step handshake, stored as one row in `trade`:

1. `requested` - the initiator picks a partner and one of their Pokemon.
2. `offered`   - the partner accepts the request by answering with one of
   their own Pokemon (the "counter-offer").
3. `completed` - the initiator accepts the final offer, both Pokemon change
   owner in ONE transaction.

Every step can end the trade instead: the partner refuses the request
(`declined`), the initiator refuses the counter-offer (`rejected`) or the
party who made the last move withdraws it (`cancelled`).

Only Pokemon that sit in the box can be traded - not the ones in the team,
the buddy or the day care (all of these are referenced by other features by
id). The held item, IVs, nature, ability, level and taught TMs travel with
the Pokemon. The receiver's Pokedex counts it as caught, like a hatched egg.

Ownership is revalidated when the trade completes (a Pokemon may have been
released or moved into the team since the offer). Finished trades stay
visible until a participant deletes them from their view; whatever is left
is purged the day after it was closed (`purge_closed`).
"""

from __future__ import annotations

import functools
import json
from datetime import datetime
from typing import Any

from fastapi import HTTPException

from . import abilities, db, evolution, held_items, i18n, rarity

OPEN_STATUSES = ("requested", "offered")
CLOSED_STATUSES = ("completed", "declined", "rejected", "cancelled")


def _now() -> datetime:
    return datetime.now().astimezone()


class _Reject(Exception):
    """Raised inside `db.cursor()` blocks: the DB lock is not reentrant, so the
    message (which reads the user's language from the DB) is only built after
    the block was left, see `_translated`."""

    def __init__(self, status_code: int, key: str) -> None:
        super().__init__(key)
        self.status_code = status_code
        self.key = key


def _translated(fn):
    """Turns `_Reject` into an HTTPException in the language of `user_id` (first argument)."""

    @functools.wraps(fn)
    def wrapper(user_id: str, *args: Any, **kwargs: Any):
        try:
            return fn(user_id, *args, **kwargs)
        except _Reject as exc:
            raise HTTPException(
                status_code=exc.status_code, detail=i18n.t(db.user_language(user_id), exc.key)
            ) from None

    return wrapper


def _shiny_sprite_url(sprite_url: str | None) -> str | None:
    return sprite_url.replace("/sprites/pokemon/", "/sprites/pokemon/shiny/") if sprite_url else None


# ---------------------------------------------------------------- Pokemon

def _tradable_error(cur, user_id: str, pokemon_id: int | None) -> tuple[dict[str, Any] | None, tuple | None]:
    """Returns `(row, None)` for a tradable Pokemon of `user_id`, else
    `(row_or_None, (status_code, i18n_key))`. Must run inside `db.cursor()`."""
    if pokemon_id is None:
        return None, (404, "error.pokemon_not_found")
    row = cur.execute("SELECT * FROM owned_pokemon WHERE id = ?", (pokemon_id,)).fetchone()
    if row is None:
        return None, (404, "error.pokemon_not_found")
    if row["user_id"] != user_id:
        return row, (403, "error.not_your_pokemon")
    if row["party_slot"] is not None:
        return row, (400, "error.trade_only_from_box")
    buddy = cur.execute("SELECT buddy_pokemon_id FROM app_user WHERE id = ?", (user_id,)).fetchone()
    if buddy and buddy["buddy_pokemon_id"] == pokemon_id:
        return row, (400, "error.trade_pokemon_is_buddy")
    if cur.execute(
        "SELECT 1 FROM daycare WHERE user_id = ? AND (parent_a = ? OR parent_b = ?)",
        (user_id, pokemon_id, pokemon_id),
    ).fetchone():
        return row, (400, "error.trade_pokemon_in_daycare")
    return row, None


def _require_tradable(cur, user_id: str, pokemon_id: int | None) -> dict[str, Any]:
    row, error = _tradable_error(cur, user_id, pokemon_id)
    if error:
        raise _Reject(*error)
    return row


def tradable_pokemon(user_id: str) -> list[dict[str, Any]]:
    """The user's own Pokemon that can go into a trade right now."""
    rows = db.query(
        "SELECT * FROM owned_pokemon WHERE user_id = ? AND party_slot IS NULL "
        "ORDER BY level DESC, id",
        (user_id,),
    )
    blocked = {
        row["id"] for row in db.query(
            "SELECT buddy_pokemon_id AS id FROM app_user WHERE id = ? "
            "UNION SELECT parent_a FROM daycare WHERE user_id = ? "
            "UNION SELECT parent_b FROM daycare WHERE user_id = ?",
            (user_id, user_id, user_id),
        )
    }
    # `favorite` only matters to the owner's picker (box filter), so it is not
    # part of the summary the trade partner sees.
    return [_summary(row) | {"favorite": bool(row.get("favorite"))}
            for row in rows if row["id"] not in blocked]


def _summary(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """What both trade partners get to see of a Pokemon."""
    if row is None:
        return None
    species = db.query_one("SELECT * FROM species WHERE id = ?", (row["species_id"],)) or {}
    ivs = row["ivs"]
    if isinstance(ivs, str):
        ivs = json.loads(ivs or "{}")
    iv_percent = round(100 * sum(ivs.values()) / (31 * 6)) if ivs else 0
    return {
        "id": row["id"],
        "species": {
            "id": species.get("id"), "name": species.get("name"),
            "name_de": species.get("name_de"), "name_en": species.get("name_en"),
            "type1": species.get("type1"), "type2": species.get("type2"),
            "sprite_url": species.get("sprite_url"),
            "shiny_sprite_url": _shiny_sprite_url(species.get("sprite_url")),
            # Same filter fields as the box (picker in www/index.html).
            "generation": species.get("generation"),
            "stage": evolution.stage_of(species["id"]) if species else None,
            "rarity_tier": rarity.tier_for(species) if species else None,
        },
        "nickname": row.get("nickname"),
        "level": row["level"],
        "shiny": bool(row.get("shiny")),
        "gender": row.get("gender"),
        "iv_percent": iv_percent,
        "ability_info": abilities.describe(row.get("ability")),
        "held_item_info": held_items.get(row.get("held_item")),
    }


# ------------------------------------------------------------------ Views

def _actions(trade: dict[str, Any], role: str) -> dict[str, bool]:
    status = trade["status"]
    initiator = role == "initiator"
    return {
        # partner answers a request with a Pokemon of their own
        "offer": status == "requested" and not initiator,
        # initiator accepts the final offer
        "accept": status == "offered" and initiator,
        "decline": (status == "requested" and not initiator) or (status == "offered" and initiator),
        "cancel": (status == "requested" and initiator) or (status == "offered" and not initiator),
        "delete": status in CLOSED_STATUSES,
    }


def _view(trade: dict[str, Any], user_id: str) -> dict[str, Any]:
    role = "initiator" if trade["initiator_id"] == user_id else "partner"
    other_id = trade["partner_id"] if role == "initiator" else trade["initiator_id"]
    other = db.get_user(other_id) or {}
    offered = db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (trade["offered_pokemon_id"],))
    requested = (
        db.query_one("SELECT * FROM owned_pokemon WHERE id = ?", (trade["requested_pokemon_id"],))
        if trade["requested_pokemon_id"] else None
    )
    actions = _actions(trade, role)
    open_trade = trade["status"] in OPEN_STATUSES
    # A completed trade shows the Pokemon as they were traded (now owned by
    # the other side); an open one flags Pokemon that went missing meanwhile.
    return {
        "id": trade["id"],
        "role": role,
        "status": trade["status"],
        "other_user_id": other_id,
        "other_name": other.get("display_name") or other.get("name") or other_id,
        "initiator_pokemon": _summary(offered),
        "partner_pokemon": _summary(requested),
        "awaiting_me": open_trade and (actions["offer"] or actions["accept"]),
        "unavailable": open_trade and (
            offered is None or offered["user_id"] != trade["initiator_id"]
            or (trade["requested_pokemon_id"] is not None
                and (requested is None or requested["user_id"] != trade["partner_id"]))
        ),
        "actions": actions,
        "created_at": trade["created_at"],
        "updated_at": trade["updated_at"],
        "closed_at": trade["closed_at"],
    }


def purge_closed(now: datetime | None = None) -> int:
    """Deletes finished trades from the day before today or earlier."""
    today = (now or _now()).astimezone().date()
    stale = [
        row["id"] for row in db.query(
            f"SELECT id, closed_at FROM trade WHERE status IN ({','.join('?' * len(CLOSED_STATUSES))})",
            CLOSED_STATUSES,
        )
        if not row["closed_at"] or datetime.fromisoformat(row["closed_at"]).astimezone().date() < today
    ]
    with db.cursor() as cur:
        cur.executemany("DELETE FROM trade WHERE id = ?", [(trade_id,) for trade_id in stale])
    return len(stale)


def overview(user_id: str) -> dict[str, Any]:
    purge_closed()
    rows = db.query(
        """
        SELECT * FROM trade
        WHERE (initiator_id = ? AND hidden_initiator = 0) OR (partner_id = ? AND hidden_partner = 0)
        ORDER BY (status IN ('requested', 'offered')) DESC, updated_at DESC, id DESC
        """,
        (user_id, user_id),
    )
    partners = [
        {"user_id": user["id"], "display_name": user["display_name"] or user["name"]}
        for user in db.eligible_users() if user["id"] != user_id
    ]
    return {"trades": [_view(row, user_id) for row in rows], "partners": partners}


def pending_count(user_id: str) -> int:
    """Trades waiting for a decision of this user (badge in the navigation)."""
    purge_closed()
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM trade WHERE (partner_id = ? AND status = 'requested') "
        "OR (initiator_id = ? AND status = 'offered')",
        (user_id, user_id),
    )
    return row["n"]


# ------------------------------------------------------------ Transitions

def _load(cur, user_id: str, trade_id: int) -> dict[str, Any]:
    trade = cur.execute("SELECT * FROM trade WHERE id = ?", (trade_id,)).fetchone()
    if trade is None or user_id not in (trade["initiator_id"], trade["partner_id"]):
        raise _Reject(404, "error.trade_not_found")
    return trade


def _wrong_state() -> _Reject:
    return _Reject(409, "error.trade_wrong_state")


def _close(cur, trade_id: int, status: str, now: str) -> None:
    cur.execute(
        "UPDATE trade SET status = ?, updated_at = ?, closed_at = ? WHERE id = ?",
        (status, now, now, trade_id),
    )


@_translated
def request_trade(user_id: str, partner_id: str, pokemon_id: int) -> dict[str, Any]:
    """Step 1: initiator offers one of their box Pokemon to `partner_id`."""
    if partner_id == user_id:
        raise _Reject(400, "error.trade_with_self")
    if db.get_user(partner_id) is None:
        raise _Reject(404, "error.user_not_found")
    now = _now().isoformat()
    with db.cursor() as cur:
        _require_tradable(cur, user_id, pokemon_id)
        if cur.execute(
            "SELECT 1 FROM trade WHERE initiator_id = ? AND partner_id = ? "
            "AND offered_pokemon_id = ? AND status IN ('requested', 'offered')",
            (user_id, partner_id, pokemon_id),
        ).fetchone():
            raise _Reject(409, "error.trade_duplicate")
        cur.execute(
            "INSERT INTO trade (initiator_id, partner_id, offered_pokemon_id, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'requested', ?, ?)",
            (user_id, partner_id, pokemon_id, now, now),
        )
        trade_id = cur.lastrowid
    return _view(db.query_one("SELECT * FROM trade WHERE id = ?", (trade_id,)), user_id)


@_translated
def offer(user_id: str, trade_id: int, pokemon_id: int) -> dict[str, Any]:
    """Step 2: the partner accepts the request and offers one of their Pokemon."""
    now = _now().isoformat()
    with db.cursor() as cur:
        trade = _load(cur, user_id, trade_id)
        if trade["partner_id"] != user_id or trade["status"] != "requested":
            raise _wrong_state()
        _require_tradable(cur, user_id, pokemon_id)
        _, error = _tradable_error(cur, trade["initiator_id"], trade["offered_pokemon_id"])
        if error:
            raise _Reject(409, "error.trade_pokemon_unavailable")
        cur.execute(
            "UPDATE trade SET status = 'offered', requested_pokemon_id = ?, updated_at = ? WHERE id = ?",
            (pokemon_id, now, trade_id),
        )
    return _view(db.query_one("SELECT * FROM trade WHERE id = ?", (trade_id,)), user_id)


def _register_caught(cur, user_id: str, species_id: int, shiny: int, now: str) -> None:
    cur.execute(
        """
        INSERT INTO dex_entry (species_id, user_id, seen_count, caught_count, shiny_caught,
                                first_seen_at, first_caught_at)
        VALUES (?, ?, 1, 1, ?, ?, ?)
        ON CONFLICT(species_id, user_id) DO UPDATE SET
            caught_count = caught_count + 1,
            seen_count = MAX(dex_entry.seen_count, 1),
            shiny_caught = MAX(dex_entry.shiny_caught, excluded.shiny_caught),
            first_seen_at = COALESCE(dex_entry.first_seen_at, excluded.first_seen_at),
            first_caught_at = COALESCE(dex_entry.first_caught_at, excluded.first_caught_at)
        """,
        (species_id, user_id, int(shiny), now, now),
    )


@_translated
def accept(user_id: str, trade_id: int) -> dict[str, Any]:
    """Step 3: the initiator accepts the final offer - the Pokemon are swapped."""
    now = _now().isoformat()
    with db.cursor() as cur:
        trade = _load(cur, user_id, trade_id)
        if trade["initiator_id"] != user_id or trade["status"] != "offered":
            raise _wrong_state()
        give_id, get_id = trade["offered_pokemon_id"], trade["requested_pokemon_id"]
        give, give_error = _tradable_error(cur, trade["initiator_id"], give_id)
        get, get_error = _tradable_error(cur, trade["partner_id"], get_id)
        if give_error or get_error:
            raise _Reject(409, "error.trade_pokemon_unavailable")
        for pokemon_id, old_owner, new_owner in (
            (give_id, trade["initiator_id"], trade["partner_id"]),
            (get_id, trade["partner_id"], trade["initiator_id"]),
        ):
            cur.execute(
                "UPDATE owned_pokemon SET user_id = ?, party_slot = NULL WHERE id = ? AND user_id = ?",
                (new_owner, pokemon_id, old_owner),
            )
            if cur.rowcount != 1:
                raise _Reject(409, "error.trade_pokemon_unavailable")
        _register_caught(cur, trade["partner_id"], give["species_id"], give["shiny"], now)
        _register_caught(cur, trade["initiator_id"], get["species_id"], get["shiny"], now)
        _close(cur, trade_id, "completed", now)
        # Both Pokemon are gone from their box: other open trades that mention
        # either one can never complete.
        cur.execute(
            "UPDATE trade SET status = 'cancelled', updated_at = ?, closed_at = ? "
            "WHERE id != ? AND status IN ('requested', 'offered') "
            "AND (offered_pokemon_id IN (?, ?) OR requested_pokemon_id IN (?, ?))",
            (now, now, trade_id, give_id, get_id, give_id, get_id),
        )
    return _view(db.query_one("SELECT * FROM trade WHERE id = ?", (trade_id,)), user_id)


@_translated
def decline(user_id: str, trade_id: int) -> dict[str, Any]:
    """Partner refuses the request, or initiator refuses the counter-offer."""
    now = _now().isoformat()
    with db.cursor() as cur:
        trade = _load(cur, user_id, trade_id)
        if trade["status"] == "requested" and trade["partner_id"] == user_id:
            _close(cur, trade_id, "declined", now)
        elif trade["status"] == "offered" and trade["initiator_id"] == user_id:
            _close(cur, trade_id, "rejected", now)
        else:
            raise _wrong_state()
    return _view(db.query_one("SELECT * FROM trade WHERE id = ?", (trade_id,)), user_id)


@_translated
def cancel(user_id: str, trade_id: int) -> dict[str, Any]:
    """Whoever made the last move withdraws it: initiator (request) or partner (counter-offer)."""
    now = _now().isoformat()
    with db.cursor() as cur:
        trade = _load(cur, user_id, trade_id)
        if not (
            (trade["status"] == "requested" and trade["initiator_id"] == user_id)
            or (trade["status"] == "offered" and trade["partner_id"] == user_id)
        ):
            raise _wrong_state()
        _close(cur, trade_id, "cancelled", now)
    return _view(db.query_one("SELECT * FROM trade WHERE id = ?", (trade_id,)), user_id)


@_translated
def delete(user_id: str, trade_id: int) -> dict[str, Any]:
    """Removes a finished trade from the caller's view; the row disappears
    for good once both sides did (or the day after, see `purge_closed`)."""
    with db.cursor() as cur:
        trade = _load(cur, user_id, trade_id)
        if trade["status"] not in CLOSED_STATUSES:
            raise _Reject(409, "error.trade_still_open")
        column = "hidden_initiator" if trade["initiator_id"] == user_id else "hidden_partner"
        cur.execute(f"UPDATE trade SET {column} = 1 WHERE id = ?", (trade_id,))
        cur.execute(
            "DELETE FROM trade WHERE id = ? AND hidden_initiator = 1 AND hidden_partner = 1", (trade_id,)
        )
    return {"ok": True, "id": trade_id}
