# PokeHOAS without Supervisor (Docker)

The add-on needs Home Assistant OS or Supervised. If you run **Home Assistant
Container** (or HA in Kubernetes, a VM without Supervisor, …) you can run PokeHOAS as a
normal Docker container next to it. It is the same app as the add-on; only three things
differ:

| | Add-on | Standalone container |
|---|---|---|
| Talks to HA via | Supervisor token, automatic | `POKEMON_HA_URL` + long-lived token |
| Knows who you are via | Ingress headers, automatic | Login with your HA account (or a reverse proxy) |
| Backups | part of every HA backup | daily snapshots in the data volume + download in the admin panel |
| Configuration | add-on Configuration tab | environment variables |

## Quick start

1. In Home Assistant, open your **profile → Security → Long-lived access tokens** and
   create a token. The user must be an **administrator** (PokeHOAS reads the entity
   registry and the user list).
2. Copy [`docker-compose.example.yml`](docker-compose.example.yml) to
   `docker-compose.yml`, fill in `POKEMON_HA_URL`, `POKEMON_HA_TOKEN` and
   `POKEMON_PUBLIC_URL`.
3. `docker compose up -d` and open `POKEMON_PUBLIC_URL` in the browser.
4. Click **Log in with Home Assistant**. You land on your normal HA login page and come
   back logged in. Every HA user gets their own team, Pokédex and bag, as in the add-on.

```bash
docker run -d --name pokehoas -p 8099:8099 -v ./pokehoas:/data \
  -e POKEMON_HA_URL=http://192.168.1.10:8123 \
  -e POKEMON_HA_TOKEN=<token> \
  -e POKEMON_PUBLIC_URL=http://192.168.1.10:8099 \
  ghcr.io/nedzad-winter/pokehoas:latest
```

The image is published for `amd64`, `arm64` and `armv7`. To build it yourself:
`docker build -f pokemon_encounter/Dockerfile.standalone -t pokehoas pokemon_encounter`.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `POKEMON_HA_URL` | – (required) | How the **container** reaches HA, e.g. `http://192.168.1.10:8123` or `http://homeassistant:8123` in the same compose network. With `network_mode: host` on both, `http://127.0.0.1:8123`. |
| `POKEMON_HA_TOKEN` | – (required) | Long-lived access token of an HA administrator. |
| `POKEMON_AUTH_MODE` | `ha_oauth` | `ha_oauth`, `trusted_header` or `dev` (see below). |
| `POKEMON_PUBLIC_URL` | derived per request | The URL you open PokeHOAS with. Used for the login redirect and cookies. Set it. |
| `POKEMON_HA_EXTERNAL_URL` | `POKEMON_HA_URL` | How the **browser** reaches HA, if different (e.g. container uses the LAN IP, browser uses `https://ha.example.com`). |
| `POKEMON_ADMIN_USERS` | – | Extra admins (HA user names or IDs, comma separated). HA administrators are admins anyway. |
| `POKEMON_POLL_INTERVAL_MINUTES` | `5` | 1–60. |
| `POKEMON_TRIGGER_CATEGORIES` | `motion,door_window,lights_switches` | Same as the add-on option `trigger_categories`. Empty = no automatic triggers. |
| `POKEMON_EXTRA_ENTITIES` / `POKEMON_EXCLUDE_ENTITIES` | – | Same as the add-on options, comma separated, `*` patterns allowed for exclusions. |
| `POKEMON_BACKUP_INTERVAL_HOURS` | `24` | Snapshot interval, `0` = off. |
| `POKEMON_BACKUP_KEEP` | `7` | Number of snapshots to keep in `/data/backups`. |
| `POKEMON_COOKIE_SAMESITE` | `lax` | `none` when PokeHOAS is embedded in HA under a different site (see *Sidebar*). Needs HTTPS. |
| `POKEMON_SESSION_SECRET` | random, stored in `/data` | Key that signs login cookies. Changing it logs everyone out. |
| `POKEMON_LOG_LEVEL` | `info` | `debug`, `info`, `warning`, `error`. |
| `POKEMON_HTTP_PORT` | `8099` | Port inside the container. |

Instead of the trigger variables you can also mount an `options.json` (same format the
Supervisor writes) at `/data/options.json`; a set environment variable wins.

## Auth modes

**`ha_oauth` (default).** Uses Home Assistant's own login (the same OAuth flow the HA
mobile apps use). Nothing needs to be registered in HA. PokeHOAS only asks HA *who* you
are and revokes the token right afterwards; it keeps a signed session cookie for 30 days.
Because users keep their real HA user IDs, admin detection works as in the add-on and a
database can move between add-on and container.

**`trusted_header`.** For setups where a reverse proxy with forward auth (Authelia,
Authentik, oauth2-proxy, …) already logs users in. The proxy sends the user name in a
header; PokeHOAS only believes it from the addresses in `POKEMON_TRUSTED_PROXIES`.

| Variable | Default |
|---|---|
| `POKEMON_TRUSTED_PROXIES` | – (required, IPs or CIDRs, e.g. `172.18.0.0/16`) |
| `POKEMON_TRUSTED_USER_HEADER` | `Remote-User` |
| `POKEMON_TRUSTED_NAME_HEADER` | `Remote-Name` |

A proxy user whose name matches an HA user name is treated as that HA user (same data,
same admin rights). Other names become separate players (`proxy:<name>`); make them admin
via `POKEMON_ADMIN_USERS`.

**`dev`.** No login at all, every visitor is the same admin user. Only for local
development; the container refuses to start in this mode unless
`POKEMON_ALLOW_DEV_MODE=1` is set as well.

## Sidebar

Add PokeHOAS to the HA sidebar as a dashboard of type **Webpage** (Settings →
Dashboards → Add dashboard → Webpage) with `POKEMON_PUBLIC_URL` as the URL.

HA's login page cannot be shown inside a frame, so in the sidebar the login opens in a
small popup window once; allow pop-ups if nothing happens. Browsers treat the embedded
page as a third party when HA and PokeHOAS live on different sites:

- Same host or subdomains of the same domain (e.g. `ha.example.com` and
  `pokemon.example.com`): works with the defaults.
- Different sites (e.g. HA on `https://my-ha.duckdns.org`, PokeHOAS on a LAN IP): serve
  PokeHOAS over HTTPS and set `POKEMON_COOKIE_SAMESITE=none`.
- An HTTPS HA cannot embed a plain `http://` page at all (mixed content); put PokeHOAS
  behind the same reverse proxy as HA.

## Backups and restore

Without Supervisor nobody backs up the volume for you, so the image writes a consistent
snapshot of the database to `/data/backups` once a day and keeps the last seven. Admins
can also download a fresh snapshot in **Settings → Admin → Download backup**.

To restore: stop the container, copy a snapshot over `/data/pokemon.sqlite3` (delete
`pokemon.sqlite3-wal` and `-shm` if present), start again.

## Moving from the add-on

Download a backup in the add-on's admin panel, put it as `pokemon.sqlite3` into the data
volume of the container and start it. With `ha_oauth` the HA user IDs are the same, so
everyone finds their team again.
