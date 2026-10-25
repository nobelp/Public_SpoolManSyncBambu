#!/bin/bash
set -e

LOG_FILE="${FILAMENT_ENRICH_LOG_FILE:-/app/logs/filament_enrich.log}"
STATE_FILE="${FILAMENT_ENRICH_STATE_FILE:-/app/data/filament_enrich_state.json}"

# Logs-Verzeichnis erstellen
mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$STATE_FILE")"

# Starte Filament-Enrichment-Job
exec python3 -u filament_enrich.py
