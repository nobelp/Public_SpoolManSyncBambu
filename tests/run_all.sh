#!/bin/bash
# Zeilen mit "# nur-privat" lesen echte Produktionsdaten aus data/ und
# werden im öffentlichen Export entfernt (publish_public.sh).
# Fuehrt alle Tests aus. Ohne Docker, ohne HA, ohne Spoolman - die Tests
# arbeiten ausschliesslich gegen temporaere Verzeichnisse und veraendern
# NICHTS in data/ (die Produktionsdaten werden nur lesend als Testdaten
# verwendet, siehe test_history_store.py).
#
# Aufruf aus dem Projektverzeichnis:
#   bash tests/run_all.sh

set -u

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR" || exit 1

# Temporaere Testdaten im RAM statt auf der Platte: SQLite synchronisiert bei jedem
# Commit, auf einem langsamen (NAS-)Volume dauerte die Suite dadurch ueber eine
# Stunde (mit RAM unter einer Minute). Ohne /dev/shm gilt das normale TMPDIR.
if [ -d /dev/shm ] && [ -w /dev/shm ]; then
    export TMPDIR=/dev/shm
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

export PYTHONPATH="$PROJECT_DIR"
FAILED=0

run() {
    local name="$1"
    shift
    printf '%-32s' "$name"
    if output=$(env "$@" python3 "tests/$name" 2>&1); then
        echo "OK"
    else
        echo "FEHLGESCHLAGEN"
        echo "$output" | sed 's/^/    /'
        FAILED=1
    fi
}

# sync.py-Tests: eigener State/Log im Temp-Verzeichnis
run test_sync_unavailable.py "STATE_FILE=$TMP/s1.json" "LOG_FILE=$TMP/s1.log"
run test_failsafe.py         "STATE_FILE=$TMP/s2.json" "LOG_FILE=$TMP/s2.log" "PRINTER_ENTITY_PREFIX=sensor.p2s_testserial0001"
# Dashboard-Zuordnung setzt den Slot-Standort in Spoolman (Regression)
run test_sync_dashboard_assignment.py "STATE_FILE=$TMP/s4.json" "LOG_FILE=$TMP/s4.log"
# Externe Spule: leerer Slot setzt Standort 'P2S - Extern' zurück (Issue #56)
run test_sync_external_release.py "STATE_FILE=$TMP/s5.json" "LOG_FILE=$TMP/s5.log"
# Filament-Zuordnung bei generischem AMS-Namen (Issue #51)
run test_filament_match_rfid.py "STATE_FILE=$TMP/s3.json" "LOG_FILE=$TMP/s3.log"

# consumption.py-Tests
CONSUMPTION_ENV=(
    "CONSUMPTION_STATE_FILE=$TMP/c.json"
    "CONSUMPTION_LOG_FILE=$TMP/c.log"
    "CONSUMPTION_HISTORY_FILE=$TMP/h.json"
    "CONSUMPTION_HISTORY_DB=$TMP/h.db"
    "CONSUMPTION_HISTORY_IMAGES_DIR=$TMP/img"
    "WARTUNG_BACKUP_FILE=$TMP/w.json"
    "CONSUMPTION_OVERRIDES_FILE=$TMP/o.json"
)
run test_colors.py "${CONSUMPTION_ENV[@]}"
run test_plate_types.py "${CONSUMPTION_ENV[@]}"
run test_title_image.py "${CONSUMPTION_ENV[@]}"
run test_shop_links.py "${CONSUMPTION_ENV[@]}" "SHOP_LINKS_STATE_FILE=$TMP/shop_links_state.json"
# Setzt sich seine Pfade selbst (testet den Erststart-Import)
# Setzt sich seine Pfade selbst (eigener Temp-Import der Prod-JSON als Seed)
# Endbuchung (Issue #43/#49, v4.2.0): setzen sich ihre Pfade selbst (booking_harness.py)
run test_final_booking.py
run test_final_booking_idempotent.py
run test_confirm_consumption.py
# Log-Stufe zur Laufzeit (v4.2.2): setzt sich seine Pfade selbst (booking_harness.py)
run test_log_level.py
# Inline-JavaScript der Verlaufsseite per Node pruefen und ausfuehren (wird ohne Node uebersprungen)
run test_history_page_scripts.py
# POST /add-history ohne cgi (v4.2.5): setzt sich seine Pfade selbst (booking_harness.py)
run test_add_history_endpoint.py
run test_external_inactive_clears_override.py "${CONSUMPTION_ENV[@]}"
# Doppelbuchung nach HA-Neustart direkt nach Druckende (Issue #53): setzt sich seine Pfade selbst (booking_harness.py)
run test_replay_unknown_start.py
# Verlaufstabelle: Sortierung und Mehrfachauswahl-Filter (Issue #52); Node/Chromium optional
run test_history_table_tools.py
run test_reconcile_locations.py "STATE_FILE=$TMP/rl.json" "LOG_FILE=$TMP/rl.log" "PRINTER_ENTITY_PREFIX=sensor.p2s_testserial0001"
# Versionsanzeige: Kopfzeile aus VERSION statt git describe
run test_version_display.py "${CONSUMPTION_ENV[@]}"
# Cloud-Namen (Issue #50): ohne Netzwerk, setzt sich seine Pfade selbst
run test_bambu_cloud.py
# filament_enrich.py (Issue #47) und SSRF-Schutz (Issue #46): ohne Netzwerk, setzen sich ihre Pfade selbst
run test_dockerfile_modules.py
run test_safe_fetch.py
run test_filament_enrich.py
# Preisvergleich /nachkaufen: ohne Netzwerk, eigener Temp-Cache
# HA-Entity-Namen aus der .env (Issue #6): ohne Netzwerk
run test_entity_config.py "${CONSUMPTION_ENV[@]}" "STATE_FILE=$TMP/ec.json" "LOG_FILE=$TMP/ec.log"
# Pruefung der HA-Hilfsentities (Issue #6, Teil A): ohne Netzwerk, setzt sich seine Pfade selbst
run test_helper_check.py
run test_price_check.py "PRICE_CACHE_FILE=$TMP/price_cache.json"

echo
if [ "$FAILED" -eq 0 ]; then
    echo "Alle Tests bestanden."
else
    echo "Es gab Fehler."
fi
exit "$FAILED"
