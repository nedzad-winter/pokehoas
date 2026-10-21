"""AttackDex: reine Nachschlage-Liste aller Attacken (kein Team-/Item-Bezug).

Anders als der Pokédex gibt es hier nichts Nutzerspezifisches zu speichern -
der Endpunkt haengt trotzdem an `get_current_user`, damit er wie die
uebrigen `/api/*`-Routen nur ueber die authentifizierte App erreichbar ist.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from .. import moves as moves_mod
from ..auth import get_current_user

router = APIRouter(prefix="/api", tags=["movedex"])


@router.get("/movedex")
def get_movedex(current_user: dict = Depends(get_current_user)) -> dict[str, Any]:
    # `learners`: who learns the move how, as in the games (moves.learners) -
    # the frontend only shows species the user has already seen.
    return {"moves": [{**moves_mod.describe(key), "learners": moves_mod.learners(key)}
                      for key in moves_mod.all_moves()]}
