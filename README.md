<p align="center">
  <img src="pokemon_encounter/www/img/banner.svg" alt="PokeHOAS" width="417">
</p>

# PokeHOAS – Pokémon for Home Assistant

[![Open your Home Assistant instance and show the add add-on repository dialog with this repository pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fnedzad-winter%2Fpokehoas)

Your smart home is the tall grass. Every event in Home Assistant – a motion sensor, a
door, a light switch, a person arriving at a zone – can spawn a wild Pokémon. Each
Home Assistant user gets their **own queue, team, Pokédex and bag** and handles the
encounters in a web app inside the HA sidebar: battle, throw balls, catch, evolve.

**Status: public beta (v1.2-beta).** It is a hobby project and playable end to end;
expect rough edges and please report them as issues.

## Features

- **Triggers from your real house**: motion, door/window, lights & switches out of the box;
  any other entity (media player, vacuum, climate, lock, person/zone …) can be added.
  Light switches favour Electric types, outdoor zones use biomes.
- **Multi-user**: HA users are recognised through Ingress – no extra login. Household
  members can trade Pokémon and battle an AI copy of each other's team (PvP).
- **Full Pokédex (Gen 1–9, ~1025 species)** with rarity tiers, shinies, IVs/EVs, abilities,
  moves, held items, Mega Evolution, breeding and eggs.
- **Turn-based battles** with a type chart, STAB, status effects and ability effects,
  plus auto-battle and per-player **auto rules** (IF/DO) for hands-off catching.
- **Gyms & League** for nine regions, fought in order (gym 1 → Elite Four → champion),
  with badges, TMs and difficulty settings; clearing a region unlocks new spawns.
- **Economy and progress**: Poké Mart, potions, balls, daily challenges, achievements.
- **Admin tools** for HA admins: trigger management, player editor, game settings.
- **8 UI languages**: English, Deutsch, Español, Français, Italiano, 日本語, 한국어, Polski.
- Everything runs locally in one add-on container (FastAPI + SQLite, no cloud). The Pokédex
  is fetched once from [PokeAPI](https://pokeapi.co); sprites are loaded from the PokeAPI
  sprites repository.

> **Disclaimer.** PokeHOAS is an unofficial, non-commercial fan project. It is not
> affiliated with, endorsed or sponsored by Nintendo, Game Freak, Creatures Inc. or The
> Pokémon Company. Pokémon and all related names, characters, sprites and artwork are
> trademarks and/or copyrighted works of their respective owners and are used here for
> fan and educational purposes only. If you are a rights holder and want something
> removed, please open an issue.

---

## How it works

```
Home Assistant                Add-on (own container)
┌──────────────┐   every 5min ┌─────────────────────────────────┐
│ Motion       │ ◀─── Poll ───│ poller.py   History/Logbook     │
│ Light switch │              │ spawn.py    which Pokémon?      │
│ Consumption… │              │ SQLite      queue               │
└──────────────┘              │ battle.py   Battle & catch      │
                              │ FastAPI + Web app (Ingress)     │
                              └─────────────────────────────────┘
```

**Pull instead of push.** The add-on queries Home Assistant every five minutes for
state changes of configured entities. No automation per sensor, no
webhook, no webhook secret. Survives HA restarts because history
holds the events anyway.

> **On the term "Logbook":** The Logbook API filters by only a single
> entity. For multiple trigger entities at once, `/api/history/period` is
> the right endpoint – same state changes content, but with
> `filter_entity_id` as a comma-separated list. That's why History is the default;
> `fetch_logbook()` remains for debugging.

**The marker is in-memory.** How far processing has gone exists only in the
running process. After a restart, we look back `poll_lookback_minutes` (default 15).
Worst case, a few minutes are missing – acceptable.

**Each selected entity triggers its own spawn.** A light switch is
a full-fledged trigger, not just a modifier: event in log → encounter →
adjust type probability based on the triggering entity type. For light switches,
that means increased chance of electric Pokémon.

**Rarity in five tiers.** Each species gets a rarity tier
(`app/rarity.py`) that determines its base weight in the spawn pool. Type bonus
of the trigger and biome weights multiply on top of that.

**Biomes only on the road.** Home-internal triggers draw from the full Pokédex.
Biome assignment applies exclusively to geolocation triggers
(`zone.*`, `person.*`, `device_tracker.*`).

**Multiple players, one queue each.** Each Home Assistant user who opens the
panel is automatically recognized via the Ingress header (no separate login)
and gets their own team, inventory, Pokédex progress – and their own encounter
queue. A trigger event creates one encounter *per registered player*, each with
its own species, level and shiny roll based on that player's unlocked regions and
rarity bonus: nobody profits from someone else's league progress, and nobody
races anyone else for the same Pokémon. Global cooldown and daily spawn cap
therefore count per player as well. New players choose a starter from all
generations (1–9) on first open, together with their **region lock**
(see [Region lock](#region-lock)). Choosing the starter also drops three
level-2 welcome encounters into that player's own queue - Pidgey, Rattata and
Caterpie - so there is something to catch right away, before the first real
trigger event fires.

Encounters created *before* this split (and admin spawns, see `app/api/admin.py`)
stay household-wide: every eligible person gets their own attempt at the same
encounter, each from the original state (full HP). Eligible means active in the
last 7 days and not a test account ("test" in the name or display name).

---

## Region lock

Everyone wants to play differently, so each player chooses how the wild spawn pool of
their own queue unlocks – when picking the starter, and later under **Settings → Gameplay**
(stored per player in `app_user.region_lock`, `PUT /api/users/me/region-lock`):

| Value | Name | Spawn pool |
|-------|------|------------|
| `unlocked` | Unlocked | Every generation spawns from the start. |
| `region` (default) | Region | Generation 1 from the start; clearing a region's league unlocks that region's generation (Johto → Gen 2, Hoenn → Gen 3, …). |
| `previous_region` | Progression | Generation 1 from the start; clearing a region's league unlocks the **next** generation (Kanto → Gen 2, Johto → Gen 3, …). |

Clearing a league means beating *all* gym leaders, the Elite Four and the champion of that
region. Galar has no Elite Four: as in Pokémon Sword, its **Champion Cup** takes that place –
the semifinals (rivals Marnie and Hop) and the finals (Bede's interruption, then gym leaders
Nessa, Bea and Raihan with stronger teams) before Champion Leon. These six count as Elite Four
members everywhere (IVs, prize money, healing perks, clearing the region; `kind: elite_four`
plus a `stage` in `data/trainers.json`). The logic lives in `achievements.unlocked_generations` / `achievements.unlock_generation`;
the region achievements' descriptions, the badges in the Gym & League tab and the Guide adapt
to the chosen mode. Players who existed before the option were migrated to `region`
(`db._migrate_region_lock`), i.e. nothing changed for them. Rewards (Master Ball, TMs, …) and the
rarity bonus of cleared leagues do not depend on the lock. For the household-wide leftovers
(old shared encounters, admin spawns) the pool is the union of all players' pools.

## Battle style

Like in the main games, every player chooses how gym, league and PvP battles (everything with a
trainer sending out several Pokémon) continue after an opponent Pokémon faints – under
**Settings → Gameplay** (stored per player in `app_user.battle_style`,
`PUT /api/users/me/battle-style`):

| Value | Name | Behaviour |
|-------|------|-----------|
| `set` (default) | Set | The trainer's next Pokémon comes out and the fight goes on right away. |
| `shift` | Shift | After each defeated opponent Pokémon (except the last one) you may **switch for free** before the next one fights – or just pick a move to decline. |

The free switch is offered once per defeated Pokémon (`battle.Battle.shift_offer`, exposed as
`shift_offer` in the battle view) and only if another team member can still fight. Using an item
or a move ends the offer; a further switch costs the turn as usual. The style is fixed when a
battle starts (a change in the settings applies from the next battle). Wild battles are not
affected. Players who existed before the option were migrated to `set`
(`db._migrate_battle_style`), i.e. nothing changed for them.

## Trainer order

Independent of every gameplay setting, the trainers of a region are fought **one after the other**,
in the order of `data/trainers.json`: gym 1, gym 2 … gym 8, then the Elite Four (Galar: the Champion
Cup rounds), then the champion. A trainer stays locked until its predecessor has been beaten
(`trainers.prerequisite`, `trainer_battle._locked_by`); `trainer_battle.start` rejects an early
challenge with a 409, and the Gyms & League tab disables the button and names the trainer to beat
first (`locked_by`). Regions are independent of each other, Training is never locked, and a trainer
beaten before stays open for rematches, so existing saves lose nothing.

## Gym difficulty

Every player can further restrict their own gym/league battles under **Settings → Gameplay**
(stored per player in `app_user.gym_difficulty`, `PUT /api/users/me/gym-difficulty`):

| Value | Name | Behaviour |
|-------|------|-----------|
| `normal` (default) | Normal | The whole party can fight, capped to the trainer's level as always. |
| `generation_limit` | Generation limit | Only party members whose species generation is the trainer's own or exactly one generation older are allowed to fight; anything younger, or more than one generation older, sits that battle out. |
| `same_generation` | Same generation | Stricter still: only party members whose species generation is *exactly* the trainer's (the league's) own generation are allowed to fight; anything younger or older, including one generation older, sits that battle out. |

The trainer's generation is its region's generation (`trainers.generation_for_region`, same
mapping as [Region lock](#region-lock)); a species' generation is `species.generation` (from
`seed.GENERATION_BOUNDS`). The filter (`trainer_battle._generation_eligible`) is applied when the
battle starts (`trainer_battle.start`) - if it leaves no eligible party member the challenge is
rejected before it can start. The Gyms & League tab marks trainers your whole current party is
ineligible for and disables the challenge button for them. This is checked only for gym/league
battles, not PvP or wild encounters. Players who existed before the option were migrated to
`normal` (`db._migrate_gym_difficulty`), i.e. nothing changed for them.

## Training

**Training** (`app/training.py`) is a sparring fight without stakes, started from the **summary of
any own Pokémon** (Team tab → tap a Pokémon → "Start training", `POST /api/training/{owned_id}/battle`).
The Pokémon may be in the team or in the box – it doesn't have to join the team, and stays where
it is. It unlocks once you have raised **101 Pokémon to level 100** – together with the level-100
achievement "Dojo Master" (the permanent `pokemon_level100` count, see "Level-100 achievements";
releasing them again doesn't take it back); until then the summary shows no Training card
(`GET /api/training`). A fainted Pokémon has to be healed first. Only that **one Pokémon**
fights, against six level-100 Pokémon. In the by-moves selection (see below for the default)
these are Pokémon that its **moves** hit super effectively (at least ×2) – not its own types: a Zapdos with only Thunder faces e.g. Flying or Water Pokémon, or a ×4
combination like Water/Flying. Only damaging moves count (none: the Pokémon's own types). Species
that every move type hits super effectively are drawn first; if there are none, those hit
super effectively by the most move types, preferably immune to none of them. Moves that can't be
super effective against anything (only Normal moves) get Pokémon hit at least neutrally instead.
They are drawn at random on every start. The sparring partners have no ability, know only Splash
and never hurt you.

By default the opponents are picked by the Pokémon's **first type**
(`POST /api/training/{owned_id}/battle?mode=type`, `training.MODE_TYPE`, `training.DEFAULT_MODE`):
six Pokémon that are ×4 weak to that type (Electric → Water/Flying, Fire → Grass/Bug …), whatever
moves it knows. First types with no ×4 weakness (e.g. Normal, Dragon) fall back to the ×2 rule
above. The choice next to the start button can switch to `mode=moves`, the by-moves selection
described above; it is kept for the rematch.

Only **EXP** is earned (with the usual trainer EXP multiplier, Lucky Egg etc.) – no money, EVs,
Pokédex entries, battle history or achievements, except the level-100 count when the EXP gets a
Pokémon there. Rematches are free. God mode (see below) unlocks it
regardless of the count.

## God mode

An admin can switch on **god mode** for an admin account (Settings → Admin → Players → Account,
`app_user.god_mode`, `app/god_mode.py`) to play without the game's limits. A "God mode" badge in
the header shows it is on. While it is on, that account:

- shops for free, and the shop also sells items that are never for sale otherwise (Master Ball),
- heals the team without a cooldown,
- can teach any TM to any Pokémon, regardless of level and type rules,
- can evolve any Pokémon into any of its evolutions right away – no level, item, trade or other
  condition (the evolve button in the Pokémon summary offers them all),
- has Training and every generation's sprite style unlocked.

Outcomes stay the same as for everyone: catch chance, damage, EXP and rewards are untouched.
God mode can only be switched on for an admin account, and `/api/users/me` switches it off again
as soon as the account is no longer an admin. Wiping the player's save data resets it.

## Installation

1. Add the repository to Home Assistant – click the button at the top of this page or go to
   *Settings → Add-ons → Add-on Store → ⋮ → Repositories* and add
   `https://github.com/nedzad-winter/pokehoas`.
2. Install and start "PokeHOAS" (the image is built locally on first install, which can
   take a few minutes). Requires Home Assistant OS or Supervised (add-on support).
3. Open via the sidebar panel (Ingress, no port needed).
4. As an admin, open **Settings → Admin → Triggers** and add entities.

On first startup, the add-on loads the Pokédex from
[PokeAPI](https://pokeapi.co) (~1025 species). If the API is unreachable,
it starts with a small bundled dataset and can load more later.

### Without Supervisor (standalone Docker)

Running **Home Assistant Container** (plain Docker, Kubernetes, a VM without Supervisor)?
Then there is no add-on store – run PokeHOAS as its own container next to Home Assistant.
It is the same app; the only differences are that users log in with their HA account
once, the configuration lives in environment variables and the container keeps its own
backups. This walkthrough uses Docker Compose; every option is listed in
[DOCKER.md](DOCKER.md).

**1. Create an access token.** In Home Assistant, click your user name at the bottom of
the sidebar → **Security** tab → **Long-lived access tokens** → **Create token**, name it
`PokeHOAS` and copy it (it is shown only once). The user must be an **administrator**:
PokeHOAS reads the entity registry and the user list with this token.

**2. Get the compose file.** In a new folder:

```bash
mkdir pokehoas && cd pokehoas
curl -o docker-compose.yml https://raw.githubusercontent.com/nedzad-winter/pokehoas/main/docker-compose.example.yml
```

**3. Fill in three values** in `docker-compose.yml`:

```yaml
    environment:
      POKEMON_HA_URL: http://192.168.1.10:8123      # how the container reaches HA
      POKEMON_HA_TOKEN: "eyJhbGciOi..."             # the token from step 1
      POKEMON_AUTH_MODE: ha_oauth
      POKEMON_PUBLIC_URL: http://192.168.1.10:8099  # the URL you will open in the browser
```

- `POKEMON_HA_URL`: use the LAN IP of your HA host, or `http://homeassistant:8123` if
  both containers share a compose network.
- `POKEMON_PUBLIC_URL`: exactly the address you type into the browser (scheme, host,
  port), without a trailing slash. The HA login sends you back to it.
- If the browser reaches HA under another address than the container does (e.g.
  `https://ha.example.com`), also set `POKEMON_HA_EXTERNAL_URL` to that address.

**4. Start it.**

```bash
docker compose up -d
docker compose logs -f pokehoas
```

The log shows `[pokehoas] Starting on port 8099 (auth mode ha_oauth)`. If a value is
missing or wrong, the container prints `[pokehoas] ERROR: …` with what to fix and stops.
As with the add-on, the first start loads the Pokédex from PokeAPI, which takes a moment.
Database, login key and backups are stored in the `./pokehoas` folder next to the compose
file.

**5. Log in.** Open `POKEMON_PUBLIC_URL` and click **Log in with Home Assistant**. You
land on your normal HA login page and come back logged in. Every household member does
this once with their own HA account (the session lasts 30 days) and gets their own team,
Pokédex and bag. HA administrators are PokeHOAS admins, as in the add-on.

**6. Add triggers.** As an admin, open **Settings → Admin → Triggers** and add entities,
exactly like in step 4 of the add-on installation.

**7. Optional: put it in the HA sidebar.** *Settings → Dashboards → Add dashboard →
Webpage*, URL = `POKEMON_PUBLIC_URL`. In the sidebar the HA login opens in a small popup
once – allow pop-ups if nothing happens. If HA runs on HTTPS, PokeHOAS must be served over
HTTPS too (browsers block an `http://` page inside an `https://` one); see
[DOCKER.md → Sidebar](DOCKER.md#sidebar) for setups where HA and PokeHOAS run on
different domains.

**Updating:** `docker compose pull && docker compose up -d`.
**Backups:** the container writes a snapshot of the database to `./pokehoas/backups` once a
day and keeps the last seven; admins can also download one at any time (**Settings →
Admin → Download backup**). Restoring and moving an existing add-on save to the container
are described in [DOCKER.md](DOCKER.md#backups-and-restore).

| Problem | Fix |
|---|---|
| "Login with Home Assistant failed" | `POKEMON_PUBLIC_URL` must match the address in the browser exactly; if the browser reaches HA under a different URL, set `POKEMON_HA_EXTERNAL_URL`. |
| No *Admin* tab for an HA administrator | The token from step 1 must belong to an HA administrator, or name the user in `POKEMON_ADMIN_USERS`. |
| Already behind Authelia / Authentik | Use `POKEMON_AUTH_MODE: trusted_header` instead of the HA login, see [DOCKER.md → Auth modes](DOCKER.md#auth-modes). |

### Data & Backup

The SQLite file is at `/data/pokemon.sqlite3`, in the
add-on data directory. The Supervisor includes it in normal backups
automatically – no separate database server, no extra configuration. Admins can also
download a snapshot at any time (**Settings → Admin → Download backup**).

---

## Configuration

The **add-on configuration** only contains things that must be fixed before startup:
log level, poll interval, port.

Everything game-relevant is managed **in the web app**, so changes take effect
without restart:

| Tab | Content |
|---|---|
| Queue | waiting encounters, throw balls, auto-battle, flee, "Run rules" (auto rules) |
| Settings | *General*: personal region lock, battle style, auto heal, whether the Team tab shows IVs and whether moving a Pokémon to the box returns its held item to the bag (every player). *Auto rules*: the player's own IF/DO rules (see below). Admins additionally get *Admin*, with sub-views: Pokémon lookup (base stats, rarity tier, spawn weight, evolutions, abilities), spawn, player editor (account incl. money and every player preference / team / box - every Pokémon field incl. EVs, EXP, ability, held item, any move and the species itself / items / Pokédex per species or in bulk / achievements and gym badges, granted with or without their reward) plus a **raw data** view that edits, adds and deletes the player's rows in every per-player database table (eggs, daycare, daily challenges, PvP records, auto rules, own encounter queue …; values are only type-checked), the grouped game settings (retention, cooldowns, daily limit, level range, rarity weights, economy, auto-battle …) with a reset to defaults (all or per group), **Triggers** (which entities can spawn, states, spawn chance, type bonus, cooldown, and a manual "Trigger now" button per entity that fires it immediately for testing, bypassing only that entity's own cooldown and `spawn_chance` roll - the daily cap and global cooldown still apply) and **Zones & Biomes** (mapping geolocation zone → biome) |
| Team | team (slots 1–6) and box, moves, IVs, EVs, EXP, held items, heal team; the box is sorted by National Dex by default and can be filtered by type, evolution stage (1–3), rarity tier, "can evolve", shiny, favorites, locked-only and minimum level, with a checkbox to invert the whole filter; its select mode releases several box Pokémon at once (all or nothing, Rare Candy rewards added up) or evolves the selected ones at once (`POST /api/team/evolve-many` – only Pokémon with exactly one level evolution and no item needed, the rest is skipped). Any Pokémon can be locked (`PUT /api/team/{id}/locked`, shown with a small Klefki) to protect it from release – legendary and mythical catches arrive locked automatically, and so does every shiny catch unless the player switches `auto_lock_shiny` off (personal, `app_user.auto_lock_shiny`, default on; toggle: `PUT /api/users/me/auto-lock-shiny`); with `auto_lock_living_dex` (personal, `app_user.auto_lock_living_dex`, default off; toggle: `PUT /api/users/me/auto-lock-living-dex`) a catch also starts locked while its evolution line still needs it for the living Pokédex – `evolution.living_dex_need` matches the line's owned Pokémon to its species (they only evolve forward, gender-bound evolutions like Kirlia → Gallade count per gender, Shedinja comes along with Ninjask), so with 2 Eevee, a Flareon and an Umbreon another Eevee locks (5 still needed) while a second Flareon stays unlocked; the select mode also locks/unlocks the selection at once (`POST /api/team/lock-many`), and "Release selected" leaves locked Pokémon out (the release endpoints reject them until unlocked) |

### Type bonus

Per trigger as JSON, e.g. `{"electric": 4.0}` – quadruples the weight of all
electric Pokémon for encounters from this entity. When adding an entity,
the add-on suggests sensible values based on domain
(`light` → electric, `climate` → fire/ice, `vacuum` → steel…), see
`app/data/biomes.json` under `trigger_type_hints`.

### Rarity tiers

Classification is always calculated from already-stored Pokédex columns or
hardcoded National Dex numbers – existing databases don't need re-seeding from PokeAPI:

| Tier | Rule | Default weight |
|---|---|---|
| `common` | Base form (no pre-evolution) **and** base stat sum ≤ 450 – except the starter Pokémon | 83 |
| `rare` | everything else non-legendary: evolutions, strong base forms (BST > 450) and the 27 starter base forms (gen 1–9 grass/fire/water) | 10 |
| `pseudo_legendary` | the ten pseudo-legendary lines (Dratini, Larvitar, Bagon, Beldum, Gible, Deino, Goomy, Jangmo-o, Dreepy, Frigibax) – **all** stages of the line | 3 |
| `paradox` | the 20 Paradox Pokémon from Scarlet/Violet including DLC (Great Tusk, Iron Treads, Roaring Moon, Walking Wake, Gouging Fire, …) | 2.5 |
| `legendary` | `is_legendary` from seeding **or** one of eleven Ultra Beasts (count as legendary by definition) | 1 |
| `mythical` | `is_mythical` from seeding (overrides everything else) | 0.5 |

Default weights without any defeated league equal exactly the distribution
83 / 10 / 3 / 2.5 / 1 / 0.5 % (sum 100). A species' spawn weight is
`tier weight × catch rate factor`, with the catch rate factor only scaling within
a tier (0.6 at catch rate 3 to 1.0 at 255). This prevents any species from "overtaking" its tier.

The six weights are editable in **Settings → Admin → Game settings**
(`rarity_weight_common` … `rarity_weight_rare`, each 0–100; at least
one must remain > 0). Queue and Pokédex detail show the tier as
a small badge.

**Bonus for cleared leagues.** Each region whose league is fully cleared
geometrically brings each tier closer to its target weight at **full**
game completion (`rarity.REGION_FULL_CLEAR_TARGET`) – each cleared league is an
equally-sized relative step toward the target:

| Tier | Default (0 leagues) | Target (all 9 leagues cleared) |
|---|---|---|
| `common` | 83 % | 58.5 % |
| `rare` | 10 % | 18 % |
| `pseudo_legendary` | 3 % | 8 % |
| `paradox` | 2.5 % | 6.5 % |
| `legendary` | 1 % | 4 % |
| `mythical` | 0.5 % | 5 % |

`mythical` deliberately grows the strongest (×10) – whoever has really cleared every league
encounters mysterious Pokémon almost as often as legendary ones.
All other tiers grow more slowly, so progress feels neither too easy
nor too hard. Since every player has their own queue, only *your own* cleared
regions count (Kanto counts too). The active bonus per tier is shown in the
**Achievements** tab, together with every other permanent bonus you have unlocked
(shiny perks, healing from beaten Elite Four/Champions, equal EXP, Training, the
generations in your spawn pool, sprite styles – `achievements.perks`). For the household-wide leftovers (old shared encounters,
admin spawns) the eligible player (active in last 7 days, not a test account)
with the most cleared regions counts.

Since only base forms spawn in the wild, you encounter `rare` mainly in the form
of strong base forms (Snorlax, Lapras, …) – evolutions only come via leveling or
evolution items. Paradox Pokémon and Ultra Beasts are base forms themselves and thus catchable in the wild.

**Baby Pokémon** (Pichu, Happiny, Munchlax, …) were retroactively inserted by PokeAPI
as the `evolves_from` of Pokémon that already existed in an earlier generation –
without a special case, Pikachu, Snorlax or Chansey would be misclassified as
"an evolution" and never spawn in the wild at all, since only base forms do
(see above). `rarity.BABY_IDS` lists these babies; their adult form counts as a
base form for both the rarity tier and the wild spawn pool
(`rarity.effective_evolves_from`, used by `spawn._spawn_pool`).

### Biomes

Two levels, so it scales to all generations:

* `type_weights` – weighting by Pokémon types, applies automatically to everything
  in the Pokédex.
* `featured` – the spawns explicitly confirmed by Niantic in the Rediscover Kanto
  event (2024), additionally weighted high.

Included: forest, mountain, beach, lake/river, city, grassland, desert, cave, snow.

---

## Game rules

The **Guide tab** in the web app explains everything below (spawns, catching, IVs, EVs,
Rare Candy, evolution, shiny odds, region/League unlocking, TMs and gym rewards, breeding,
held items, Mega Evolution, PvP, achievements, plus a full 18×18 type chart) in player-facing language, fully translated (`guide.*` keys in
`app/data/i18n/`) – this section is the technical reference for the same systems.

**Level & Experience** like in the main games. Fight and get stronger.

**Catching** based on PokéRogue:
* Instead of attacking, you can throw a ball anytime – even as the
  very first action, without attacking first.
* Catch chance depends on remaining HP – weaken first, then catch.
* A catch attempt costs a ball; if it fails, the opponent gets another
  attack turn.
* Critical catch as a random bonus, chance increases with Pokédex progress.
* **A wild Pokémon that has leveled up during this battle is no longer catchable in this battle.**

**Ball tiers:** Poké Ball (×1), Great Ball (×1.5), Ultra Ball (×2), Master Ball
(always catches, not purchasable). Plus special balls like in the main series
(`app/battle.py`, `ball_multiplier`); if the condition isn't met, they work like a Poké Ball:

| Ball | Condition | Bonus |
|---|---|---|
| Net Ball | wild Pokémon is water or bug | ×3.5 |
| Dive Ball | encounter from lake or beach biome (geo-trigger) | ×3.5 |
| Nest Ball | level < 30 | (41 − level) / 10, max ×4 |
| Repeat Ball | species already caught by you | ×3.5 |
| Timer Ball | per battle turn played ≈+0.3 | up to ×4 |
| Quick Ball | no turns played against this Pokémon yet (first throw) | ×5 |
| Dusk Ball | nighttime, 20–6 local time | ×3 |
| Level Ball | own active Pokémon (outside battle: team slot 1) higher / > 2× / ≥ 4× | ×2 / ×4 / ×8 |
| Fast Ball | speed base stat ≥ 100 | ×4 |
| Heal Ball | – (caught Pokémon arrives with full HP) | ×1 |

In battle and in the queue, currently boosted balls are marked with ★ and their
current multiplier. The old Rogue Ball was removed; existing copies are automatically
swapped for two Ultra Balls each on startup.

**Shiny Pokémon:** every spawn independently rolls a `shiny_rate` chance (default
1/4096, like recent main games) to be shiny – a cosmetic recolor with its own sprite
and a sparkle (✨) wherever it's shown. Owning a Shiny Charm (`spawn.shiny_charm_owned`,
reward for "League Champion", see below) multiplies the rate by `shiny_charm_multiplier`
(default ×2) – for your own spawns and eggs, since everyone spawns into their own
queue. Purely cosmetic – no stat effect. Every shiny you catch starts locked
(see Team tab above, `auto_lock_shiny`) unless you opt out, so it can't be
released from the box by accident.

**Pokédex achievements** (group "Pokédex" in the Achievements tab; the battle/gym
achievements form the group "Gyms & Battles"). Unlocks are permanent and per player;
they are checked after every catch, trainer battle and whenever the tab is opened or
a shiny is rolled (`achievements.sync_pokedex` / `shiny_boost`):

| Achievement | Condition | Perk |
|---|---|---|
| Seen 'Em All | every species seen – wild encounters **and** the teams of gym/league trainers you have fought count | Shiny Charm ×`shiny_charm_seen_factor` (1.1, i.e. 10 % stronger: ×2 → ×2.2) |
| Gotta Catch 'Em All | every species caught at least once | Shiny Charm ×`shiny_charm_caught_factor` (2.0: ×2 → ×4); replaces the seen perk, they don't stack |
| Living Pokédex | every species owned at the same time (team or box) | every spawn is shiny with `living_dex_shiny_rate` (1/100), charm or not |

The two charm perks only work while you own the Shiny Charm.

**Generation achievements** (group "Generations", two per generation, `achievements.GEN_CAUGHT_PREFIX`;
checked together with the Pokédex achievements by `achievements.sync_generations`). Mythical
Pokémon (`species.is_mythical`) do **not** count towards them – they are the living reward instead.
Progress per generation: `achievements.generation_progress`.

| Achievement | Condition | Reward (once) | Perk (permanent) |
|---|---|---|---|
| `gen_caught_<n>` "<Region> Pokédex Complete" | every species of generation n caught at least once | ₽200,000 + 40 Ultra Balls (`GEN_CAUGHT_MONEY` / `GEN_CAUGHT_ITEMS`) | wild spawns of generation n roll shiny ×`gen_caught_shiny_factor` (1.5) |
| `gen_living_<n>` "<Region> Living Pokédex" | every species of generation n owned at the same time (team or box) | every mythical base form of generation n appears once in your queue (`spawn.create_event_encounters`) | the sprite style of generation n can be picked per Pokémon in its Pokédex entry |

* The generation shiny factor **multiplies** with everything else (`spawn.roll_shiny`): base rate
  × charm × Pokédex charm factor × generation factor – e.g. 1/4096 × 2 × 1.5 ≈ 1/1365. It also works
  without the charm. A living Pokédex's flat 1/100 is still the minimum on top.
* Event encounters (`source_kind = 'event'`, source "event:gen_living_<n>") stay
  `spawn.EVENT_RETENTION_DAYS` (365) days and count neither for the daily cap nor for the global
  cooldown. A mythical evolution (Melmetal) is not spawned, only its base form – like the wild pool.
* Sprite style per Pokémon (`species_sprite_style`, `PUT /api/users/me/sprite-style/{species_id}`, no row = normal; set in the Pokédex entry): the PokeAPI
  sprites of that generation's games (Red/Blue, Crystal, Emerald, Platinum, Black/White animated, X/Y,
  Ultra Sun/Ultra Moon, Sword/Shield, Scarlet/Violet – `SPRITE_STYLES` in `www/index.html`). A
  MutationObserver rewrites every PokeAPI sprite `<img>`; species newer than that game, Gen-1 shinies
  (no shiny sprites back then) and missing files keep the normal sprite.

**Level-100 achievements** (group "Level 100" in the Achievements tab, `achievements.LEVEL100_KEYS`):
raising your 1st, 10th, 20th and 35th own Pokémon EVER to level 100 each reward an Exp. Share
(`exp_share`, `battle.exp_shares`) - a held item that gives its holder the SAME EXP as a
fighting team member without it joining the battle itself (instead of the usual
one-fifth for non-participants). The 50th pays no item: from then on EVERY team member
gets the full EXP of a fight (`achievements.share_all_exp`). The 101st ("Dojo Master") pays no
item either: it unlocks **Training** (see "Training", same threshold). Counted in `pokemon_level100`, checked
(`achievements.check_level100`) after every source of a level: battle EXP (wild, gym/
league, PvP and Training), Rare Candy and Buddy walking; opening the Achievements tab catches
up milestones the count already passed (`achievements.sync_level100`). Like every achievement
the milestone is permanent - it survives releasing or trading the Pokémon away again.

**Milestone achievements** (`achievements.MILESTONES`, unlocked by `achievements.sync_milestones` after
every catch, Gym/League win, daily-challenge win and when the tab is opened; each pays out once):

| Group | Achievement | Reward |
|---|---|---|
| Catching | 10 / 100 / 500 / 1000 catches (sum of the Pokédex catch counters) | 10 Poké / 15 Great / 15 Ultra / 10 Ultra + Master Ball |
| Catching | first roaming legendary caught / all eight | 1 Master Ball each |
| Pokédex | 25 / 50 / 75 % of all species caught | 5 / 10 / 15 Ultra Balls + 3 / 5 / 10 Rare Candy |
| Daily challenge | a quest won on 7 / 30 / 100 days in a row (longest run ever counts) | 10 / 25 / 30 Ultra Balls + 5 / 15 / 30 Rare Candy (+ Master Ball at 100) |

Balls are balanced against the gym badges (≤5 of a tier) and the daily challenge (10–20 of each tier
per quest): Poké early, Ultra later, Master Balls only for the long hauls.

**Queue:** Encounters flee after `retention_days` (default 7) without
interaction. By default max 15 spawns per day and player with 30 minutes global
cooldown per player. An encounter disappears from your queue as soon as you're
done with it (caught, defeated, or fled) – for your own encounters (`encounter.user_id`)
that also closes them. An old shared encounter (`user_id` NULL) is fully closed
only when all eligible players are done or retention expires. Each player's
progress (wild Pokémon's HP, outcome) is stored in `encounter_user` – see
`app/encounter_state.py`. The queue card names the triggering device by its Home
Assistant friendly name (`trigger_entity.friendly_name`, see `spawn.source_label`);
the raw entity ID is still in the card's tooltip.

**Auto-battle** ("Auto-battle" on the queue card): one click, your team battles the
Pokémon down with the same engine (EXP, EVs, money) - it never catches. Needs the
household switch `auto_battle_enabled` (admin, off by default).

**Auto rules** (`app/auto_rules.py`, `api/auto_rules.py`, **Settings → Auto rules**): every
player keeps an ordered list of IF/DO rules (table `auto_rule`, max. 50). For an encounter the
**first enabled rule whose conditions all match** decides; without a match it stays in the
queue. Rules run on "Run rules" in the queue and - when the player turns on
`app_user.auto_rules_enabled` - right after a spawn (`spawn.process_events` →
`auto_rules.run_on_spawn`, true offline play). Both need `auto_battle_enabled`.

* **IF** (all set conditions must hold; a list matches any value): level range, shiny,
  generation, rarity tier, types (any/all), evolution stage, hidden ability, nature, species
  only/never, caught before / caught at most N times, needed for the living Pokédex
  (`evolution.living_dex_need`, see Team tab), full IVs unlocked
  (`economy.species_ivs_maxed`), IV % of this individual, IVs better than the best owned one of
  the species, catch chance ≥ N % with a given ball, trigger entity, biome, team HP ≥ N %, level
  advantage, super-effective move available, money ≥ N, balls in bag ≥ N, free team slot.
* **DO** - `mode`: *defeat* (battle until it faints), *catch* (weaken with moves that won't
  knock it out, throw once the chosen ball reaches `chance_min`; a miss costs the turn; a catch
  gives EXP + catch rewards like a manual catch, no win money/EVs), *throw* (balls straight from
  the queue, no battle), *flee*, *skip* (leave it to the player, stops evaluation). Plus fighters
  (party order, fixed lineup incl. box Pokémon, best type matchup first, lowest level first),
  auto heal first below N % team HP (works without the Auto heal switch), ball strategy
  (specific, own order, cheapest reaching `chance_min`, best chance; never-use list), max. balls,
  what to do on a failed catch (leave in queue / let go / beat it), auto-buy a ball below N up
  to M keeping a money reserve, send catches to the box, lock the catch (`lock`: as in the player's settings, always, or if
  the living Pokédex needs it – only ever adds a lock), Home Assistant notification
  (`persistent_notification.create`, sent by the poller).
* **Guards:** shiny, legendary and mythical Pokémon are never let go and the Master Ball is
  never thrown unless the rule allows it (`allow_flee_special`, `allow_master_ball`); auto-buy
  respects the player's daily budget (`app_user.auto_rules_daily_cap`, 0 = none).
* "Test against my queue" (`GET /api/auto-rules/preview`) is a dry run; every action is logged
  (`auto_rule_log`, last 200 per player) and battles carry the rule name in the history
  (`battle_log.rule_name`). Templates: shiny hunter, catch new species, fill the living Pokédex,
  train on commons.
* Migration: the retired switches `auto_battle_also_catches` / `auto_battle_only_known_species`
  became one "Auto-catch" rule per player for households that had auto-catch on.

**Manual battle** ("Battle" on the queue card): turn-based with
attack, switch (costs the turn), ball, or flee. Speed (including priority)
determines turn order; accuracy, critical hits, STAB,
type effectiveness, and physical/special count. The battle claims the
encounter – for a shared encounter, no one else in the household can battle or
throw at it during this time.
Win → EXP, encounter done for this player. Flee (`run`) → the Pokémon returns
to the queue; damage is tracked per player, for all others it starts
undamaged. Loss (your active Pokémon faints) → unlike the mainline games
there's no forced switch to the next team member - the fight ends right there
and the wild Pokémon flees, done for this player for good (same as a roaming
legendary escaping, see below). Unused battles free the encounter after
`battle_timeout_minutes`. Auto-battle uses the same engine.

**Roaming legendaries** (`app/roaming.py`: Raikou, Entei, Suicune, Latias, Latios, Tornadus,
Thundurus, Landorus) run away instead of waiting for you:

* **Queue:** the first throw that doesn't catch it makes it flee – the attempt is over for you
  and it leaves your queue. So in the queue you get exactly one ball: it has to catch on that throw.
* **Battle:** on its turn it escapes at once, so a faster Pokémon gets to attack first. A battle
  ends with `fled` as soon as it gets a turn it can use. Only two things keep it there: sleep
  (**Hypnosis** – it loses its turns while asleep) or a trap (**Mean Look**, **Spider Web**).
  Paralysis or freezing can also stop a turn at random.
* **Damage sticks:** a roamer that fled hurt is noted per player and species (table
  `roamer_wound`). The next spawn of that species starts from the same HP fraction.
* Auto rules' throw and catch modes follow the same escape rules.

**Moves:** Which Pokémon learns which move, and when, follows the main games. The learnsets
in `app/data/learnsets.json` (built by `scripts/build_learnsets.py` from Pokémon Showdown's data,
which follows Bulbapedia) hold, for every species and every move of `app/data/moves.json`, the
**level-up** level, the **TM** moves and the **egg** moves – Scarlet/Violet (gen 9) for every species
in it, otherwise the newest generation it appeared in (gen 8/7). Moves learned on evolution or only
via the Move Reminder count as level 1; egg moves are listed for the base stage only; only base forms.
Off-type moves are normal (Pikachu learns Iron Tail at Lv. 28), and a move outside a species'
learnset is never learned naturally (Mean Look: Gastly at Lv. 8, Umbreon at Lv. 50, Snivy only as an
egg move – not Pikachu). Each Pokémon deterministically gets up to four battle moves from its
level-up moves (`moves.moveset_for`: strongest damaging move per own type + normal, at most one status
move of its own types, then the next best damaging moves); the Team tab lists every learned move
with its level and the moves still to come. A species without any damaging level-up move yet gets
its earliest one ahead of time (Magikarp: Tackle), one without any at all (Abra, Ditto, Metapod …)
the best move of the old "own types + normal" rule, so it can still fight. A move's `min_level` is
only the level a TM needs. Egg moves: an egg inherits every egg move of its species that either
parent knows (one of its four battle moves), up to four (`breeding._inherit_egg_moves`,
`pokemon_egg.egg_moves` → `owned_pokemon.egg_moves`); the hatchling starts with them. Existing saves
drop battle moves the Pokémon can't know any more on startup (`db._migrate_real_learnsets`; TM and
egg moves stay). The AttackDex shows for every move which seen Pokémon learn it and how. Beyond damage the catalog covers stat stages
(Swords Dance, Growl, self-lowering moves like Overheat), status conditions, healing, drain, recoil,
flinch, high crit rate and priority moves. Active stat stages show up as green (+) / red (−) tags
next to the status tag of both fighters in battle (tooltip: stage and multiplier); the move buttons
list a move's stat changes and healing.

**TMs and gym rewards** (`app/tms.py`, `app/rewards.py`, catalog `app/data/tms.json`):
every gym leader hands out a TM the first time you beat them – the one they hand out in
the main games wherever that move exists here, otherwise the closest move of the same type
and damage class (`canonical` in `tms.json` documents the original). Alola has island
trials instead of gyms and therefore awards none.

* A TM teaches **its move to a compatible Pokémon**, off-type included, from the move's
  `min_level` on – the level gate stays, so a TM never hands a level 5 Pokémon a 110 BP attack.
  Compatibility follows the games (`tms.compatible`): if the move is a TM in the species'
  learnset generation (Scarlet/Violet, or the newest game the species was in), the species must
  learn it by TM there (`tm` in `app/data/learnsets.json`; Pikachu learns Surf, not Aerial Ace).
  For a move that is no TM in that generation, the species' PokeAPI TM learnset decides: every
  move it learns by learn method `machine` in any main-series game is stored in
  `species.tm_moves` (JSON list of move keys; `NULL` = not fetched yet, `[]` = learns none).
  New species get it while being seeded (`seed._fetch_one`), existing databases are filled in
  by a background backfill at startup (`tms.start_background_backfill`, one request per
  species, `tms.backfill`); the migration is `db._migrate_tms`.
  A type-based approximation (`compatibility` in `tms.json`: the move's type must be one of
  the Pokémon's own types or in the `type_affinity` list of one of them, and a damaging move
  needs a base Attack / Sp. Atk of at least `min_offense`, mythical Pokémon learn everything)
  only stands in while a species has no learnset yet (offline start, fallback Pokédex) and for
  the substitute TMs listed under `type_based` in `tms.json` – their move is no TM in any
  main-series game (Razor Leaf, Spark, Aurora Beam, Icicle Crash, Ancient Power, Dragon Rush),
  so PokeAPI knows no learner. Moves taught before this rule stay learned. Incompatible
  Pokémon are not offered in the Bag and are rejected by the API (400).
* TMs are **single use**: teaching consumes one (`inventory`), which is why a TM awarded by
  several regions (Rock Tomb) is worth beating each of those gyms for. The shop's TM section
  follows **Scarlet/Violet** (the generation with the most TMs, where every TM is craftable):
  every SV TM whose move our battle engine knows (`data/moves.json`) is **buyable**, gym TMs
  included, and Department-Store/Poké-Mart TMs of older games (Celadon, Goldenrod, Alola:
  Submission, Rock Smash, Steel Wing, Rock Polish, Headbutt, Iron Tail, …) are buyable too.
  Price (`price` in `app/data/tms.json`) = move power × 100, so a stronger attack always costs
  more; self-boosting/healing status moves 10,000, status-inflicting ones 4,000, other status
  moves 5,000. Only the substitute TMs without a real TM (Razor Leaf, Spark, Icicle Crash,
  Ancient Power, Dragon Rush) plus Fury Cutter and Roost stay gym-only. SV TMs whose move the
  engine lacks (Protect, weather, screens, Hyper Beam, U-turn, …) are not sold.
* Taught moves live in `owned_pokemon.taught_moves` and simply join the pool the player picks
  their four battle moves from (Team tab); a free slot is filled immediately.
* Apply a TM in the **Bag** tab (`POST /api/pokemon/{id}/use-item`, own category "TMs").
* Each badge also pays out **balls**, scaled to how far into the region that gym is:
  2 Poké Balls for the first gym, 5 Poké + 2 Great for the fourth, 5 Poké + 4 Great + 2 Ultra
  for the sixth, up to 5 Poké + 5 Great + 5 Ultra Balls for the eighth (Galar's ninth and
  tenth gym pay out like an eighth). Never more than the three regular tiers, at most five each.
* Clearing a **whole region** (all its gym leaders, Elite Four and champion) adds one
  **Master Ball**; clearing every region still earns the Shiny Charm ("League Champion").
* Rewards are handed out **only on the first unlock** of the achievement
  (`achievements.on_trainer_defeated`) – a rematch pays prize money, never the items again.

**Healing:** HP regenerates at `heal_regen_percent_per_hour` per hour (+1 percentage point
for every Elite Four member the player has defeated), plus
"Heal team" with cooldown `heal_team_cooldown_minutes` (-1 minute for every Elite Four
member or Champion the player has defeated, floor 0 minutes) – counted per player
(`app_user.last_team_heal_at`), so no one blocks anyone else.

**Healing items** (shop category "Medicine", `app/medicine.py`): potion (+20 HP),
super potion (+60), hyper potion (+120), full heal (full HP), full restore (full HP + status),
revive (fainted → 50% HP), max revive (→ full HP) plus antidote, awakening, burn heal,
ice heal, and full heal. Usable in the "Bag" tab (`POST /api/pokemon/{id}/use-item`)
or in battle via "Bag" (action `item`, also in gym/league) – costs the turn,
then opponent acts. Status only exists in battle, so pure status healers only work there.
Ineffective use (full HP, potion on fainted, revive on healthy, wrong status) is rejected
without consuming the item.

**Auto heal** (`app/autoheal.py`, per player, **Settings → Gameplay**, `PUT /api/users/me/auto-heal`;
`app_user.auto_heal` / `app_user.auto_buy_potions`, both off by default): adds an "Auto heal" button to the
team view (`POST /api/team/auto-heal`) that heals the whole team from the bag with the cheapest fitting
potions – a Pokémon missing 180 HP gets a Hyper Potion + a Super Potion instead of two Hyper Potions
(lowest total price = least overheal, then fewest items; Max Potion wins when it is cheaper). Only plain
HP potions are used (Potion, Super, Hyper, Max Potion; Full Restore stays manual), only for the team, and
never for fainted Pokémon. Pokémon are processed by ascending missing HP so a small stock heals as many as
possible completely; one the stock cannot fully heal is left untouched and reported. Not available during
battles. **Auto buy used potions** (only switchable together with auto heal, turning auto heal off turns it
off too) re-buys exactly the potions an auto heal used, as far as the player's money reaches.

**EVs / effort values** (`app/evs.py`, shop category "Vitamins & Berries"): the part of a
Pokémon the player decides, in contrast to its randomly rolled IVs. Six values, one per stat;
every 4 EVs in a stat count like one more IV point in the stat formulas
(`battle.stat_hp`/`stat_other` use `floor(EV/4)`), so 252 EVs are worth +63 points at level 100.
Caps are the main-series ones and settings: `ev_max_per_stat` (252) and `ev_max_total` (510).

* **Defeated opponents give EVs** like in the main games: every species has an EV yield
  (`species.ev_yield`, JSON, taken from the `effort` of the PokeAPI `stats` while seeding – Pikachu
  gives 2 Speed, Geodude 1 Defense, …; migration `db._migrate_ev_yield`). Every own Pokémon that
  took part in a wild battle (also auto-battle) or a gym/league battle and was **still standing
  when that opponent fell** gets the yield of the opponent, capped at the limits (per stat, in
  total); a Pokémon that fainted later in the fight keeps its EVs. A gym/league battle is settled
  per opponent, so the EVs stay even if the whole team is wiped out later. The battle log shows the
  summary at the end of the fight (`evs.grant_battle_evs`). Catches (nothing is defeated), player
  battles (sparring against an AI copy would be farmable) and benched Pokémon get nothing. Setting
  `ev_battle_gain_enabled` (default on) switches it off – then EVs come from items only. Existing
  databases have no yield yet: the background backfill (`seed.start_name_en_backfill`, same one as
  for `name_en`) fetches it from the PokeAPI on the next start; until then such species yield
  nothing. The small offline fallback Pokédex ships its own yields.
* **Items** bought with money train EVs without any battle: a **vitamin** (HP Up, Protein, Iron,
  Calcium, Zinc, Carbos, 1500 ₽) gives +10 EVs on its stat, a **wing** (200 ₽) +1 for fine-tuning
  below a cap, and a **berry** (Pomeg, Kelpsy, Qualot, Hondew, Grepa, Tamato, 500 ₽) takes 10 back
  off, so a misinvested point doesn't stay in the 510 budget forever.
* Applied in the **Bag** tab (`POST /api/pokemon/{id}/use-item`) on any own Pokémon, team or box –
  not during a battle, where a changing max HP would be confusing. An item without effect
  (stat at its cap, budget spent, berry on a stat without EVs) is rejected without being
  consumed; a gain is capped at the limits instead (the last vitamin below a cap still gives
  what fits). Like Rare Candy, an optional `quantity` applies several at once (a "Max" button
  in the Bag offers the whole stack); it only consumes as many as still have an effect, so
  bulk-using a stack past a cap never overshoots or wastes items.
* EVs live in `owned_pokemon.evs` (JSON, migration `db._migrate_evs`; NULL = all zero, so
  existing save data needs no backfill). Raising the HP EV raises max HP right away and adds
  the gained HP on top of the current HP, like a level up does.
* They belong to the Pokémon: they survive evolution and trades, and the PvP snapshot copies
  them (`pvp.snapshot_for_user`). **Team codes carry no EVs** – an imported team fights with
  zero, so a code stays short and can't smuggle in stats. Wild Pokémon, gym/league teams and
  hatched Eggs have no EVs. Lowering a limit later only stops further training; already
  trained EVs are kept. The Pokémon's detail view shows EVs, their point value and the
  remaining budget. Tests: `tests/test_evs.py`, `tests/test_ev_yield.py`.

**Held items** (`app/data/held_items.json`, e.g. leftovers, life orb,
choice band, assault vest, sitrus berry, exp share) take effect in battle. Equip/unequip
in the Team tab moves the item between inventory and Pokémon; berries and assault vest are consumed.
Setting `box_item_return` (personal, `app_user.box_item_return`, default on): a Pokémon that
is moved from the team to the box (`PUT /api/team/party`) automatically drops its held item back
into the inventory first, like unequipping it; switching the setting off leaves the item on the
Pokémon in the box instead. Toggle: `PUT /api/users/me/box-item-return`.

**Abilities** (`app/abilities.py`, catalog `app/data/abilities.json`): every Pokémon rolls one of
its species' abilities at spawn (hidden one with `hidden_ability_chance`) and keeps it. The
catalog lists every main-series ability with German/English name and description
(`scripts/build_ability_catalog.py` fills it from PokeAPI). Abilities with an `effect` work in
battle: the engine fires events on the hub `app/battle_events.py` (switch-in, damage and stat
modifiers, status/stat-drop/flinch vetoes, type immunity, hit taken, end of turn) and
`app/ability_effects.py` holds one handler per effect - e.g. Intimidate/Download on entering
the field (battle start, switch, a trainer's next Pokémon), Clear Body/Hyper Cutter/Big Pecks
against drops by the foe, Defiant/Competitive, Water/Volt Absorb, Sap Sipper, Lightning Rod,
Flash Fire, Sturdy, status immunities, contact status, Blaze & co., Guts, Levitate, Wonder Guard
(only a super-effective damaging move gets through; its own type check is unaffected). All others
carry `inactive` with the reason they do nothing here (`doubles` – 1-on-1 battles only,
`weather` – no weather/terrain yet, `overworld`, `mechanic` – a battle mechanic the engine
lacks, `todo` – implementable, not done yet); the frontend shows it under the description.
Ability state that lasts until switching out (Flash Fire's boost) is `Combatant.volatile`,
saved with the battle session. Tests: `tests/test_abilities.py`, `tests/test_battle_events.py`.

**AbilityDex** (Dex tab, next to Pokédex and AttackDex; `GET /api/abilitydex`, `app/api/abilitydex.py`):
every catalog ability with name, description and whether it works in battle (or why not).
Filterable by search (name and description), active / not active / inactive reason, a seen
species and "only my Pokémon's abilities". Per user, each ability lists only the species the
player has already **seen** that can have it (hidden ability flagged), so it never spoils
undiscovered Pokémon, plus how many of the player's own Pokémon have it. Test: `tests/test_abilitydex.py`.

**Amulet Coin** (`amulet_coin`, effect `held_items.any_holds`) doubles the prize money from a
won battle - wild, gym/league and PvP alike - as long as ANY team member holds it, like Exp.
Share it does not need to join the battle. It does not affect the fixed money reward of daily
challenges (`challenge.py`), which is a flat quest payout rather than scaled prize money.

**Mega Evolution** (`app/mega.py`, catalog `app/data/mega_evolutions.json`): all 47
Mega Evolutions with a Mega stone plus the two Primal forms (Kyogre/Groudon with the
Blue/Red Orb, same mechanic, own log line). A Mega stone is an ordinary held item
(`effect: "mega_stone"`, shop category "Mega stones") that only works on its own species –
Charizardite X does nothing on anything but a Charizard. Rules as in the main games:

* In battle, a button above the moves offers the Mega Evolution. It is voluntary and
  **does not cost the turn** (like in the games it happens before the attacks).
* Only **one Pokémon per side and battle** (`Battle.mega_used` / `foe_mega_used`).
  A trainer or PvP opponent whose Pokémon holds a matching stone mega evolves by itself
  at the start of the turn; auto-battle does the same for your team. Wild Pokémon never
  hold items and therefore never mega evolve.
* The form lasts until the end of the battle, including across a switch out, and swaps
  base stats, types, ability, name and sprite.
* It exists **only in battle**: `owned_pokemon.species_id` is never touched, the form lives
  in the battle session state like status conditions, and level ups still count for the
  base species. After the battle the Pokémon is exactly as it was; the stone stays equipped.
* Mega forms are no Pokédex entries of their own – dex number, base EXP, catch rate and
  growth rate stay those of the base species. The shop only offers stones whose species
  exists in this installation's Pokédex.

**Player versus player** (`app/pvp.py`) always runs asynchronously: you never battle
a present co-player but an AI-controlled **copy** of their team. A snapshot is taken
on start (species, level, moves, IVs, ability, held items) and used in battle; the
opponent's Pokémon, items, and HP are never changed – the copy always fights at full health,
later team changes by the opponent don't affect the ongoing battle. Your team takes damage
during battle like in gym battles but is **not** level-capped; after battle end (win or loss)
your HP is fully restored – a player battle is sparring without lasting HP cost. Reward: EXP
like in trainer battle (`trainer_exp_multiplier`) plus smaller prize money
(`pvp_money_per_level` × opponent's highest level). Challenge is via another household member's
profile; your record against this player is shown there too (`pvp_record`, only from challenger's perspective).
Only one battle can run at a time (wild, gym/league, or PvP).

**Daily challenges** (`app/challenge.py`, tab "Challenges"): every user gets three quests per
local day, drawn at random from all gym leaders, Elite Four members, champions and the other
household users' teams (rolled once, stored in `daily_challenge`, identical all day; they reset at
local midnight). The fight runs through the PvP engine, but **the opponent always fights at level
100** – your own team keeps its real level, IVs, EVs and moves, just as a full-HP copy, so nothing
is written back (no EXP, no HP cost). Picking a lineup for the quest (team + box picker) is just as
temporary: it only applies to that fight and is never written to the real party, unlike picking a
team for a gym/league or PvP fight, which does replace the real party. The easy opponent brings at
most 3 Pokémon. Tiers:

| Quest | Opponent IVs / EVs | Reward |
|---|---|---|
| 1 easy | all 0 / all 0 | 20,000 money + 10 each of Poké/Great/Ultra Ball |
| 2 normal | random, exactly two IVs at 31 / 250 EVs spread randomly | 50,000 + 15 each ball + 5 Rare Candy |
| 3 hard | all 31 / all 252 | 100,000 + 20 each ball + 10 Rare Candy |

A lost quest can be retried any number of times; the reward is paid once per quest per day.

**Team codes** (`app/team_code.py`): Your profile generates a short, copyable
code of your current team (versioned, base64url-encoded JSON with CRC32 checksum).
Anyone who enters a code sees a team preview first, then battles its copy like above.
Entered codes are treated as untrusted and strictly validated (max 6 Pokémon, known species,
level 1–100, IVs 0–31, known moves that the level allows – off-type moves stay valid, a TM
can teach them – known abilities and items, size limit);
invalid codes explain what's wrong in the message. Code battles don't count toward the record.

**Trading** (`app/trading.py`, tab "Trading"): household members swap Pokémon in three steps.
Player A picks a partner and one of their Pokémon (request); player B accepts by offering one
of their own (counter-offer) or refuses; A then accepts the final offer – only now both
Pokémon change owner, atomically – or refuses. Whoever made the last move can also withdraw
it. Only **box** Pokémon can be traded (not team members, the buddy or day care parents);
level, IVs, nature, ability, taught TMs and the held item go along, the receiver's Pokédex
counts the Pokémon as caught. Ownership is revalidated on completion, and completing a trade
cancels other open trades that involve either Pokémon. Finished trades (completed, refused,
withdrawn) can be removed from your own view and are purged automatically the day after they
were closed. The nav button shows a badge with the number of trades waiting for your decision.

**Buddy** (`app/buddy.py`, `app/api/buddy.py`, tab Team → Buddy; guide section "Buddy & Home Assistant"):
every player picks one Pokémon as buddy (Buddy view or the "Make buddy" button in the Pokémon
detail view). The add-on mirrors it into Home Assistant as `sensor.pokemon_buddy_<ha_username>`
via the Core API (no custom integration): state = level, `entity_picture` = sprite (shiny sprite
if shiny), attributes with name, species, types, artwork and step progress. It is refreshed on
every poll and immediately on change; without a buddy the entity is removed. The player also
picks an HA step or distance sensor there (the same one that hatches Eggs); every
`buddy_steps_per_level` steps (default 6500 ≈ 5 km / 3 mi) give the buddy +1 level like a Rare
Candy (max 100, evolutions stay manual). The first reading is only a baseline, counter resets
are handled, jumps over 60 000 steps are ignored, and banked steps are lost when switching
buddy. The buddy can't be traded. Dashboard card examples are in `pokemon_encounter/DOCS.md`.
**Buddy finds**: while the buddy walks, every step has a `buddy_find_chance` (default 1/3000 per
step – a probability, not a fixed interval) of finding one item on the ground. The item is drawn
from the weighted `buddy_find_pool` (default: *common* 90 % – Poké Ball, Great Ball, Potion, Super
Potion, Ultra Ball (“Hyper Ball” in German); *rare* 10 % – Rare Candy, Revive, Max Revive), goes
straight into the bag and shows up under "Found by your buddy" in the Buddy view. Both settings
are in the admin settings (pool shown read-only for now).
Tests: `tests/test_buddy.py`.

**History** (History tab, `GET /api/battles/history`): the last 20
own battles – wild, auto-battle, gym/league, and player battle – with date, opponent,
outcome, money earned, whether caught, and EXP breakdown per team member (name, EXP, level ups).
Battles from before this feature still appear in the list, just without EXP breakdown.
Only the latest 50 battles per user are kept; `battle_history.record()` prunes older
`battle_log` rows on every write (same pattern as the auto-rule log).

**Shop and bag** are divided into categories (balls, medicine, TMs,
evolution items, held items, vitamins & berries, then the rest), sorted by price within;
a quick filter above lets you filter to a single category.

**Profile** (click your name in the header): team with sprites and
levels, badges per region, champion titles, catch count and Pokédex progress,
balance and unlocked achievements, plus your team code and field for
a foreign code. Via the household bar you can view co-players' profiles – read-only
(`GET /api/users`, `GET /api/users/{id}/profile`); the challenge button is there too.

---

## Open points

Deliberately not yet decided, prepared in code as settings:

* ~~**Reward for catching.**~~ Decided: money (₽) for catches and
  won battles, scales with level and rarity, plus IV gain for already-owned Pokémon
  of the same evolution family (one random, not-yet-maxed IV per Pokémon +1–2; see
  `app/economy.py`, settings `money_*` / `iv_repeat_catch_*`). Each catch also raises
  the household-wide IV minimum of the caught species (`species.min_ivs`, starts at 5
  on every IV) the same way; new spawns never roll an IV below it, so after enough
  catches of one species every new spawn has perfect IVs. Once that floor is maxed
  (all six stats at 31), another catch of the species can no longer improve it, so
  its spawn weight is multiplied by `maxed_ivs_spawn_weight_factor` (default 0.2,
  0-1, setting group "spawns") instead of dropping out of the pool entirely - it
  stays catchable for the Pokédex, EVs and the repeat-catch bonus of less-maxed
  family members (`economy.species_ivs_maxed`, `spawn._rarity_weight`).
  Hatching an Egg (`breeding.hatch`) gives the same IV rewards as a catch - the egg's IVs are
  lifted to the species floor, one family member gains +1–2 on one IV, and the floor rises -
  but no money.
* ~~**Auto-battle and catching.**~~ Decided: the auto-battle button only battles;
  catching (and much more) is configured per player with auto rules (see above).
* **Ball balancing.** Differences between tiers are not yet balanced.
* ~~**Items and evolutions.**~~ Decided: Poké Mart (Shop tab,
  catalog in `app/data/shop.json`) for balls and evolution items (stones,
  link cable instead of trade). Apply in Bag tab. Evolution data
  comes from PokeAPI evolution chains (`app/evolution.py`).
  Level evolutions don't happen automatically: once the level is reached,
  the Pokémon menu (Team tab) shows an "Evolve" button.

---

## Development

```bash
# Test core logic without Home Assistant
python3 tests/test_core.py
python3 tests/test_economy.py
python3 tests/test_evolution.py
python3 tests/test_queue.py

# Run locally
cd pokemon_encounter
POKEMON_DATA_DIR=/tmp/pokedata \
POKEMON_HA_URL=http://homeassistant.local:8123 \
POKEMON_HA_TOKEN=<long-lived-token> \
python3 -m uvicorn app.main:app --reload --port 8099
```

API docs: `/api/docs`

### Structure

| File | Purpose |
|---|---|
| `app/poller.py` | 5-minute poll, in-memory cursor, retention |
| `app/spawn.py` | weighting, type bonus, biomes, encounter creation |
| `app/rarity.py` | rarity tiers (common … mythical) |
| `app/battle.py` | type chart, battle engine, damage, EXP, catch formula, auto-battle |
| `app/auto_rules.py` | per-player IF/DO auto rules: conditions, defeat/catch/throw actions, guards, auto-buy |
| `app/battle_session.py` | manual battle: state, actions, encounter claim |
| `app/pvp.py` / `app/team_code.py` | battle against copy of foreign team, team codes |
| `app/trading.py` | Pokémon trades between household members (request → offer → accept) |
| `app/moves.py` / `app/held_items.py` | moves/movesets, held items (data in `app/data/`) |
| `app/mega.py` | Mega Evolution / Primal forms: stone → form, battle-only form swap |
| `app/healing.py` | HP regeneration and "heal team" |
| `app/autoheal.py` | Auto heal: cheapest potion combination per Pokémon, optional re-buy |
| `app/evs.py` | effort values: battle yield of defeated opponents, vitamins, wings and berries |
| `app/ha_client.py` | Core API via supervisor token (or a long-lived token outside the add-on) |
| `app/auth.py` | user detection: Ingress header, HA login, trusted proxy header or dev (`POKEMON_AUTH_MODE`) |
| `app/api/login.py` | login with an HA account (OAuth) for the standalone container |
| `app/backup.py` | database snapshots (periodic in the container, download for admins) |
| `entrypoint.py` | container start for add-on and standalone image: config check, uvicorn |
| `app/db.py` | SQLite schema and access |
| `app/seed.py` | Pokédex from PokeAPI, fallback dataset |
| `app/api/` | REST endpoints (including `users.py`: profile & starter choice) |
| `www/index.html` | web app (single file, vanilla JS) |

### Add new generation

Update `GENERATION_BOUNDS` in `app/seed.py` and call `seed_species(limit=…)`
again. The upsert is idempotent, existing data is preserved.

---

## License

The source code is released under the [MIT License](LICENSE). This does not cover
Pokémon-related names, sprites and artwork, which belong to their respective owners
(see the disclaimer at the top).
