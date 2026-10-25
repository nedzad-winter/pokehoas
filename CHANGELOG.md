# Changelog

## 1.2-beta

- New setting **Auto-lock catches the living Pokédex needs** (Settings → General, off by
  default): a catch starts locked while its evolution line still needs it to be owned
  complete at the same time in team or box. The line is worked out per evolution tree –
  Pokémon only evolve forward, gender-bound evolutions (Kirlia → Gallade) count per gender –
  so with two Eevee, a Flareon and an Umbreon another Eevee locks (five still needed),
  while a second Flareon stays unlocked.
- Auto rules: new condition **Needed for the living Pokédex**, new action **Lock the catch**
  (as in my settings / always / if the living Pokédex needs it) and a new template
  **Fill the living Pokédex**.
- Fix: the Admin tab under Settings stayed blank (Triggers and Zones & Biomes views were
  missing from the build).

## 1.1-beta – first public release

- Home Assistant events (motion, door/window, lights & switches, zones, …) spawn wild
  Pokémon into each Home Assistant user's own queue.
- Turn-based battles and catching in a web app (Ingress): type chart, STAB, status effects,
  abilities, moves, held items, Mega Evolution, auto-battle and per-player auto rules.
- Pokédex for Generations 1–9 with rarity tiers, shinies, IVs/EVs, breeding and eggs.
- Gyms & League for nine regions, fought in order (gym 1 → Elite Four → champion), with
  badges, TMs and per-player difficulty settings; clearing a region unlocks new spawns.
- Multi-user: separate team, bag, Pokédex and money per HA user; trading and PvP against
  an AI copy of another player's team.
- Poké Mart, daily challenges, achievements, battle history.
- Admin panel for trigger management, player editing and game settings.
- UI in English, Deutsch, Español, Français, Italiano, 日本語, 한국어 and Polski.
