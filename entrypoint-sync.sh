#!/bin/bash
set -e

LOG_FILE="${LOG_FILE:-/app/logs/sync.log}"
STATE_FILE="${STATE_FILE:-/app/data/state.json}"

# Logs-Verzeichnis erstellen
mkdir -p "$(dirname "$LOG_FILE")" "$(dirname "$STATE_FILE")"

# Beim ersten Start: Locations reconciliieren
if [ ! -f "$STATE_FILE" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [INFO] Erster Start erkannt – führe Location-Reconciliation aus..." | tee -a "$LOG_FILE"
    if python3 reconcile_locations.py >> "$LOG_FILE" 2>&1; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') [INFO] Reconciliation erfolgreich" | tee -a "$LOG_FILE"
    else
        echo "$(date '+%Y-%m-%d %H:%M:%S') [WARN] Reconciliation fehlgeschlagen (aber nicht kritisch)" | tee -a "$LOG_FILE"
    fi
fi

# Starte Hauptprogramm
exec python3 -u sync.py
