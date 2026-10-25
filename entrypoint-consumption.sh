#!/bin/bash
set -e

LOG_FILE="${CONSUMPTION_LOG_FILE:-/app/logs/consumption.log}"
STATE_FILE="${CONSUMPTION_STATE_FILE:-/app/data/consumption_state.json}"

# Logs-Verzeichnis erstellen
mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$STATE_FILE")"

# Starte Verbrauchs-Job
exec python3 -u consumption.py
