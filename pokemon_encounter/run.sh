#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
set -e

export POKEMON_LOG_LEVEL="$(bashio::config 'log_level')"
export POKEMON_POLL_INTERVAL_MINUTES="$(bashio::config 'poll_interval_minutes')"
export POKEMON_HTTP_PORT="$(bashio::config 'http_port')"

# /data ist das Add-on-Datenverzeichnis und wird vom Supervisor-Backup erfasst.
export POKEMON_DATA_DIR="/data"
export POKEMON_WWW_DIR="/opt/pokemon/www"

# SUPERVISOR_TOKEN wird vom Supervisor gesetzt (homeassistant_api: true).
export POKEMON_HA_URL="http://supervisor/core"

# Zusaetzliche Admins (Liste -> eine Zeile pro Eintrag, siehe app/config.py).
if bashio::config.has_value 'admin_users'; then
  export POKEMON_ADMIN_USERS="$(bashio::config 'admin_users')"
fi

# Im Add-on kommt die Identitaet immer ueber Ingress-Header (siehe app/auth.py).
export POKEMON_AUTH_MODE="ingress"

bashio::log.info "Starte PokeHOAS auf Port ${POKEMON_HTTP_PORT}"

# Gemeinsamer Start fuer Add-on und Standalone-Image: prueft die Konfiguration,
# bildet HA-Log-Level auf uvicorn ab und startet den Server.
exec python3 /opt/pokemon/entrypoint.py
