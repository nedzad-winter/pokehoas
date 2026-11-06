# Changelog

## 1.4-beta

A big thank you to **@vicdilou**, who contributed most of this release: PokeHOAS now speaks
your language far beyond the menus. Merci beaucoup!

- Translations (by @vicdilou): species, ability, move, type, nature and biome names, plus
  ability descriptions, now appear in the player's language (es, fr, it, ja, ko, and German
  nature names). This covers team and box cards, the Pokémon detail sheet, search filters,
  the zone form and the battle log. Search ignores accents and upper/lower case.
- Header (by @vicdilou): a globe language picker on the right that lists every language in
  its own language. **Poll now** shows a short toast instead of a blocking message.
- Queue (by @vicdilou): the auto-rules now sit in a slim full-width bar above the encounter cards.
- Box (by @vicdilou): new **Duplicates only** filter.
- Fix: a battle left behind by a dropped connection or a closed tab no longer blocks starting
  a different battle type (gym/league, PvP, wild) until it times out.
- Fix: hatched Pokémon follow the same auto-lock rules as caught ones (living dex, first
  catch, shiny, legendary), so a hatchling the living dex still needs can't be released by
  accident.
- Fix: catches thrown straight from the queue now show up in the History tab.
- Fix: locked Pokémon show the lock badge in the Team tab's party list too.
- Fix: the Pokédex sprite-style picker only offers games from the species' own generation
  onward.
- Fix: the standalone Docker image builds for armv7 again.

## 1.3-beta

- PokeHOAS now also runs **without Supervisor** (Home Assistant Container): a standalone
  Docker image (`ghcr.io/nedzad-winter/pokehoas`) configured with environment variables,
  see [DOCKER.md](DOCKER.md). Users log in with their Home Assistant account; a reverse
  proxy with forward auth (Authelia, Authentik, …) is supported as well.
- Admins can download a consistent backup of the database (**Settings → Admin → Download
  backup**). The standalone image additionally keeps daily snapshots.
- Security: the identity source is now an explicit auth mode. Ingress headers are only
  trusted in the add-on, and the no-login dev mode is never picked by accident.
- Pokédex: the status filter has a new option **Not in team or box**, which lists the species
  you don't own a copy of right now. Handy for spotting gaps in the living Pokédex.
- Poké Mart: you can buy up to 999 of an item at once (was 99).
- Fix: confirmations and messages now use the app's own dialog instead of the browser's.
  A browser that blocks pop-ups ("don't let this site prompt you again") or the Home
  Assistant companion app could suppress them, so buttons such as **Accept trade**,
  release or evolve silently did nothing.
- Fix: the Training card now follows the **Dojo Master** achievement, so an achievement
  granted by an admin unlocks it right away.

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
