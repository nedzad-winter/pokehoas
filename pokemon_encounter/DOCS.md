# PokeHOAS

Random Pokémon encounters triggered by Home Assistant events.

## Getting started

1. Start the add-on and open via the sidebar panel.
2. On first open: choose a starter (see below).
3. Motion sensors, door/window sensors and lights & switches automatically
   become triggers (see [Automatic Triggers](#automatic-triggers)).
   Search for additional entities in the **Trigger** tab → *add*.
4. Click **Poll now** instead of waiting five minutes.
5. Encounters appear in the **Queue** tab.

## Multiple players (Multi-user)

Each Home Assistant user who opens the panel automatically gets their own
team, inventory, Pokédex progress – no separate login needed, HA already
authenticated – **and their own encounter queue**. A trigger event creates
one encounter per registered player, each with its own species, level and
shiny roll based on that player's region lock and rarity bonus: nobody
profits from someone else's league progress, and nobody races anyone else
for the same Pokémon. Encounters created before this per-player split (and
admin spawns) stay household-wide instead: every eligible player gets their
own attempt at the same encounter.

Requirement is access via the panel in the HA sidebar (Ingress) – only there
does the add-on know the logged-in user. Via the optional direct port
(see `ports_description`) all calls run under a shared fallback profile.

## Choosing a starter

Each new player chooses a starter once – across generations, not just Kanto:
all 27 grass/fire/water starters from generations 1–9 are available.
The starter goes to team slot 1 (level 5) and comes with a small starting
inventory of balls (setting `starter_inventory`). The choice is final – no
second starter is planned.

Together with the starter each player also picks their **region lock** (see
*Region lock* below). It can be changed later under Settings → Gameplay.

## Add-on options

| Option | Meaning |
|---|---|
| `log_level` | Log verbosity |
| `poll_interval_minutes` | Interval between queries to Home Assistant (default 5) |
| `admin_users` | Additional admins (HA username or user ID), see *Admin panel* |
| `http_port` | Port of the web app; irrelevant when using Ingress |
| `trigger_categories` | Which entity categories automatically become triggers (multi-select, see below) |
| `extra_entities` | Additional entity IDs that should always be triggers |
| `exclude_entities` | Entity IDs (wildcards `*`/`?` allowed) that never automatically become triggers |

Everything else is set in the web app – see **Settings** tab.

## Automatic triggers

In the **Configuration** tab of the add-on, choose under `trigger_categories`
which types of entities are allowed to automatically trigger spawns:

| Category | Entities | Triggers on | Spawn chance | Default |
|---|---|---|---|---|
| `motion` | `binary_sensor` with device_class motion / occupancy / presence | `on` | 0.25 | ✔ |
| `door_window` | `binary_sensor` with device_class door / window / opening / garage_door | `on` (open) | 0.15 | ✔ |
| `lights_switches` | all `light.*` and `switch.*` | `on` | light 0.05, switch 0.03 | ✔ |
| `media_player` | `media_player.*` | `playing` | 0.10 | |
| `vacuum` | `vacuum.*` | `cleaning` | 0.50 | |
| `climate` | `climate.*` | heating/cooling/auto… | 0.20 | |
| `cover` | `cover.*` | `open` | 0.10 | |
| `lock` | `lock.*` | `unlocked` | 0.15 | |
| `fan` | `fan.*` | `on` | 0.10 | |
| `person` | `person.*` | any zone change | 0.30 | |

Lights and switches toggle often – that's why the low chance; they come with
an electric type bonus. Global cooldown and daily limit (Settings tab) apply additionally.

Sync runs on startup and on each poll:

- New matching entities are created with values from the table.
  Entities that are currently `unavailable` are added once they're reachable.
- **Your customizations are preserved**: spawn chance, states,
  type bonus, cooldown, and active/inactive status of an existing trigger are
  never overwritten by the sync.
- If you uncheck a category or exclude an entity, its automatic trigger
  disappears (along with customizations).
- Entities manually added in the **Trigger** tab are never touched.

Automatically skipped are configuration and diagnostic entities
(e.g. "status LED" switches of network devices, window-open detection
of thermostats), entities hidden or disabled in HA (e.g. the original
switch behind a "switch as light" helper), and groups and template binary sensors
that only aggregate other sensors – otherwise an event would be counted twice.

`extra_entities` bypasses these filters. If an additional entity doesn't fit
any category, it starts with state `on` and chance 0.20 – e.g. for a status
sensor, enter the appropriate states in the **Trigger** tab.

Example to exclude Docker container switches and one sensor:

```yaml
exclude_entities:
  - switch.*_container
  - binary_sensor.offene_fenster_tueren
```

An empty `trigger_categories` list disables automatic triggers entirely.
Changes to options take effect after restarting the add-on.

## Repeat catch (IV gain)

When you catch a species whose evolution family you already own (e.g. a Pichu
while Pikachu or Raichu is on your team/in the box), your existing family members
get slightly stronger: exactly **one** random IV value goes up by 1–2 points
(max 31) on each. Values already at 31 are skipped; if all six values are at 31,
there's no gain. The IVs of the newly caught Pokémon don't matter, it stays unchanged.

You can disable this in settings (`iv_repeat_catch_enabled`) and adjust the range
(`iv_repeat_catch_min_gain` / `iv_repeat_catch_max_gain`, each 1–31, minimum ≤ maximum).

## Gym & League

In the **Gym & League** tab you can challenge all gym leaders, Elite Four members,
and champions of all regions anytime (trainers in `app/data/trainers.json`).
To prevent you from just overlevel-ing, any team Pokémon above the opponent's highest
level is capped to that level for the battle (HP proportionally, moves above the level
are removed). Lower Pokémon are not boosted. The real level stays unchanged.

EXP is earned for each defeated opponent Pokémon (× `trainer_exp_multiplier`),
even on loss. Like wild battles, now fainted team members also get their share.
A win earns prize money (`trainer_money_per_level` × highest opponent level × 1 / 1.5 / 2
for gym leader / Elite Four / champion). Giving up counts as loss.

League progress is **per player** – each fights their own gym battles. Which generations
spawn in a player's own wild spawn pool depends on their **region lock**:

| Region lock | Spawn pool |
|-------------|------------|
| `unlocked` – Unlocked | Every generation spawns from the start. Leagues still pay rewards and raise the rarity bonus. |
| `region` – Region (default) | Generation 1 (Kanto) spawns from the start; clearing a region's league (all gym leaders, Elite Four, champion) unlocks that region's own generation (Johto → Gen 2, Hoenn → Gen 3, …). |
| `previous_region` – Progression | Generation 1 spawns from the start; clearing a region's league unlocks the **next** generation (Kanto → Gen 2, Johto → Gen 3, …). |

The lock is chosen when picking the starter and can be changed under Settings →
Gameplay (or `PUT /api/users/me/region-lock`). It is per player like the queue – nobody
profits from someone else's league progress. Players who existed before the option was
introduced were set to `region`, which is exactly how the game worked before. Switching
never removes Pokémon you already own; it only changes what can spawn for you.

## Training

Once you have raised **101 Pokémon to level 100** (achievement "Dojo Master"), every Pokémon's
summary (Team tab → tap a Pokémon) gets a **Training** card. "Start training" lets exactly that
Pokémon spar – from the team or straight from the box, without moving it into the team – against
six level-100 Pokémon that its moves hit super effectively (at least ×2, e.g. Flying or Water
Pokémon for a Pokémon with only Thunder). The sparring partners only know Splash and never hurt you.
Training grants **EXP only** – no money, no EVs. Rematches are free.

## Achievements

Each defeated trainer unlocks an achievement (Achievements tab). Whoever defeats
them all becomes **League Master** and earns the **Shiny Charm**. As long as a
player owns it, the shiny chance of *that player's own* spawns and Eggs is
multiplied by `shiny_charm_multiplier` (default 2) – since everyone spawns
into their own queue, one player's Shiny Charm doesn't affect anyone else's.

## Buddy in Home Assistant

Each player can pick **one** Pokémon as their buddy. The add-on mirrors it into Home
Assistant as `sensor.pokemon_buddy_<ha_username>` – no custom integration needed, the
add-on writes the state through the Core API it already has access to
(`homeassistant_api: true`).

* **State** = level (numeric, so history graphs and automations work).
* **`entity_picture`** = sprite URL (shiny sprite for shiny buddies).
* **Attributes**: `pokemon_name`, `species`, `species_id`, `level`, `shiny`, `types`,
  `artwork_url` (official artwork), `steps`, `steps_per_level`, `steps_to_next_level`,
  `progress_percent`, `steps_total`, `step_sensor`.

The sensor is refreshed on every poll (default every 5 minutes) and immediately when you
change your buddy. Limitation of this approach: the entity has no `unique_id`, so it
can't be renamed in the HA UI, and after an HA restart it's missing until the next poll.

### Choosing your buddy in the app

* **Team → Buddy**: shows your buddy (artwork, level, walking progress, the HA entity id),
  lets you pick the step sensor and lists all your Pokémon – tap one to make it your buddy.
  "Remove buddy" clears it and deletes the HA entity.
* **Pokémon detail view**: the "Make buddy" / "Remove buddy" button and a 🐾 badge for the
  current buddy.
* The **Guide** tab has a "Buddy & Home Assistant" section that explains all of this to players.
* The step sensor list only offers step and distance sensors (unit `steps`, device class
  `distance`, or a `…step…` entity without another unit). It is the same sensor that hatches
  your Eggs, so picking it in either place sets both.
* If Home Assistant can't be reached when you change your buddy, the choice is still saved
  and the app tells you; the sensor catches up on the next poll.

API (also used by the app – see `/api/docs`):

| Request | Purpose |
|---|---|
| `GET /api/buddy` | your buddy, step progress, HA entity id |
| `PUT /api/buddy` `{"pokemon_id": 12}` | set buddy (`null` = remove, also removes the HA entity) |
| `GET /api/buddy/sensors` | step/distance sensors found in HA |
| `PUT /api/buddy/sensor` `{"entity_id": "sensor.pixel_steps_sensor"}` | count steps from this sensor (`null` = off) |

### Levelling by walking

Pick a step sensor (e.g. the Companion App's `sensor.<phone>_steps_sensor` on Android or
`sensor.<phone>_steps` on iOS). Every `buddy_steps_per_level` steps (default **6500 ≈ 5 km
/ 3 mi**) give the buddy +1 level (like a Rare Candy; max 100). Internally everything is
counted in steps, so no unit setting is needed; distance sensors (`m`, `km`, `mi`, `ft`,
`yd`) are converted at 0.77 m per step. Details:

* The first reading after choosing a sensor is only the baseline – nothing is credited
  retroactively.
* If the counter drops (daily reset at midnight, phone reboot), the new value counts as
  the steps walked since the reset.
* Jumps over 60 000 steps between two polls are treated as sensor glitches and ignored.
* Banked steps belong to the current buddy and are dropped when you switch buddies.
* Level evolutions are not automatic – the "Evolve" button appears in the Pokémon menu.

### Dashboard cards

All examples use `sensor.pokemon_buddy_alice` – replace `alice` with your own entity id
(shown in the app under **Team → Buddy**). Paste them via *Edit dashboard → Add card →
Manual*. Checked against Home Assistant 2026.10.

Two things to know:

* `entity_picture` is the small 96 px game sprite; the attribute `artwork_url` is the large
  official artwork. Cards that show the entity picture need `show_entity_picture: true` –
  without it the tile card shows the generic Poké Ball icon and the picture-entity card
  refuses to load ("No image source configured").
* After a Home Assistant restart the sensor is missing until the add-on's next poll (up to
  5 minutes by default), so cards show "Entity not available" for a moment.

#### Tile card

Compact: sprite, name and level.

```yaml
type: tile
entity: sensor.pokemon_buddy_alice
show_entity_picture: true
```

#### Picture entity card

Sprite as a picture with name and level underneath.

```yaml
type: picture-entity
entity: sensor.pokemon_buddy_alice
show_entity_picture: true
fit_mode: contain
aspect_ratio: "1"
```

#### Walking progress

Level gauge and progress towards the next level, side by side:

```yaml
type: horizontal-stack
cards:
  - type: gauge
    entity: sensor.pokemon_buddy_alice
    name: Level
    min: 1
    max: 100
  - type: gauge
    entity: sensor.pokemon_buddy_alice
    attribute: progress_percent
    name: Next level
    unit: "%"
    min: 0
    max: 100
```

#### Buddy card (markdown)

Official artwork, name (✨ for shinies), level, types, a progress bar, the steps left and
the total distance walked together. Shows a hint while no buddy is set.

```yaml
type: markdown
content: |
  {% set e = 'sensor.pokemon_buddy_alice' %}
  {% if has_value(e) %}
  {% set a = states[e].attributes %}
  {% set p = a.progress_percent | int(0) %}
  <img src="{{ a.artwork_url }}" width="140">

  ## {{ a.pokemon_name }}{{ ' ✨' if a.shiny }}
  **Lv. {{ states(e) }}** · {{ a.types | map('capitalize') | join(' / ') }}

  {{ '▰' * (p // 10) }}{{ '▱' * (10 - p // 10) }} {{ p }} %
  {% if a.steps_to_next_level is not none %}
  {{ '{:,}'.format(a.steps_to_next_level) }} steps to Lv. {{ states(e) | int + 1 }}
  {% endif %}

  <font size="2">Walked together: {{ '{:,}'.format(a.steps_total) }} steps ≈ {{ (a.steps_total * 0.77 / 1000) | round(1) }} km</font>
  {% else %}
  No buddy chosen yet – pick one in the PokeHOAS app (Team → Buddy).
  {% endif %}
```

#### Household leaderboard (markdown)

Lists every player's buddy, highest level first. It finds all `sensor.pokemon_buddy_*`
entities itself, so it needs no editing and picks up new players automatically.

```yaml
type: markdown
title: Buddies
content: |
  | | Trainer | Buddy | Lv. | Next level |
  |:-:|---|---|:-:|---|
  {% for s in states.sensor
       | selectattr('entity_id', 'match', 'sensor.pokemon_buddy_')
       | selectattr('state', 'is_number')
       | sort(attribute='attributes.level', reverse=true) -%}
  {% set p = s.attributes.progress_percent | int(0) -%}
  | <img src="{{ s.attributes.entity_picture }}" width="48"> | {{ s.entity_id | replace('sensor.pokemon_buddy_', '') | title }} | {{ s.attributes.pokemon_name }}{{ ' ✨' if s.attributes.shiny }} | **{{ s.state }}** | {{ '▰' * (p // 10) }}{{ '▱' * (10 - p // 10) }} {{ p }} % |
  {% endfor %}
```

#### Level-up notification (automation)

Not a card, but handy: a push message whenever the buddy gains a level (walking, battles or
items). Switching to a different buddy doesn't trigger it. Replace `notify.notify` with
your phone's `notify.mobile_app_…` action.

```yaml
alias: Buddy level up
triggers:
  - trigger: state
    entity_id: sensor.pokemon_buddy_alice
    not_from: [unavailable, unknown]
    not_to: [unavailable, unknown]
conditions:
  - condition: template
    value_template: >
      {{ trigger.from_state is not none
         and trigger.from_state.attributes.species_id == trigger.to_state.attributes.species_id
         and trigger.to_state.state | int(0) > trigger.from_state.state | int(0) }}
actions:
  - action: notify.notify
    data:
      title: "{{ trigger.to_state.attributes.pokemon_name }} reached Lv. {{ trigger.to_state.state }}!"
      message: >
        {% set left = trigger.to_state.attributes.steps_to_next_level %}
        {{ 'Max level reached!' if left is none else left ~ ' steps to the next level.' }}
```

## Admin panel

The **Admin** tab is only visible to admins – and only they can call endpoints
under `/api/admin/…` (checked server-side, HTTP 403 for everyone else). You're an
admin if you're an administrator or owner in Home Assistant (queried via HA WebSocket
API, briefly cached), or listed in the `admin_users` option. In the panel you can
spawn Pokémon into the queue or give them directly to players, edit teams
(level, IVs, nature, slot, HP, …) or change other players' inventory, Pokédex and
starter choice, or wipe their save data.

Local development without `SUPERVISOR_TOKEN`: the dev user is admin unless
`POKEMON_DEV_ADMIN=0` is set.

## Data

`/data/pokemon.sqlite3`. Automatically backed up by the supervisor backup.

## Recommended triggers

| Entity | Spawn chance | Type bonus |
|---|---|---|
| Motion sensor hallway/living room | 0.25 | – |
| Light switch | 0.05 | `{"electric": 4.0}` |
| AC / heating | 0.20 | `{"fire": 2.5, "ice": 2.5}` |
| Vacuum robot | 0.30 | `{"steel": 3.0}` |
| Washing machine (consumption sensor) | 0.30 | `{"water": 3.0}` |

With many motion sensors, prefer lowering spawn chance over raising global
cooldown – otherwise one hallway sensor blocks all other rooms.
