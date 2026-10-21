# PokeHOAS (Home Assistant add-on)

Home-Assistant events (motion sensors, lights, zones) spawn Pokémon into each HA
user's own queue; every user battles/catches them in a web app. Background, game
rules and settings are documented in `README.md` — read the relevant section there
instead of exploring the code from scratch.

## Layout

- `pokemon_encounter/app/` — FastAPI backend (Python 3.13, stdlib `sqlite3`, no ORM)
  - `db.py` schema + all migrations + `DEFAULT_SETTINGS`; `battle.py` engine, damage,
    EXP, catch formula, balls; `battle_events.py` event hub the engine fires (STAB, damage,
    status veto, end of turn …) + `ability_effects.py` one handler per ability `effect`;
    `battle_session.py` manual wild battle;
    `trainer_battle.py` gym/league battles vs AI teams; `pvp.py` battles vs an AI copy of
    another user's team + `team_code.py` (export/import team codes); `encounter_state.py` per-user
    attempt/outcome on the shared queue; `spawn.py` spawn weighting;
    `economy.py` money + repeat-catch IVs; `evolution.py`; `medicine.py`; `healing.py`;
    `achievements.py` + `rewards.py` item rewards per achievement; `tms.py` TMs (gym rewards,
    teach off-type moves); `trainers.py`; `auth.py` user detection (Ingress header)
  - `api/` — REST routes, one module per area (`team.py`, `encounters.py`, `shop.py`, …)
  - `data/*.json` — static catalogs (shop, moves, TMs, trainers, held items, biomes)
  - `data/i18n/{en,de}.json` — ALL user-facing strings, flat `key → text`, alphabetically sorted
- `pokemon_encounter/www/index.html` — the whole frontend (single file, vanilla JS)
- `pokemon_encounter/config.yaml` — add-on manifest (`version`); `app/__init__.py` has `__version__`
- `tests/test_*.py` — plain scripts, not pytest

## Commands

```bash
# run every test script (each must print no FAIL and exit 0)
for f in tests/test_*.py; do python3 "$f" >/dev/null 2>&1 && echo "PASS $f" || echo "FAIL $f"; done
python3 tests/test_battle.py      # one file, full output
```

Running tests creates `__pycache__/` — it is gitignored; never commit it.

## Conventions

- **i18n is mandatory**: no hard-coded UI or battle-log text. Backend uses `i18n.t(language, key, **params)`;
  frontend uses `tr('key', {vars})` and `data-i18n="key"`. Add every new key to BOTH `en.json`
  and `de.json`, keeping alphabetical order. Catalog entries use `label`/`label_de`,
  `description`/`description_de`; frontend picks via `pick(obj, 'label')`.
- **DB changes** go through an idempotent `_migrate_*` function in `db.py` (`ALTER TABLE ADD COLUMN`
  guarded by a column check). Never break existing databases; users have live save data.
- **New settings**: add to `DEFAULT_SETTINGS` in `db.py`, validate in `api/config_routes.py`,
  label them in i18n.
- Code comments and docstrings are English (identifiers were always English). Match the
  surrounding style; don't reformat unrelated code.
- Every change is per-user aware: team, inventory, Pokédex, money are scoped by `user_id`;
  the encounter queue is shared by the household.
- Frontend: reuse existing CSS variables/classes and helpers (`esc`, `tr`, `pick`, `api`); keep
  everything in `index.html`.
- Add or extend a `tests/test_*.py` script for new logic; all test scripts must pass before committing.
- Do NOT bump versions, deploy to Home Assistant or push unless told to.

## Dev-only extension (not in the public repo)

The maintainer's checkout has an agent task queue on top of the public app: `app/api/agent_tasks.py`,
`www/agent.js`, `app/data/i18n/ext/`, `tests/test_agent_tasks.py`, `REQUIREMENTS.md` and the agent/cron
scripts in `scripts/`. They are gitignored; `app/main.py` loads the module when present
(`EXTENSIONS`, `GET /api/extensions`) and `i18n.py` merges `data/i18n/ext/*.<lang>.json`. Tracked code
must never import or reference them directly, and every other test must pass without them.
