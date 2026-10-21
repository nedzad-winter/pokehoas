"""God mode: an admin plays without the game's limits.

A per-account switch (`app_user.god_mode`, see `db._migrate_god_mode`) that
only an admin can turn on, and only for an admin account (api/admin.py
`patch_user`). `/api/users/me` switches it off again for an account that is
no longer an admin (`clear_unless_admin`), so the flag never outlives the
admin rights it was granted with.

While it is on, that player skips the limits that cost time or progress -
outcomes (catch chance, damage, EXP) stay exactly as for everyone else:

* Shop: everything is free and every item is for sale, Master Ball included
  (`economy.purchase`, api/shop.py).
* Team heal: no cooldown (`healing.heal_status`).
* TMs: any TM on any Pokémon, regardless of level and type rules
  (`tms.teachable` / `tms.teach`).
* Evolution: every evolution of the species is available right away, without
  level, item or other conditions (`evolution.level_evolutions`).
* Training: unlocked without the level-100 requirement (`training.unlocked`).
* Sprite styles: every generation's style can be picked (api/users.py).
"""

from __future__ import annotations

from . import db


def active(user_id: str | None) -> bool:
    if not user_id:
        return False
    row = db.query_one("SELECT god_mode FROM app_user WHERE id = ?", (user_id,))
    return bool(row and row.get("god_mode"))


def clear_unless_admin(user: dict, is_admin: bool) -> None:
    """Switch god mode off for an account that has lost its admin rights."""
    if user.get("god_mode") and not is_admin:
        db.execute("UPDATE app_user SET god_mode = 0 WHERE id = ?", (user["id"],))
        user["god_mode"] = 0
