#!/usr/bin/env python3
"""
DIV_SpoolManSyncBambu - consumption.py v1.5
Verbrauchs-Job: liest die aktuellen Restmengen nach und reduziert Spoolman
regelbasiert anhand des tatsaechlichen Filamentverbrauchs pro Druckauftrag.

WARUM eigener Job statt remain%:
  Das Bambu-AMS meldet den RFID-`remain`-Prozentwert nur traege/unzuverlaessig
  (bleibt oft bei 100% stehen). Deshalb wird der Verbrauch deterministisch aus
  `gewicht_des_drucks` (Slicer-Gewicht pro Tray) x Druckfortschritt berechnet
  und ueber den Spoolman-/use-Endpoint abgezogen.

Tracking-Modell (inkrementell, idempotent):
  - Jeder Druckauftrag = eindeutiger Key aus (gcode-Datei | Startzeit).
  - Pro Auftrag wird der bereits abgezogene Fortschrittsanteil gespeichert.
  - Bei jedem Poll wird nur das NEUE Delta (aktueller Fortschritt - bereits
    abgezogen) x Tray-Gewicht abgezogen.
  - Laeuft beim Start bereits ein Druck, wird dessen aktueller Fortschritt als
    Baseline gesetzt -> kein Doppelabzug fuer bereits Gedrucktes.
"""

import html
from email.parser import BytesParser
from email.policy import HTTP as _HTTP_EMAIL_POLICY
import json
import logging
import math
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import requests

# Wiederverwendete Session statt requests.get/post/patch/put(): haelt die
# TCP/TLS-Verbindung zu HA und Spoolman offen (Keep-Alive-Connection-Pooling)
# statt bei jedem einzelnen API-Call neu zu verbinden.
_http = requests.Session()

import bambu_cloud
import history_store
from entity_config import (ChangeReporter, entity_from_env, external_active_entity,
                           external_spool_entity, find_missing, missing_helpers_message,
                           printer_prefix)
import price_check
import shop_links
from plate_types import PLATE_TYPE_LABELS, is_known_plate_type, normalize_plate_type
from title_image import image_fits_job, mask_tokens, parse_ts, reuse_key, utc_now_iso

# Versions-Info auslesen (aus Git tags)
def _get_git_version():
    """Liest die aktuelle Version aus git tags, Fallback zu .version Datei oder 1.5"""
    try:
        import subprocess
        result = subprocess.run(
            ["git", "describe", "--tags", "--always"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True,
            text=True,
            timeout=5
        )
        version = result.stdout.strip()
        if version:
            return version
    except Exception:
        pass

    # Fallback: Aus .version Datei lesen (vom deploy_to_nas.sh erstellt)
    try:
        version_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".version")
        if os.path.exists(version_file):
            with open(version_file, "r") as f:
                version = f.read().strip()
                if version:
                    return version
    except Exception:
        pass

    return "1.5"


def _get_git_info():
    """Liest Git-Info aus Build-Zeit-Dateien (Container-safe)"""
    try:
        app_dir = os.path.dirname(os.path.abspath(__file__))

        version = "1.5"
        commit = "unknown"
        timestamp = ""
        date = ""

        # Version aus git tag (Build-Zeit)
        version_file = os.path.join(app_dir, ".version")
        if os.path.exists(version_file):
            with open(version_file, "r") as f:
                version = f.read().strip() or "1.5"

        # Release-Version aus VERSION (wird bei jedem Release gepflegt) hat
        # Vorrang: `git pull` im NAS-Checkout holt keine neuen Tags, daher
        # lieferte `git describe` in .version eine veraltete Version.
        release_file = os.path.join(app_dir, "VERSION")
        if os.path.exists(release_file):
            with open(release_file, "r") as f:
                version = f.read().strip() or version

        # Commit Hash (Build-Zeit)
        commit_file = os.path.join(app_dir, ".commit")
        if os.path.exists(commit_file):
            with open(commit_file, "r") as f:
                commit = f.read().strip() or "unknown"

        # Timestamp (Build-Zeit)
        timestamp_file = os.path.join(app_dir, ".timestamp")
        if os.path.exists(timestamp_file):
            with open(timestamp_file, "r") as f:
                timestamp = f.read().strip() or ""

        # Parse timestamp zu Date (YYYY-MM-DD)
        if timestamp:
            try:
                dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                date = dt.strftime("%d.%m.%Y")
            except (ValueError, AttributeError):
                date = timestamp[:10]

        return {
            "version": version,
            "commit": commit,
            "timestamp": timestamp,
            "date": date,
        }
    except Exception as e:
        log.debug(f"Git-Info konnte nicht gelesen werden: {e}")
        return {
            "version": "1.5",
            "commit": "unknown",
            "timestamp": "",
            "date": "",
        }


GIT_VERSION = _get_git_version()

# .env laden
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    try:
        with open(_env_path, encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    os.environ.setdefault(_k.strip(), _v.strip())
    except Exception as _e:
        sys.stderr.write(f"⚠️ Fehler beim Laden der .env-Datei: {_e}\n")

# KONFIGURATION
HA_URL        = os.environ.get("HA_URL", "http://homeassistant.local:8123")
HA_TOKEN      = os.environ.get("HA_TOKEN", "")
SPOOLMAN_URL  = os.environ.get("SPOOLMAN_URL", "http://spoolman.local:7912").rstrip("/")
# Oeffentliche Spoolman-Adresse fuer Links im Browser (z.B. "Link eintragen"
# auf /nachkaufen); ohne Angabe die interne SPOOLMAN_URL.
SPOOLMAN_PUBLIC_URL = os.environ.get("SPOOLMAN_PUBLIC_URL", SPOOLMAN_URL).rstrip("/")
# Oeffentliche Adresse dieses Dienstes fuer das Galaxus-Lesezeichen; ohne
# Angabe aus dem Host-Header der Anfrage (Reverse-Proxy: X-Forwarded-*).
NACHKAUFEN_PUBLIC_URL = os.environ.get("NACHKAUFEN_PUBLIC_URL", "").rstrip("/")
POLL_INTERVAL = int(os.environ.get("CONSUMPTION_INTERVAL", "60"))
STATE_FILE    = os.environ.get("CONSUMPTION_STATE_FILE", "/app/data/consumption_state.json")
OVERRIDES_FILE = os.environ.get("CONSUMPTION_OVERRIDES_FILE", "/app/data/consumption_overrides.json")
LOG_FILE      = os.environ.get("CONSUMPTION_LOG_FILE", "/app/logs/consumption.log")
RUN_ONCE      = os.environ.get("RUN_ONCE", "0") == "1"
# Endbuchung (siehe Abschnitt "ENDBUCHUNG"): Timeout fuer PUT /use grosszuegiger
# als fuer Lesezugriffe - Spoolman antwortet unter NAS-Last teils erst nach
# >10 s, obwohl die Buchung schon angewendet wurde (Issue #49).
SM_USE_TIMEOUT_S = float(os.environ.get("SM_USE_TIMEOUT_S", "30"))

HISTORY_FILE        = os.environ.get("CONSUMPTION_HISTORY_FILE", "/app/data/print_history.json")
HISTORY_IMAGES_DIR  = os.environ.get("CONSUMPTION_HISTORY_IMAGES_DIR", "/app/data/history_images")
HISTORY_WEB_PORT    = int(os.environ.get("CONSUMPTION_HISTORY_WEB_PORT", "8100"))
CLOUD_TOKEN_MAX_BODY = 4096  # Bytes - ein Token hat < 1 KB

# Der Druckverlauf liegt seit v1.7 in SQLite (siehe history_store.py).
# Nur DB, kein JSON-Spiegel mehr.
HISTORY_DB_FILE = os.environ.get("CONSUMPTION_HISTORY_DB", "/app/data/print_history.db")

# Unabhaengiges Backup der P2S-Wartungsdaten (Datum-Helfer + Stunden-Baselines).
# HA hat diese Werte bereits zweimal auf uralte YAML-"initial:"-Werte
# zurueckgesetzt (vermutlich bei bestimmten HA-Neustarts/Reloads) - dieses
# Backup lebt komplett ausserhalb von HAs eigenem State-Mechanismus, damit ein
# erneuter Reset in Sekunden per /restore-wartung behoben werden kann, statt
# 25 Zeilen manuell im Dashboard neu zu setzen.
WARTUNG_BACKUP_FILE = os.environ.get("WARTUNG_BACKUP_FILE", "/app/data/p2s_wartung_backup.json")
WARTUNG_DATE_ENTITIES = [
    "input_datetime.p2s_ersetzt_ptfe_tube",
    "input_datetime.p2s_ersetzt_zahnriemen",
    "input_datetime.p2s_ersetzt_nozzle",
    "input_datetime.p2s_ersetzt_carbonfilter",
    "input_datetime.p2s_ersetzt_silikon_socke",
    "input_datetime.p2s_gereinigt_build_plate",
    "input_datetime.p2s_gereinigt_hotend",
    "input_datetime.p2s_gereinigt_steel_rods",
    "input_datetime.p2s_gereinigt_carbon_rods",
    "input_datetime.p2s_gereinigt_linear_rails",
    "input_datetime.p2s_gereinigt_nozzle_wiper",
    "input_datetime.p2s_gereinigt_purge_chute",
    "input_datetime.p2s_gereinigt_x_achse",
    "input_datetime.p2s_gereinigt_y_achse",
    "input_datetime.p2s_gereinigt_z_achse",
    "input_datetime.p2s_gereinigt_innenraum",
    "input_datetime.p2s_gereinigt_kamera_lidar",
    "input_datetime.p2s_geschmiert_steel_rods",
    "input_datetime.p2s_geschmiert_linear_rails",
    "input_datetime.p2s_geschmiert_x_achse",
    "input_datetime.p2s_geschmiert_y_achse",
    "input_datetime.p2s_geschmiert_z_spindeln",
    "input_datetime.p2s_geschmiert_z_staebe",
    "input_datetime.p2s_geschmiert_z_lager",
    "input_datetime.p2s_kalibriert_full",
]
WARTUNG_BASELINE_ENTITIES = [
    "input_number.p2s_rt_ersetzen_ptfe_tube",
    "input_number.p2s_rt_ersetzen_zahnriemen",
    "input_number.p2s_rt_ersetzen_nozzle",
    "input_number.p2s_rt_reinigen_build_plate",
    "input_number.p2s_rt_reinigen_hotend",
    "input_number.p2s_rt_reinigen_steel_rods",
    "input_number.p2s_rt_reinigen_carbon_rods",
    "input_number.p2s_rt_reinigen_linear_rails",
    "input_number.p2s_rt_schmieren_steel_rods",
    "input_number.p2s_rt_schmieren_linear_rails",
]
# 0 = unbegrenzt (Standard seit dem Umstieg auf SQLite). Vorher wurden aeltere
# Eintraege bei >200 verworfen, weil die komplette JSON-Datei bei jedem Poll
# geschrieben wurde. In der DB kostet Aufbewahren praktisch nichts, und die
# Kennzahlen im Dashboard (Drucke gesamt, Filament verbraucht) bleiben damit
# ueber die gesamte Historie korrekt statt abgeschnitten.
HISTORY_MAX_RECORDS = int(os.environ.get("CONSUMPTION_HISTORY_MAX_RECORDS", "0"))

# HA-ENTITIES: Standardwerte gelten fuer die urspruengliche Installation. Wer andere
# Namen hat, setzt sie in der .env (Namen siehe .env.example, Vorschlaege per
# discover_entities.py) - kein Eingriff in den Code noetig (Issue #6).
PRINT_STATUS_ENTITY   = entity_from_env("PRINT_STATUS_ENTITY",   "sensor.bambu_p2s_druckstatus")
PRINT_PROGRESS_ENTITY = entity_from_env("PRINT_PROGRESS_ENTITY", "sensor.bambu_p2s_druckfortschritt")
PRINT_WEIGHT_ENTITY   = entity_from_env("PRINT_WEIGHT_ENTITY",   "sensor.bambu_p2s_gewicht_des_drucks")
GCODE_ENTITY          = entity_from_env("GCODE_ENTITY",          "sensor.bambu_p2s_gcode_dateiname")
STARTTIME_ENTITY      = entity_from_env("STARTTIME_ENTITY",      "sensor.bambu_p2s_startzeit")
TASK_NAME_ENTITY      = entity_from_env("TASK_NAME_ENTITY",      "sensor.bambu_p2s_name_der_aufgabe")
TITLE_IMAGE_ENTITY    = entity_from_env("TITLE_IMAGE_ENTITY",    "image.bambu_p2s_titelbild")
# Titelbild nach Druckende noch so lange nachholen, falls HA es waehrend des
# Drucks nicht liefern konnte (z.B. image_proxy HTTP 500, Issue #33).
TITLE_IMAGE_RETRY_AFTER_END_S = 600
# Wiederholte Titelbild-Meldungen je Druck hoechstens so oft loggen
TITLE_IMAGE_LOG_INTERVAL_S = 600
# Liefert HA so lange nach Druckstart kein eigenes Bild, wird das Titelbild des
# letzten gleichnamigen Drucks uebernommen (Nachdruck). Ein spaeter doch
# gelieferter, passender HA-Bild ersetzt die Uebernahme.
TITLE_IMAGE_REUSE_AFTER_S = 120
LIVE_CAMERA_ENTITY    = entity_from_env("LIVE_CAMERA_ENTITY", "camera.bambu_p2s_kamera")
PRINTER_ENTITY_PREFIX = printer_prefix()
# Welche Druckplatte gerade eingelegt ist. Bild-Zuordnung identisch zum
# Lovelace-Dashboard "lab-dashboard" (conditional picture cards): die
# media_content_ids dort zeigen auf dieselben, in HA hochgeladenen Bilder.
PLATE_TYPE_ENTITY = entity_from_env("PLATE_TYPE_ENTITY", "sensor.bambu_p2s_druckbett_typ")
# Schluessel/Anzeigetexte und die Normalisierung (Cloud liefert "textured_plate",
# lokale Drucke den Bambu-Studio-Text "Textured PEI Plate") stehen in plate_types.py.
PLATE_TYPE_IMAGE_IDS = {
    "textured_plate": "9f9829773b1e145006ab6217959b3213",
    "supertack_plate": "40d5471cdc99545edee28aac1c371598",
}
# Binary-Sensor, der anzeigt, ob die externe Spule GERADE aktiv druckt. Nur wenn
# dieser waehrend eines Jobs mindestens einmal "on" war, wird der externe Anteil
# gebucht - so bucht ein reiner AMS-Druck keinen faelschlichen Extern-Verbrauch,
# der sonst aus Purge-/Rundungsdifferenzen (state - Summe AMS) entstehen koennte.
EXTERNAL_ACTIVE_ENTITY = external_active_entity(PRINTER_ENTITY_PREFIX)
# Mindest-Anteil (g), ab dem die externe Spule als tatsaechlich am Druck beteiligt
# gebucht wird - filtert Rundungsrauschen der Differenz state - Summe(AMS).
EXTERNAL_MIN_G = float(os.environ.get("EXTERNAL_MIN_G", "1.0"))
# Obergrenze der Bambu-Lab-Integration selbst (pybambu iteriert AMS-Einheiten
# x Trays bis 16 = 4 AMS x 4 Slots, siehe get_print_weights()/get_print_lengths()
# in custom_components/bambu_lab/pybambu/models.py) - hoeher muss hier nie
# gesucht werden, auch mit mehreren angeschlossenen AMS-Einheiten.
MAX_AMS_UNITS = 4
MAX_TRAYS_PER_AMS = 4


def _build_tray_key_to_slot(states):
    """Baut das Tray-Key -> Slot-Sensor-Mapping JEDE Runde neu aus den aktuell
    vorhandenen HA-Entities auf, statt einer festen Liste. Damit wird ein
    zusaetzliches AMS (z.B. AMS 2) automatisch erkannt und mitverarbeitet,
    sobald HA dessen Slot-Sensoren registriert hat - ohne Codeaenderung."""
    mapping = {}
    for ams_n in range(1, MAX_AMS_UNITS + 1):
        for tray_m in range(1, MAX_TRAYS_PER_AMS + 1):
            entity_id = f"{PRINTER_ENTITY_PREFIX}_ams_{ams_n}_slot_{tray_m}"
            if entity_id in states:
                mapping[f"AMS {ams_n} Tray {tray_m}"] = entity_id
    external_entity = external_spool_entity(PRINTER_ENTITY_PREFIX)
    if external_entity in states:
        mapping["External"] = external_entity
    return mapping

EMPTY_TAG_UID   = "0000000000000000"
EMPTY_TRAY_UUID = "00000000000000000000000000000000"

# Zustaende, in denen die Bambu-Lab-Integration keine gueltigen Slot-Daten
# liefert (bekannter Verbindungsabbruch der Integration). Solche Aussetzer
# duerfen NIE als "leer" oder "keine Spule" interpretiert werden.
UNAVAILABLE_STATES = ("unavailable", "unknown")


def _entity_state(states, entity_id):
    """Liefert den State einer Entity, oder None bei fehlendem/nicht
    verfuegbarem Sensor - vermeidet, dass "unavailable"/"unknown" faelschlich
    als echter Wert (z.B. Druckbett-Typ) gespeichert wird."""
    value = states.get(entity_id, {}).get("state")
    return value if value not in UNAVAILABLE_STATES else None


_logged_unknown_plates = set()


def _read_plate_type(states):
    """Druckplatten-Typ vom Bambu-Sensor, einheitlich normalisiert. Unbekannte
    Formate bleiben als Text erhalten und werden einmal geloggt."""
    raw = _entity_state(states, PLATE_TYPE_ENTITY)
    value = normalize_plate_type(raw)
    if value and not is_known_plate_type(value) and value not in _logged_unknown_plates:
        _logged_unknown_plates.add(value)
        log.warning(f"Unbekannter Druckplatten-Typ vom Sensor: {raw!r} - wird als Text gespeichert "
                    f"(bitte in plate_types.py ergaenzen)")
    return value

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)

# Log-Stufe zur Laufzeit umschaltbar (Auswahl oben rechts im Druckverlauf,
# POST /log-level) und in data/ gespeichert, damit sie einen Neustart
# ueberdauert. "Aus" heisst bewusst "nur Fehler" - ein komplett stummer Dienst
# wuerde Buchungsfehler verbergen.
LOG_LEVEL_FILE = os.environ.get(
    "CONSUMPTION_LOG_LEVEL_FILE", os.path.join(os.path.dirname(STATE_FILE), "log_level.json")
)
LOG_LEVELS = {
    "off": ("Aus (nur Fehler)", logging.ERROR),
    "normal": ("Normal", logging.INFO),
    "debug": ("Debug", logging.DEBUG),
}
DEFAULT_LOG_LEVEL = "normal"


def _load_log_level():
    """Gespeicherte Log-Stufe; bei fehlender/defekter Datei die Standardstufe."""
    try:
        with open(LOG_LEVEL_FILE, encoding="utf-8") as f:
            key = json.load(f).get("level")
    except (OSError, ValueError, AttributeError):
        return DEFAULT_LOG_LEVEL
    return key if key in LOG_LEVELS else DEFAULT_LOG_LEVEL


logging.basicConfig(
    level=LOG_LEVELS[_load_log_level()][1],
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("consumption")


def current_log_level():
    """Schluessel der aktuell wirksamen Stufe (off/normal/debug)."""
    effective = logging.getLogger().level
    for key, (_, level) in LOG_LEVELS.items():
        if level == effective:
            return key
    return DEFAULT_LOG_LEVEL


def set_log_level(key):
    """Wirkt sofort (kein Neustart noetig) und wird fuer den naechsten Start
    gespeichert. Wirft ValueError bei unbekannter Stufe."""
    if key not in LOG_LEVELS:
        raise ValueError(f"Unbekannte Log-Stufe: {key!r}")
    label, level = LOG_LEVELS[key]
    logging.getLogger().setLevel(level)
    try:
        _atomic_json_write(LOG_LEVEL_FILE, {"level": key})
    except OSError as e:
        # Die Stufe gilt trotzdem bis zum Neustart
        log.error(f"Log-Stufe konnte nicht gespeichert werden (gilt nur bis zum Neustart): {e}")
    # Die Aenderung selbst muss auch bei "nur Fehler" im Log erscheinen
    (log.error if level > logging.WARNING else log.warning)(f"Log-Stufe geaendert: {label}")


# Schuetzt consumption_state.json vor gleichzeitigem Zugriff durch den
# Haupt-Loop (process_once/save_state) und den History-Webserver-Thread
# (z.B. /assign-spool) - ohne Lock ueberschreibt der Haupt-Loop mit seinem
# alten In-Memory-Stand jede Aenderung, die der Webserver zwischenzeitlich
# auf die Platte geschrieben hat (verlorene Updates).
_state_lock = threading.Lock()
_history_lock = threading.Lock()


def _atomic_json_write(filepath, data):
    """Atomic write: schreibe zu .tmp, dann rename (verhindert Dateikorruption bei Crashes)."""
    tmp_path = filepath + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, filepath)
    except Exception as e:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        raise e


def _load_overrides():
    """Lade user_spool_overrides aus separater Datei (verhindert Race Conditions)."""
    if os.path.exists(OVERRIDES_FILE):
        try:
            with open(OVERRIDES_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning(f"Overrides konnte nicht geladen werden: {e}")
    return {}


def _save_overrides(overrides):
    """Speichere user_spool_overrides in separater Datei (verhindert Überschreibung)."""
    try:
        _atomic_json_write(OVERRIDES_FILE, overrides)
    except Exception as e:
        log.error(f"Overrides konnte nicht gespeichert werden: {e}")


def load_state():
    state = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                state = json.load(f)
        except Exception as e:
            log.warning(f"State konnte nicht geladen werden: {e}")
    # GARANTIERE dass alle notwendigen Felder vorhanden sind
    state.setdefault("jobs", {})
    state.setdefault("current_job_key", None)
    # Lade user_spool_overrides AUS SEPARATER DATEI (verhindert Race Conditions)
    state["user_spool_overrides"] = _load_overrides()
    return state


def save_state(state):
    try:
        # Speichere user_spool_overrides SEPARAT um zu verhindern dass sie überschrieben werden.
        # WICHTIG: auch ein leeres Dict muss persistiert werden - state kommt hier immer aus
        # einem vorherigen load_state() (das die Datei bereits frisch gelesen hat), ein
        # "if overrides:"-Guard wuerde das Entfernen des letzten Eintrags (z.B. durch
        # _clear_stale_override(), wenn RFID/tray_uuid wieder greift, oder beim Leerraeumen
        # eines Slots) verhindern - die Datei behaelt dann dauerhaft die veraltete Zuordnung,
        # die bei jedem folgenden Poll erneut greift statt geloescht zu bleiben (Bug: Spule
        # blieb nach dem Entfernen faelschlich einem neuen, andersfarbigen Filament zugeordnet).
        overrides = state.get("user_spool_overrides", {})
        _save_overrides(overrides)
        # Speichere State OHNE user_spool_overrides (sie sind in separater Datei) — atomic write
        state_copy = dict(state)
        state_copy.pop("user_spool_overrides", None)
        _atomic_json_write(STATE_FILE, state_copy)
    except Exception as e:
        log.error(f"State konnte nicht gespeichert werden: {e}")


def load_state_locked():
    """Thread-safe Variante von load_state() mit automatischem Locking."""
    with _state_lock:
        return load_state()


def init_history_storage():
    """Schema anlegen und beim ersten Start die bestehende JSON-Datei
    uebernehmen. Idempotent - kann bei jedem Containerstart laufen."""
    try:
        history_store.init_schema(HISTORY_DB_FILE)
        imported = history_store.import_from_json(HISTORY_DB_FILE, HISTORY_FILE)
        if imported is None:
            log.info(
                f"History-Speicher: SQLite unter {HISTORY_DB_FILE} "
                f"({history_store.count_records(HISTORY_DB_FILE)} Records)"
            )
    except Exception as e:
        log.error(f"History-Speicher konnte nicht initialisiert werden: {e}", exc_info=True)
        raise


def load_history():
    try:
        history = history_store.load(HISTORY_DB_FILE)
    except Exception as e:
        log.error(f"History konnte nicht aus der DB geladen werden: {e}")
        return {"next_id": 1, "records": []}
    _dedup_history_records(history)
    return history


_SAME_PRINT_TOLERANCE_SEC = 90


def _parse_start_dt(start):
    try:
        return datetime.fromisoformat(start)
    except (TypeError, ValueError):
        return None


def _same_print(gcode_a, start_a, gcode_b, start_b):
    """Gilt als derselbe physische Druck, wenn gcode uebereinstimmt und die
    Startzeiten nah beieinander liegen. Toleranz statt exaktem String-
    Vergleich, weil HAs Start-Sensor nach einem Reconnect/HA-Neustart mit
    leicht abweichenden Sekunden neu gemeldet werden kann - dieselbe Minute
    in der Anzeige, aber ein anderer ISO-String, wodurch der bisherige
    exakte Vergleich das Duplikat nicht erkannt hat."""
    if not gcode_a or gcode_a == "?" or gcode_a != gcode_b:
        return False
    dt_a, dt_b = _parse_start_dt(start_a), _parse_start_dt(start_b)
    if dt_a is not None and dt_b is not None:
        return abs((dt_a - dt_b).total_seconds()) <= _SAME_PRINT_TOLERANCE_SEC
    return start_a == start_b


_STATUS_RANK = {"finish": 3, "failed": 3, "started": 1, "cancelled": 0}


def _existing_history_image(*candidates):
    """Waehlt beim Merge das Bild, dessen Datei tatsaechlich noch auf der
    Platte liegt, statt blind das erste nicht-leere Feld zu nehmen - sonst
    kann ein Record mit einer bereits fehlenden/verwaisten Bilddatei ein
    tatsaechlich vorhandenes Bild des anderen Records verdraengen."""
    for name in candidates:
        if name and os.path.exists(os.path.join(HISTORY_IMAGES_DIR, name)):
            return name
    return next((c for c in candidates if c), None)


def _merge_history_records(keep, other):
    """Fuehrt zwei Records zusammen, die denselben physischen Druck
    beschreiben, statt einen davon zu verwerfen - sonst gingen Verbrauchs-
    /Statusdaten der verworfenen Seite verloren. Die hoehere (neuere) ID
    ueberlebt, da ein noch laufender Job typischerweise auf diese ID zeigt
    (job["history_id"] wurde zuletzt vergeben) - so entfaellt fuer ihn der
    Umweg ueber den Layer-2-Fallback bei jedem weiteren Schreibvorgang.
    Verbrauchswerte je Tray werden ADDIERT statt das Maximum zu nehmen: die
    Duplikate stammen aus der Zeit der Teilbuchungen (bis v4.1.x), in der ein
    neu angelegter Job bei der aktuellen Fortschritts-Baseline startete - die
    beiden Records beschreiben aufeinanderfolgende Teilstuecke desselben
    Drucks, keine doppelte Messung desselben Bereichs. Seit der Endbuchung
    (v4.2.0) entstehen keine solchen Duplikate mehr (siehe _resolve_job_key)."""
    if other.get("id", 0) > keep.get("id", 0):
        keep, other = other, keep

    merged = dict(keep)

    if _STATUS_RANK.get(other.get("status"), 0) > _STATUS_RANK.get(merged.get("status"), 0):
        merged["status"] = other["status"]
        merged["end"] = other.get("end")
    elif not merged.get("end"):
        merged["end"] = other.get("end")

    if other.get("start") and (not merged.get("start") or other["start"] < merged["start"]):
        merged["start"] = other["start"]

    durations = [d for d in (keep.get("duration_min"), other.get("duration_min")) if d is not None]
    merged["duration_min"] = max(durations) if durations else None

    merged["image"] = _existing_history_image(merged.get("image"), other.get("image"))
    merged["camera_image"] = _existing_history_image(merged.get("camera_image"), other.get("camera_image"))

    if other.get("name_overridden") and not merged.get("name_overridden"):
        merged["name"] = other["name"]
        merged["name_overridden"] = True
    elif other.get("cloud_name_applied") and not (merged.get("name_overridden") or merged.get("cloud_name_applied")):
        merged["name"] = other["name"]
        merged["cloud_name_applied"] = True

    colors_by_tray = {c.get("tray_key"): dict(c) for c in merged.get("colors", [])}
    for c in other.get("colors", []):
        tray_key = c.get("tray_key")
        existing = colors_by_tray.get(tray_key)
        if existing is None:
            colors_by_tray[tray_key] = dict(c)
            continue
        existing["grams"] = round((existing.get("grams") or 0) + (c.get("grams") or 0), 2)
        existing["length_m"] = round((existing.get("length_m") or 0) + (c.get("length_m") or 0), 3)
        if (c.get("last_update") or "") > (existing.get("last_update") or ""):
            existing["last_update"] = c.get("last_update")
    merged["colors"] = list(colors_by_tray.values())

    return merged


def _dedup_history_records(history):
    """Fuehrt doppelte Records zu einem zusammen, die denselben physischen
    Druck beschreiben (Failsafer fuer HA-Neustarts, siehe _same_print)."""
    records = history.get("records", [])
    if not records:
        return

    cleaned = []
    for rec in sorted(records, key=lambda r: r.get("id", 0)):
        match_idx = next(
            (i for i, existing in enumerate(cleaned)
             if _same_print(rec.get("gcode"), rec.get("start"), existing.get("gcode"), existing.get("start"))),
            None
        )
        if match_idx is None:
            cleaned.append(rec)
        else:
            merged = _merge_history_records(cleaned[match_idx], rec)
            log.warning(
                f"History: Duplikate fuer {merged.get('gcode')} zusammengefuehrt "
                f"(#{cleaned[match_idx]['id']} + #{rec['id']} -> #{merged['id']}, "
                f"Status={merged['status']})"
            )
            cleaned[match_idx] = merged

    if len(cleaned) < len(records):
        history["records"] = cleaned
        save_history(history)


def save_history(history):
    """Schreibt in die SQLite-DB (einzige Quelle)."""
    try:
        history_store.save(HISTORY_DB_FILE, history)
    except Exception as e:
        log.error(f"History konnte nicht in die DB gespeichert werden: {e}")
        raise


def _ha_headers():
    return {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}


def ha_states():
    r = _http.get(f"{HA_URL}/api/states", headers=_ha_headers(), timeout=15)
    r.raise_for_status()
    return {s["entity_id"]: s for s in r.json()}


def ha_call_service(domain, service, data):
    r = _http.post(
        f"{HA_URL}/api/services/{domain}/{service}",
        headers=_ha_headers(), json=data, timeout=15,
    )
    r.raise_for_status()


def ha_set_select_options(entity_id, options):
    ha_call_service("input_select", "set_options", {"entity_id": entity_id, "options": options})


def ha_select_option(entity_id, option):
    ha_call_service("input_select", "select_option", {"entity_id": entity_id, "option": option})


def _title_image_log(job, key, level, message):
    """Titelbild-Meldungen je Druck und Grund gedrosselt loggen (erste Meldung
    sofort, danach hoechstens alle TITLE_IMAGE_LOG_INTERVAL_S) - nachvollziehbar,
    ohne das Log bei minuetlichen Retries zu fluten."""
    marks = job.setdefault("_title_image_log", {})
    now = time.time()
    if now - marks.get(key, 0) >= TITLE_IMAGE_LOG_INTERVAL_S:
        marks[key] = now
        getattr(log, level)(f"Titelbild #{job.get('history_id')}: {mask_tokens(message)}")


def _save_title_image(states, job):
    """Sichert das 3D-Vorschaubild (HA image-Entity) fuer den History-Eintrag
    des Jobs. Uebernimmt nur ein Bild, das zeitlich zum Druck passt - sonst
    koennte das Bild des vorherigen Drucks im neuen Eintrag landen (Issue #33).
    Liefert den Dateinamen oder None (Grund steht im Log); Best effort."""
    img_state = states.get(TITLE_IMAGE_ENTITY, {})
    image_ts = img_state.get("state")
    if not image_fits_job(image_ts, job.get("start"), job.get("ended_at")):
        _title_image_log(job, "stale", "info",
                         f"Vorschaubild passt (noch) nicht zum Druck (Bild von {image_ts}, "
                         f"Druckstart {job.get('start')}) - warte auf neues Bild")
        return None
    token = img_state.get("attributes", {}).get("access_token")
    if not token:
        _title_image_log(job, "no-token", "info", "HA liefert kein access_token fuer das Vorschaubild - Retry")
        return None
    try:
        r = _http.get(
            f"{HA_URL}/api/image_proxy/{TITLE_IMAGE_ENTITY}?token={token}",
            headers=_ha_headers(), timeout=15,
        )
        r.raise_for_status()
        if not r.content:
            raise ValueError("leere Antwort")
        os.makedirs(HISTORY_IMAGES_DIR, exist_ok=True)
        filename = f"{job['history_id']}.png"
        tmp = os.path.join(HISTORY_IMAGES_DIR, filename + ".tmp")
        with open(tmp, "wb") as f:
            f.write(r.content)
        os.replace(tmp, os.path.join(HISTORY_IMAGES_DIR, filename))  # atomar
        refreshed = bool(job.get("image_file"))
        job["image_ts"] = image_ts
        job.pop("image_last_error", None)
        job.pop("image_source", None)  # echtes Bild ersetzt ein uebernommenes
        log.info(f"Titelbild #{job['history_id']}: {'aktualisiert' if refreshed else 'gesichert'} "
                 f"(Bild von {image_ts}, {len(r.content)} Bytes)")
        return filename
    except Exception as e:
        job["image_last_error"] = mask_tokens(e)[:200]
        until = ""
        if job.get("ended_at"):
            until = f" - Retry bis {TITLE_IMAGE_RETRY_AFTER_END_S // 60} min nach Druckende"
        _title_image_log(job, "error", "warning", f"konnte nicht gesichert werden: {e}{until}")
        return None


def _title_image_changed(states, job):
    """True, wenn HA waehrend des (noch passenden) Drucks ein NEUERES Bild hat
    als das gesicherte - z.B. zweiter Bildwechsel direkt nach dem Start."""
    current = parse_ts(states.get(TITLE_IMAGE_ENTITY, {}).get("state"))
    saved = parse_ts(job.get("image_ts"))
    return bool(current and saved and current > saved
                and image_fits_job(current, job.get("start"), job.get("ended_at")))


def _update_history_image(job):
    """Traegt ein nachtraeglich gesichertes Titelbild im History-Eintrag nach."""
    with _history_lock:
        history = load_history()
        for rec in history.get("records", []):
            if rec.get("id") == job.get("history_id"):
                rec["image"] = job.get("image_file")
                rec["title_image_ts"] = job.get("image_ts")
                rec["title_image_source"] = job.get("image_source")
                save_history(history)
                return True
    return False


def _find_reusable_title_image(name, before_id, records=None):
    """History-ID des letzten frueheren Drucks mit gleichem Aufgabennamen und
    vorhandenem Titelbild (Nachdruck) - oder None."""
    key = reuse_key(name)
    if key is None:
        return None
    if records is None:
        records = load_history().get("records", [])
    candidates = []
    for rec in records:
        try:
            rid = int(rec.get("id"))
        except (TypeError, ValueError):
            continue
        if rid < int(before_id) and reuse_key(rec.get("name")) == key \
                and os.path.exists(os.path.join(HISTORY_IMAGES_DIR, f"{rid}.png")):
            candidates.append(rid)
    return max(candidates) if candidates else None


def _copy_title_image(src_id, dst_id):
    src = os.path.join(HISTORY_IMAGES_DIR, f"{src_id}.png")
    dst = os.path.join(HISTORY_IMAGES_DIR, f"{dst_id}.png")
    tmp = dst + ".tmp"
    with open(src, "rb") as fin, open(tmp, "wb") as fout:
        fout.write(fin.read())
    os.replace(tmp, dst)
    return f"{dst_id}.png"


def _reuse_title_image(job, reason):
    """Uebernimmt das Titelbild des letzten gleichnamigen Drucks, wenn HA kein
    eigenes liefert (z.B. Nachdruck ohne Cloud-Vorschau). Liefert True bei Erfolg."""
    if job.get("image_file"):
        return False
    src = _find_reusable_title_image(job.get("task_name"), job["history_id"])
    if src is None:
        return False
    job["image_file"] = _copy_title_image(src, job["history_id"])
    job["image_source"] = f"#{src}"
    log.info(f"Titelbild #{job['history_id']}: vom gleichnamigen Druck #{src} uebernommen ({reason})")
    return True


def _job_age_s(job):
    started = parse_ts(job.get("start"))
    return (datetime.now(timezone.utc) - started).total_seconds() if started else 0


def backfill_title_images():
    """Einmalig beim Start: Eintraege ohne Titelbild-Datei bekommen das Bild des
    letzten frueheren, gleichnamigen Drucks (Nachdrucke, Issue #33). Gekennzeichnet
    mit title_image_source, damit nachvollziehbar bleibt, woher das Bild stammt."""
    filled = []
    with _history_lock:
        history = load_history()
        records = history.get("records", [])
        for rec in sorted(records, key=lambda r: int(r.get("id") or 0)):
            rid = rec.get("id")
            if rid is None or os.path.exists(os.path.join(HISTORY_IMAGES_DIR, f"{rid}.png")):
                continue
            src = _find_reusable_title_image(rec.get("name"), rid, records)
            if src is None:
                continue
            rec["image"] = _copy_title_image(src, rid)
            rec["title_image_source"] = f"#{src}"
            filled.append(f"#{rid}<-#{src}")
        if filled:
            save_history(history)
    if filled:
        log.info(f"Titelbild: {len(filled)} Eintraege von gleichnamigen Drucken uebernommen: {', '.join(filled)}")
    return filled


def _retry_missing_title_images(state, states):
    """Holt fehlende Titelbilder abgeschlossener Drucke nach (bis
    TITLE_IMAGE_RETRY_AFTER_END_S nach Druckende). Laeuft bei jedem Poll,
    unabhaengig davon, ob gerade gedruckt wird. Ein uebernommenes Bild
    (Nachdruck) wird dabei weiter durch ein echtes HA-Bild ersetzt."""
    now = datetime.now(timezone.utc)
    for job in (state.get("jobs") or {}).values():
        if (not job.get("history_finalized") or (job.get("image_file") and not job.get("image_source"))
                or job.get("image_given_up") or not job.get("history_id")):
            continue
        ended = parse_ts(job.get("ended_at"))
        if ended is None:
            job["image_given_up"] = True  # Altbestand ohne Endzeit: nicht rueckwirkend
            continue
        if (now - ended).total_seconds() > TITLE_IMAGE_RETRY_AFTER_END_S:
            job["image_given_up"] = True
            if job.get("image_source"):
                log.info(f"Titelbild #{job['history_id']}: HA lieferte kein eigenes Bild - "
                         f"uebernommenes Bild von {job['image_source']} bleibt")
            else:
                log.warning(f"Titelbild #{job['history_id']}: nach {TITLE_IMAGE_RETRY_AFTER_END_S // 60} min "
                            f"nach Druckende aufgegeben (letzter Fehler: {job.get('image_last_error') or 'kein passendes Bild'})")
            continue
        filename = _save_title_image(states, job)
        if filename:
            job["image_file"] = filename
            if _update_history_image(job):
                log.info(f"Titelbild #{job['history_id']}: nach Druckende nachgetragen")
        elif _reuse_title_image(job, "HA liefert kein Bild") and _update_history_image(job):
            log.info(f"Titelbild #{job['history_id']}: uebernommenes Bild im Eintrag nachgetragen")


def _save_end_camera_photo(history_id):
    """Sichert EIN Foto der Werkstatt-Kamera beim Druckabschluss mit Retry-Logik.

    Best effort mit bis zu 3 Versuchen (bei transienten Fehlern wie Timeout).
    Returns: filename if OK, None if persistent error (HA-Auth, Entity nicht vorhanden).
    """
    max_retries = 3
    retry_delay = 1

    for attempt in range(1, max_retries + 1):
        try:
            r = _http.get(
                f"{HA_URL}/api/camera_proxy/{LIVE_CAMERA_ENTITY}",
                headers=_ha_headers(),
                timeout=15,
            )
            r.raise_for_status()

            ext = "png" if "png" in r.headers.get("Content-Type", "") else "jpg"
            os.makedirs(HISTORY_IMAGES_DIR, exist_ok=True)
            filename = f"{history_id}_camera.{ext}"
            with open(os.path.join(HISTORY_IMAGES_DIR, filename), "wb") as f:
                f.write(r.content)

            log.info(f"Kamera-Foto für Print #{history_id} erfolgreich gespeichert")
            return filename

        except requests.exceptions.Timeout:
            if attempt < max_retries:
                log.warning(f"Kamera-Foto Versuch {attempt}/{max_retries}: Timeout (retry in {retry_delay}s)")
                time.sleep(retry_delay)
                retry_delay *= 2
            else:
                log.error(f"Kamera-Foto für Print #{history_id}: Timeout nach {max_retries} Versuchen")
                return None

        except requests.exceptions.HTTPError as e:
            # 401/403: Auth-Problem (persistent)
            if e.response.status_code in (401, 403):
                log.error(f"Kamera-Foto für Print #{history_id}: Auth-Fehler ({e.response.status_code}) - Check HA_TOKEN")
                return None
            # 404: Entity nicht vorhanden (persistent)
            elif e.response.status_code == 404:
                log.error(f"Kamera-Foto für Print #{history_id}: Entity '{LIVE_CAMERA_ENTITY}' nicht vorhanden")
                return None
            # Andere HTTP-Fehler (transient?)
            elif attempt < max_retries:
                log.warning(f"Kamera-Foto Versuch {attempt}/{max_retries}: HTTP {e.response.status_code} (retry)")
                time.sleep(retry_delay)
                retry_delay *= 2
            else:
                log.error(f"Kamera-Foto für Print #{history_id}: HTTP {e.response.status_code} nach {max_retries} Versuchen")
                return None

        except requests.exceptions.ConnectionError:
            if attempt < max_retries:
                log.warning(f"Kamera-Foto Versuch {attempt}/{max_retries}: Verbindungsfehler (retry)")
                time.sleep(retry_delay)
                retry_delay *= 2
            else:
                log.error(f"Kamera-Foto für Print #{history_id}: HA nicht erreichbar nach {max_retries} Versuchen")
                return None

        except Exception as e:
            log.error(f"Kamera-Foto für Print #{history_id}: Unerwarteter Fehler: {type(e).__name__}: {e}")
            return None

    return None


def _calc_length_m(grams, density, diameter_mm):
    """Rechnet abgezogene Gramm anhand Dichte/Durchmesser des Filaments (aus
    der Spoolman-/use-Antwort) in Meter Filamentlaenge um."""
    if not grams or not density or not diameter_mm:
        return 0.0
    radius_cm = (diameter_mm / 10) / 2
    area_cm2 = math.pi * (radius_cm ** 2)
    volume_cm3 = grams / density
    return (volume_cm3 / area_cm2) / 100


_SM = {"Content-Type": "application/json"}


def sm_get(path, params=None):
    r = _http.get(f"{SPOOLMAN_URL}/api/v1{path}", headers=_SM, params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def sm_use(spool_id, grams):
    r = _http.put(
        f"{SPOOLMAN_URL}/api/v1/spool/{spool_id}/use",
        headers=_SM, json={"use_weight": round(grams, 2)}, timeout=SM_USE_TIMEOUT_S,
    )
    r.raise_for_status()
    return r.json()


def sm_patch(path, data):
    r = _http.patch(f"{SPOOLMAN_URL}/api/v1{path}", headers=_SM, json=data, timeout=10)
    r.raise_for_status()
    return r.json()


def _get_all_ha_states():
    """Wie ha_states(), aber best effort - leeres Dict statt Exception, damit
    die Slots-Seite bei einem HA-Ausfall statt eines 500ers eine leere
    Slot-Liste zeigt."""
    try:
        return ha_states()
    except Exception as e:
        log.warning(f"HA-States abruf fehlgeschlagen: {e}")
        return {}


def _normalize_uid(value):
    if isinstance(value, str):
        return value.strip('"').strip()
    return str(value).strip('"').strip()


def _sm_extra_val(value):
    """Spoolman erwartet Extra-Felder als JSON-kodierte Strings."""
    return json.dumps(str(value))


def _write_shop_link(filament_id, field, url):
    """Traegt einen automatisch gefundenen Shop-Link ein (Issue #35) - nur, wenn
    das Feld in Spoolman JETZT noch leer ist (frisch gelesen): ein inzwischen von
    Hand eingetragener Link wird nie ueberschrieben. Andere Zusatzfelder bleiben."""
    fil = sm_get(f"/filament/{int(filament_id)}")
    if price_check._extra_value(fil, field):
        log.info(f"Shop-Links: Filament #{filament_id} {field} inzwischen von Hand gesetzt - unveraendert")
        return False
    extra = dict(fil.get("extra") or {})
    extra[field] = _sm_extra_val(url)
    sm_patch(f"/filament/{int(filament_id)}", {"extra": extra})
    return True


def _run_shop_links():
    """Ein Lauf der automatischen Shop-Link-Ergaenzung (Bambu Lab, CCTREE)."""
    try:
        summary = shop_links.run(sm_get("/filament"), _write_shop_link)
        if summary:
            log.info(f"Shop-Links: Lauf beendet - {summary['filled']} ergaenzt, {summary['not_offered']} ohne Angebot, "
                     f"{summary['open']} offen" + (f", nicht erreichbar: {', '.join(summary['blocked'])}"
                                                   if summary["blocked"] else ""))
    except Exception as e:
        log.warning(f"Shop-Links: Lauf fehlgeschlagen: {e}")


def _shop_links_loop():
    """Hintergrund: erster Lauf 5 min nach dem Start, danach alle SHOP_LINKS_INTERVAL_H."""
    time.sleep(300)
    while True:
        _run_shop_links()
        time.sleep(shop_links.RUN_INTERVAL_S)


def _run_cloud_names():
    """Ein Lauf der Cloud-Namens-Ersetzung (Issue #50)."""
    try:
        bambu_cloud.run_once(load_history, save_history, _history_lock)
    except Exception as e:
        log.warning(f"Cloud-Namen: Lauf fehlgeschlagen: {e}")


def _cloud_names_loop():
    """Hintergrund: erster Lauf kurz nach dem Start, danach alle BAMBU_CLOUD_NAMES_INTERVAL_S.
    Wiederholung ist noetig, weil die Cloud einen Druck erst verzoegert auflistet."""
    time.sleep(bambu_cloud.FIRST_RUN_DELAY_S)
    while True:
        _run_cloud_names()
        time.sleep(bambu_cloud.RUN_INTERVAL_S)


def _get_iso_timestamp():
    """Aktuelle Zeit als ISO 8601 String (UTC)."""
    return datetime.now(timezone.utc).isoformat()


_LOCAL_TZ = ZoneInfo("Europe/Zurich")


def _to_local_display(iso_str):
    """HA liefert Zeitstempel als UTC-ISO-String (z.B. '...T10:03:22+00:00') -
    fuer die Anzeige in die Schweizer Lokalzeit (inkl. Sommerzeit) umrechnen,
    statt UTC ungewandelt anzuzeigen."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(_LOCAL_TZ).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso_str[:16].replace("T", " ")


def _update_last_used(spool_id):
    """Last Used Timestamp für eine Spule aktualisieren (einmal pro Tag)."""
    try:
        spool = sm_get(f"/spool/{spool_id}")
        now = _get_iso_timestamp()
        now_date = now[:10]

        extra = spool.get("extra", {})
        last_used_str = (extra.get("last_used", "") or "").strip('"')
        last_used_date = last_used_str[:10] if last_used_str else ""

        # Nur updaten wenn nicht bereits heute
        if last_used_date != now_date:
            updates = {"extra": dict(extra)}
            updates["extra"]["last_used"] = _sm_extra_val(now)
            sm_patch(f"/spool/{spool_id}", updates)
    except Exception as e:
        log.debug(f"Last Used Update fehlgeschlagen (Spool {spool_id}): {e}")


def find_existing_history_entry(gcode, start):
    """Sucht nach existierendem History-Eintrag fuer denselben physischen
    Druck (siehe _same_print). Verhindert Duplikate nach HA-Neustart, wenn
    consumption_state.json nicht synchronisiert wurde oder der Start-Sensor
    mit abweichenden Sekunden neu gemeldet wird."""
    if not gcode or gcode == "?":
        return None
    history = load_history()
    for rec in history.get("records", []):
        if _same_print(gcode, start, rec.get("gcode"), rec.get("start")) and rec.get("status") != "cancelled":
            return rec
    return None


def find_spool_by_uid(uid, spools=None):
    """spools: optional bereits geladene Spoolman-Liste (siehe resolve_spool_for_tray)
    - vermeidet einen weiteren vollen Spoolman-Abruf, wenn der Aufrufer die Liste
    fuer denselben Poll-Zyklus schon geladen hat."""
    target = uid.strip('"').upper()
    for spool in (spools if spools is not None else sm_get("/spool")):
        if _normalize_uid(spool.get("extra", {}).get("bambu_tag_uid", "")).upper() == target:
            return spool
    return None


def find_spool_by_tray_uuid(tray_uuid, spools=None):
    target = tray_uuid.strip('"').upper()
    for spool in (spools if spools is not None else sm_get("/spool")):
        if _normalize_uid(spool.get("extra", {}).get("bambu_tray_uuid", "")).upper() == target:
            return spool
    return None


def find_replacement_spool(empty_spool, spools=None, min_weight=50):
    """Findet eine Ersatz-Spule fuer eine leere Spule (gleiche Farbe/Material, genug Gewicht).
    min_weight: Mindest-Restgewicht fuer Ersatz-Spule in Gramm (default 50g)."""
    if not spools:
        spools = sm_get("/spool")

    empty_material = empty_spool.get("filament", {}).get("material", "")
    empty_color = empty_spool.get("filament", {}).get("color_hex", "")

    if not empty_material or not empty_color:
        return None

    candidates = [
        s for s in spools
        if (s["id"] != empty_spool["id"] and
            s.get("filament", {}).get("material") == empty_material and
            s.get("filament", {}).get("color_hex") == empty_color and
            s.get("remaining_weight", 0) >= min_weight)
    ]
    return candidates[0] if candidates else None


def archive_empty_spools(spools=None):
    """DEAKTIVIERT: Auto-Archivierung zu aggressiv (archiviert auch neue Spulen falsch).
    Nutzer soll manuell in Spoolman archivieren."""
    # Kein Auto-Archiving mehr - Nutzer macht das manuell
    pass


def resolve_spool_for_tray(tray_key, all_states, state_data=None, spools=None):
    """Reihenfolge ist bewusst RFID-first: eine physisch ausgelesene Spule
    (tag_uid) oder eine per tray_uuid erkannte Spule ist IMMER zuverlaessiger
    als eine alte manuelle Auswahl. Der User-Override ist nur ein Fallback
    fuer Spulen OHNE RFID-Chip (z.B. Generic PETG) - wird die Spule im Slot
    spaeter gegen eine RFID-Spule getauscht, muss die automatische Erkennung
    sofort wieder greifen, statt fuer immer an der alten Auswahl haengen zu
    bleiben (Bug: fuehrte dazu, dass Verbrauch faelschlich von einer laengst
    ausgetauschten Spule abgezogen wurde).

    spools: optional bereits fuer diesen Poll geladene Spoolman-Spulenliste
    (siehe process_once) - ohne sie wuerde jeder Tray-Aufruf die komplette
    Liste erneut von Spoolman abrufen (bis zu 3x pro Tray)."""
    slot_entity = _build_tray_key_to_slot(all_states).get(tray_key)

    # Sensor fehlt komplett (Integration hat die AMS-Entities abgemeldet) oder
    # meldet unavailable/unknown: die Spule liegt physisch weiter im Slot, HA
    # kann sie nur nicht lesen. Auf die zuletzt per RFID erkannte Spule
    # zurueckfallen, sonst bricht das Verbrauchs-Tracking bei jedem
    # Verbindungsaussetzer ab (bekannter Bug der Bambu-Lab-Integration).
    slot_state = all_states.get(slot_entity, {}) if slot_entity else {}
    if not slot_entity or slot_state.get("state") in UNAVAILABLE_STATES:
        reason = "kein Slot-Mapping" if not slot_entity else slot_state.get("state")
        last_known = (state_data or {}).get("last_known_tray_spools", {}).get(tray_key)
        if last_known and spools is not None:
            spool = next((s for s in spools if s.get("id") == last_known.get("spool_id")), None)
            if spool:
                log.warning(
                    f"Tray '{tray_key}': Sensor {reason} - nutze zuletzt erkannte "
                    f"Spule #{spool['id']} ({spool.get('filament', {}).get('name', '?')})"
                )
                return spool, last_known.get("uid") or f"LASTKNOWN_{spool['id']}"
        log.warning(f"Tray '{tray_key}': Sensor {reason} und keine zuletzt erkannte Spule bekannt.")
        return None, None

    attrs = slot_state.get("attributes", {})
    tag_uid = attrs.get("tag_uid", EMPTY_TAG_UID)

    if tag_uid and tag_uid != EMPTY_TAG_UID:
        spool = find_spool_by_uid(tag_uid, spools)
        if spool:
            _clear_stale_override(state_data, tray_key, spool["id"])
            _remember_tray_spool(state_data, tray_key, spool["id"], tag_uid)
            return spool, tag_uid

    tray_uuid = attrs.get("tray_uuid", EMPTY_TRAY_UUID)
    if tray_uuid and tray_uuid != EMPTY_TRAY_UUID:
        spool = find_spool_by_uid(f"NOTAG_{tray_uuid}", spools)
        if spool:
            _clear_stale_override(state_data, tray_key, spool["id"])
            _remember_tray_spool(state_data, tray_key, spool["id"], f"NOTAG_{tray_uuid}")
            return spool, f"NOTAG_{tray_uuid}"
        spool = find_spool_by_tray_uuid(tray_uuid, spools)
        if spool:
            _clear_stale_override(state_data, tray_key, spool["id"])
            _remember_tray_spool(state_data, tray_key, spool["id"], tray_uuid)
            return spool, tray_uuid

    # Slot ist nachweislich leer (Sensor erreichbar, meldet aber kein Filament):
    # gemerkte Spule verwerfen, damit nach dem Entnehmen nicht weiter auf sie
    # gebucht wird, falls der Sensor spaeter ausfaellt.
    if slot_state.get("state") in ("Empty", "empty", "–", "-", ""):
        _forget_tray_spool(state_data, tray_key)

    # Fallback: manuelle Auswahl NUR wenn RFID/tray_uuid nichts liefern
    # (typischerweise Spulen ganz ohne Chip, z.B. Generic PETG).
    # WICHTIG: Nur User-Overrides erlauben, NICHT automatische Fallback-Suche!
    if state_data:
        overrides = state_data.get("user_spool_overrides", {})
        if tray_key in overrides:
            spool_id = overrides[tray_key]
            spool = next((s for s in spools if s.get("id") == spool_id), None) if spools is not None else None
            if spool:
                ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                user_uid = f"USER_{ts}_{spool_id}"
                log.info(f"Tray '{tray_key}': ✓ User-bestätigte Auswahl Spool {spool_id}")
                return spool, user_uid
            try:
                spool = sm_get(f"/spool/{spool_id}")
                if spool:
                    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                    user_uid = f"USER_{ts}_{spool_id}"
                    log.info(f"Tray '{tray_key}': ✓ User-bestätigte Auswahl Spool {spool_id}")
                    return spool, user_uid
            except Exception as e:
                log.error(f"User-Spool {spool_id} nicht geladen: {e}")

    # SICHERHEIT: AMS-Wechsel-Fallback ist DEAKTIVIERT (führte zu Verbrauchsabzug auf falscher Spule)
    # Grund: Wenn eine Spule nicht RFID-erkannt wird und keine User-Override existiert,
    # ist es SICHERER, zu warten, bis der Benutzer sie im Dashboard bestätigt, als eine
    # Spule automatisch zu erraten und möglicherweise auf die falsche zu buchen.
    #
    # Vorherige Logik (Auto-Fallback zu anderer AMS-Spule) wurde entfernt, da sie
    # zu Verbrauchsabzug auf Spule #7 (statt der tatsächlichen Spule) führte.

    log.warning(
        f"Tray '{tray_key}' ({slot_entity}): ⚠ Keine bestätigte Spule gefunden. "
        f"Bitte im P2S Dashboard diese Spule manuell zuordnen (Input-Select)."
    )
    return None, None


def _remember_tray_spool(state_data, tray_key, spool_id, uid):
    """Merkt sich die zuletzt automatisch (RFID/tray_uuid) erkannte Spule je Tray.
    Dient als Rueckfallebene, solange der Slot-Sensor unavailable ist oder die
    Integration die AMS-Entities ganz abgemeldet hat - siehe
    resolve_spool_for_tray()."""
    if state_data is None:
        return
    known = state_data.setdefault("last_known_tray_spools", {})
    if known.get(tray_key, {}).get("spool_id") != spool_id:
        log.info(f"Tray '{tray_key}': zuletzt erkannte Spule gemerkt -> #{spool_id}")
    known[tray_key] = {"spool_id": spool_id, "uid": uid}


def _forget_tray_spool(state_data, tray_key):
    """Verwirft die gemerkte Spule, sobald der Slot nachweislich leer ist -
    sonst wuerde nach dem Entnehmen einer Spule weiterhin auf sie gebucht.
    Entfernt AUCH eine manuelle Zuordnung (user_spool_overrides): eine
    Auswahl gilt nur fuer die Spule, die zum Auswahlzeitpunkt im Slot lag,
    nicht dauerhaft fuer den Tray/Slot selbst - sonst wuerde nach einem
    Spulenwechsel (neue, chiplose Spule ohne RFID) automatisch wieder die
    alte Auswahl greifen, ohne dass der User das je bestaetigt hat."""
    if state_data is None:
        return
    known = state_data.get("last_known_tray_spools", {})
    if tray_key in known:
        del known[tray_key]
        log.info(f"Tray '{tray_key}': Slot leer - gemerkte Spule verworfen")
    overrides = state_data.get("user_spool_overrides", {})
    if tray_key in overrides:
        del overrides[tray_key]
        log.info(f"Tray '{tray_key}': Slot leer - manuelle Zuordnung verworfen")


def _clear_stale_override(state_data, tray_key, resolved_spool_id):
    """Entfernt eine veraltete manuelle Zuordnung, sobald die automatische
    Erkennung (RFID/tray_uuid) fuer diesen Tray wieder erfolgreich greift -
    verhindert, dass eine alte Auswahl nach einem Spulenwechsel erneut
    faelschlich zum Zug kommt, falls die neue Spule spaeter mal kurzzeitig
    nicht ausgelesen werden kann."""
    if not state_data:
        return
    overrides = state_data.get("user_spool_overrides", {})
    old_id = overrides.get(tray_key)
    if old_id is not None and old_id != resolved_spool_id:
        del overrides[tray_key]
        log.info(
            f"Veraltete manuelle Zuordnung fuer '{tray_key}' entfernt "
            f"(war Spool {old_id}, RFID erkennt jetzt Spool {resolved_spool_id})"
        )


def _tray_weights(pw_state, tray_key_to_slot):
    attrs = pw_state.get("attributes", {})
    out = {}
    for k, v in attrs.items():
        if k in tray_key_to_slot:
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                pass
    return out


def _progress_fraction(prog_state, status):
    if status == "finish":
        return 1.0
    try:
        return max(0.0, min(1.0, float(prog_state.get("state", 0)) / 100.0))
    except (TypeError, ValueError):
        return 0.0


def _is_internal_system_job(gcode_path):
    """Interne Systemroutinen (z.B. Kamera-Kalibrierung) sind keine echten
    Drucke und verbrauchen kein Filament - erkennbar am Pfad (/usr/etc/...
    statt /data/Metadata/... bei echten Slicer-Auftraegen)."""
    return bool(gcode_path) and gcode_path.startswith("/usr/etc/")


def _new_color_entry(spool_id, filament, tray_key=None):
    return {
        "spool_id": spool_id,
        "tray_key": tray_key,
        "filament_name": filament.get("name"),
        "material": filament.get("material"),
        "color_hex": filament.get("color_hex"),
        "density": filament.get("density"),
        "diameter": filament.get("diameter"),
        "grams": 0.0,
        "length_m": 0.0,
        "last_update": None,
    }


# ─────────────────────────────────────────────────────────────────
# ENDBUCHUNG (seit v4.2.0)
#
# Waehrend eines Drucks wird NICHTS bei Spoolman gebucht. Jeder Poll
# beobachtet nur: hoechster gesehener Fortschritt (max_progress, nie
# fallend) und die erkannte Spule je Tray (segments). Gebucht wird genau
# EINMAL pro Job und Spule, wenn der Druck endet (FERTIG, FEHLER oder
# Abbruch):
#   FERTIG          -> Plangewicht x 1.0
#   FEHLER/Abbruch  -> Plangewicht x max_progress
# Die Buchung ist idempotent (siehe _book_item) - ein Spoolman-Timeout
# fuehrt nie zu einer Doppelbuchung (Issue #49), ein Neustart nie zu einer
# verlorenen Buchung. Das ersetzt die alten Teilbuchungen pro Poll
# (tray_fractions, BACKWARD-Reset, PENDING-Eintraege), die bei jeder
# Bestaetigung mitten im Druck doppelt gebucht haben (Issue #43).
# ─────────────────────────────────────────────────────────────────
ITEM_OPEN = "open"                    # bereit zur Buchung
ITEM_AWAITING_USER = "awaiting_user"  # Spule unbekannt oder Bestaetigung noetig
ITEM_BOOKED = "booked"                # bei Spoolman verbucht (final)

MIN_BOOKING_G = 0.01            # Spoolman rechnet auf 2 Nachkommastellen
BOOKING_VERIFY_TOLERANCE_G = 0.05
# Ein Posten, dessen PUT ohne Antwort blieb, wird fruehestens nach dieser Zeit
# erneut gesendet: Spoolman kann die Anfrage auch NACH dem Client-Timeout noch
# verarbeiten, die Verifikation per used_weight sieht das erst danach.
BOOKING_RETRY_MIN_S = float(os.environ.get("BOOKING_RETRY_MIN_S", "120"))

# Job-Status -> History-Status
_FINAL_HISTORY_STATUS = {"finish": "finish", "failed": "failed", "cancelled": "cancelled"}


def _is_unreadable_start(start):
    """True, wenn der Start-Sensor keinen Zeitstempel liefert ('unknown', 'unavailable',
    '?', leer) - typisch direkt nach einem HA-Neustart."""
    return _parse_start_dt(start) is None


def _same_tray_plan(plan_a, plan_b):
    """Gleiche Tray-Plangewichte (auf 0,01 g genau)?"""
    plan_a, plan_b = plan_a or {}, plan_b or {}
    return set(plan_a) == set(plan_b) and all(abs(plan_a[k] - plan_b[k]) <= 0.01 for k in plan_a)


def _find_replayed_job_key(jobs, gcode, start, tray_weights, status):
    """Schluessel des fertigen Jobs, dessen Druck der Drucker gerade nur noch einmal
    meldet - sonst None (Issue #53).

    Nach einem HA-Neustart direkt nach Druckende meldet der Drucker weiter 'finish'
    mit demselben G-Code und Plangewicht, der Start-Sensor ist aber 'unknown'. Der
    Job-Schluessel (G-Code|Start) ist dann neu, und ohne diese Pruefung wuerde ein
    zweiter Job mit Status 'finish' sofort ein zweites Mal buchen.
    Gilt nur, wenn ALLES zutrifft: Status finish/failed, Start nicht lesbar, kein
    Job offen, und der zuletzt beendete Job hat denselben G-Code und dieselben
    Tray-Plangewichte. Ein neuer Druck durchlaeuft 'prepare'/'running' und legt
    seinen Job dort an, er ist davon nicht betroffen."""
    if status not in ("finish", "failed") or not _is_unreadable_start(start):
        return None
    # Verwaiste offene Jobs aelterer Versionen (ohne 'segments') zaehlen nicht, siehe _open_job_keys
    if not gcode or gcode == "?" or any(not job.get("done") and "segments" in job for job in jobs.values()):
        return None
    finished = [(job["ended_at"], key) for key, job in jobs.items() if job.get("ended_at")]
    if not finished:
        return None
    last_key = max(finished)[1]
    last = jobs[last_key]
    if last.get("gcode") == gcode and _same_tray_plan(last.get("tray_weights"), tray_weights):
        return last_key
    return None


def _resolve_job_key(jobs, gcode, start):
    """Job-Schluessel fuer den aktuellen Druck. Der Start-Sensor des Druckers
    springt in der ersten Minute um einige Sekunden (06:29:00 -> 06:29:49);
    ein exakter Vergleich legte dafuer einen zweiten Job an. Ein noch
    laufender Job desselben physischen Drucks (siehe _same_print) wird
    deshalb weiterverwendet. Abgeschlossene Jobs werden bewusst nie
    unscharf gematcht - ein neuer Druck binnen 90 s nach einem fertigen
    darf nicht als dessen Fortsetzung gelten."""
    exact_key = f"{gcode}|{start}"
    if exact_key in jobs:
        return exact_key
    for key, job in jobs.items():
        if not job.get("done") and _same_print(gcode, start, job.get("gcode"), job.get("start")):
            return key
    # Verliert der Start-Sensor mitten im Druck seinen Wert (HA-Neustart: 'unknown'),
    # ist es derselbe Druck - nicht ein neuer, der den laufenden Job abbricht und
    # danach sofort noch einmal bucht (Issue #53). Nur bei genau einem offenen Job
    # mit gleichem G-Code, sonst bleibt es beim neuen Schluessel.
    if _is_unreadable_start(start):
        open_same_gcode = [key for key, job in jobs.items()
                           if not job.get("done") and gcode and gcode != "?" and job.get("gcode") == gcode]
        if len(open_same_gcode) == 1:
            return open_same_gcode[0]
    return exact_key


def _open_job_keys(jobs, current_job_key):
    """Schluessel noch nicht abgeschlossener Jobs, die die Endbuchung betrifft:
    alle Jobs im neuen Format sowie der zuletzt laufende Job aus einer aelteren
    Version. Verwaiste Jobs aelterer Versionen (Duplikate nach HA-Neustart)
    bleiben bewusst unangetastet."""
    return [
        key for key, job in jobs.items()
        if not job.get("done") and ("segments" in job or key == current_job_key)
    ]


def _cancel_open_jobs(state, keys, states, all_spools, reason):
    """Schliesst Jobs ab, deren Druck nicht mit FERTIG/FEHLER endete (Abbruch,
    Folgedruck): gebucht wird Plan x hoechster Fortschritt."""
    for key in keys:
        job = state["jobs"][key]
        log.warning(f"Druckauftrag {job.get('gcode')} abgebrochen ({reason})")
        _ensure_job_schema(job)
        _finalize_job(job, "cancelled", states, state, all_spools)
        _process_final_bookings(state, all_spools, only_job_key=key)


def _filament_summary(filament):
    """Die fuer History/Laengenberechnung noetigen Filament-Felder."""
    filament = filament or {}
    return {key: filament.get(key) for key in ("name", "material", "color_hex", "density", "diameter")}


def _new_segment(spool_id, uid, filament, start_fraction, carried_grams=0.0):
    """Abschnitt eines Trays, in dem dieselbe Spule eingelegt war. 'to' bleibt
    None, solange der Abschnitt offen ist. carried_grams: nur fuer die
    Migration alter Jobs - bereits gebuchte Gramm, die in der Anzeige
    mitgezaehlt, aber NIE erneut gebucht werden."""
    return {
        "spool_id": spool_id,
        "uid": uid,
        "filament": _filament_summary(filament),
        "from": start_fraction,
        "to": None,
        "carried_grams": carried_grams,
    }


def _ensure_job_schema(job):
    """Stellt die Felder der Endbuchung sicher. Jobs aus Version < 4.2.0
    (laufender Druck beim Update) werden uebernommen: bereits gebuchte
    Anteile (tray_fractions) und Gramm (colors_used) zaehlen als erledigt."""
    job.setdefault("max_progress", 0.0)
    if "segments" in job:
        return
    fractions = job.get("tray_fractions") or {}
    segments = {}
    for tray_key in job.get("tray_weights", {}):
        entries = (job.get("colors_used") or {}).get(tray_key) or []
        last = entries[-1] if entries else {}
        carried = round(sum(entry.get("grams", 0.0) for entry in entries), 2)
        segments[tray_key] = [
            _new_segment(last.get("spool_id"), None, last, fractions.get(tray_key, 0.0), carried)
        ]
    job["segments"] = segments
    job["max_progress"] = max([job["max_progress"], *fractions.values()])
    if fractions:
        log.warning(
            f"Job {job.get('gcode')} aus aelterer Version uebernommen "
            f"(bereits gebucht bis {job['max_progress']*100:.0f}%)"
        )


def _track_tray_spool(job, tray_key, spool, uid):
    """Haelt fest, welche Spule im Tray steckt. Kein Spoolman-Zugriff.
    - noch kein Abschnitt: anlegen (auch ohne erkannte Spule = Platzhalter)
    - gleiche Spule: nichts zu tun
    - Platzhalter und jetzt erkannt: Platzhalter fuellen (rueckwirkend)
    - andere Spule: Spulenwechsel - Abschnitt schliessen, neuen oeffnen; der
      Verbrauch bis hierher bleibt der vorigen Spule zugeordnet
    - nicht erkannt (Sensor-Aussetzer, Slot leer): letzte Spule behalten"""
    segments = job["segments"].setdefault(tray_key, [])
    spool_id = spool["id"] if spool else None
    filament = spool["filament"] if spool else {}
    if not segments:
        segments.append(_new_segment(spool_id, uid, filament, 0.0))
        return
    last = segments[-1]
    if spool is None:
        return
    if last["spool_id"] == spool_id:
        last["uid"] = uid
        return
    if last["spool_id"] is None:
        last.update(spool_id=spool_id, uid=uid, filament=_filament_summary(filament))
        log.info(f"  → Spule fuer '{tray_key}' erkannt: #{spool_id} ({filament.get('name', '?')})")
        return
    last["to"] = job["max_progress"]
    segments.append(_new_segment(spool_id, uid, filament, job["max_progress"]))
    log.warning(
        f"Spulenwechsel in '{tray_key}' bei {job['max_progress']*100:.0f}%: "
        f"#{last['spool_id']} -> #{spool_id}"
    )


def _segment_grams(job, tray_key, segment, open_fraction):
    """Noch zu buchende Gramm eines Abschnitts (ohne carried_grams)."""
    end = segment["to"] if segment["to"] is not None else open_fraction
    plan_g = job.get("tray_weights", {}).get(tray_key, 0.0)
    return round(max(0.0, plan_g * (end - segment["from"])), 2)


def _rebuild_colors_used(job, open_fraction):
    """Leitet die History-Anzeige (colors_used) aus den Abschnitten ab. Waehrend
    des Drucks ist das die Schaetzung Plangewicht x Fortschritt (reine
    Anzeige, es wurde nichts gebucht), nach Druckende der gebuchte Betrag."""
    colors_used = {}
    now = _get_iso_timestamp()
    for tray_key, segments in job.get("segments", {}).items():
        entries = []
        for segment in segments:
            entry = _new_color_entry(segment["spool_id"], segment["filament"], tray_key)
            grams = _segment_grams(job, tray_key, segment, open_fraction) + segment.get("carried_grams", 0.0)
            entry["grams"] = round(grams, 2)
            entry["length_m"] = round(_calc_length_m(grams, entry.get("density"), entry.get("diameter")), 3)
            entry["last_update"] = now
            entries.append(entry)
        colors_used[tray_key] = entries
    job["colors_used"] = colors_used


def _track_job_spools(job, states, state, all_spools):
    """Erkennt die Spule je beteiligtem Tray und fuehrt die Abschnitte nach.
    Ein Fehler bei einem Tray (z.B. Spoolman nicht erreichbar) bricht den
    Poll nicht ab - beim naechsten Poll wird erneut versucht."""
    for tray_key in job["tray_weights"]:
        try:
            spool, uid = resolve_spool_for_tray(tray_key, states, state, all_spools)
        except Exception as e:
            log.warning(f"Spule fuer '{tray_key}' konnte nicht ermittelt werden: {mask_tokens(e)}")
            continue
        _track_tray_spool(job, tray_key, spool, uid)
    # Trays, die nach einer Korrektur der Planwerte nicht mehr beteiligt sind
    # (Sensor lieferte anfangs noch die Werte des Vorgaengerdrucks)
    for tray_key in [k for k in job["segments"] if k not in job["tray_weights"]]:
        if not any(seg.get("carried_grams") for seg in job["segments"][tray_key]):
            del job["segments"][tray_key]


def _refresh_plan_weights(job, sensor_weights, pw_state, external_possible):
    """Uebernimmt die aktuellen Planwerte (g je Tray) des Gewichtssensors; der
    letzte gueltige Wert gewinnt. AMS-Trays kommen aus den Sensor-Attributen
    (fehlen sie, gelten die bisherigen Werte weiter). Der Anteil der externen
    Spule ist der Rest: Sensor-Gesamtgewicht minus Summe der AMS-Trays -
    gerechnet nur, wenn die externe Spule waehrend des Drucks aktiv war
    (sonst wuerden Purge-/Rundungsdifferenzen faelschlich als Extern-Verbrauch
    erscheinen)."""
    previous = job["tray_weights"]
    weights = {k: v for k, v in (sensor_weights or previous).items() if k != "External"}
    external_g = None
    if job.get("external_seen") and external_possible:
        try:
            external_g = round(float(pw_state.get("state")) - sum(weights.values()), 2)
        except (TypeError, ValueError):
            external_g = None
    if external_g is not None:
        # Die Rechnung entscheidet in BEIDE Richtungen: Zeigt der Sensor zuerst
        # nur das Gesamtgewicht (AMS-Gewichte noch nicht da), steht anfangs das
        # ganze Gewicht als "extern" da. Kommen die AMS-Gewichte, ist der Rest
        # ~0 und der Posten muss wieder entfallen (Druck #215: 14,95 g wurden
        # faelschlich auch auf die externe Spule #26 gebucht).
        if external_g >= EXTERNAL_MIN_G:
            weights["External"] = external_g
    elif "External" in previous:
        weights["External"] = previous["External"]
    if weights != previous:
        log.info(f"Planwerte {job.get('gcode')}: {previous} -> {weights}")
    job["tray_weights"] = weights


def _history_already_booked(history_record):
    """True, wenn der Verlaufseintrag eines abgeschlossenen Drucks bereits
    Verbrauch enthaelt."""
    return bool(
        history_record
        and history_record.get("status") in ("finish", "failed")
        and any(c.get("grams", 0) > 0 for c in history_record.get("colors", []))
    )


def _has_unbooked_items(job):
    final_booking = job.get("final_booking")
    return bool(final_booking) and any(i["status"] != ITEM_BOOKED for i in final_booking["items"])


def _needs_user_confirmation(tray_key, uid):
    """AMS-Slots und per RFID/Zuordnung erkannte Spulen werden automatisch
    gebucht. Eine externe Spule gilt als bestaetigt, wenn der Nutzer sie im
    Dashboard gewaehlt hat (USER_) oder sie per RFID erkannt wurde. Nur eine
    unverifizierte Fallback-UID (MANUAL_/AUTO_) braucht eine Bestaetigung."""
    if not tray_key.startswith("External"):
        return False
    return not uid or uid.startswith(("MANUAL_", "AUTO_"))


def _create_final_booking(job, status):
    """Legt aus den Abschnitten die einmalige Endbuchung an (idempotent: ein
    bereits angelegter Plan wird nie neu berechnet)."""
    if job.get("final_booking"):
        return job["final_booking"]
    final_fraction = 1.0 if status == "finish" else job["max_progress"]
    items = []
    for tray_key, segments in job["segments"].items():
        segments[-1]["to"] = final_fraction
        for index, segment in enumerate(segments):
            grams = _segment_grams(job, tray_key, segment, final_fraction)
            if grams < MIN_BOOKING_G:
                continue
            ready = segment["spool_id"] is not None and not _needs_user_confirmation(tray_key, segment["uid"])
            items.append({
                "tray_key": tray_key,
                "segment": index,
                "spool_id": segment["spool_id"],
                "uid": segment["uid"],
                "grams": grams,
                "status": ITEM_OPEN if ready else ITEM_AWAITING_USER,
            })
    job["final_booking"] = {
        "status": status,
        "fraction": final_fraction,
        "created_at": _get_iso_timestamp(),
        "items": items,
    }
    _rebuild_colors_used(job, final_fraction)
    log.info(
        f"Endbuchung angelegt ({status}, {final_fraction*100:.0f}%): "
        + (", ".join(f"{i['tray_key']} -> Spule {i['spool_id']}: {i['grams']:.2f}g" for i in items) or "nichts zu buchen")
    )
    return job["final_booking"]


def _spool_used_weight(spool_id):
    """Aktuelles used_weight laut Spoolman. Wirft bei Verbindungsfehlern."""
    value = sm_get(f"/spool/{spool_id}").get("used_weight")
    return None if value is None else float(value)


def _warn_if_spool_empty(spool_id, result, all_spools):
    """Hinweis und Ersatz-Vorschlag, wenn die Buchung die Spule geleert hat.
    HINWEIS: Auto-Archivierung ist bewusst deaktiviert (archivierte auch neue
    Spulen falsch) - der Nutzer archiviert manuell in Spoolman."""
    if result.get("remaining_weight", 0) > 0:
        return
    spool_name = result.get("filament", {}).get("name", "?")
    log.warning(f"⚠️  Spule {spool_id} ({spool_name}) ist LEER (remaining_weight <= 0) → bitte manuell in Spoolman archivieren")
    replacement = find_replacement_spool(result, all_spools)
    if not replacement:
        return
    replacement_name = replacement["filament"]["name"]
    replacement_location = replacement.get("location", "Lager")
    log.warning(
        f"Spule {spool_id} ({spool_name}) ist LEER! "
        f"Ersatz verfuegbar: Spule {replacement['id']} ({replacement_name}) "
        f"in {replacement_location} ({replacement.get('remaining_weight', 0):.0f}g)"
    )
    try:
        ha_call_service("notify", "notify", {
            "title": f"Spule {spool_id} LEER - Ersatz verfuegbar",
            "message": f"{spool_name}\n→ Spule {replacement['id']} ({replacement_name}) "
                      f"in {replacement_location} ({replacement.get('remaining_weight', 0):.0f}g)",
        })
    except Exception as e:
        log.warning(f"HA-Notification fuer leere Spule fehlgeschlagen: {e}")


def _book_item(state, item, all_spools):
    """Bucht EINEN Posten genau einmal bei Spoolman. True = verbucht.

    Idempotenz gegen Spoolman-Timeouts (Issue #49): Vor dem ersten Versuch
    wird used_weight (u0) gelesen und samt 'attempted' persistiert. Bleibt
    der PUT /use ohne Antwort, ist offen, ob Spoolman ihn trotzdem angewendet
    hat. Der naechste Versuch prueft deshalb ZUERST, ob used_weight bereits
    um den Betrag gestiegen ist, und bucht nur, wenn nicht. (Nur wenn
    Spoolman used_weight gar nicht liefert, ist keine Pruefung moeglich und
    es wird erneut gebucht.)"""
    spool_id, grams = item["spool_id"], item["grams"]
    try:
        used_now = _spool_used_weight(spool_id)
    except Exception as e:
        log.warning(f"Endbuchung Spule {spool_id}: Spoolman nicht erreichbar, spaeter erneut ({mask_tokens(e)})")
        return False

    if item.get("attempted"):
        u0 = item.get("u0")
        if u0 is not None and used_now is not None and used_now >= u0 + grams - BOOKING_VERIFY_TOLERANCE_G:
            item.update(status=ITEM_BOOKED, booked_at=_get_iso_timestamp(), verified_after_error=True)
            log.warning(
                f"Endbuchung Spule {spool_id}: {grams:.2f}g waren trotz Fehler bereits angekommen "
                f"(used_weight {u0:.2f} -> {used_now:.2f}) - NICHT erneut gebucht"
            )
            save_state(state)
            return True
        if time.time() - item.get("last_attempt", 0.0) < BOOKING_RETRY_MIN_S:
            return False
        if used_now is not None:
            item["u0"] = used_now
    else:
        item["u0"] = used_now
        item["attempted"] = True
    item["attempts"] = item.get("attempts", 0) + 1
    item["last_attempt"] = time.time()
    save_state(state)  # VOR dem PUT: ein Absturz danach wird beim Neustart verifiziert

    try:
        result = sm_use(spool_id, grams)
    except Exception as e:
        log.error(f"Endbuchung Spule {spool_id}: PUT /use fehlgeschlagen ({mask_tokens(e)}) - wird beim naechsten Poll verifiziert")
        return False

    item.update(status=ITEM_BOOKED, booked_at=_get_iso_timestamp())
    save_state(state)
    log.info(
        f"✓ Endbuchung: {item['tray_key']} -> Spule {spool_id} "
        f"({result.get('filament', {}).get('name', '?')}): -{grams:.2f}g | "
        f"Rest={result.get('remaining_weight', 0):.1f}g"
    )
    # Nebenwirkungen nach erfolgreicher Buchung duerfen sie nie gefaehrden
    try:
        _update_last_used(spool_id)
        _warn_if_spool_empty(spool_id, result, all_spools)
    except Exception as e:
        log.warning(f"Nachbereitung der Buchung (Spule {spool_id}) fehlgeschlagen: {mask_tokens(e)}")
    return True


def _process_final_bookings(state, all_spools, only_job_key=None):
    """Bucht alle offenen Posten. Laeuft in JEDEM Poll vor allem anderen,
    unabhaengig vom Druckerstatus - so wird eine Buchung nach Neustart oder
    Spoolman-Ausfall nachgeholt, auch wenn der Drucker laengst idle ist."""
    # Ein noch ungeklaerter Posten (PUT ohne Antwort) blockiert alle weiteren
    # Posten derselben Spule: deren Buchungen wuerden used_weight erhoehen und
    # die Verifikation des offenen Postens faelschlich bestaetigen.
    blocked_spools = set()
    for job_key, job in state.get("jobs", {}).items():
        final_booking = job.get("final_booking")
        if not final_booking:
            continue
        for item in final_booking["items"]:
            if item["status"] != ITEM_OPEN or item["spool_id"] in blocked_spools:
                continue
            if only_job_key is not None and job_key != only_job_key:
                # Posten anderer Jobs nicht buchen, aber ungeklaerte weiterhin als Sperre beachten
                if item.get("attempted"):
                    blocked_spools.add(item["spool_id"])
                continue
            if not _book_item(state, item, all_spools):
                blocked_spools.add(item["spool_id"])


def _finalize_job(job, status, states, state, all_spools):
    """Druckende (finish/failed/cancelled): noch unerkannte Spulen ein letztes
    Mal aufloesen, History abschliessen, Endbuchung anlegen und ausfuehren.
    Die uebrigen Spulen kommen aus den Abschnitten des Drucks - NICHT aus
    einer neuen Aufloesung, weil die manuelle Zuordnung der externen Spule
    nach Druckende vom Dashboard verworfen wird."""
    # Jeder beteiligte Tray braucht einen Abschnitt - sonst entstuende fuer ihn
    # kein Posten und sein Verbrauch ginge still verloren (z.B. wenn die
    # Spulenerkennung waehrend des ganzen Drucks fehlschlug). Ohne bekannte
    # Spule wartet der Posten auf die Auswahl im Dashboard.
    for tray_key in job["tray_weights"]:
        job["segments"].setdefault(tray_key, [_new_segment(None, None, {}, 0.0)])
    for tray_key, segments in job["segments"].items():
        if segments and segments[-1]["spool_id"] is None and all_spools is not None:
            try:
                spool, uid = resolve_spool_for_tray(tray_key, states, state, all_spools)
            except Exception as e:
                log.warning(f"Spule fuer '{tray_key}' bei Druckende nicht ermittelbar: {mask_tokens(e)}")
                continue
            _track_tray_spool(job, tray_key, spool, uid)
    job["done"] = True
    job.setdefault("ended_at", utc_now_iso())
    _create_final_booking(job, status)
    _finalize_history(job)


def _finalize_history(job):
    """Schliesst den Verlaufseintrag ab. Ein Fehler hier (z.B. gesperrte
    Datenbank) darf die Endbuchung NIE blockieren - sie ist vor dem
    Verlaufseintrag angelegt. Misslingt der Abschluss, bleibt
    history_finalized False und process_once versucht es im naechsten Poll
    erneut."""
    if "history_id" not in job:
        job["history_finalized"] = True
        return
    try:
        if not job.get("camera_image"):
            job["camera_image"] = _save_end_camera_photo(job["history_id"])
        _write_history_record(job, _FINAL_HISTORY_STATUS[job["final_booking"]["status"]])
        job["history_finalized"] = True
    except Exception as e:
        log.error(f"Verlaufseintrag #{job['history_id']} konnte nicht abgeschlossen werden: {mask_tokens(e)}")


def confirm_final_booking(state, job_key, spool_id, tray_key=None, segment=None):
    """Dashboard-Bestaetigung: ordnet wartende Posten eines Jobs der gewaehlten
    Spule zu und bucht sie. Mit tray_key/segment nur diesen einen Posten, sonst
    alle wartenden des Jobs. Gibt (ok, Meldung) zurueck."""
    job = state.get("jobs", {}).get(job_key)
    final_booking = job.get("final_booking") if job else None
    awaiting = [
        i for i in (final_booking or {}).get("items", [])
        if i["status"] == ITEM_AWAITING_USER
        and (tray_key is None or i["tray_key"] == tray_key)
        and (segment is None or i["segment"] == segment)
    ]
    if not awaiting:
        return False, "Keine ausstehende Buchung fuer diesen Druck"
    try:
        spool = sm_get(f"/spool/{spool_id}")
    except Exception as e:
        return False, f"Spule {spool_id} konnte nicht geladen werden: {mask_tokens(e)}"
    uid = f"USER_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{spool_id}"
    overrides = state.setdefault("user_spool_overrides", {})
    for item in awaiting:
        segment = job["segments"][item["tray_key"]][item["segment"]]
        segment.update(spool_id=spool_id, uid=uid, filament=_filament_summary(spool.get("filament")))
        item.update(spool_id=spool_id, uid=uid, status=ITEM_OPEN)
        overrides[item["tray_key"]] = spool_id
    _rebuild_colors_used(job, final_booking["fraction"])
    _process_final_bookings(state, None, only_job_key=job_key)
    _write_history_record(job, _FINAL_HISTORY_STATUS[final_booking["status"]])
    log.info(f"✅ Verbrauch BESTAETIGT: Job {job_key} -> Spule {spool_id} ({len(awaiting)} Posten)")
    if all(item["status"] == ITEM_BOOKED for item in awaiting):
        return True, "gebucht"
    return True, "zugeordnet, Buchung folgt beim naechsten Poll (Spoolman antwortet nicht)"


def _write_history_record(job, status):
    """Legt bei Druckstart einen History-Eintrag an und aktualisiert ihn bei
    jedem Poll (Status/Farben/Mengen), statt nur einmal am Ende zu schreiben.
    So sind Bild/Name/Datum/Farbe sofort sichtbar, nicht erst nach Abschluss.
    Failsafer: Falls ein History-Eintrag mit gleichem gcode|start existiert,
    wird dieser aktualisiert statt einen neuen anzulegen."""
    if "history_id" not in job:
        return

    now = _get_iso_timestamp()
    # Das Druckende ist der Zeitpunkt des Abschlusses (ended_at), nicht der des
    # letzten Schreibens - sonst verschiebt eine spaetere Bestaetigung der
    # Endbuchung im Dashboard Ende und Dauer des Drucks.
    end = (job.get("ended_at") or now) if status in ("finish", "failed", "cancelled") else None
    duration_min = None
    try:
        start_dt = datetime.fromisoformat(job["start"])
        ref_dt = datetime.fromisoformat(end or now)
        duration_min = round((ref_dt - start_dt).total_seconds() / 60)
    except Exception:
        pass

    with _history_lock:
        history = load_history()
        existing = next((r for r in history.get("records", []) if r.get("id") == job["history_id"]), None)

        # Flatten colors_used: von {tray_key: [entries...], ...} zu flache Liste [entries...]
        # WICHTIG: Neue Spulen hinzufügen (auch wenn History leer), aber keine doppelten NULL-Einträge
        colors = []
        existing_spool_ids = {c.get("spool_id") for c in existing.get("colors", []) if existing} if existing else set()
        for color_list in job.get("colors_used", {}).values():
            if isinstance(color_list, list):
                for color_entry in color_list:
                    spool_id = color_entry.get("spool_id")
                    # Neue Spulen IMMER hinzufügen (auch wenn nicht in bestehender History)
                    # Nur NULL-Spulen filtern (wenn bereits NULL in History, keine weitere NULL)
                    if spool_id is not None:
                        # Spule erkannt → hinzufügen
                        colors.append(color_entry)
                    elif not existing or not any(c.get("spool_id") is None for c in existing.get("colors", [])):
                        # NULL-Spule: nur wenn History noch keine NULL hat
                        colors.append(color_entry)
            else:
                # Fallback für alte Format (einzelner dict statt list)
                spool_id = color_list.get("spool_id")
                if spool_id is not None:
                    colors.append(color_list)
                elif not existing or not any(c.get("spool_id") is None for c in existing.get("colors", [])):
                    colors.append(color_list)

        # Zusätzliche Sicherheit: Falls kein Record mit dieser ID existiert, aber
        # einer fuer denselben physischen Druck vorhanden ist (siehe _same_print),
        # nutze diesen statt einen Duplikat anzulegen (sichert gegen Timestamp-
        # Abweichungen nach Reconnect/HA-Neustart ab).
        if not existing:
            existing = next(
                (r for r in history.get("records", [])
                 if _same_print(job.get("gcode"), job.get("start"), r.get("gcode"), r.get("start")) and
                    r.get("status") != "cancelled"),
                None
            )
            if existing:
                job["history_id"] = existing["id"]
                log.info(
                    f"History: Nutze existierenden Record #{existing['id']} "
                    f"statt Duplikat für gcode|start: {job.get('gcode')}|{job.get('start')}"
                )

        name = job.get("task_name") or job.get("gcode") or "?"
        name_overridden = False
        cloud_name_applied = bool(existing and existing.get("cloud_name_applied"))
        if existing and (existing.get("name_overridden") or cloud_name_applied):
            name = existing["name"]
            name_overridden = bool(existing.get("name_overridden"))

        record = {
            "id": job["history_id"],
            "gcode": job.get("gcode"),
            "name": name,
            "name_overridden": name_overridden,
            "cloud_name_applied": cloud_name_applied,
            "start": job.get("start"),
            "end": end,
            "duration_min": duration_min,
            "status": status,
            "image": job.get("image_file"),
            "camera_image": job.get("camera_image"),
            "title_image_ts": job.get("image_ts"),
            "title_image_source": job.get("image_source"),
            "plate_type": normalize_plate_type(job.get("plate_type"))
                          or (existing.get("plate_type") if existing else None),
            "colors": colors,
        }

        # Vorhandenen Eintrag mit derselben ID ersetzen (laufende Aktualisierung
        # bzw. erneutes "finish" nach Retry) statt zu duplizieren.
        history["records"] = [r for r in history.get("records", []) if r.get("id") != record["id"]]
        history["records"].append(record)
        # HISTORY_MAX_RECORDS=0 (Standard) = unbegrenzt aufbewahren. Ein Limit
        # greift nur, wenn es ausdruecklich gesetzt wurde.
        if HISTORY_MAX_RECORDS > 0 and len(history["records"]) > HISTORY_MAX_RECORDS:
            dropped = len(history["records"]) - HISTORY_MAX_RECORDS
            history["records"] = history["records"][-HISTORY_MAX_RECORDS:]
            log.info(f"History: {dropped} aelteste Eintraege ueber dem Limit ({HISTORY_MAX_RECORDS}) entfernt")
        save_history(history)
    log.info(f"History-Eintrag #{record['id']} aktualisiert: '{record['name']}' ({status})")


# ─────────────────────────────────────────────────────────────────
# AMS-SLOT-DROPDOWNS IM HA-DASHBOARD (input_select je Slot)
# Gleiches Muster wie filament_enrich.py: Python POLLT die HA-Entities und
# reagiert auf Aenderungen, statt HA-seitige Automationen/rest_command zu
# brauchen - ein Aenderungs-Trigger auf HA-Seite waere ein zweiter, parallel
# gepflegter Mechanismus fuer denselben Zweck.
# ─────────────────────────────────────────────────────────────────
_SELECT_PLACEHOLDER = "– Spule wählen –"
_SELECT_EMPTY_LABEL = "– leer –"


def _backup_wartung_data(states):
    """Sichert die aktuellen P2S-Wartungsdaten (Datum-Helfer + Stunden-
    Baselines) unabhaengig von HAs eigenem State-Mechanismus in eine lokale
    JSON-Datei. Laeuft bei jedem Poll mit - reiner Datei-Schreibvorgang ohne
    Netzwerk-Overhead, da die Werte schon aus dem bereits geladenen 'states'
    Dict stammen."""
    try:
        dates = {}
        for eid in WARTUNG_DATE_ENTITIES:
            val = states.get(eid, {}).get("state")
            if val and val not in ("unknown", "unavailable"):
                dates[eid] = val

        baselines = {}
        for eid in WARTUNG_BASELINE_ENTITIES:
            val = states.get(eid, {}).get("state")
            if val and val not in ("unknown", "unavailable"):
                baselines[eid] = val

        if not dates and not baselines:
            return

        backup = {
            "backed_up_at": _get_iso_timestamp(),
            "dates": dates,
            "baselines": baselines,
        }
        tmp_path = WARTUNG_BACKUP_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(backup, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, WARTUNG_BACKUP_FILE)
    except Exception as e:
        log.warning(f"P2S-Wartung-Backup fehlgeschlagen: {e}")


def _restore_wartung_data():
    """Stellt die zuletzt gesicherten P2S-Wartungsdaten ueber offizielle
    HA-Service-Calls wieder her (z.B. nach einem erneuten HA-Reset auf die
    YAML-'initial:'-Werte). Aufrufbar per POST /restore-wartung."""
    if not os.path.exists(WARTUNG_BACKUP_FILE):
        return {"ok": False, "error": "Kein Backup vorhanden"}
    try:
        with open(WARTUNG_BACKUP_FILE, encoding="utf-8") as f:
            backup = json.load(f)
    except Exception as e:
        return {"ok": False, "error": f"Backup konnte nicht gelesen werden: {e}"}

    restored = 0
    errors = []
    for eid, val in backup.get("dates", {}).items():
        try:
            ha_call_service("input_datetime", "set_datetime", {"entity_id": eid, "date": val})
            restored += 1
        except Exception as e:
            errors.append(f"{eid}: {e}")
    for eid, val in backup.get("baselines", {}).items():
        try:
            ha_call_service("input_number", "set_value", {"entity_id": eid, "value": float(val)})
            restored += 1
        except Exception as e:
            errors.append(f"{eid}: {e}")

    return {
        "ok": len(errors) == 0,
        "restored": restored,
        "backed_up_at": backup.get("backed_up_at"),
        "errors": errors,
    }


# ─────────────────────────────────────────────────────────────────
# DRUCKZEIT-AKKUMULATION FUER STUNDEN-BASIERTE WARTUNGSKOMPONENTEN
# HA hat sich als unzuverlaessig erwiesen, um einen kontinuierlich
# akkumulierenden Zaehler ueber Neustarts hinweg korrekt zu halten (YAML-
# 'initial:' wird bei JEDEM Neustart neu angewendet, nicht nur bei
# Ersterstellung - siehe P2S_WARTUNG.md "Bekannter Bug"). Deshalb lebt der
# eigentliche Zaehler komplett in consumption_state.json (unser Docker-Volume,
# ausserhalb von HAs Kontrolle) und wird jeden Poll (60s) selbstheilend nach
# HA gepusht - selbst wenn HA seinen eigenen Wert zwischendurch verliert,
# ist er spaetestens nach dem naechsten Poll wieder korrekt.
# ─────────────────────────────────────────────────────────────────
MAX_DRUCKZEIT_STEP_H = 1.0  # Sicherheitsnetz gegen grosse Zeitspruenge (z.B. nach langem Container-Stopp)


def _update_druckzeit_counters(state, status):
    """Kumuliert die tatsaechliche aktive Druckzeit (status == 'running') pro
    Stunden-basierter Komponente. Muss bei JEDEM Poll aufgerufen werden
    (auch wenn nicht gedruckt wird), damit last_poll_ts fortlaufend aktuell
    bleibt und kein grosser Sprung entsteht, sobald der Druck wieder startet."""
    now = datetime.now(timezone.utc)
    dz = state.setdefault("druckzeit", {})
    counters = dz.setdefault("counters", {})
    for eid in WARTUNG_BASELINE_ENTITIES:
        counters.setdefault(eid, 0.0)

    last_ts_str = dz.get("last_poll_ts")
    elapsed_h = 0.0
    if last_ts_str:
        try:
            last_ts = datetime.fromisoformat(last_ts_str)
            elapsed_h = max(0.0, (now - last_ts).total_seconds() / 3600.0)
            elapsed_h = min(elapsed_h, MAX_DRUCKZEIT_STEP_H)
        except Exception:
            elapsed_h = 0.0

    if status == "running" and elapsed_h > 0:
        for eid in WARTUNG_BASELINE_ENTITIES:
            counters[eid] = round(counters[eid] + elapsed_h, 4)

    dz["last_poll_ts"] = now.isoformat()
    return counters


def _push_druckzeit_to_ha(counters):
    """Schreibt jeden Poll die aktuellen Zaehlerwerte nach HA - selbstheilend,
    falls HA seinen eigenen Wert (z.B. durch einen Neustart) verloren hat."""
    for eid, hours in counters.items():
        try:
            ha_call_service("input_number", "set_value", {"entity_id": eid, "value": round(hours, 4)})
        except Exception as e:
            log.warning(f"Druckzeit-Push fehlgeschlagen fuer {eid}: {e}")


def _reset_druckzeit_counter(state, entity_id):
    """Setzt EINEN Komponenten-Zaehler auf 0 zurueck (Aufruf bei 'Wartung
    erledigt') und pusht den neuen Wert sofort nach HA fuer instant Feedback,
    statt auf den naechsten Poll zu warten."""
    dz = state.setdefault("druckzeit", {})
    counters = dz.setdefault("counters", {})
    counters[entity_id] = 0.0
    try:
        ha_call_service("input_number", "set_value", {"entity_id": entity_id, "value": 0})
    except Exception as e:
        log.warning(f"Sofort-Reset fuer {entity_id} an HA fehlgeschlagen: {e}")


def _dashboard_select_entity_id(tray_key):
    """Leitet aus dem Tray-Key den Namen des zugehoerigen input_select-Helfers
    ab (muss vorher in HA angelegt sein, z.B. input_select.p2s_slot_ams_1_tray_1
    fuer 'AMS 1 Tray 1' bzw. input_select.p2s_slot_external fuer 'External')."""
    if tray_key == "External":
        return "input_select.p2s_slot_external"
    m = re.match(r"AMS (\d+) Tray (\d+)$", tray_key)
    if not m:
        return None
    return f"input_select.p2s_slot_ams_{m.group(1)}_tray_{m.group(2)}"


def _sync_dashboard_selects(states, state, tray_key_to_slot, all_spools):
    """Laeuft bei JEDEM Poll (unabhaengig vom Druckstatus, damit Zuordnungen
    auch vor Druckstart moeglich sind). Zwei Aufgaben pro Slot:
    1) Erkennen, ob der User seit dem letzten Poll eine neue Spule ausgewaehlt
       hat (Wert-Vergleich gegen den zuletzt gesehenen Wert) -> als Override
       uebernehmen (identische Logik wie /assign-spool).
    2) Anzeige aktuell halten: RFID-Slots zeigen nur die erkannte Spule
       (einzige Option, quasi gesperrt), leere Slots '– leer –', unbekannte
       Slots die volle, aktuelle Spoolman-Liste mit der bisherigen Auswahl
       vorselektiert."""
    seen = state.setdefault("dashboard_select_seen", {})
    spool_options = [
        f"{s['id']} - {_stripped_filament_name(s.get('filament', {}))}"
        for s in sorted(all_spools, key=lambda x: x["id"])
    ]
    overrides = state.setdefault("user_spool_overrides", {})

    for tray_key, slot_entity in tray_key_to_slot.items():
        select_entity = _dashboard_select_entity_id(tray_key)
        if not select_entity or select_entity not in states:
            continue  # Helfer (noch) nicht in HA angelegt - ueberspringen

        slot_state = states.get(slot_entity, {})
        filament_name = slot_state.get("state", "–")
        attrs = slot_state.get("attributes", {})

        # Aussetzer der Bambu-Lab-Integration: Dropdown unveraendert lassen.
        # Sonst wuerde die angezeigte RFID-Zuordnung bei jedem Verbindungs-
        # abbruch auf den Platzhalter zurueckfallen, obwohl die Spule
        # physisch im Slot liegt.
        if filament_name in UNAVAILABLE_STATES:
            log.debug(f"{tray_key}: Slot-Sensor {filament_name} - Dropdown unveraendert")
            continue

        tag_uid = attrs.get("tag_uid", EMPTY_TAG_UID)
        is_rfid = bool(tag_uid) and tag_uid != EMPTY_TAG_UID
        is_empty = filament_name in ("–", "-", "Empty", "empty", "")

        # Die externe Spule meldet ihren zuletzt geladenen Namen (z.B. "Generic
        # PLA") unveraendert weiter, auch wenn sie laengst entnommen/inaktiv ist -
        # der Slot-Sensor selbst hat dafuer keinen zuverlaessigen "leer"-Zustand
        # (anders als die AMS-Trays). Einzig verlaessliches Signal ist der
        # separate Aktiv-Sensor: ist er aus, gilt der Tray als leer, sonst
        # bleibt eine alte Zuordnung (z.B. Spule #26) faelschlich fuer immer
        # im Dropdown stehen und wuerde dem naechsten, ganz anderen Filament
        # zugerechnet.
        if tray_key == "External" and states.get(EXTERNAL_ACTIVE_ENTITY, {}).get("state") != "on":
            is_empty = True

        current_value = states.get(select_entity, {}).get("state")
        current_options = states.get(select_entity, {}).get("attributes", {}).get("options", [])

        if is_empty:
            desired_options = [_SELECT_EMPTY_LABEL]
            desired_selected = _SELECT_EMPTY_LABEL
            overrides.pop(tray_key, None)
        elif is_rfid:
            # Gleiche Aufloesung UND gleiches Anzeigeformat wie /slots:
            # resolve_spool_for_tray() statt eines reinen tag_uid-Abgleichs,
            # weil sich der tag_uid einer physischen Spule aendern kann (neue
            # Rolle gleicher Farbe eingelegt), waehrend tray_uuid/NOTAG_-Match
            # weiterhin greift - reiner find_spool_by_uid()-Abgleich verpasste
            # dadurch Treffer, die /slots (ueber resolve_spool_for_tray) fand.
            result = resolve_spool_for_tray(tray_key, states, state, all_spools) if all_spools else None
            resolved_spool = result[0] if result else None
            if resolved_spool:
                label = _spool_display_label(resolved_spool)
            else:
                # Spoolman-Abgleich (noch) nicht moeglich - Rohname als
                # Fallback, redundanten "Bambu"-Praefix trotzdem entfernen.
                label = filament_name
                if label.lower().startswith("bambu "):
                    label = label[len("bambu "):].strip()
            desired_options = [label]
            desired_selected = label
        else:
            desired_options = [_SELECT_PLACEHOLDER] + spool_options
            # Hat der User seit dem letzten Poll etwas Neues ausgewaehlt (und
            # ist es kein Platzhalter/unbekannter Wert)? Dann als Override
            # uebernehmen - IDENTISCHE Logik wie /assign-spool.
            if (
                current_value
                and current_value != seen.get(tray_key)
                and current_value not in ("unknown", "unavailable", _SELECT_PLACEHOLDER)
            ):
                try:
                    new_spool_id = int(current_value.split(" - ")[0].strip())
                    overrides[tray_key] = new_spool_id
                    log.info(f"Dashboard-Auswahl: {tray_key} -> Spool {new_spool_id}")
                except (ValueError, IndexError):
                    log.warning(f"Dashboard-Auswahl '{current_value}' fuer {tray_key} nicht parsebar")

            override_id = overrides.get(tray_key)
            desired_selected = next(
                (o for o in spool_options if o.startswith(f"{override_id} - ")), _SELECT_PLACEHOLDER
            ) if override_id else _SELECT_PLACEHOLDER

        if current_options != desired_options:
            try:
                ha_set_select_options(select_entity, desired_options)
            except Exception as e:
                log.warning(f"Optionen fuer {select_entity} konnten nicht gesetzt werden: {e}")

        if current_value != desired_selected:
            try:
                ha_select_option(select_entity, desired_selected)
            except Exception as e:
                log.warning(f"Auswahl fuer {select_entity} konnte nicht gesetzt werden: {e}")

        seen[tray_key] = desired_selected


# Hilfsentities (input_*), die der Dienst benutzt, aber NICHT selbst anlegt - sie
# muessen in HA existieren (Beschreibung: HELPERS.md). Fehlen sie, wird nur
# gemeldet; die Verbrauchsbuchung selbst haengt nicht davon ab.
VERSION_TEXT_ENTITY = "input_text.spoolman_version"
COMMIT_TEXT_ENTITY = "input_text.spoolman_commit"
RELEASE_TEXT_ENTITY = "input_text.spoolman_release"
_helper_reporter = ChangeReporter()


def required_helpers(tray_key_to_slot):
    """Alle Hilfsentities, die dieser Dienst in HA erwartet. Die Dashboard-Dropdowns
    nur fuer Slots, die es gibt (weitere AMS-Einheiten brauchen erst dann Helfer)."""
    helpers = [*WARTUNG_DATE_ENTITIES, *WARTUNG_BASELINE_ENTITIES,
               VERSION_TEXT_ENTITY, COMMIT_TEXT_ENTITY, RELEASE_TEXT_ENTITY,
               _dashboard_select_entity_id("External")]
    for tray_key in tray_key_to_slot:
        select_entity = _dashboard_select_entity_id(tray_key)
        if select_entity and select_entity not in helpers:
            helpers.append(select_entity)
    return helpers


def report_missing_helpers(states, tray_key_to_slot):
    """Fehlende Hilfsentities melden - beim ersten Poll und bei jeder Aenderung."""
    missing = find_missing(states, required_helpers(tray_key_to_slot))
    if not _helper_reporter.is_new(tuple(missing)):
        return
    message = missing_helpers_message(missing)
    if message:
        log.warning(message)
    else:
        log.info("Alle benoetigten HA-Hilfsentities (input_*) sind vorhanden")


# Pflicht: ohne diese Entities kann kein Verbrauch gebucht werden. Optional: es
# fehlen nur Zusatzfunktionen (Titelbild, Kamera, Druckplatte, Extern-Erkennung ...).
REQUIRED_ENTITIES = (PRINT_STATUS_ENTITY, PRINT_PROGRESS_ENTITY, PRINT_WEIGHT_ENTITY)
OPTIONAL_ENTITIES = (GCODE_ENTITY, STARTTIME_ENTITY, TASK_NAME_ENTITY, TITLE_IMAGE_ENTITY,
                     LIVE_CAMERA_ENTITY, PLATE_TYPE_ENTITY, EXTERNAL_ACTIVE_ENTITY)
_last_reported_missing = None


def report_missing_entities(states):
    """Fehlende konfigurierte Entities melden - nur beim ersten Poll und bei jeder
    Aenderung, nicht jede Minute (sonst fuellt ein Tippfehler das Log). Verbrauch
    ohne Pflicht-Entities wuerde still nichts buchen, daher ERROR statt Abbruch:
    HA kann kurz nach dem Start noch nicht alle Entities geliefert haben."""
    global _last_reported_missing
    missing_required = find_missing(states, REQUIRED_ENTITIES)
    missing_optional = find_missing(states, OPTIONAL_ENTITIES)
    current = (tuple(missing_required), tuple(missing_optional))
    if current == _last_reported_missing:
        return
    _last_reported_missing = current
    hint = "Namen in der .env anpassen (Vorschlaege: python3 discover_entities.py)"
    if missing_required:
        log.error(f"❌ Pflicht-Entities nicht in Home Assistant: {', '.join(missing_required)} - "
                  f"Verbrauch kann nicht gebucht werden. {hint}")
    if missing_optional:
        log.warning(f"Optionale Entities nicht in Home Assistant: {', '.join(missing_optional)} - "
                    f"zugehoerige Funktionen sind eingeschraenkt. {hint}")
    if not missing_required and not missing_optional:
        log.info("Alle konfigurierten HA-Entities sind vorhanden")


def process_once(state):
    states = ha_states()

    report_missing_entities(states)

    tray_key_to_slot = _build_tray_key_to_slot(states)
    report_missing_helpers(states, tray_key_to_slot)

    # Spulenliste EINMAL pro Poll laden und fuer den gesamten Rest von
    # process_once wiederverwenden (Dashboard-Dropdowns + resolve_spool_for_tray
    # je Tray) - sonst wuerde derselbe volle Spoolman-Abruf pro Tray bis zu
    # 3x wiederholt werden.
    try:
        all_spools = sm_get("/spool")
    except Exception as e:
        log.warning(f"Spoolman-Spulenliste konnte nicht geladen werden: {e}")
        all_spools = None

    # Dashboard-Dropdowns SOFORT abgleichen, unabhaengig vom Druckstatus - eine
    # Zuordnung soll auch VOR Druckstart moeglich sein, nicht erst waehrend
    # eines laufenden Jobs.
    try:
        _sync_dashboard_selects(states, state, tray_key_to_slot, all_spools)
    except Exception as e:
        log.warning(f"Dashboard-Dropdown-Abgleich fehlgeschlagen: {e}")

    # Offene Endbuchungen zuerst und unabhaengig vom Druckstatus (Nachholen
    # nach Neustart/Spoolman-Ausfall, siehe Abschnitt ENDBUCHUNG).
    try:
        _process_final_bookings(state, all_spools)
    except Exception as e:
        log.error(f"Endbuchung fehlgeschlagen: {mask_tokens(e)}", exc_info=True)

    # P2S-Wartungsdaten unabhaengig von HA sichern (siehe _backup_wartung_data
    # Docstring - HA hat diese Werte bereits mehrfach auf alte YAML-Defaults
    # zurueckgesetzt).
    _backup_wartung_data(states)

    # Fehlende Titelbilder abgeschlossener Drucke nachholen (Issue #33)
    try:
        _retry_missing_title_images(state, states)
    except Exception as e:
        log.warning(f"Titelbild-Nachholen fehlgeschlagen: {mask_tokens(e)}")

    # WICHTIG: Default NICHT "idle"! Fehlt der Sensor kurzzeitig in der HA-Antwort
    # (z.B. waehrend eines HA-Neustarts, bis die Bambu-Integration ihre Entities
    # neu registriert hat), wuerde ein Default von "idle" faelschlich einen
    # laufenden Druck als abgebrochen markieren (siehe Vorfaelle: Job wurde nach
    # jedem HA-Neustart faelschlich gecancelt, obwohl der Drucker durchlief).

    # Dynamische Status-Entity-Discovery WIRD SPÄTER gemacht (Zeile ~1217)
    # Aber hier nutzen wir ERST die hardcoded zur schnellen Vorprüfung
    status_state = states.get(PRINT_STATUS_ENTITY, {})
    status = status_state.get("state", "unknown")
    gcode  = states.get(GCODE_ENTITY, {}).get("state", "") or "?"
    start  = states.get(STARTTIME_ENTITY, {}).get("state", "") or "?"
    job_key = _resolve_job_key(state.setdefault("jobs", {}), gcode, start)

    # FALLBACK: Wenn Status-Sensor zu alt ist (>300s), nutze Fortschritts-Sensor als Indikator
    # (der ist aktueller und zeigt ob der Druck wirklich läuft)
    status_last_report = status_state.get("last_reported", "")
    if status_last_report:
        try:
            last_report_dt = datetime.fromisoformat(status_last_report.replace('Z', '+00:00'))
            now_dt = datetime.fromisoformat(_get_iso_timestamp())
            age_seconds = (now_dt - last_report_dt).total_seconds()
            if age_seconds > 300 and status == "running":
                # Status ist älter als 5 Minuten - überprüfe Fortschritt
                prog_state_test = states.get(PRINT_PROGRESS_ENTITY, {})
                prog_test = _progress_fraction(prog_state_test, status)
                if prog_test > 0:
                    log.info(f"⚠️  Status-Sensor alt ({age_seconds:.0f}s), nutze Fortschritts-Sensor als Fallback (Fortschritt: {prog_test*100:.0f}%)")
                else:
                    log.warning(f"⚠️  Status-Sensor alt ({age_seconds:.0f}s) und Fortschritt=0 → Status ist wahrscheinlich veraltet")
        except Exception as e:
            log.debug(f"Fallback-Status-Check fehlgeschlagen: {e}")

    # Externe-Spule-Aktivitaet UNABHAENGIG vom weiteren Ablauf erfassen: waehrend
    # die externe Spule aktiv druckt, ersetzt der Gewichtssensor die AMS-Attribute
    # komplett, sodass _tray_weights() leer ist und process_once() sonst unten bei
    # "not tray_weights" frueh zurueckkehrt - der Hinweis "extern war aktiv" wuerde
    # damit nie den Job erreichen. Deshalb hier pro Job-Key im State vermerken,
    # bevor irgendein Early-Return greift.
    if states.get(EXTERNAL_ACTIVE_ENTITY, {}).get("state") == "on":
        state.setdefault("external_seen_jobs", {})[job_key] = True

    # Druckzeit-Zaehler IMMER aktualisieren (auch bei internen Systemjobs
    # oder Idle) - sonst entsteht beim naechsten echten Druckstart ein
    # kuenstlicher Zeitsprung, weil last_poll_ts zu lange nicht aktualisiert wurde.
    try:
        druckzeit_counters = _update_druckzeit_counters(state, status)
        _push_druckzeit_to_ha(druckzeit_counters)
    except Exception as e:
        log.warning(f"Druckzeit-Akkumulation fehlgeschlagen: {e}")

    if _is_internal_system_job(gcode):
        return

    # Benutze hardcoded Entity-Namen direkt (keine dynamische Discovery nötig)
    weight_entity = PRINT_WEIGHT_ENTITY
    progress_entity = PRINT_PROGRESS_ENTITY
    status_entity = PRINT_STATUS_ENTITY

    pw_state = states.get(weight_entity, {})
    prog_state = states.get(progress_entity, {})
    tray_weights = _tray_weights(pw_state, tray_key_to_slot)
    fraction = _progress_fraction(prog_state, status)

    # Reine Extern-Spule-Drucke liefern NIE AMS-Tray-Attribute (der Gewichtssensor
    # ersetzt sie durch "External Spool" == Gesamtgewicht, siehe Nachtrag-Logik
    # weiter unten) - "tray_weights leer" ist hier der Normalfall, kein Fehler.
    external_seen_for_job = state.get("external_seen_jobs", {}).get(job_key, False)

    # DEBUG: Überprüfe warum Gewichte möglicherweise leer sind
    if not tray_weights and status == "running" and not external_seen_for_job:
        log.error("❌ CRITICAL: Tray-Weights LEER!")
        log.error(f"   weight_entity={weight_entity}")
        log.error(f"   pw_state keys={list(pw_state.keys())}")
        if "attributes" in pw_state:
            log.error(f"   attributes keys={list(pw_state['attributes'].keys())}")
            log.error(f"   attributes={pw_state['attributes']}")
        log.error(f"   tray_key_to_slot={tray_key_to_slot}")

    # DEBUG: Überprüfe warum tray_weights leer sein könnte
    if status == "running" and not tray_weights and not external_seen_for_job:
        log.warning(f"DEBUG: tray_weights leer bei status={status}")
        log.warning(f"  PRINT_WEIGHT_ENTITY='{PRINT_WEIGHT_ENTITY}'")
        log.warning(f"  pw_state.keys()={list(pw_state.keys())}")
        if "attributes" in pw_state:
            log.warning(f"  Attributes: {pw_state['attributes']}")
        log.warning(f"  tray_key_to_slot={tray_key_to_slot}")

    jobs = state.setdefault("jobs", {})

    # Aussetzer der Bambu-Lab-Integration: Fallen die AMS-Entities weg, liefert
    # der Gewichtssensor keine Tray-Attribute mehr und das Tracking wuerde
    # unten am Early-Return abbrechen, obwohl der Druck weiterlaeuft. Fuer
    # einen bereits bekannten Job die beim Druckstart erfassten Planwerte
    # weiterverwenden - sie sind ueber den ganzen Druck konstant.
    if not tray_weights and status in ("running", "prepare", "finish", "failed"):
        cached_job = jobs.get(job_key)
        if cached_job and cached_job.get("tray_weights"):
            tray_weights = dict(cached_job["tray_weights"])
            # Bei reinen Extern-Drucken der Normalfall (der Sensor ersetzt die
            # AMS-Attribute) und wuerde sonst jede Minute das Log fuellen.
            log.debug(
                f"Tray-Gewichte fehlen (Integration nicht verfuegbar) - nutze "
                f"Planwerte des laufenden Jobs weiter: {tray_weights}"
            )

    # Reine Extern-Spule-Drucke (kein einziges AMS-Tray beteiligt) haben ab dem
    # allerersten Poll leere tray_weights - ohne diese Ausnahme wuerde der Job
    # hier nie angelegt (Bug: Druck vom 2026-09-29 12:30 wurde deshalb komplett
    # nicht erfasst). external_seen_for_job stammt aus dem Extern-Aktiv-Sensor
    # (siehe oben) und ist unabhaengig von tray_weights zuverlaessig.
    if status not in ("running", "prepare", "finish", "failed") or (not tray_weights and not external_seen_for_job):
        prev_key = state.get("current_job_key")
        open_keys = _open_job_keys(jobs, prev_key)
        if status == "idle" and open_keys:
            # Debounce: ein einzelner "idle"-Read kann ein kurzer Aussetzer sein
            # (z.B. HA-Neustart, MQTT-Reconnect) - erst nach 2 aufeinanderfolgenden
            # idle-Polls (~2x POLL_INTERVAL) wird der Job wirklich als abgebrochen
            # gewertet, um echte Drucke nicht durch einen einzigen Wackler zu verlieren.
            idle_streak = state.get("_idle_streak", 0) + 1
            state["_idle_streak"] = idle_streak
            if idle_streak >= 2:
                _cancel_open_jobs(
                    state, open_keys, states, all_spools,
                    f"Status wechselte zu 'idle' ohne finish/failed, {idle_streak}x bestaetigt",
                )
                state["current_job_key"] = None
            else:
                log.info(
                    f"Status 'idle' erkannt (1/2) - warte auf Bestaetigung, "
                    f"bevor {len(open_keys)} offene(r) Druckauftrag/-auftraege als abgebrochen gewertet werden"
                )
            return
        state["_idle_streak"] = 0
        # Ein HA-Ausfall (unavailable/unknown) darf den Verweis auf den
        # laufenden Job nicht ueberschreiben.
        if status == "idle":
            state["current_job_key"] = None
        elif status not in UNAVAILABLE_STATES:
            state["current_job_key"] = job_key
        return

    state["_idle_streak"] = 0

    job = jobs.get(job_key)
    if job is None:
        replayed_key = _find_replayed_job_key(jobs, gcode, start, tray_weights, status)
        if replayed_key:
            ignored = state.setdefault("ignored_replays", {})
            if job_key not in ignored:
                log.warning(
                    f"Druck {gcode} (Start {start}, Status {status}) ist derselbe wie der bereits "
                    f"gebuchte Job {replayed_key} - kein neuer Job, keine zweite Buchung (Issue #53)"
                )
                ignored[job_key] = replayed_key
                for old_key in list(ignored)[:-20]:
                    del ignored[old_key]
            return
    just_created = job is None
    existing_hist = None

    if just_created:
        job = {
            "gcode": gcode,
            "start": start,
            "tray_weights": tray_weights,
            "max_progress": 0.0,
            "segments": {},
            "done": False,
        }
        # Ein neuer Druck beendet jeden noch offenen Job (z.B. wenn der
        # Folgedruck startet, bevor der zweite idle-Poll den Abbruch bestaetigte).
        superseded_keys = _open_job_keys(jobs, state.get("current_job_key"))
        jobs[job_key] = job
        state["current_job_key"] = job_key
        _cancel_open_jobs(state, superseded_keys, states, all_spools, "ein neuer Druck wurde gestartet")

        # Failsafer: Prüfe, ob nach HA-Neustart bereits ein History-Eintrag mit
        # gleichem gcode|start existiert (consumption_state.json wurde nicht geladen).
        # Falls ja, nutze dessen history_id statt eine neue zu erzeugen.
        existing_hist = find_existing_history_entry(gcode, start)
        if existing_hist:
            job["history_id"] = existing_hist["id"]
            log.info(
                f"Neuer Druckauftrag erkannt (nach HA-Neustart): {gcode} (Start {start}) | "
                f"Nutze existierenden History-Eintrag #{job['history_id']} | "
                f"Trays={tray_weights}"
            )
        else:
            log.info(
                f"Neuer Druckauftrag erkannt: {gcode} (Start {start}) | "
                f"Trays={tray_weights}"
            )
    _ensure_job_schema(job)

    if state.get("external_seen_jobs", {}).get(job_key):
        job["external_seen"] = True

    # Planwerte laufend aktualisieren (letzter gueltiger Wert gewinnt): der
    # Gewichtssensor liefert direkt nach Druckstart teils noch den Wert des
    # VORHERIGEN Drucks (z.B. 95.5g statt 151.47g). Erst bei FERTIG/FEHLER
    # zaehlt der Wert endgueltig - dort wird nicht mehr aktualisiert.
    # (Ein bereits abgeschlossener Job bleibt unangetastet - der Start-Sensor
    # kann beim naechsten Druck kurz noch den alten Wert melden.)
    if not job.get("done") and (just_created or status in ("running", "prepare")):
        _refresh_plan_weights(job, tray_weights, pw_state, "External" in tray_key_to_slot)
    if just_created:
        _track_job_spools(job, states, state, all_spools)

    # History-Grunddaten anlegen (auch fuer Jobs aus der Zeit vor diesem Feature):
    # Bild + Aufgabenname werden nur EINMAL beim ersten Erkennen des Jobs
    # gesichert, nicht bei jedem Poll.
    # Der History-Eintrag selbst wird aber sofort geschrieben (Status "started"),
    # nicht erst bei Abschluss.
    if "history_id" not in job:
        with _history_lock:
            history = load_history()
            job["history_id"] = history["next_id"]
            history["next_id"] += 1
            save_history(history)
        job["task_name"] = states.get(TASK_NAME_ENTITY, {}).get("state") or gcode
        job["image_file"] = _save_title_image(states, job)
        job["plate_type"] = _read_plate_type(states)
        _rebuild_colors_used(job, job["max_progress"])
        _write_history_record(job, "started")

    # Selbstheilend: Aufgabenname/Titelbild kommen von HA-Sensoren, die der
    # Drucker per MQTT erst mit Verzoegerung befuellt - beim allerersten Poll
    # direkt nach Druckstart (wo sie oben einmalig abgefragt werden) sind sie
    # oft noch leer. Ohne Retry blieb der Fallback (Gcode-Pfad statt Name,
    # kein Bild) fuer die gesamte Restlaufzeit des Drucks bestehen, da
    # _write_history_record() unten bei jedem Poll ohnehin neu schreibt.
    if job.get("task_name") == job.get("gcode"):
        fresh_name = states.get(TASK_NAME_ENTITY, {}).get("state")
        if fresh_name and fresh_name != job.get("gcode"):
            job["task_name"] = fresh_name
    if not job.get("image_file") or job.get("image_source"):
        # fehlt ganz oder ist nur uebernommen: echtes HA-Bild hat Vorrang
        job["image_file"] = _save_title_image(states, job) or job.get("image_file")
        if not job.get("image_file") and _job_age_s(job) >= TITLE_IMAGE_REUSE_AFTER_S:
            _reuse_title_image(job, f"HA liefert {TITLE_IMAGE_REUSE_AFTER_S // 60} min nach Start kein Bild")
    elif not job.get("done") and _title_image_changed(states, job):
        job["image_file"] = _save_title_image(states, job) or job["image_file"]
    # Leer ODER unbekanntes Format: bei jedem Poll bis einschliesslich Druckende
    # neu lesen (die Integration liefert den Typ teils verzoegert oder erst
    # nach einem MQTT-/Cloud-Aussetzer).
    if not is_known_plate_type(job.get("plate_type")):
        fresh = _read_plate_type(states)
        if fresh and (is_known_plate_type(fresh) or not job.get("plate_type")):
            job["plate_type"] = fresh

    if job.get("done"):
        if job.get("final_booking") and not job.get("history_finalized"):
            _finalize_history(job)
        return

    # Nur beobachten, NICHTS buchen. Reihenfolge ist Absicht: erst die Spule
    # erkennen, dann den Fortschritt fortschreiben - bei einem Spulenwechsel
    # gehoert der Zuwachs seit dem letzten Poll (wie bisher) der Spule, die in
    # diesem Poll erkannt wurde.
    if status in ("running", "prepare"):
        _track_job_spools(job, states, state, all_spools)
        # Direkt nach Druckstart kann der Sensor noch die 100 % des Vorgaengers
        # melden. Ein echter Druck faellt nie von >=99 % auf einen kleinen
        # Wert zurueck - der Altwert wird verworfen (es wurde nichts gebucht).
        if job["max_progress"] >= 0.99 and 0 < fraction < 0.5:
            log.warning(f"Veralteter Fortschritt verworfen: {job['max_progress']*100:.0f}% -> {fraction*100:.0f}%")
            job["max_progress"] = fraction
    if status in ("running", "prepare", "failed"):
        # Der hoechste je gesehene Fortschritt fuehrt nie zurueck - ein kurzer
        # Sensor-Aussetzer (0/unavailable am Druckende) darf den Stand nicht
        # verfaelschen.
        job["max_progress"] = max(job["max_progress"], fraction)

    if status in ("running", "prepare"):
        _rebuild_colors_used(job, job["max_progress"])
        _write_history_record(job, "started")
    else:
        if just_created and _history_already_booked(existing_hist):
            # consumption_state.json ging verloren, der Drucker meldet aber noch
            # den Abschluss des alten Drucks: dessen Verbrauch steht schon im
            # Verlauf - nicht noch einmal buchen.
            log.warning(
                f"Druckauftrag {gcode} ({status}) ist im Verlauf #{existing_hist['id']} bereits "
                f"verbucht - keine erneute Endbuchung"
            )
            job.update(done=True, history_finalized=True, ended_at=utc_now_iso(),
                       final_booking={"status": status, "fraction": 1.0, "items": [],
                                      "skipped": "bereits im Verlauf verbucht"})
        else:
            _finalize_job(job, status, states, state, all_spools)
            _process_final_bookings(state, all_spools, only_job_key=job_key)
        log.info(f"Druckauftrag abgeschlossen ({status}): {gcode}")

    state["current_job_key"] = job_key

    if len(jobs) > 50:
        for old_key in list(jobs.keys())[:-50]:
            # Offene/wartende Endbuchungen niemals mit dem Job wegraeumen
            if not _has_unbooked_items(jobs[old_key]):
                jobs.pop(old_key, None)

    # Extern-Aktiv-Merker mitschrumpfen lassen (nur fuer noch bekannte Jobs
    # behalten), sonst waechst external_seen_jobs unbegrenzt.
    external_seen_jobs = state.get("external_seen_jobs")
    if external_seen_jobs:
        for old_key in [k for k in external_seen_jobs if k not in jobs]:
            external_seen_jobs.pop(old_key, None)

    # Archiviere leere Spulen (falls nicht während Verbrauch-Abzug archiviert wurden)
    archive_empty_spools(all_spools)


def _hex_text_color(hex_color):
    """Waehlt Schwarz oder Weiss als Textfarbe fuer eine Hintergrundfarbe -
    je nachdem, welche Kombination laut WCAG-Kontrastformel besser lesbar ist."""
    hex_color = (hex_color or "").lstrip("#")
    if len(hex_color) != 6:
        return "#ffffff"
    try:
        r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return "#ffffff"

    def _linear(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    bg_luminance = 0.2126 * _linear(r) + 0.7152 * _linear(g) + 0.0722 * _linear(b)
    contrast_white = 1.05 / (bg_luminance + 0.05)
    contrast_black = (bg_luminance + 0.05) / 0.05
    return "#ffffff" if contrast_white >= contrast_black else "#111111"


def _stripped_filament_name(filament):
    """Filament-Name ohne den fuer die Anzeige redundanten Marken-Praefix
    (z.B. 'Bambu PLA Basic Cocoa Brown' -> 'PLA Basic Cocoa Brown';
    Fremdhersteller ohne solchen Praefix bleiben unveraendert)."""
    name = filament.get("name", "")
    vendor_word = ((filament.get("vendor") or {}).get("name") or "").split(" ")[0]
    if vendor_word and name.lower().startswith(vendor_word.lower() + " "):
        name = name[len(vendor_word):].strip()
    return name


def _spool_display_label(spool):
    """'#<Spulen-ID> · <Material/Farbe>' - siehe _stripped_filament_name()."""
    name = _stripped_filament_name(spool.get("filament", {}))
    return f"#{spool['id']} · {name}" if name else f"#{spool['id']}"


def _display_tray_label(tray_key):
    """'AMS 1 Tray 1' -> 'AMS 1 · Slot 1' (gleiche Schreibweise wie die
    HA-Dashboard-Entities), 'External' -> 'Externe Spule'."""
    if tray_key == "External":
        return "Externe Spule"
    m = re.match(r"(AMS \d+) Tray (\d+)$", tray_key)
    return f"{m.group(1)} · Slot {m.group(2)}" if m else tray_key


def _render_slots_page(embed=False):
    """Slot-Zuordnungs-UI: zeigt alle 5 AMS-Slots. Automatisch (RFID) oder
    bereits manuell zugewiesene Spulen erscheinen als farbige Kachel
    (Spulen-Nummer, Material und Farbe als Hintergrund); nur wirklich
    unbekannte Slots zeigen zusaetzlich das Auswahl-Dropdown.

    embed=True (per /slots?embed=1): kompakte Variante ohne Kopfzeile/
    Info-Box - fuer das Einbetten per iframe im HA-Dashboard, damit dort
    kein Scrollbalken durch ueberfluessigen Platz entsteht."""
    state = load_state_locked()
    overrides = state.get("user_spool_overrides", {})

    # Lade alle Spulen aus Spoolman
    try:
        all_spools = sm_get("/spool")
    except Exception as e:
        log.warning(f"Spoolman-Abruf fehlgeschlagen: {e}")
        all_spools = None

    # Hole aktuelle Slot-Informationen von HA
    states = _get_all_ha_states()
    tray_key_to_slot = _build_tray_key_to_slot(states)

    slot_data = []
    for tray_key in tray_key_to_slot:
        slot_entity = tray_key_to_slot.get(tray_key)
        slot_state = states.get(slot_entity, {}) if slot_entity else {}
        filament_name = slot_state.get("state", "–")
        attrs = slot_state.get("attributes", {})
        tag_uid = attrs.get("tag_uid", EMPTY_TAG_UID)

        # RFID nur, wenn der Bambu-AMS tatsaechlich einen physischen Chip
        # ausgelesen hat (tag_uid gesetzt) - der reine Sensor-Name (z.B. bei
        # "Generic PETG") sagt nichts darueber aus, ob eine RFID vorliegt.
        is_rfid = bool(tag_uid) and tag_uid != EMPTY_TAG_UID
        is_empty = filament_name in ("–", "-", "unknown", "Empty", "empty", "")
        override_id = overrides.get(tray_key)

        # Fuer die Anzeige die tatsaechliche Spoolman-Spule aufloesen (Nummer,
        # Material, Farbe) - dieselbe RFID-first-Logik wie beim Verbrauchs-
        # Abzug, damit Kachel und Verbrauchsberechnung nie auseinanderlaufen.
        resolved_spool = None
        if not is_empty and all_spools:
            result = resolve_spool_for_tray(tray_key, states, state, all_spools)
            resolved_spool = result[0] if result else None

        slot_data.append({
            "tray_key": tray_key,
            "filament_name": filament_name,
            "is_rfid": is_rfid,
            "is_empty": is_empty,
            "override_id": override_id,
            "resolved_spool": resolved_spool,
        })

    slots_html = ""
    for slot in slot_data:
        tray = slot["tray_key"]
        name = slot["filament_name"]
        spool = slot["resolved_spool"]
        title_text = html.escape(_display_tray_label(tray))

        # Ganze Kachel (nicht nur ein kleines Element darin) faerben, sobald
        # eine Spule aufgeloest ist - Titel und Inhalt teilen sich denselben
        # Hintergrund, wie bei den nativen HA-Tile-Karten. Ohne aufgeloeste
        # Spule (leer/noch nicht zugewiesen) bleibt die Kachel neutral dunkel.
        card_color_style = ""
        if spool:
            hex_color = (spool.get("filament", {}).get("color_hex") or "").strip() or "6b7280"
            text_color = _hex_text_color(hex_color)
            card_color_style = f' style="background:#{hex_color}; color:{text_color};"'

        if slot["is_empty"]:
            subtitle_html = '<div class="slot-subtitle slot-subtitle-empty">– leer –</div>'
        elif slot["is_rfid"]:
            if spool:
                subtitle_html = f'<div class="slot-subtitle">{html.escape(_spool_display_label(spool))}</div>'
            else:
                # Von der AMS per RFID erkannt, aber (noch) keine passende
                # Spoolman-Spule gefunden - Rohname als Fallback zeigen.
                subtitle_html = f'<div class="slot-subtitle">{html.escape(name)}</div>'
        else:
            # Unbekannt/manuell: Dropdown zum Auswählen bleibt IMMER verfügbar
            # (auch nach einer Auswahl), damit z.B. beim Tausch einer Generic-
            # Spule ohne Chip jederzeit umgestellt werden kann. Der <select>
            # selbst ist transparent gestylt und uebernimmt so die Farbe der
            # umgebenden (bereits gefaerbten) Kachel - eine durchgehende
            # Flaeche statt eines separaten Chips.
            selected_id = slot["override_id"] or ""

            def _spool_option_html(s):
                opt_hex = (s.get("filament", {}).get("color_hex") or "").strip() or "6b7280"
                opt_text_color = _hex_text_color(opt_hex)
                opt_label = html.escape(_spool_display_label(s))
                opt_selected = " selected" if s["id"] == selected_id else ""
                return (
                    f'<option value="{s["id"]}"{opt_selected} '
                    f'style="background:#{opt_hex}; color:{opt_text_color};">{opt_label}</option>'
                )

            spools_options_for_slot = '\n'.join(
                _spool_option_html(s) for s in sorted(all_spools, key=lambda x: x["id"])
            ) if all_spools else ""

            subtitle_html = (
                f'<select class="slot-dropdown{" slot-dropdown-colored" if spool else ""}" '
                f'onchange="assignSlot(\'{html.escape(tray)}\', this.value)">'
                f'    <option value="">– Spule wählen –</option>'
                f'    {spools_options_for_slot}'
                f'  </select>'
            )

        slots_html += f'''
        <div class="slot-card"{card_color_style}>
            <div class="slot-header">{title_text}</div>
            {subtitle_html}
        </div>
        '''

    # Kein f-string: Diese CSS-Regeln enthalten keine Python-Variablen, daher
    # normale (einfache) geschweifte Klammern statt der doppelten Escapes.
    body_padding = "4px" if embed else "20px"
    grid_gap = "6px" if embed else "20px"
    card_padding = "8px 10px" if embed else "20px"
    header_margin = "4px" if embed else "12px"
    css = f'''
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #1a1a1a; color: #fff; margin: 0; padding: {body_padding}; }}
        .container {{ max-width: 1200px; margin: 0 auto; }}
        .header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 30px; }}
        .header h1 {{ margin: 0; }}
        .back-link {{ color: #0ea5e9; text-decoration: none; }} .back-link:hover {{ text-decoration: underline; }}
        .slots-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: {grid_gap}; }}
        .slot-card {{ border: 2px solid #333; border-radius: 12px; padding: {card_padding}; background: #222; transition: all 0.3s; }}
        .slot-header {{ font-weight: 700; font-size: 15px; margin-bottom: {header_margin}; color: inherit; text-shadow: 0 1px 2px rgba(0,0,0,0.25); }}
        .slot-subtitle {{ font-size: 13px; font-weight: 500; color: inherit; opacity: 0.95; text-shadow: 0 1px 2px rgba(0,0,0,0.25); }}
        .slot-subtitle-empty {{ font-style: italic; opacity: 0.5; }}
        .slot-dropdown {{ width: 100%; padding: 6px 8px; border: 1px solid #444; border-radius: 6px; background: #333; color: #fff; font-size: 13px; font-weight: 500; cursor: pointer; }}
        .slot-dropdown-colored {{ background: transparent; border-color: rgba(255,255,255,0.35); color: inherit; }}
        .slot-dropdown:focus {{ outline: none; border-color: #0ea5e9; }}
        .info-box {{ background: #1e40af; border-left: 4px solid #0ea5e9; padding: 12px; border-radius: 6px; margin-bottom: 20px; font-size: 13px; line-height: 1.5; }}
    </style>
    '''

    header_html = "" if embed else '''
            <div class="header">
                <h1>🎛️ AMS-Spulen-Zuordnung</h1>
                <a href="/" class="back-link">← Zurück zur History</a>
            </div>

            <div class="info-box">
                <strong>Farbige Kachel:</strong> Automatisch (RFID) oder bereits manuell zugewiesene Spule – Nummer, Material und Farbe<br>
                <strong>Dropdown:</strong> Erscheint nur bei unbekannten Slots – wähle die korrekte Spule aus, damit der Verbrauch richtig berechnet wird
            </div>
    '''

    html_page = f'''<!DOCTYPE html>
    <html lang="de">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>AMS-Spulen-Zuordnung</title>
        {css}
    </head>
    <body>
        <div class="container">
            {header_html}
            <div class="slots-grid">
                {slots_html}
            </div>
        </div>

        <script>
            function assignSlot(trayKey, spoolId) {{
                // spoolId leer = Platzhalter ausgewaehlt -> Zuordnung
                // ausdruecklich zuruecksetzen (nicht einfach ignorieren),
                // damit z.B. eine entnommene externe Spule wieder als
                // "nicht zugewiesen" gespeichert werden kann.
                var body = 'tray=' + encodeURIComponent(trayKey) + '&spool_id=' + encodeURIComponent(spoolId);
                var xhr = new XMLHttpRequest();
                xhr.open('POST', '/assign-spool', true);
                xhr.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded');
                xhr.onload = function() {{
                    if (xhr.status === 303 || xhr.status === 200) {{
                        location.reload();
                    }} else {{
                        alert('Fehler beim Speichern: ' + xhr.status);
                    }}
                }};
                xhr.onerror = function() {{ alert('Netzwerkfehler'); }};
                xhr.send(body);
            }}
        </script>
    </body>
    </html>
    '''
    return html_page


def log_remaining_report():
    try:
        spools = sm_get("/spool")
        log.info("-- Aktuelle Restmengen in Spoolman --")
        for sp in sorted(spools, key=lambda s: s["id"]):
            f = sp["filament"]
            uid = _normalize_uid(sp.get("extra", {}).get("bambu_tag_uid", ""))
            log.info(
                f"   Spool {sp['id']}: {f['name'][:26]:26s} | "
                f"Rest={sp['remaining_weight']:6.1f}g / {sp.get('initial_weight', 0):.0f}g "
                f"(verbr. {sp['used_weight']:.1f}g) | uid={uid}"
            )
    except Exception as e:
        log.error(f"Report fehlgeschlagen: {e}")


# ─────────────────────────────────────────────────────────────────
# HISTORY-WEBSEITE (Druckverlauf-Tabelle, Umbenennen per Stift-Icon)
# ─────────────────────────────────────────────────────────────────
_STATUS_LABELS = {
    "started": "🟡 Gestartet",
    "finish": "✅ Fertig",
    "failed": "❌ Fehler",
    "cancelled": "⚠️ Abgebrochen",
}

_ADD_ENTRY_JAVASCRIPT = """
<script>
function openAddEntryModal() {
  document.getElementById('add-entry-modal').style.display = 'flex';
  document.getElementById('spools-container').innerHTML = '';
  addSpoolRow();
  var now = new Date();
  now.setMinutes(now.getMinutes() - now.getTimezoneOffset());
  document.getElementById('entry-start').value = now.toISOString().slice(0, 16);
}

function closeAddEntryModal() {
  document.getElementById('add-entry-modal').style.display = 'none';
  document.getElementById('add-entry-form').reset();
}

function addSpoolRow() {
  var container = document.getElementById('spools-container');
  var rowId = 'spool-row-' + Math.random().toString(36).substr(2, 9);
  var row = document.createElement('div');
  row.id = rowId;
  row.style.cssText = 'display: flex; gap: 0.5em; margin-bottom: 0.5em; background: var(--panel-bg); padding: 0.8em; border-radius: 4px;';

  var spoolOptions = '<option value="">-- Spule wählen --</option>';
  for (var id in SPOOLS) {
    spoolOptions += '<option value="' + id + '">' + (SPOOLS[id] || 'Unbekannt') + ' (#' + id + ')</option>';
  }

  row.innerHTML = '<select class="spool-select" style="flex: 1; padding: 0.5em; background: var(--input-bg); color: var(--fg); border: 1px solid var(--input-border); border-radius: 4px;" onchange="updateSpoolInfo(\\'' + rowId + '\\')">' + spoolOptions + '</select>' +
    '<input type="number" class="spool-weight" placeholder="Grams" step="0.01" style="width: 100px; padding: 0.5em; background: var(--input-bg); color: var(--fg); border: 1px solid var(--input-border); border-radius: 4px;" oninput="calculateLength(\\'' + rowId + '\\')">' +
    '<input type="number" class="spool-length" placeholder="Länge (m)" step="0.1" style="width: 100px; padding: 0.5em; background: var(--input-bg); color: var(--fg); border: 1px solid var(--input-border); border-radius: 4px; background: var(--panel-bg);" readonly>' +
    '<button type="button" onclick="removeSpoolRow(\\'' + rowId + '\\')" style="padding: 0.5em 0.8em; background: #ef4444; color: white; border: none; border-radius: 4px; cursor: pointer;">✕</button>';

  container.appendChild(row);
}

function removeSpoolRow(rowId) {
  var row = document.getElementById(rowId);
  if (row) row.remove();
}

function updateSpoolInfo(rowId) {
  var row = document.getElementById(rowId);
  if (!row) return;
  var spoolId = row.querySelector('.spool-select').value;
  var spoolData = SPOOLS_DATA[spoolId];
  if (spoolData && spoolData.filament) {
    row.setAttribute('data-spool-id', spoolId);
    row.setAttribute('data-density', spoolData.filament.density || 1.24);
    row.setAttribute('data-diameter', spoolData.filament.diameter || 1.75);
  }
}

function calculateLength(rowId) {
  var row = document.getElementById(rowId);
  if (!row) return;
  var grams = parseFloat(row.querySelector('.spool-weight').value) || 0;
  var density = parseFloat(row.getAttribute('data-density')) || 1.24;
  var diameter = parseFloat(row.getAttribute('data-diameter')) || 1.75;

  if (grams > 0 && density > 0 && diameter > 0) {
    var radiusCm = (diameter / 10) / 2;
    var volumeCm3 = grams / density;
    var lengthCm = volumeCm3 / (Math.PI * radiusCm * radiusCm);
    var lengthM = lengthCm / 100;
    row.querySelector('.spool-length').value = lengthM.toFixed(2);
  } else {
    row.querySelector('.spool-length').value = '';
  }
}

// Das Skript steht im <head>; das Formular existiert erst nach dem Laden der Seite.
document.addEventListener('DOMContentLoaded', function() {
document.getElementById('add-entry-form').addEventListener('submit', function(e) {
  e.preventDefault();

  var name = document.getElementById('entry-name').value.trim();
  var start = document.getElementById('entry-start').value;
  var end = document.getElementById('entry-end').value;
  var status = document.getElementById('entry-status').value;
  var gcode = document.getElementById('entry-gcode').value.trim();

  if (!name || !start || !end || !status) {
    alert('Bitte alle Pflichtfelder ausfüllen');
    return;
  }

  var spoolRows = document.querySelectorAll('#spools-container > div');
  var colors = [];

  spoolRows.forEach(function(row) {
    var spoolId = row.querySelector('.spool-select').value;
    var weight = parseFloat(row.querySelector('.spool-weight').value);
    var length = parseFloat(row.querySelector('.spool-length').value);

    if (spoolId && weight > 0) {
      var spoolData = SPOOLS_DATA[spoolId];
      colors.push({
        spool_id: parseInt(spoolId),
        filament_name: spoolData && spoolData.filament ? spoolData.filament.name : 'Unbekannt',
        material: spoolData && spoolData.filament ? spoolData.filament.material : 'Unknown',
        color_hex: spoolData && spoolData.filament && spoolData.filament.color_hex ? spoolData.filament.color_hex : 'CCCCCC',
        density: spoolData && spoolData.filament ? spoolData.filament.density : 1.24,
        diameter: spoolData && spoolData.filament ? spoolData.filament.diameter : 1.75,
        grams: weight,
        length_m: length
      });
    }
  });

  if (colors.length === 0) {
    alert('Bitte mindestens eine Spule mit Gewicht hinzufügen');
    return;
  }

  var formData = new FormData();
  formData.append('name', name);
  formData.append('start', start);
  formData.append('end', end);
  formData.append('status', status);
  formData.append('gcode', gcode);
  formData.append('colors', JSON.stringify(colors));

  var imageFile = document.getElementById('entry-image').files[0];
  if (imageFile) formData.append('image', imageFile);

  var cameraFile = document.getElementById('entry-camera').files[0];
  if (cameraFile) formData.append('camera', cameraFile);

  var xhr = new XMLHttpRequest();
  xhr.open('POST', '/add-history', true);
  xhr.onload = function() {
    if (xhr.status === 200 || xhr.status === 303) {
      alert('Eintrag erfolgreich erstellt!');
      closeAddEntryModal();
      location.reload();
    } else {
      alert('Fehler beim Speichern: ' + xhr.status + ' - ' + xhr.responseText);
    }
  };
  xhr.onerror = function() { alert('Netzwerkfehler'); };
  xhr.send(formData);
});
});
</script>
"""

_HISTORY_PAGE = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Druckverlauf</title>
<script>
(function() {{
  var saved = localStorage.getItem('historyTheme');
  if (saved === 'light' || saved === 'dark') {{
    document.documentElement.setAttribute('data-theme', saved);
  }}
}})();
</script>
<style>
  * {{ box-sizing: border-box; }}
  :root {{
    --bg: #ffffff; --fg: #1a1a1a; --panel-bg: #f0f0f0; --border: #ccc;
    --row-alt: #f7f7f7; --muted: #666; --input-bg: #ffffff; --input-border: #999;
    --overlay-bg: rgba(0,0,0,0.75);
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #1c1c1c; --fg: #eee; --panel-bg: #2b2b2b; --border: #444;
      --row-alt: #242424; --muted: #aaa; --input-bg: #2b2b2b; --input-border: #555;
      --overlay-bg: rgba(0,0,0,0.85);
    }}
  }}
  html[data-theme="light"] {{
    --bg: #ffffff; --fg: #1a1a1a; --panel-bg: #f0f0f0; --border: #ccc;
    --row-alt: #f7f7f7; --muted: #666; --input-bg: #ffffff; --input-border: #999;
    --overlay-bg: rgba(0,0,0,0.75);
  }}
  html[data-theme="dark"] {{
    --bg: #1c1c1c; --fg: #eee; --panel-bg: #2b2b2b; --border: #444;
    --row-alt: #242424; --muted: #aaa; --input-bg: #2b2b2b; --input-border: #555;
    --overlay-bg: rgba(0,0,0,0.85);
  }}
  html, body {{ width: 100%; margin: 0; padding: 0; background: var(--bg); color: var(--fg);
                font-family: sans-serif; }}
  .wrap {{ width: 100%; padding: 1.2em; }}
  .head-row {{ display: flex; align-items: center; gap: 0.6em; }}
  h2 {{ margin-top: 0; flex: 1; }}
  .theme-toggle {{ background: var(--panel-bg); color: var(--fg); border: 1px solid var(--border);
                    border-radius: 6px; padding: 0.4em 0.6em; cursor: pointer; font-size: 1.1em; }}
  .kpis {{ display: flex; flex-wrap: wrap; gap: 0.8em; margin-bottom: 1.2em; }}
  .kpi {{ background: var(--panel-bg); border: 1px solid var(--border); border-radius: 8px;
          padding: 0.8em 1.2em; min-width: 130px; }}
  .kpi .val {{ font-size: 1.6em; font-weight: bold; display: block; }}
  .kpi .lbl {{ font-size: 0.8em; color: var(--muted); }}
  .toolbar {{ margin-bottom: 0.8em; }}
  .toolbar input[type=search] {{ background: var(--input-bg); color: var(--fg);
                                  border: 1px solid var(--input-border);
                                  padding: 0.4em; width: 260px; max-width: 100%; }}
  .table-scroll {{ width: 100%; overflow-x: auto; }}
  table {{ border-collapse: collapse; width: 100%; min-width: 880px; table-layout: fixed; }}
  th, td {{ border: 1px solid var(--border); padding: 0.5em; text-align: left; vertical-align: middle;
            word-wrap: break-word; overflow: hidden; }}
  th {{ background: var(--panel-bg); position: relative; }}
  th a {{ color: inherit; text-decoration: none; }}
  .col-resizer {{ position: absolute; top: 0; right: 0; width: 8px; height: 100%;
                  cursor: col-resize; user-select: none; }}
  .col-resizer:hover, .col-resizer.active {{ background: rgba(128,128,128,0.4); }}
  tr:nth-child(even) {{ background: var(--row-alt); }}
  img.thumb {{ max-width: 110px; max-height: 90px; border-radius: 4px; display: block; cursor: pointer; }}
  img.thumb.plate-thumb {{ max-width: 44px; max-height: 44px; margin: 0 auto; cursor: default; }}
  .plate-text {{ font-size: 0.75rem; line-height: 1.2; display: inline-block; max-width: 90px; }}
  .swatch {{ display: inline-block; width: 12px; height: 12px; border: 1px solid var(--muted);
             margin-right: 5px; border-radius: 2px; vertical-align: middle; }}
  .edit-btn, .manage-btn, .del-btn {{ cursor: pointer; text-decoration: none; margin-left: 6px; }}
  .pending-hint {{ color: var(--muted); font-size: 0.9em; font-style: italic; }}
  .verbrauch-ts {{ color: var(--muted); font-size: 0.8em; margin-top: 2px; }}
  input[type=text] {{ background: var(--input-bg); color: var(--fg); border: 1px solid var(--input-border);
                       padding: 0.2em; width: 60%; }}
  .empty {{ color: var(--muted); padding: 3em 1em; text-align: center; width: 100%; }}
  #cam-modal {{ display: none; position: fixed; inset: 0; background: var(--overlay-bg);
                z-index: 999; align-items: center; justify-content: center; }}
  #cam-modal img {{ max-width: 92%; max-height: 92%; border-radius: 6px; }}
  @media (max-width: 600px) {{
    .wrap {{ padding: 0.6em; }}
    .toolbar input[type=search] {{ width: 100%; }}
    .kpi {{ min-width: 100px; padding: 0.6em 0.8em; }}
  }}
</style>
<script>
function toggleTheme() {{
  var root = document.documentElement;
  var current = root.getAttribute('data-theme');
  var isDark = current === 'dark' || (!current && window.matchMedia('(prefers-color-scheme: dark)').matches);
  var next = isDark ? 'light' : 'dark';
  root.setAttribute('data-theme', next);
  localStorage.setItem('historyTheme', next);
}}
var COL_STORAGE_KEY = 'historyColWidths';
function applyStoredColWidths() {{
  try {{
    var saved = JSON.parse(localStorage.getItem(COL_STORAGE_KEY) || '{{}}');
    Object.keys(saved).forEach(function(key) {{
      var col = document.getElementById('col-' + key);
      if (col) col.style.width = saved[key] + 'px';
    }});
  }} catch (e) {{}}
}}
function saveColWidth(key, px) {{
  try {{
    var saved = JSON.parse(localStorage.getItem(COL_STORAGE_KEY) || '{{}}');
    saved[key] = px;
    localStorage.setItem(COL_STORAGE_KEY, JSON.stringify(saved));
  }} catch (e) {{}}
}}
function initColResizers() {{
  document.querySelectorAll('.col-resizer').forEach(function(handle) {{
    handle.addEventListener('mousedown', function(e) {{
      e.preventDefault();
      e.stopPropagation();
      var key = handle.getAttribute('data-col');
      var col = document.getElementById('col-' + key);
      var startX = e.pageX;
      var startWidth = col.offsetWidth;
      handle.classList.add('active');
      function onMove(e2) {{
        col.style.width = Math.max(40, startWidth + (e2.pageX - startX)) + 'px';
      }}
      function onUp() {{
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        handle.classList.remove('active');
        saveColWidth(key, col.offsetWidth);
      }}
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    }});
  }});
}}
document.addEventListener('DOMContentLoaded', function() {{
  applyStoredColWidths();
  initColResizers();
}});
function editName(id) {{
  document.getElementById('name-view-' + id).style.display = 'none';
  document.getElementById('name-edit-' + id).style.display = 'inline';
}}
function deleteEntry(id) {{
  if (confirm('Eintrag #' + id + ' wirklich loeschen?')) {{
    document.getElementById('del-form-' + id).submit();
  }}
  return false;
}}
function filterTable() {{
  if (typeof applyTableView === 'function') applyTableView();
}}
function showCameraPhoto(id) {{
  document.getElementById('cam-img').src = '/camera_image/' + id;
  document.getElementById('cam-modal').style.display = 'flex';
}}
function hideCameraPhoto() {{
  document.getElementById('cam-modal').style.display = 'none';
}}
const SPOOLS = {{}};
function populateSpoolDropdowns() {{
  document.querySelectorAll('select.spool-dropdown').forEach(function(sel) {{
    if (sel.options.length > 1) return;  // already populated
    sel.innerHTML = '<option value="">-- Spule wählen --</option>';
    Object.entries(SPOOLS).forEach(function([id, name]) {{
      var opt = document.createElement('option');
      opt.value = id;
      opt.textContent = '#' + id + ' ' + name;
      sel.appendChild(opt);
    }});
  }});
}}
function assignSpool(trayKey, element) {{
  var spool_id = element.value;
  if (!spool_id) {{
    alert('Bitte eine Spule auswählen');
    return;
  }}
  var body = 'tray=' + encodeURIComponent(trayKey) + '&spool_id=' + encodeURIComponent(spool_id);
  var xhr = new XMLHttpRequest();
  xhr.open('POST', '/assign-spool', true);
  xhr.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded');
  xhr.onload = function() {{
    if (xhr.status === 303 || xhr.status === 200) {{
      location.reload();
    }} else {{
      alert('Fehler beim Speichern: ' + xhr.status);
    }}
  }};
  xhr.onerror = function() {{ alert('Netzwerkfehler'); }};
  xhr.send(body);
}}
var SPOOLS_DATA = {{}};
fetch('/api/spools.json')
  .then(function(r) {{ return r.json(); }})
  .then(function(spools) {{
    spools.forEach(function(s) {{
      SPOOLS[s.id] = s.filament ? s.filament.name : 'unbekannt';
      SPOOLS_DATA[s.id] = s;
    }});
    populateSpoolDropdowns();
  }})
  .catch(function(e) {{ console.log('Spulen-List nicht erreichbar:', e); }});
</script>
{add_entry_js}
{table_tools}
</head>
<body>
<div class="wrap">
<div class="head-row">
<h2>Druckverlauf <span style="font-size: 0.6em; color: var(--muted); font-weight: normal; margin-left: 10px;">({version})</span></h2>
<div style="display: flex; gap: 10px;">
  <button onclick="openAddEntryModal()" style="color: #22c55e; text-decoration: none; font-size: 14px; padding: 8px 12px; border: 1px solid #22c55e; border-radius: 4px; transition: all 0.2s; background: transparent; cursor: pointer; font-weight: 500;">➕ Neuer Eintrag</button>
  <a href="/slots" style="color: #0ea5e9; text-decoration: none; font-size: 14px; padding: 8px 12px; border: 1px solid #0ea5e9; border-radius: 4px; transition: all 0.2s;">🎛️ AMS-Spulen</a>
  <a href="/cloud-token" style="color: #0ea5e9; text-decoration: none; font-size: 14px; padding: 8px 12px; border: 1px solid #0ea5e9; border-radius: 4px; transition: all 0.2s;">☁️ Cloud-Token</a>
  {log_level_control}
  <button class="theme-toggle" onclick="toggleTheme()" title="Hell/Dunkel umschalten">🌓</button>
</div>
</div>
{kpis}
<div class="toolbar">
  <input type="search" id="search" list="names" placeholder="Nach Name filtern..." oninput="filterTable()">
  <datalist id="names">{name_options}</datalist>
  <button type="button" id="filter-reset" class="filter-reset" hidden>Filter zur&uuml;cksetzen</button>
  <span id="filter-status" class="filter-status" role="status" aria-live="polite"></span>
</div>
{content}
</div>
<div id="cam-modal" onclick="hideCameraPhoto()">
  <img id="cam-img" src="" alt="Kamerafoto bei Druckende">
</div>

<div id="add-entry-modal" style="display: none; position: fixed; inset: 0; background: var(--overlay-bg); z-index: 1000; align-items: center; justify-content: center;">
  <div style="background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 2em; max-width: 600px; width: 90%; max-height: 90vh; overflow-y: auto;">
    <h3 style="margin-top: 0;">Neuer Druckverlauf-Eintrag</h3>

    <form id="add-entry-form" style="display: flex; flex-direction: column; gap: 1em;">
      <div>
        <label style="display: block; font-weight: bold; margin-bottom: 0.3em;">Druckname *</label>
        <input type="text" id="entry-name" required style="width: 100%; padding: 0.5em; background: var(--input-bg); color: var(--fg); border: 1px solid var(--input-border); border-radius: 4px;">
      </div>

      <div style="display: flex; gap: 1em;">
        <div style="flex: 1;">
          <label style="display: block; font-weight: bold; margin-bottom: 0.3em;">Startzeit *</label>
          <input type="datetime-local" id="entry-start" required style="width: 100%; padding: 0.5em; background: var(--input-bg); color: var(--fg); border: 1px solid var(--input-border); border-radius: 4px;">
        </div>
        <div style="flex: 1;">
          <label style="display: block; font-weight: bold; margin-bottom: 0.3em;">Endzeit *</label>
          <input type="datetime-local" id="entry-end" required style="width: 100%; padding: 0.5em; background: var(--input-bg); color: var(--fg); border: 1px solid var(--input-border); border-radius: 4px;">
        </div>
      </div>

      <div>
        <label style="display: block; font-weight: bold; margin-bottom: 0.3em;">Status *</label>
        <select id="entry-status" required style="width: 100%; padding: 0.5em; background: var(--input-bg); color: var(--fg); border: 1px solid var(--input-border); border-radius: 4px;">
          <option value="finish">✓ Erfolgreich (finish)</option>
          <option value="failed">✗ Fehler (failed)</option>
          <option value="cancelled">⊘ Abgebrochen (cancelled)</option>
        </select>
      </div>

      <div>
        <label style="display: block; font-weight: bold; margin-bottom: 0.3em;">G-Code Datei (optional)</label>
        <input type="text" id="entry-gcode" style="width: 100%; padding: 0.5em; background: var(--input-bg); color: var(--fg); border: 1px solid var(--input-border); border-radius: 4px; font-size: 0.9em;" placeholder="/data/Metadata/plate_1.gcode">
      </div>

      <div>
        <label style="display: block; font-weight: bold; margin-bottom: 0.3em;">Filamente (Spulen) *</label>
        <div id="spools-container"></div>
        <button type="button" onclick="addSpoolRow()" style="margin-top: 0.5em; padding: 0.4em 0.8em; background: #0ea5e9; color: white; border: none; border-radius: 4px; cursor: pointer;">+ Spule hinzufügen</button>
      </div>

      <div style="display: flex; gap: 1em;">
        <div style="flex: 1;">
          <label style="display: block; font-weight: bold; margin-bottom: 0.3em;">Thumbnail (optional)</label>
          <input type="file" id="entry-image" accept="image/*" style="width: 100%; padding: 0.3em; background: var(--input-bg); color: var(--fg); border: 1px solid var(--input-border); border-radius: 4px; font-size: 0.9em;">
        </div>
        <div style="flex: 1;">
          <label style="display: block; font-weight: bold; margin-bottom: 0.3em;">Kamerafoto (optional)</label>
          <input type="file" id="entry-camera" accept="image/*" style="width: 100%; padding: 0.3em; background: var(--input-bg); color: var(--fg); border: 1px solid var(--input-border); border-radius: 4px; font-size: 0.9em;">
        </div>
      </div>

      <div style="display: flex; gap: 1em; margin-top: 1em;">
        <button type="submit" style="flex: 1; padding: 0.8em; background: #22c55e; color: white; border: none; border-radius: 4px; cursor: pointer; font-weight: bold;">Speichern</button>
        <button type="button" onclick="closeAddEntryModal()" style="flex: 1; padding: 0.8em; background: var(--panel-bg); color: var(--fg); border: 1px solid var(--border); border-radius: 4px; cursor: pointer;">Abbrechen</button>
      </div>
    </form>
  </div>
</div>

</body>
</html>
"""


def _compute_kpis(records):
    total = len(records)
    finished = sum(1 for r in records if r.get("status") == "finish")
    terminal = sum(1 for r in records if r.get("status") in ("finish", "failed", "cancelled"))
    success_pct = round(finished / terminal * 100) if terminal else 0

    # Sicheres Type-Casting für numerische Felder (können Strings sein, wenn manuell in DB editiert)
    def safe_float(v):
        try:
            return float(v) if v else 0
        except (ValueError, TypeError):
            return 0

    def safe_int(v):
        try:
            return int(v) if v else 0
        except (ValueError, TypeError):
            return 0

    total_minutes = sum(safe_int(r.get("duration_min")) for r in records)
    all_colors = [c for r in records for c in (r.get("colors") or [])]
    total_grams = sum(safe_float(c.get("grams")) for c in all_colors)
    total_meters = sum(safe_float(c.get("length_m")) for c in all_colors)
    return {
        "total": total,
        "success_pct": success_pct,
        "total_hours": total_minutes / 60,
        "total_kg": total_grams / 1000,
        "total_meters": total_meters,
    }


def _render_kpis(k):
    tiles = [
        (str(k["total"]), "Drucke gesamt"),
        (f'{k["success_pct"]}%', "Erfolgsquote"),
        (f'{k["total_hours"]:.1f} h', "Druckzeit gesamt"),
        (f'{k["total_kg"]:.2f} kg', "Filament verbraucht"),
        (f'{k["total_meters"]:.1f} m', "Laenge verbraucht"),
    ]
    tiles_html = "".join(
        f'<div class="kpi"><span class="val">{v}</span><span class="lbl">{l}</span></div>'
        for v, l in tiles
    )
    return f'<div class="kpis">{tiles_html}</div>'


_COLUMNS = [
    ("id", "ID"),
    ("platte", "Platte"),
    ("bild", "Bild"),
    ("name", "Name"),
    ("datum", "Datum"),
    ("dauer", "Dauer"),
    ("farbe", "Farbe"),
    ("material", "Material"),
    ("spule", "Spule"),
    ("verbrauch_g", "Verbrauch (g)"),
    ("verbrauch_m", "Verbrauch (m)"),
    ("status", "Status"),
]

# Sortierbar sind alle Spalten ausser dem Vorschaubild. Filterbar per
# Mehrfachauswahl sind die Spalten, deren Inhalt sich wiederholt (Issue #52).
SORTABLE_COLUMNS = tuple(key for key, _ in _COLUMNS if key != "bild")
FILTER_COLUMNS = ("farbe", "material", "spule")
FILTER_EMPTY_LABEL = "(leer)"


def _render_colgroup():
    return "".join(f'<col id="col-{key}">' for key, _ in _COLUMNS)


def _render_filter_dropdown(key, label, values):
    """Mehrfachauswahl fuer eine Spalte. Das Panel wird erst vom Skript positioniert
    und eingeblendet (Tastatur: Enter/Leertaste oeffnet, Esc schliesst)."""
    panel_id = f"filter-panel-{key}"
    checkboxes = "".join(
        f'<label><input type="checkbox" value="{html.escape(value, quote=True)}">'
        f'<span>{html.escape(value) if value else FILTER_EMPTY_LABEL}</span></label>'
        for value in values
    )
    return (
        f'<button type="button" class="filter-btn" data-filter="{key}" aria-haspopup="true" '
        f'aria-expanded="false" aria-controls="{panel_id}">'
        f'<span aria-hidden="true">&#9662;</span><span class="sr-only">{label} filtern</span>'
        f'<span class="filter-count" aria-hidden="true"></span></button>'
        f'<div class="filter-panel" id="{panel_id}" role="group" aria-label="{label} filtern" hidden>'
        f'<button type="button" class="filter-clear" data-filter-clear="{key}">Auswahl zur&uuml;cksetzen</button>'
        f'{checkboxes}</div>'
    )


def _render_table_header(sort_dir, filter_options=None):
    filter_options = filter_options or {}
    cells = []
    for key, label in _COLUMNS:
        if key in SORTABLE_COLUMNS:
            is_active = key == "datum"
            arrow = ("\u25bc" if sort_dir == "desc" else "\u25b2") if is_active else ""
            inner = (f'<button type="button" class="sort-btn" data-sort="{key}">{label}'
                     f'<span class="sort-arrow" aria-hidden="true">{arrow}</span></button>')
        else:
            is_active = False
            inner = label
        if key in FILTER_COLUMNS:
            inner += _render_filter_dropdown(key, label, filter_options.get(key, []))
        aria_sort = ""
        if is_active:
            aria_sort = ' aria-sort="descending"' if sort_dir == "desc" else ' aria-sort="ascending"'
        cells.append(f'<th{aria_sort}><div class="th-inner">{inner}</div>'
                     f'<span class="col-resizer" data-col="{key}"></span></th>')
    return "<thead><tr>" + "".join(cells) + "</tr></thead>"


def _line_filter_values(color, overrides, spools_by_id):
    """(Farbe, Material, Spule) einer Verbrauchszeile als reiner Text - dieselbe
    Zuordnung wie die Anzeige in _render_history_rows. Leer = nicht vorhanden."""
    spool_id = color.get("spool_id")
    if spool_id is not None:
        name = color.get("filament_name") or ""
        return ("" if name == "-" else name), color.get("material") or "", f"#{spool_id}"
    override_id = overrides.get(color.get("tray_key")) if color.get("tray_key") else None
    override_spool = spools_by_id.get(override_id) if override_id else None
    if override_spool:
        filament = override_spool.get("filament") or {}
        return filament.get("name") or "", filament.get("material") or "", f"#{override_id}"
    return "", color.get("material") or "", ""


def _filter_sort_key(column, value):
    """Reihenfolge der Filterauswahl: Spulen nach Nummer, sonst alphabetisch."""
    if column == "spule":
        digits = value.lstrip("#")
        return (0, int(digits), "") if digits.isdigit() else (1, 0, value.lower())
    return (0, 0, value.lower())


def _collect_filter_options(records, overrides, spools_by_id):
    """Alle tatsaechlich vorkommenden Werte je Filterspalte; '' (leer) zuerst."""
    found = {column: set() for column in FILTER_COLUMNS}
    for rec in records:
        for color in rec.get("colors") or [{}]:
            for column, value in zip(FILTER_COLUMNS, _line_filter_values(color, overrides, spools_by_id)):
                found[column].add(value)
    return {column: sorted(values, key=lambda v, c=column: (v != "", _filter_sort_key(c, v)))
            for column, values in found.items()}


def _number_attr(value):
    return str(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else ""


def _record_table_attrs(rec, colors, status_label, plate_text, overrides, spools_by_id):
    """data-Attribute fuer die erste Tabellenzeile eines Drucks: Sortierwerte (data-s-*)
    und Filtermengen (data-f-*). Das Skript sortiert/filtert damit ganze Drucke, auch
    wenn sie ueber mehrere Zeilen (Mehrfarbdruck) laufen."""
    lines = [_line_filter_values(color, overrides, spools_by_id) for color in colors]
    gram_values = [c.get("grams") for c in colors if isinstance(c.get("grams"), (int, float))]
    meter_values = [c.get("length_m") for c in colors if isinstance(c.get("length_m"), (int, float))]
    first_farbe, first_material, first_spule = lines[0] if lines else ("", "", "")
    spool_number = first_spule.lstrip("#")
    sort_values = {
        "id": _number_attr(rec.get("id")),
        # Datum sortiert wie bisher nach der Erkennungsreihenfolge (ID), nicht nach dem
        # rohen Startzeit-Sensorwert - siehe Kommentar in _render_history_rows.
        "datum": _number_attr(rec.get("id")),
        "platte": plate_text.lower(),
        "name": (rec.get("name") or "").lower(),
        "dauer": _number_attr(rec.get("duration_min")),
        "farbe": first_farbe.lower(),
        "material": first_material.lower(),
        "spule": spool_number if spool_number.isdigit() else "",
        "verbrauch_g": _number_attr(round(sum(gram_values), 3)) if gram_values else "",
        "verbrauch_m": _number_attr(round(sum(meter_values), 3)) if meter_values else "",
        # ohne das Symbol vor dem Text ("✅ Fertig"), sonst sortiert nach dem Emoji statt nach dem Wort
        "status": re.sub(r"^\W+", "", str(status_label)).lower(),
    }
    attrs = [f' data-s-{key}="{html.escape(value, quote=True)}"' for key, value in sort_values.items()]
    for index, column in enumerate(FILTER_COLUMNS):
        values = list(dict.fromkeys(line[index] for line in lines)) or [""]
        attrs.append(f" data-f-{column}='{html.escape(json.dumps(values, ensure_ascii=False), quote=True)}'")
    return "".join(attrs)


def _render_history_rows(records, sort_dir="desc", overrides=None, spools_by_id=None):
    overrides = overrides or {}
    spools_by_id = spools_by_id or {}
    rows = []
    # Sortierung nach interner ID statt nach dem rohen "start"-Sensorwert: die
    # ID ist ein monoton steigender Zaehler in Erkennungsreihenfolge und daher
    # zuverlaessig, waehrend "start" bei fehlendem/"?"-Sensorwert (z.B. HA kurz
    # nicht erreichbar) faelschlich ueber echten Datums-Strings einsortiert.
    ordered = sorted(records, key=lambda r: r.get("id", 0), reverse=(sort_dir != "asc"))
    row_number = 0
    for rec in ordered:
        colors = rec.get("colors") or [{"filament_name": "-", "grams": 0, "color_hex": None}]
        rowspan = len(colors)
        rec_id = rec["id"]
        status_label = _STATUS_LABELS.get(rec.get("status"), rec.get("status", "?"))
        date_str = _to_local_display(rec.get("start")) if not _is_unreadable_start(rec.get("start")) else "Start unbekannt"
        duration = rec.get("duration_min")
        duration_str = f"{duration} min" if duration is not None else "?"
        name = html.escape(rec.get("name") or "?")
        name_attr = html.escape((rec.get("name") or "").lower())

        # Nur anzeigen, wenn die Datei wirklich existiert (Altbestand kann einen
        # Dateinamen ohne Datei haben, Issue #33). ?v= verhindert, dass der
        # Browser nach einem nachgetragenen/aktualisierten Bild das alte zeigt.
        if rec_id is not None and os.path.exists(os.path.join(HISTORY_IMAGES_DIR, f"{rec_id}.png")):
            version = re.sub(r"[^0-9A-Za-z]", "", str(rec.get("title_image_ts") or ""))[:20]
            img_src = f"/image/{rec_id}" + (f"?v={version}" if version else "")
            if rec.get("camera_image"):
                img_html = (
                    f'<img class="thumb" src="{img_src}" alt="" loading="lazy" '
                    f'onclick="showCameraPhoto({rec_id})" title="Kamerafoto bei Druckende anzeigen">'
                )
            else:
                img_html = f'<img class="thumb" src="{img_src}" alt="" loading="lazy">'
        else:
            img_html = "–"

        # Anzeige normalisiert auch Altbestaende (z.B. "Textured PEI Plate"),
        # ohne die gespeicherten Daten zu veraendern.
        plate_type = normalize_plate_type(rec.get("plate_type"))
        plate_sort_text = PLATE_TYPE_LABELS.get(plate_type, plate_type or "")
        if plate_type in PLATE_TYPE_IMAGE_IDS:
            plate_label = html.escape(PLATE_TYPE_LABELS[plate_type])
            plate_html = (
                f'<img class="thumb plate-thumb" src="/plate-image/{plate_type}" '
                f'alt="{plate_label}" title="{plate_label}" loading="lazy">'
            )
        elif plate_type:
            known = is_known_plate_type(plate_type)
            plate_text = html.escape(PLATE_TYPE_LABELS.get(plate_type, plate_type))
            hint = "" if known else ' title="Unbekannter Plattentyp vom Drucker"'
            plate_html = f'<span class="plate-text"{hint}>{plate_text}</span>'
        else:
            plate_html = "–"

        name_html = (
            f'<span id="name-view-{rec_id}">{name}'
            f'<a class="edit-btn" href="#" onclick="editName({rec_id});return false;">✏️</a>'
            f'<a class="manage-btn" href="/manage-history/{rec_id}" title="Verbrauch bearbeiten">⚙️</a>'
            f'<a class="del-btn" href="#" onclick="return deleteEntry({rec_id});">🗑️</a></span>'
            f'<span id="name-edit-{rec_id}" style="display:none">'
            f'<form method="post" action="/rename" style="display:inline">'
            f'<input type="hidden" name="id" value="{rec_id}">'
            f'<input type="text" name="name" value="{name}">'
            f'<button type="submit">OK</button></form></span>'
            f'<form id="del-form-{rec_id}" method="post" action="/delete" style="display:none">'
            f'<input type="hidden" name="id" value="{rec_id}"></form>'
        )

        for i, c in enumerate(colors):
            color_hex = c.get("color_hex") or ""
            swatch = f'<span class="swatch" style="background:#{html.escape(color_hex)}"></span>' if color_hex else ""
            filament_name = html.escape(c.get("filament_name") or "-")
            material = html.escape(c.get("material") or "-")
            spool_id = c.get("spool_id")
            spool_str = f"#{spool_id}" if spool_id is not None else "-"
            grams = c.get("grams")
            grams_str = f"{grams:.1f}g" if isinstance(grams, (int, float)) else "-"
            # Failsafe: solange der Druck nicht "Fertig" ist, Zeitstempel des
            # letzten Verbrauchs-Updates anzeigen, damit auf einen Blick
            # erkennbar ist, ob der Wert noch aktuell ist (statt z.B. durch
            # eine Poll-Stoerung eingefroren) - nach Abschluss ist der Wert
            # ohnehin final und der Zeitstempel nicht mehr relevant.
            if rec.get("status") != "finish" and c.get("last_update"):
                time_part = _to_local_display(c["last_update"]).split(" ")[-1]
                grams_str += f'<div class="verbrauch-ts">Stand: {time_part} Uhr</div>'
            length_m = c.get("length_m")
            length_str = f"{length_m:.2f}m" if isinstance(length_m, (int, float)) and length_m else "-"

            # Zebra-Streifen je Zeile; das Skript berechnet sie nach Sortieren/Filtern neu
            row_class = ' class="row-alt"' if row_number % 2 == 1 else ""
            row_number += 1
            first_row_attrs = (
                _record_table_attrs(rec, colors, status_label, plate_sort_text, overrides, spools_by_id)
                if i == 0 else ""
            )
            rows.append(f'<tr{row_class} data-name="{name_attr}" data-rec="{rec_id}"{first_row_attrs}>')
            if i == 0:
                rows.append(f'<td rowspan="{rowspan}" style="text-align:center;font-weight:bold;color:#666;">#{rec_id}</td>')
                rows.append(f'<td rowspan="{rowspan}" style="text-align:center;">{plate_html}</td>')
                rows.append(f'<td rowspan="{rowspan}">{img_html}</td>')
                rows.append(f'<td rowspan="{rowspan}">{name_html}</td>')
                rows.append(f'<td rowspan="{rowspan}">{date_str}</td>')
                rows.append(f'<td rowspan="{rowspan}">{duration_str}</td>')
            # Kein Dropdown mehr in der History (der tray_key ging hier frueher
            # verloren -> Zuordnung landete auf "unknown" und griff nie).
            # Zuordnung passiert ausschliesslich ueber /slots ("AMS-Spulen");
            # hier wird nur noch deren Ergebnis angezeigt (source of truth).
            if spool_id is None:
                tray_key = c.get("tray_key")
                override_id = overrides.get(tray_key) if tray_key else None
                override_spool = spools_by_id.get(override_id) if override_id else None
                if override_spool:
                    ov_hex = (override_spool.get("filament") or {}).get("color_hex") or ""
                    ov_swatch = (
                        f'<span class="swatch" style="background:#{html.escape(ov_hex)}"></span>'
                        if ov_hex else ""
                    )
                    ov_name = html.escape((override_spool.get("filament") or {}).get("name") or "-")
                    spool_cell = f'{ov_swatch}{ov_name} <em class="pending-hint">(wird verbucht)</em>'
                    material = html.escape((override_spool.get("filament") or {}).get("material") or "-")
                    spool_str = f"#{override_id}"
                else:
                    spool_cell = '<em class="pending-hint">– nicht zugeordnet (siehe 🎛️ AMS-Spulen) –</em>'
            else:
                spool_cell = f'{swatch}{filament_name}'
            
            rows.append(f'<td>{spool_cell}</td>')
            rows.append(f'<td>{material}</td>')
            rows.append(f'<td>{spool_str}</td>')
            rows.append(f'<td>{grams_str}</td>')
            rows.append(f'<td>{length_str}</td>')
            if i == 0:
                rows.append(f'<td rowspan="{rowspan}">{status_label}</td>')
            rows.append("</tr>")
    return "".join(rows)


def _render_pending_consumption_section(spools):
    """Zeigt Endbuchungen, die auf eine Spulen-Bestaetigung warten (Druck ist
    beendet, die Spule ist unbekannt oder braucht Bestaetigung), mit Dropdown
    und Bestaetigen-Knopf.

    spools: von _render_history_page bereits fuer denselben Seitenaufruf
    geladene Spoolman-Liste - vermeidet einen zweiten, redundanten
    sm_get("/spool")-Aufruf (Spoolman antwortet unter Last teils erst nach
    mehreren Sekunden, siehe Performance-Analyse zu Issue #44)."""
    try:
        state = load_state_locked()
        jobs = state.get("jobs", {})
    except Exception as e:
        log.error(f"Endbuchungs-State konnte nicht geladen werden: {e}")
        return ""

    user_overrides = state.get("user_spool_overrides", {})

    pending_items = []
    for job_key, job in jobs.items():
        final_booking = job.get("final_booking") or {}
        awaiting = [i for i in final_booking.get("items", []) if i["status"] == ITEM_AWAITING_USER]
        if not awaiting:
            continue
        job_name = job.get("task_name") or job.get("gcode") or "?"
        for item in awaiting:
            # Vorauswahl: die im Dashboard (/slots) gewaehlte Spule, sonst die erkannte
            spool_id = user_overrides.get(item["tray_key"]) or item.get("spool_id") or 0
            pending_items.append((job_key, job_name, item["tray_key"], item["segment"],
                                  item["grams"], spool_id, item.get("uid") or "?"))

    if not pending_items:
        return ""

    # HTML für Pending-Items
    sections = []
    for idx, (job_key, job_name, tray_key, segment, grams, spool_id, uid) in enumerate(pending_items):
        # Verwende Index statt job_key für die ID (job_key hat Sonderzeichen!)
        dropdown_id = f"spool_{idx}"
        pending_html = f"""
<div style="border: 2px solid #ff9800; background: #fff3e0; padding: 15px; margin: 10px 0; border-radius: 5px;">
  <h3 style="margin-top: 0; color: #ff6f00;">⏳ Ausstehender Verbrauch: {html.escape(job_name)}</h3>
  <p><strong>Zu verbuchende Menge:</strong> {grams:.2f}g ({html.escape(tray_key)}, Druck beendet)</p>
  <p style="font-size: 0.9em; color: #666;">UID: {uid} | Spool-ID: {spool_id}</p>

  <label for="{dropdown_id}" style="display: block; margin: 10px 0 5px 0; font-weight: bold;">
    Spule auswählen (vorausgewählt: #{spool_id}):
  </label>
  <select id="{dropdown_id}" style="padding: 8px; width: 100%; max-width: 300px; margin-bottom: 10px;">
    <option value="">-- Bitte Spule auswählen --</option>
"""
        if spools:
            for spool in sorted(spools, key=lambda s: s["id"]):
                spool_name = spool.get("filament", {}).get("name", "?")
                selected = "selected" if spool["id"] == spool_id else ""
                pending_html += f'    <option value="{spool["id"]}" {selected}>{spool["id"]} - {html.escape(spool_name)}</option>\n'
        else:
            pending_html += f'    <option value="{spool_id}" selected>#{spool_id} (Spoolman nicht erreichbar)</option>\n'

        pending_html += f"""
  </select>

  <button onclick="confirmConsumption('{dropdown_id}', '{html.escape(job_key)}', '{html.escape(tray_key)}', '{segment}', '{spool_id}')"
          style="padding: 8px 16px; background: #4caf50; color: white; border: none; border-radius: 3px; cursor: pointer; font-weight: bold;">
    ✅ Bestätigen und Abbuchen
  </button>
</div>
"""
        sections.append(pending_html)

    pending_section = '<div style="margin-bottom: 20px;">'.join(sections) + '</div>'

    # JavaScript für Bestätigung
    confirm_js = """
<script>
function confirmConsumption(dropdownId, jobKey, trayKey, segment, defaultSpoolId) {
  // Suche den Dropdown mit dem Index
  const select = document.getElementById(dropdownId);
  if (!select) {
    alert('Fehler: Dropdown nicht gefunden');
    return;
  }

  const spoolId = select.value || defaultSpoolId;

  if (!spoolId) {
    alert('Bitte eine Spule auswählen');
    return;
  }

  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/confirm-consumption', true);
  xhr.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded');
  xhr.onload = function() {
    if (xhr.status === 303 || xhr.status === 200) {
      location.reload();
    } else {
      alert('Fehler beim Bestätigen: ' + xhr.status);
    }
  };
  xhr.onerror = function() { alert('Netzwerkfehler'); };
  xhr.send('job_key=' + encodeURIComponent(jobKey) + '&tray_key=' + encodeURIComponent(trayKey)
    + '&segment=' + encodeURIComponent(segment) + '&spool_id=' + encodeURIComponent(spoolId));
}
</script>
"""

    return pending_section + confirm_js


def _render_log_level_control():
    """Auswahl der Log-Stufe fuer die Kopfzeile. Natives <select> mit Label
    (tastaturbedienbar); die Aenderung wirkt sofort, ohne Neustart."""
    selected = current_log_level()
    options = "".join(
        f'<option value="{key}"{" selected" if key == selected else ""}>{html.escape(label)}</option>'
        for key, (label, _) in LOG_LEVELS.items()
    )
    return (
        '<form method="post" action="/log-level" style="display: flex; align-items: center; gap: 6px;">'
        '<label for="log-level" style="font-size: 14px; color: var(--muted);">Log</label>'
        '<select id="log-level" name="level" onchange="this.form.submit()" '
        'title="Log-Stufe, wirkt sofort ohne Neustart" '
        'style="font-size: 14px; padding: 8px 12px; border: 1px solid #0ea5e9; border-radius: 4px; '
        'background: transparent; color: inherit; cursor: pointer;">'
        f"{options}</select>"
        '<noscript><button type="submit">Setzen</button></noscript>'
        "</form>"
    )


# Sortierung und Mehrfachauswahl-Filter der Verlaufstabelle (Issue #52). Wird als WERT
# in die Seite eingesetzt (kein Template): Klammern also NICHT verdoppeln. Die reine
# Logik (compareGroups/sortGroups/groupMatches/sanitizeFilters) steht in
# window.HistoryTableLogic und wird in tests/test_history_table_tools.py unter Node geprueft.
_TABLE_TOOLS_HEAD = """<style>
  .sr-only { position: absolute; width: 1px; height: 1px; margin: -1px; padding: 0; overflow: hidden;
             clip: rect(0 0 0 0); white-space: nowrap; border: 0; }
  .th-inner { display: flex; align-items: center; flex-wrap: wrap; gap: 0.25em; }
  .sort-btn, .filter-btn { background: none; border: 0; color: inherit; font: inherit; cursor: pointer;
                           padding: 0.1em 0.2em; border-radius: 4px; text-align: left; }
  .sort-btn:hover, .filter-btn:hover { background: rgba(128,128,128,0.2); }
  .sort-btn:focus-visible, .filter-btn:focus-visible, .filter-clear:focus-visible,
  .filter-reset:focus-visible, .filter-panel input:focus-visible { outline: 2px solid #0ea5e9; outline-offset: 2px; }
  .sort-arrow { margin-left: 0.2em; }
  .filter-btn.is-active { color: #0ea5e9; font-weight: bold; }
  .filter-count { font-size: 0.8em; }
  .filter-panel { position: fixed; z-index: 50; min-width: 200px; max-width: 90vw; max-height: 60vh;
                  overflow: auto; background: var(--bg); color: var(--fg); border: 1px solid var(--border);
                  border-radius: 6px; padding: 0.5em; box-shadow: 0 4px 14px rgba(0,0,0,0.3);
                  font-weight: normal; text-align: left; }
  .filter-panel label { display: flex; align-items: center; gap: 0.5em; padding: 0.3em 0.2em; cursor: pointer; }
  .filter-clear, .filter-reset { background: var(--panel-bg); color: var(--fg); border: 1px solid var(--border);
                                 border-radius: 4px; padding: 0.3em 0.6em; cursor: pointer; font: inherit; }
  .filter-clear { margin-bottom: 0.4em; width: 100%; }
  .filter-status { margin-left: 0.8em; color: var(--muted); }
  .toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 0.5em; }
  table.history-table tr:nth-child(even) { background: none; }
  table.history-table tbody tr.row-alt { background: var(--row-alt); }
</style>
<script>
(function () {
  'use strict';
  var SORT_STORAGE_KEY = 'historySort';
  var FILTER_STORAGE_KEY = 'historyFilters';
  var FILTER_COLUMNS = ['farbe', 'material', 'spule'];
  var SORT_KEYS = ['id', 'platte', 'name', 'datum', 'dauer', 'farbe', 'material', 'spule',
                   'verbrauch_g', 'verbrauch_m', 'status'];
  var NUMERIC_KEYS = { id: 1, datum: 1, dauer: 1, spule: 1, verbrauch_g: 1, verbrauch_m: 1 };

  function isMissing(value) { return value === '' || value === undefined || value === null; }

  // Leere Werte stehen immer hinten, egal in welcher Richtung. Zahlen numerisch,
  // Text ohne Gross-/Kleinschreibung und mit natuerlicher Zahlenreihenfolge.
  function compareGroups(a, b, key, dir) {
    var va = a.sort[key], vb = b.sort[key];
    var missingA = isMissing(va), missingB = isMissing(vb);
    if (missingA && missingB) return Number(b.id) - Number(a.id);
    if (missingA) return 1;
    if (missingB) return -1;
    var result = NUMERIC_KEYS[key]
      ? Number(va) - Number(vb)
      : String(va).localeCompare(String(vb), 'de', { sensitivity: 'base', numeric: true });
    if (result === 0) return Number(b.id) - Number(a.id);
    return dir === 'asc' ? result : -result;
  }

  function sortGroups(groups, key, dir) {
    return groups.slice().sort(function (a, b) { return compareGroups(a, b, key, dir); });
  }

  // Innerhalb einer Spalte gilt ODER (ein Druck mit mehreren Farben passt, sobald
  // eine davon gewaehlt ist), zwischen den Spalten und dem Namensfeld gilt UND.
  function groupMatches(group, filters, query) {
    if (query && String(group.name || '').indexOf(query) === -1) return false;
    for (var i = 0; i < FILTER_COLUMNS.length; i++) {
      var column = FILTER_COLUMNS[i];
      var selected = (filters && filters[column]) || [];
      if (!selected.length) continue;
      var values = (group.filters && group.filters[column]) || [''];
      if (!values.some(function (v) { return selected.indexOf(v) !== -1; })) return false;
    }
    return true;
  }

  // Gespeicherte Auswahl darf nichts enthalten, was es nicht mehr gibt - sonst waere
  // die Tabelle durch einen unsichtbaren Filter leer.
  function sanitizeFilters(saved, available) {
    var clean = {};
    FILTER_COLUMNS.forEach(function (column) {
      var wanted = saved && Array.isArray(saved[column]) ? saved[column] : [];
      var allowed = (available && available[column]) || [];
      clean[column] = wanted.filter(function (v) { return typeof v === 'string' && allowed.indexOf(v) !== -1; });
    });
    return clean;
  }

  function hasActiveFilter(filters) {
    return FILTER_COLUMNS.some(function (column) { return (filters[column] || []).length > 0; });
  }

  window.HistoryTableLogic = { compareGroups: compareGroups, sortGroups: sortGroups,
                               groupMatches: groupMatches, sanitizeFilters: sanitizeFilters };

  // ---- Seite ------------------------------------------------------------------
  var tbody = null, groups = [], state = null, sortDirty = true;

  function readJson(key) {
    try { return JSON.parse(localStorage.getItem(key) || 'null'); } catch (e) { return null; }
  }
  function writeJson(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch (e) { /* privater Modus: ohne Speicherung weiter */ }
  }

  function collectGroups() {
    var result = [], current = null;
    Array.prototype.forEach.call(tbody.rows, function (tr) {
      var id = tr.getAttribute('data-rec');
      if (!current || current.id !== id) {
        current = { id: id, rows: [], sort: {}, filters: {}, name: tr.getAttribute('data-name') || '' };
        SORT_KEYS.forEach(function (key) { current.sort[key] = tr.getAttribute('data-s-' + key) || ''; });
        FILTER_COLUMNS.forEach(function (column) {
          try { current.filters[column] = JSON.parse(tr.getAttribute('data-f-' + column) || '[""]'); }
          catch (e) { current.filters[column] = ['']; }
        });
        result.push(current);
      }
      current.rows.push(tr);
    });
    return result;
  }

  function availableFilterValues() {
    var available = {};
    FILTER_COLUMNS.forEach(function (column) {
      var panel = document.getElementById('filter-panel-' + column);
      available[column] = panel
        ? Array.prototype.map.call(panel.querySelectorAll('input[type=checkbox]'), function (i) { return i.value; })
        : [];
    });
    return available;
  }

  function updateHeader() {
    Array.prototype.forEach.call(document.querySelectorAll('.sort-btn'), function (btn) {
      var th = btn.closest('th');
      var isActive = btn.getAttribute('data-sort') === state.sort.key;
      btn.querySelector('.sort-arrow').textContent = isActive ? (state.sort.dir === 'asc' ? '▲' : '▼') : '';
      if (isActive) th.setAttribute('aria-sort', state.sort.dir === 'asc' ? 'ascending' : 'descending');
      else th.removeAttribute('aria-sort');
    });
    Array.prototype.forEach.call(document.querySelectorAll('.filter-btn'), function (btn) {
      var column = btn.getAttribute('data-filter');
      var selected = state.filters[column] || [];
      var panel = document.getElementById(btn.getAttribute('aria-controls'));
      var label = panel ? panel.getAttribute('aria-label') : column;
      btn.classList.toggle('is-active', selected.length > 0);
      btn.querySelector('.filter-count').textContent = selected.length ? '(' + selected.length + ')' : '';
      btn.setAttribute('aria-label', label + (selected.length ? ', ' + selected.length + ' ausgewählt' : ''));
      Array.prototype.forEach.call(panel.querySelectorAll('input[type=checkbox]'), function (input) {
        input.checked = selected.indexOf(input.value) !== -1;
      });
    });
  }

  function applyTableView() {
    if (!tbody || !state) return;
    var searchField = document.getElementById('search');
    var query = searchField ? searchField.value.trim().toLowerCase() : '';
    var ordered = sortDirty ? sortGroups(groups, state.sort.key, state.sort.dir) : groups;
    var shown = 0, stripe = 0;
    ordered.forEach(function (group) {
      var visible = groupMatches(group, state.filters, query);
      group.rows.forEach(function (tr) {
        if (sortDirty) tbody.appendChild(tr);
        tr.hidden = !visible;
        if (visible) { tr.classList.toggle('row-alt', stripe % 2 === 1); stripe++; }
      });
      if (visible) shown++;
    });
    if (sortDirty) { groups = ordered; sortDirty = false; }
    updateHeader();

    var isFiltered = hasActiveFilter(state.filters) || query !== '';
    var status = document.getElementById('filter-status');
    if (status) {
      status.textContent = isFiltered
        ? shown + ' von ' + groups.length + ' Drucken' + (shown === 0 ? ' – keine Treffer' : '')
        : groups.length + ' Drucke';
    }
    var reset = document.getElementById('filter-reset');
    if (reset) reset.hidden = !hasActiveFilter(state.filters);
  }

  function setSort(key) {
    if (state.sort.key === key) state.sort.dir = state.sort.dir === 'asc' ? 'desc' : 'asc';
    else state.sort = { key: key, dir: NUMERIC_KEYS[key] && key !== 'spule' ? 'desc' : 'asc' };
    writeJson(SORT_STORAGE_KEY, state.sort);
    sortDirty = true;
    applyTableView();
  }

  function setFilter(column, values) {
    state.filters[column] = values;
    writeJson(FILTER_STORAGE_KEY, state.filters);
    applyTableView();
  }

  function closePanels(returnFocusTo) {
    Array.prototype.forEach.call(document.querySelectorAll('.filter-panel'), function (panel) { panel.hidden = true; });
    Array.prototype.forEach.call(document.querySelectorAll('.filter-btn'), function (btn) {
      btn.setAttribute('aria-expanded', 'false');
    });
    if (returnFocusTo) returnFocusTo.focus();
  }

  // Das Panel ist position:fixed (sonst schneidet das Scroll-Fenster der Tabelle es ab)
  // und wird daher beim Oeffnen, Scrollen und Groessenaendern unter den Knopf gesetzt.
  function positionPanel(btn) {
    var panel = document.getElementById(btn.getAttribute('aria-controls'));
    var rect = btn.getBoundingClientRect();
    panel.style.top = (rect.bottom + 4) + 'px';
    panel.style.left = Math.max(4, Math.min(rect.left, window.innerWidth - panel.offsetWidth - 4)) + 'px';
  }

  function repositionOpenPanel() {
    var open = document.querySelector('.filter-btn[aria-expanded="true"]');
    if (open) positionPanel(open);
  }

  function openPanel(btn) {
    closePanels();
    document.getElementById(btn.getAttribute('aria-controls')).hidden = false;
    positionPanel(btn);
    btn.setAttribute('aria-expanded', 'true');
  }

  function initTableTools() {
    tbody = document.getElementById('history-body');
    if (!tbody) return;
    groups = collectGroups();
    var savedSort = readJson(SORT_STORAGE_KEY);
    var serverDir = tbody.getAttribute('data-sort-dir') === 'asc' ? 'asc' : 'desc';
    state = {
      sort: savedSort && SORT_KEYS.indexOf(savedSort.key) !== -1 && (savedSort.dir === 'asc' || savedSort.dir === 'desc')
        ? savedSort : { key: 'datum', dir: serverDir },
      filters: sanitizeFilters(readJson(FILTER_STORAGE_KEY), availableFilterValues())
    };
    window.applyTableView = applyTableView;

    Array.prototype.forEach.call(document.querySelectorAll('.sort-btn'), function (btn) {
      btn.addEventListener('click', function () { setSort(btn.getAttribute('data-sort')); });
    });
    Array.prototype.forEach.call(document.querySelectorAll('.filter-btn'), function (btn) {
      btn.addEventListener('click', function () {
        if (btn.getAttribute('aria-expanded') === 'true') closePanels(btn); else openPanel(btn);
      });
    });
    Array.prototype.forEach.call(document.querySelectorAll('.filter-panel'), function (panel) {
      var column = panel.id.replace('filter-panel-', '');
      panel.addEventListener('change', function () {
        var chosen = [];
        Array.prototype.forEach.call(panel.querySelectorAll('input[type=checkbox]'), function (input) {
          if (input.checked) chosen.push(input.value);
        });
        setFilter(column, chosen);
      });
      panel.querySelector('.filter-clear').addEventListener('click', function () { setFilter(column, []); });
    });
    var reset = document.getElementById('filter-reset');
    if (reset) reset.addEventListener('click', function () {
      state.filters = sanitizeFilters({}, {});
      writeJson(FILTER_STORAGE_KEY, state.filters);
      applyTableView();
    });
    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape') return;
      var open = document.querySelector('.filter-btn[aria-expanded="true"]');
      if (open) closePanels(open);
    });
    document.addEventListener('click', function (e) {
      if (!e.target.closest || !e.target.closest('.filter-panel, .filter-btn')) closePanels();
    });
    window.addEventListener('resize', repositionOpenPanel);
    document.addEventListener('scroll', function (e) {
      if (e.target && e.target.closest && e.target.closest('.filter-panel')) return;
      repositionOpenPanel();
    }, true);
    applyTableView();
  }

  document.addEventListener('DOMContentLoaded', initTableTools);
})();
</script>
"""


def _render_history_page(sort_dir="desc"):
    history = load_history()
    records = history.get("records", [])
    kpis_html = _render_kpis(_compute_kpis(records))
    names = sorted({r.get("name") for r in records if r.get("name")})
    name_options = "".join(f'<option value="{html.escape(n)}">' for n in names)

    # Version auslesen
    # Release-Version aus VERSION (wie HA und /api/version), NICHT aus git describe:
    # der NAS-Checkout holt keine Tags, describe bliebe auf dem alten Release stehen.
    git_info = _get_git_info()
    version = git_info["version"]
    if version[:1].isdigit():
        version = f"v{version}"
    if git_info.get("commit") not in (None, "", "unknown"):
        version = f"{version} {git_info['commit']}"

    # History-Seite ist read-only, daher kein Lock nötig (Micro-Optimierung)
    overrides = load_state().get("user_spool_overrides", {}) or {}
    # EINMAL pro Seitenaufruf laden und wiederverwenden (fuer spools_by_id UND
    # die Pending-Sektion) - vorher wurde dieselbe Liste zweimal unabhaengig
    # abgerufen, was sich bei langsamem Spoolman direkt in der Ladezeit der
    # Seite verdoppelte.
    try:
        all_spools = sm_get("/spool")
    except Exception as e:
        log.warning(f"Spoolman-Abruf fuer History-Anzeige fehlgeschlagen: {e}")
        all_spools = []
    spools_by_id = {s["id"]: s for s in all_spools}

    # Pending-Verbrauch-Sektion OBEN
    pending_section = _render_pending_consumption_section(all_spools)

    if not records:
        content = '<p class="empty">Noch keine abgeschlossenen Druckauftraege.</p>'
    else:
        filter_options = _collect_filter_options(records, overrides, spools_by_id)
        content = (
            '<div class="table-scroll"><table class="history-table">'
            f"<colgroup>{_render_colgroup()}</colgroup>"
            + _render_table_header(sort_dir, filter_options)
            + f'<tbody id="history-body" data-sort-dir="{sort_dir}">'
            + _render_history_rows(records, sort_dir, overrides, spools_by_id)
            + "</tbody></table></div>"
        )

    # add_entry_js wird als WERT in die Seite eingesetzt (kein Template) - die
    # Klammern duerfen nicht verdoppelt werden, sonst ist das JavaScript ungueltig.
    escaped_js = _ADD_ENTRY_JAVASCRIPT
    content = pending_section + content  # Pending OBEN, dann History
    return _HISTORY_PAGE.format(add_entry_js=escaped_js, table_tools=_TABLE_TOOLS_HEAD, kpis=kpis_html, name_options=name_options, version=version,
                                content=content, log_level_control=_render_log_level_control())


# Obergrenze fuer ein Formular mit Bildern (POST /add-history). Der Dienst liegt
# ohne Anmeldung im LAN - ohne Limit koennte ein einziger Request den Speicher fuellen.
MAX_FORM_BYTES = int(os.environ.get("CONSUMPTION_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))


class FormTooLargeError(ValueError):
    """Der Request ist groesser als MAX_FORM_BYTES."""


def parse_form_data(content_type, body):
    """Liest ein HTML-Formular (multipart/form-data oder x-www-form-urlencoded).

    Ersetzt cgi.FieldStorage: das Modul cgi wurde in Python 3.13 entfernt, ein
    Wechsel des Container-Images haette das Speichern neuer Eintraege gebrochen.
    Nur Standardbibliothek (email), gleiches Verhalten in 3.11 und 3.13+.

    Gibt (felder, dateien) zurueck: felder = {name: text}, dateien =
    {name: (dateiname, bytes)}. Eine Datei ohne Namen oder ohne Inhalt (leeres
    Dateifeld im Browser) zaehlt als nicht gesendet. Wirft ValueError bei
    unbrauchbarem Inhaltstyp oder kaputtem multipart."""
    content_type = (content_type or "").strip()
    media_type = content_type.split(";", 1)[0].strip().lower()
    fields, files = {}, {}

    if media_type == "application/x-www-form-urlencoded":
        for key, values in parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True).items():
            fields[key] = values[0]
        return fields, files

    if media_type != "multipart/form-data":
        raise ValueError(f"Nicht unterstuetzter Inhaltstyp: {media_type or 'keiner'}")

    message = BytesParser(policy=_HTTP_EMAIL_POLICY).parsebytes(
        b"MIME-Version: 1.0\r\nContent-Type: " + content_type.encode("latin-1", errors="replace") + b"\r\n\r\n" + body
    )
    if not message.is_multipart():
        if message.get_boundary():
            return fields, files          # gueltige Begrenzung, aber keine Teile: leeres Formular
        raise ValueError("Ungueltiges multipart-Formular (Begrenzung fehlt)")
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename is None:
            fields[name] = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        elif filename and payload:
            files[name] = (filename, payload)
    return fields, files


class _HistoryHandler(BaseHTTPRequestHandler):
    def _send_html(self, body):
        b = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    # ── Nachkaufen / Preisvergleich (Issue #31, Logik in price_check.py) ──

    _RESTOCK_NOTICES = {
        "gestartet": "Preise werden neu abgerufen – die Seite aktualisiert sich automatisch.",
        "aktuell": "Alle Preise sind aktuell oder werden bereits abgerufen.",
        "galaxus-schluessel": "Galaxus-Preis nicht übernommen: ungültiges Lesezeichen. Bitte das Lesezeichen von dieser Seite neu anlegen.",
        "galaxus-keine-galaxus-seite": "Galaxus-Preis nicht übernommen: Das Lesezeichen funktioniert nur auf einer Galaxus-Produktseite.",
        "galaxus-unbekannt": "Galaxus-Preis nicht übernommen: Dieses Produkt ist bei keinem Filament im Spoolman-Feld „Galaxus“ eingetragen.",
        "galaxus-waehrung": "Galaxus-Preis nicht übernommen: Es werden nur Preise in CHF angenommen.",
        "galaxus-preis": "Galaxus-Preis nicht übernommen: Der Preis ist nicht plausibel (erlaubt CHF 1–500).",
        "galaxus-fehler": "Galaxus-Preis nicht übernommen: Spoolman ist momentan nicht erreichbar.",
    }

    def _public_base_url(self):
        """Adresse, unter der der Browser diesen Dienst erreicht - fuer das
        Lesezeichen. Host-Header nur uebernehmen, wenn er wie ein Hostname aussieht."""
        if NACHKAUFEN_PUBLIC_URL:
            return NACHKAUFEN_PUBLIC_URL
        host = (self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "").split(",")[0].strip()
        if not re.fullmatch(r"[A-Za-z0-9.-]+(:\d{1,5})?", host):
            host = f"localhost:{HISTORY_WEB_PORT}"
        proto = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip().lower()
        return f"{'https' if proto == 'https' else 'http'}://{host}"

    def _handle_galaxus_price(self):
        """Vom Lesezeichen: Galaxus-Preis pruefen/speichern, dann zur Liste."""
        params = parse_qs(urlsplit(self.path).query)
        first = lambda k: (params.get(k) or [""])[0][:2000]
        try:
            code, fil = price_check.submit_manual_price(
                sm_get("/filament"), first("url"), first("price"), first("currency"), first("token"))
        except requests.RequestException as e:
            log.error(f"/nachkaufen/galaxus: Spoolman nicht erreichbar: {e}")
            code, fil = "fehler", None
        hint = f"galaxus-ok-{fil['id']}" if code == "ok" else f"galaxus-{code}"
        self.send_response(303)
        self.send_header("Location", f"/nachkaufen?hinweis={hint}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_galaxus_round_page(self):
        try:
            # immer alle Filamente mit Galaxus-Link, unabhaengig von der Bestandsschwelle
            round_id, urls = price_check.start_round(sm_get("/filament"))
            self._send_html(price_check.render_round_page(
                round_id, urls, f"{self._public_base_url()}/nachkaufen/galaxus.user.js"))
        except requests.RequestException as e:
            log.error(f"/nachkaufen/galaxus-runde: Spoolman nicht erreichbar: {e}")
            self._send_html('<p style="font-family:sans-serif">Spoolman ist momentan nicht erreichbar.</p>')

    def _handle_galaxus_round_step(self):
        """Vom Tampermonkey-Skript: Preis einer Galaxus-Seite melden, naechsten Link holen."""
        length = min(int(self.headers.get("Content-Length", 0) or 0), 8192)
        params = parse_qs(self.rfile.read(length).decode("utf-8", errors="replace"))
        first = lambda k: (params.get(k) or [""])[0][:2000]
        try:
            result = price_check.round_step(first("round"), first("url"), first("price"), first("currency"),
                                            first("token"), sm_get("/filament"))
        except requests.RequestException as e:
            log.error(f"/nachkaufen/galaxus-runde/schritt: Spoolman nicht erreichbar: {e}")
            result = {"ok": False, "code": "spoolman", "next": None}
        body = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _galaxus_ok_notice(self, fid):
        try:
            fil = sm_get(f"/filament/{int(fid)}")
        except (requests.RequestException, ValueError):
            return "Galaxus-Preis übernommen."
        url = price_check.normalize_url(price_check._extra_value(fil, "url_galaxus"))
        entry = price_check.get_cached_prices().get(price_check.fetch_key(url)) or {}
        price = f" CHF {entry['price']:.2f}" if entry.get("price") else ""
        return f"✓ Galaxus-Preis für {fil.get('name', 'Filament')} übernommen:{price}"

    def _restock_groups(self, threshold):
        return price_check.select_low_stock(sm_get("/spool"), threshold)

    def _handle_restock(self, as_json):
        try:
            params = parse_qs(urlsplit(self.path).query)
            threshold = price_check.parse_threshold((params.get("max") or [None])[0])
            embed = (params.get("embed") or [""])[0] in ("1", "true", "yes")
            groups = self._restock_groups(threshold)
            # Fehlende/veraltete Preise im Hintergrund holen - blockiert nie
            price_check.refresh_prices(groups)
            fx = price_check.get_eur_chf_rate()
            rows = price_check.build_rows(groups, price_check.get_cached_prices(), fx["rate"])
            if as_json:
                body = json.dumps(price_check.rows_to_json(rows, threshold, fx),
                                  ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            code = (params.get("hinweis") or [""])[0]
            round_match = re.fullmatch(r"runde-(\d{1,3})-(\d{1,3})", code)
            if code.startswith("galaxus-ok-") and code[11:].isdigit():
                notice = self._galaxus_ok_notice(code[11:])
            elif round_match:
                notice = (f"✓ Galaxus-Runde fertig: {round_match.group(1)} von {round_match.group(2)} "
                          f"Preisen übernommen.")
            elif code.startswith("warten-") and code[7:].isdigit():
                notice = f"Preise wurden gerade erst aktualisiert – nächster Abruf in {int(code[7:]) // 60 + 1} min möglich."
            else:
                notice = self._RESTOCK_NOTICES.get(code, "")
            bookmarklet = price_check.bookmarklet_js(
                f"{self._public_base_url()}/nachkaufen/galaxus", price_check.get_bookmarklet_token())
            self._send_html(price_check.render_page(
                rows, threshold, fx, embed=embed, refreshing=price_check.is_refreshing(),
                spoolman_public_url=SPOOLMAN_PUBLIC_URL, notice=notice, bookmarklet_href=bookmarklet))
        except requests.RequestException as e:
            log.error(f"/nachkaufen: Spoolman nicht erreichbar: {e}")
            self._send_html('<p style="font-family:sans-serif">Spoolman ist momentan nicht erreichbar. '
                            'Bitte später erneut versuchen.</p>')
        except Exception as e:
            log.error(f"/nachkaufen fehlgeschlagen: {e}", exc_info=True)
            self._send_html('<p style="font-family:sans-serif">Die Nachkauf-Liste konnte nicht erstellt werden.</p>')

    def _handle_restock_refresh(self):
        length = min(int(self.headers.get("Content-Length", 0) or 0), 4096)
        params = parse_qs(self.rfile.read(length).decode("utf-8", errors="replace"))
        threshold = price_check.parse_threshold((params.get("max") or [None])[0])
        embed = (params.get("embed") or [""])[0] == "1"
        try:
            started, wait_s = price_check.request_manual_refresh(self._restock_groups(threshold))
            code = f"warten-{wait_s}" if wait_s else ("gestartet" if started else "aktuell")
        except requests.RequestException as e:
            log.error(f"/nachkaufen/refresh: Spoolman nicht erreichbar: {e}")
            code = ""
        target = f"/nachkaufen?max={threshold:g}{'&embed=1' if embed else ''}{'&hinweis=' + code if code else ''}"
        # 303: nach dem POST per GET zurueck zur Liste (kein erneutes Absenden bei F5)
        self.send_response(303)
        self.send_header("Location", target)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_add_history(self):
        try:
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError:
                raise ValueError("Ungueltige Content-Length")
            if length <= 0:
                raise ValueError("Leere Anfrage")
            if length > MAX_FORM_BYTES:
                raise FormTooLargeError(f"Anfrage zu gross (max. {MAX_FORM_BYTES // (1024 * 1024)} MB)")
            fields, files = parse_form_data(self.headers.get("Content-Type"), self.rfile.read(length))

            name = fields.get('name', '').strip()[:200]
            start_str = fields.get('start', '').strip()
            end_str = fields.get('end', '').strip()
            status = fields.get('status', 'finish').strip()
            gcode = fields.get('gcode', '').strip()
            colors_json = fields.get('colors', '{}')

            if not all([name, start_str, end_str, status]):
                raise ValueError("Fehlende Pflichtfelder: name, start, end, status")

            if status not in ('finish', 'failed', 'cancelled'):
                raise ValueError(f"Ungültiger Status: {status}")

            try:
                colors = json.loads(colors_json) if isinstance(colors_json, str) else colors_json
                if not isinstance(colors, list) or len(colors) == 0:
                    raise ValueError("colors muss ein nicht-leeres Array sein")
            except json.JSONDecodeError:
                raise ValueError("colors muss gültiges JSON sein")

            start_dt = datetime.fromisoformat(start_str.replace('T', ' '))
            end_dt = datetime.fromisoformat(end_str.replace('T', ' '))

            if start_dt >= end_dt:
                raise ValueError("Startzeit muss vor Endzeit liegen")

            duration_min = int((end_dt - start_dt).total_seconds() / 60)

            with _history_lock:
                history = load_history()
                next_id = history.get("next_id", 1)

                image_filename = None
                camera_filename = None

                if 'image' in files:
                    try:
                        image_filename = f"{next_id}.png"
                        image_path = os.path.join(HISTORY_IMAGES_DIR, image_filename)
                        with open(image_path, 'wb') as f:
                            f.write(files['image'][1])
                        log.info(f"Thumbnail gespeichert: {image_filename}")
                    except Exception as e:
                        log.warning(f"Fehler beim Speichern des Thumbnails: {e}")
                        image_filename = None

                if 'camera' in files:
                    try:
                        camera_filename = f"{next_id}_camera.jpg"
                        camera_path = os.path.join(HISTORY_IMAGES_DIR, camera_filename)
                        with open(camera_path, 'wb') as f:
                            f.write(files['camera'][1])
                        log.info(f"Kamerafoto gespeichert: {camera_filename}")
                    except Exception as e:
                        log.warning(f"Fehler beim Speichern des Kamerafotos: {e}")
                        camera_filename = None

                new_record = {
                    "id": next_id,
                    "gcode": gcode if gcode else None,
                    "name": name,
                    "name_overridden": True,
                    "start": start_dt.isoformat() + "+00:00" if start_dt.tzinfo is None else start_dt.isoformat(),
                    "end": end_dt.isoformat() + "+00:00" if end_dt.tzinfo is None else end_dt.isoformat(),
                    "duration_min": duration_min,
                    "status": status,
                    "image": image_filename,
                    "camera_image": camera_filename,
                    "colors": colors
                }

                history["records"].insert(0, new_record)
                history["next_id"] = next_id + 1
                save_history(history)

            log.info(f"Neuer History-Eintrag erstellt: #{next_id} '{name}' ({status})")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            response = json.dumps({"ok": True, "id": next_id}).encode("utf-8")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        except ValueError as e:
            log.warning(f"Validierungsfehler bei /add-history: {e}")
            # 413 fuer zu grosse Anfragen, sonst 400 (der Client soll den Unterschied erkennen)
            self.send_response(413 if isinstance(e, FormTooLargeError) else 400)
            self.send_header("Content-Type", "application/json")
            response = json.dumps({"ok": False, "error": str(e)}).encode("utf-8")
            self.send_header("Content-Length", str(len(response)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(response)
        except Exception as e:
            log.error(f"Fehler bei /add-history: {e}", exc_info=True)
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            response = json.dumps({"ok": False, "error": "Interner Fehler"}).encode("utf-8")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    def _send_cloud_page(self, status, body_html):
        body = body_html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _handle_cloud_token_get(self):
        message = bambu_cloud.PAGE_MESSAGES.get(parse_qs(urlsplit(self.path).query).get("msg", [""])[0], "")
        self._send_cloud_page(200, bambu_cloud.render_page(message=message))

    def _handle_cloud_token_post(self):
        """Token pruefen und speichern. Nie das Token loggen oder zurueckgeben."""
        length = int(self.headers.get("Content-Length", 0))
        if length > CLOUD_TOKEN_MAX_BODY:
            self._send_cloud_page(413, bambu_cloud.render_page(error="Eingabe zu gross."))
            return
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        token = bambu_cloud.clean_token(parse_qs(raw).get("token", [""])[0])
        if not bambu_cloud.is_valid_token(token):
            self._send_cloud_page(400, bambu_cloud.render_page(
                error="Das sieht nicht wie ein Token aus (20-1000 Zeichen: Buchstaben, Ziffern, - _ . = + /)."))
            return
        try:
            bambu_cloud.check_token(token)
        except bambu_cloud.CloudAuthError:
            self._send_cloud_page(400, bambu_cloud.render_page(error="Die Cloud lehnt dieses Token ab. Nicht gespeichert."))
            return
        except bambu_cloud.CloudError as e:
            self._send_cloud_page(502, bambu_cloud.render_page(error=f"Pruefung nicht moeglich, nicht gespeichert: {e}"))
            return
        bambu_cloud.save_token(token)
        log.info("Cloud-Namen: neues Token gespeichert")
        threading.Thread(target=_run_cloud_names, name="cloud-names-now", daemon=True).start()
        self._redirect("/cloud-token?msg=saved")

    def _redirect(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path == "/cloud-token" or self.path.startswith("/cloud-token?"):
            self._handle_cloud_token_get()
            return

        if self.path == "/api/version":
            try:
                git_info = _get_git_info()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(git_info, ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                log.error(f"GET /api/version fehlgeschlagen: {e}")
                self.send_response(500)
                self.end_headers()
            return

        if self.path == "/api/spools.json":
            try:
                spools = sm_get("/spool")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(spools).encode("utf-8"))
            except Exception as e:
                log.error(f"GET /api/spools.json fehlgeschlagen: {e}")
                self.send_response(500)
                self.end_headers()
            return

        # Der eigentliche Gewinn der DB: gezielte Abfrage statt den kompletten
        # Verlauf zu laden und in Python zu filtern.
        # Beispiel: /api/spool-prints?spool_id=20
        if self.path.startswith("/api/spool-prints"):
            try:
                params = parse_qs(urlsplit(self.path).query)
                raw_id = (params.get("spool_id") or [""])[0]
                spool_id = int(raw_id)
            except (TypeError, ValueError):
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(
                    {"ok": False, "error": "spool_id fehlt oder ist keine Zahl"}
                ).encode("utf-8"))
                return
            try:
                prints = history_store.find_prints_by_spool(HISTORY_DB_FILE, spool_id)
                payload = {
                    "ok": True,
                    "spool_id": spool_id,
                    "count": len(prints),
                    "total_grams": round(sum(p.get("grams") or 0 for p in prints), 2),
                    "prints": prints,
                }
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            except Exception as e:
                log.error(f"GET /api/spool-prints fehlgeschlagen: {e}", exc_info=True)
                self.send_response(500)
                self.end_headers()
            return

        if self.path.startswith("/nachkaufen/galaxus?"):
            self._handle_galaxus_price()
            return

        if self.path == "/nachkaufen/galaxus-runde" or self.path.startswith("/nachkaufen/galaxus-runde?"):
            self._handle_galaxus_round_page()
            return

        if self.path == "/nachkaufen/shop-links" or self.path.startswith("/nachkaufen/shop-links?"):
            params = parse_qs(urlsplit(self.path).query)
            notices = {"gestartet": "Suche gestartet – die Seite aktualisiert sich automatisch.",
                       "laeuft": "Die Suche läuft bereits."}
            try:
                filaments = sm_get("/filament")
            except requests.RequestException:
                self._send_html('<p style="font-family:sans-serif">Spoolman ist momentan nicht erreichbar.</p>')
                return
            self._send_html(shop_links.render_overview(
                filaments, shop_links.load_state(), running=shop_links.is_running(),
                notice=notices.get((params.get("hinweis") or [""])[0], "")))
            return

        if self.path.startswith("/nachkaufen/galaxus-runde/protokoll"):
            params = parse_qs(urlsplit(self.path).query)
            self._send_html(price_check.render_round_protocol((params.get("round") or [""])[0][:40]))
            return

        if self.path == "/nachkaufen/galaxus.user.js":
            body = price_check.userscript_js(self._public_base_url(),
                                             price_check.get_bookmarklet_token()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/nachkaufen" or self.path.startswith("/nachkaufen?"):
            self._handle_restock(as_json=False)
            return

        if self.path == "/api/nachkaufen.json" or self.path.startswith("/api/nachkaufen.json?"):
            self._handle_restock(as_json=True)
            return

        if self.path == "/slots" or self.path.startswith("/slots?"):
            try:
                params = parse_qs(urlsplit(self.path).query)
                embed = (params.get("embed") or [""])[0] in ("1", "true", "yes")
                self._send_html(_render_slots_page(embed=embed))
            except Exception as e:
                log.error(f"Slots-Seite konnte nicht gerendert werden: {e}", exc_info=True)
                self._send_html(f'<p>Fehler: {html.escape(str(e))}</p>')
            return

        if self.path.startswith("/image/"):
            self._serve_image()
            return
        if self.path.startswith("/camera_image/"):
            self._serve_camera_image()
            return
        if self.path.startswith("/plate-image/"):
            self._serve_plate_image()
            return
        if self.path.startswith("/set-plate-type/"):
            try:
                parts = self.path.split("/set-plate-type/", 1)[1].split("?")[0].split("/")
                rec_id = int(parts[0]) if parts[0].isdigit() else None
                plate_type = parts[1] if len(parts) > 1 else None

                if rec_id and plate_type:
                    result = _set_plate_type(rec_id, plate_type)
                    self._send_html(result)
                else:
                    self._send_html("<p>Ungültige Parameter für /set-plate-type/</p>")
            except Exception as e:
                log.error(f"Set-Plate-Type fehlgeschlagen: {e}")
                self._send_html(f'<p>Fehler: {html.escape(str(e))}</p>')
            return
        if self.path.startswith("/set-spool-grams/"):
            try:
                parts = self.path.split("/set-spool-grams/", 1)[1].split("?")[0].split("/")
                rec_id = int(parts[0]) if parts[0].isdigit() else None
                spool_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
                grams = float(parts[2]) if len(parts) > 2 else 0.0

                if rec_id and spool_id:
                    result = _set_spool_grams(rec_id, spool_id, grams)
                    self._send_html(result)
                else:
                    self._send_html("<p>Ungültige Parameter für /set-spool-grams/</p>")
            except Exception as e:
                log.error(f"Set-Spool-Grams fehlgeschlagen: {e}")
                self._send_html(f'<p>Fehler: {html.escape(str(e))}</p>')
            return

        if self.path.startswith("/fix-history/"):
            try:
                parts = self.path.split("/fix-history/", 1)[1].split("?")[0].split("/")
                rec_id = int(parts[0]) if parts[0].isdigit() else None
                spool_id_add = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
                spool_id_from = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
                grams = float(parts[3]) if len(parts) > 3 else 0.0

                if rec_id and spool_id_add and spool_id_from:
                    result = _fix_history_consumption(rec_id, spool_id_add, spool_id_from, grams)
                    self._send_html(result)
                else:
                    self._send_html("<p>Ungültige Parameter für /fix-history/</p>")
            except Exception as e:
                log.error(f"Fix-History fehlgeschlagen: {e}")
                self._send_html(f'<p>Fehler: {html.escape(str(e))}</p>')
            return

        if self.path.startswith("/manage-history/"):
            try:
                rec_id = self.path.split("/manage-history/", 1)[1].split("?")[0].split("/")[0]
                if rec_id.isdigit():
                    self._send_html(_render_manage_history_page(int(rec_id)))
                else:
                    self._send_html("<p>Ungültige History-ID</p>")
            except Exception as e:
                log.error(f"Manage-History-Seite fehlgeschlagen: {e}")
                self._send_html(f'<p>Fehler: {html.escape(str(e))}</p>')
            return
        try:
            qs = parse_qs(urlsplit(self.path).query)
            sort_dir = qs.get("sort", ["desc"])[0]
            if sort_dir not in ("asc", "desc"):
                sort_dir = "desc"
            self._send_html(_render_history_page(sort_dir))
        except Exception as e:
            log.error(f"History-Seite konnte nicht gerendert werden: {e}", exc_info=True)
            self._send_html(f'<p>Fehler: {html.escape(str(e))}</p>')

    def _serve_camera_image(self):
        rec_id = self.path.split("/camera_image/", 1)[1].split("?")[0].split("/")[0]
        if not rec_id.isdigit():
            self.send_response(400)
            self.end_headers()
            return
        for ext, content_type in (("jpg", "image/jpeg"), ("png", "image/png")):
            path = os.path.join(HISTORY_IMAGES_DIR, f"{rec_id}_camera.{ext}")
            if os.path.exists(path):
                with open(path, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
        self.send_response(404)
        self.end_headers()

    def _serve_image(self):
        rec_id = self.path.split("/image/", 1)[1].split("?")[0].split("/")[0]
        if not rec_id.isdigit():
            self.send_response(400)
            self.end_headers()
            return
        path = os.path.join(HISTORY_IMAGES_DIR, f"{rec_id}.png")
        try:
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            # Titelbild wird pro history_id genau einmal geschrieben und nie
            # ueberschrieben (history_id ist ein monoton steigender Zaehler) -
            # sicher fuer eine lange Cache-Lebensdauer. NUR hier im Erfolgsfall:
            # der 404-Zweig unten bleibt ungecacht, damit ein Bild, das erst bei
            # einem spaeteren Poll erfolgreich gespeichert wird, nicht durch einen
            # frueh gecachten 404 blockiert wird.
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.end_headers()
            self.wfile.write(data)
        except Exception:
            self.send_response(404)
            self.end_headers()

    def _serve_plate_image(self):
        """Liefert das kleine Vorschaubild einer Druckplatte (textured_plate/
        supertack_plate). Die Bilder liegen in HA (image_upload) - genau die
        gleichen, die im Lovelace-Dashboard "lab-dashboard" verwendet werden.
        Wird einmal lokal gecacht (die zwei Bilder aendern sich praktisch nie),
        statt bei jedem Seitenaufruf erneut von HA zu laden."""
        plate_type = self.path.split("/plate-image/", 1)[1].split("?")[0].split("/")[0]
        image_id = PLATE_TYPE_IMAGE_IDS.get(plate_type)
        if not image_id:
            self.send_response(404)
            self.end_headers()
            return

        cache_path = os.path.join(HISTORY_IMAGES_DIR, f"plate_{plate_type}.png")
        data = None
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "rb") as f:
                    data = f.read()
            except OSError:
                data = None

        if data is None:
            try:
                r = _http.get(
                    f"{HA_URL}/api/image/serve/{image_id}/256x256",
                    headers=_ha_headers(), timeout=15,
                )
                r.raise_for_status()
                data = r.content
                os.makedirs(HISTORY_IMAGES_DIR, exist_ok=True)
                with open(cache_path, "wb") as f:
                    f.write(data)
            except Exception as e:
                log.warning(f"Druckplatten-Bild '{plate_type}' konnte nicht geladen werden: {e}")
                self.send_response(404)
                self.end_headers()
                return

        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=604800")
        self.end_headers()
        self.wfile.write(data)

    def _handle_log_level_post(self):
        length = min(int(self.headers.get("Content-Length", 0) or 0), 1024)
        params = parse_qs(self.rfile.read(length).decode("utf-8", errors="replace"))
        level = params.get("level", [""])[0]
        if level not in LOG_LEVELS:
            log.warning(f"POST /log-level mit ungueltiger Stufe abgelehnt: {level[:20]!r}")
            self.send_response(400)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        set_log_level(level)
        self._redirect("/")

    def do_POST(self):
        if self.path == "/log-level":
            try:
                self._handle_log_level_post()
            except Exception as e:
                log.error(f"POST /log-level fehlgeschlagen: {type(e).__name__}")
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()
            return

        if self.path == "/cloud-token":
            try:
                self._handle_cloud_token_post()
            except Exception as e:
                log.error(f"POST /cloud-token fehlgeschlagen: {type(e).__name__}")
                self.send_response(500)
                self.end_headers()
            return

        if self.path == "/cloud-token/delete":
            bambu_cloud.delete_token()
            log.info("Cloud-Namen: Token geloescht")
            self._redirect("/cloud-token?msg=deleted")
            return

        if self.path == "/restore-wartung":
            try:
                result = _restore_wartung_data()
                body = json.dumps(result, ensure_ascii=False).encode("utf-8")
                self.send_response(200 if result.get("ok") else 500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                log.info(f"P2S-Wartung-Restore ausgefuehrt: {result}")
            except Exception as e:
                log.error(f"POST /restore-wartung fehlgeschlagen: {e}", exc_info=True)
                self.send_response(500)
                self.end_headers()
            return

        if self.path == "/reset-druckzeit":
            try:
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length).decode("utf-8", errors="replace")
                params = parse_qs(raw)
                entity_id = params.get("entity_id", [""])[0]
                if entity_id not in WARTUNG_BASELINE_ENTITIES:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b'{"ok": false, "error": "unbekannte entity_id"}')
                    return
                with _state_lock:
                    state = load_state()
                    _reset_druckzeit_counter(state, entity_id)
                    save_state(state)
                log.info(f"Druckzeit-Zaehler zurueckgesetzt: {entity_id}")
                body = json.dumps({"ok": True, "entity_id": entity_id}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                log.error(f"POST /reset-druckzeit fehlgeschlagen: {e}", exc_info=True)
                self.send_response(500)
                self.end_headers()
            return

        if self.path == "/nachkaufen/refresh":
            self._handle_restock_refresh()
            return

        if self.path == "/nachkaufen/galaxus-runde/schritt":
            self._handle_galaxus_round_step()
            return

        if self.path == "/nachkaufen/shop-links":
            length = min(int(self.headers.get("Content-Length", 0) or 0), 1024)
            self.rfile.read(length)
            code = "laeuft" if shop_links.is_running() else "gestartet"
            if code == "gestartet":
                threading.Thread(target=_run_shop_links, name="shop-links-manual", daemon=True).start()
            self.send_response(303)
            self.send_header("Location", f"/nachkaufen/shop-links?hinweis={code}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if self.path == "/nachkaufen/galaxus-runde/ping":
            length = min(int(self.headers.get("Content-Length", 0) or 0), 2048)
            params = parse_qs(self.rfile.read(length).decode("utf-8", errors="replace"))
            first = lambda k: (params.get(k) or [""])[0][:200]
            result = price_check.round_ping(first("token"), first("version"), first("pid"), first("hash"),
                                            first("expect"), first("round"), first("decision"))
            body = json.dumps(result).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/nachkaufen/galaxus-runde/status":
            length = min(int(self.headers.get("Content-Length", 0) or 0), 4096)
            params = parse_qs(self.rfile.read(length).decode("utf-8", errors="replace"))
            first = lambda k: (params.get(k) or [""])[0][:500]
            result = price_check.round_status(first("round"), first("url"), first("phase"),
                                              first("detail"), first("token"))
            body = json.dumps(result).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/add-history":
            try:
                self._handle_add_history()
            except Exception as e:
                log.error(f"POST /add-history fehlgeschlagen: {e}", exc_info=True)
                self.send_response(500)
                self.end_headers()
                self.wfile.write(f"Fehler: {str(e)}".encode("utf-8"))
            return

        if self.path not in ("/rename", "/delete", "/assign-spool", "/confirm-consumption", "/manage-history/delete-spool"):
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            params = parse_qs(raw)
            rec_id = params.get("id", [""])[0]

            if self.path == "/rename":
                new_name = params.get("name", [""])[0].strip()[:200]
                if rec_id and new_name:
                    with _history_lock:
                        history = load_history()
                        for r in history.get("records", []):
                            if str(r.get("id")) == rec_id:
                                r["name"] = new_name
                                r["name_overridden"] = True
                                break
                        save_history(history)
            elif self.path == "/delete":
                if rec_id and not rec_id.isdigit():
                    log.warning(f"POST /delete: ungueltige id '{rec_id}' verworfen")
                elif rec_id:
                    with _history_lock:
                        history = load_history()
                        history["records"] = [
                            r for r in history.get("records", []) if str(r.get("id")) != rec_id
                        ]
                        save_history(history)
                    for fname in (f"{rec_id}.png", f"{rec_id}_camera.jpg", f"{rec_id}_camera.png"):
                        try:
                            os.remove(os.path.join(HISTORY_IMAGES_DIR, fname))
                        except OSError:
                            pass
                    log.info(f"History-Eintrag #{rec_id} geloescht")
            elif self.path == "/assign-spool":
                tray_key = params.get("tray", [""])[0]
                spool_id = params.get("spool_id", [""])[0]
                if tray_key and spool_id:
                    with _state_lock:
                        state = load_state()
                        if "user_spool_overrides" not in state:
                            state["user_spool_overrides"] = {}
                        state["user_spool_overrides"][tray_key] = int(spool_id)
                        save_state(state)

                    # AUCH ZU SPOOLMAN speichern! (extra dict mergen, nicht ersetzen)
                    try:
                        spool = sm_get(f"/spool/{int(spool_id)}")
                        if spool:
                            ts = datetime.now(timezone.utc).isoformat()
                            # Lade existierende extra dict
                            existing_extra = spool.get("extra") or {}
                            if not isinstance(existing_extra, dict):
                                existing_extra = {}
                            # MERGE: Füge neue Felder hinzu (nicht ersetzen!)
                            existing_extra["assigned_tray_key"] = _sm_extra_val(tray_key)
                            existing_extra["last_assigned_timestamp"] = _sm_extra_val(ts)
                            # Speichere merged dict
                            sm_patch(f"/spool/{int(spool_id)}", {"extra": existing_extra})
                            log.info(f"✅ User-Auswahl zu Spoolman vererbt: {tray_key} -> Spool {spool_id}")
                    except Exception as e:
                        log.warning(f"⚠️  Konnte Zuordnung nicht zu Spoolman vererben: {e}")

                    log.info(f"User-Auswahl gespeichert: {tray_key} -> Spool {spool_id}")
                elif tray_key:
                    # Leere spool_id = Platzhalter "– Spule wählen –" ausdruecklich
                    # gewaehlt -> Zuordnung entfernen (z.B. wenn eine externe Spule
                    # entnommen wurde und kein Override mehr gelten soll), statt es
                    # stillschweigend zu ignorieren.
                    with _state_lock:
                        state = load_state()
                        overrides = state.get("user_spool_overrides", {})
                        removed = overrides.pop(tray_key, None) is not None
                        save_state(state)
                    if removed:
                        log.info(f"User-Auswahl fuer {tray_key} zurueckgesetzt (keine Spule mehr zugewiesen)")
                else:
                    log.warning(f"assign-spool: fehlender Parameter tray={tray_key}, spool_id={spool_id}")

            elif self.path == "/confirm-consumption":
                job_key = params.get("job_key", [""])[0]
                spool_id = params.get("spool_id", [""])[0]
                tray_key = params.get("tray_key", [""])[0] or None
                segment = params.get("segment", [""])[0]
                if job_key and spool_id.isdigit():
                    with _state_lock:
                        state = load_state()
                        confirmed, message = confirm_final_booking(
                            state, job_key, int(spool_id), tray_key,
                            int(segment) if segment.isdigit() else None,
                        )
                        if confirmed:
                            save_state(state)
                    if not confirmed:
                        log.warning(f"confirm-consumption abgelehnt: {message}")
                else:
                    log.warning(f"confirm-consumption: fehlende/ungueltige Parameter job_key={job_key}, spool_id={spool_id}")

            elif self.path == "/manage-history/delete-spool":
                record_id = params.get("record_id", [""])[0]
                color_idx = params.get("color_idx", [""])[0]
                spool_id = params.get("spool_id", [""])[0]
                grams = params.get("grams", ["0"])[0]

                if record_id and color_idx and spool_id:
                    try:
                        record_id_int = int(record_id)
                        color_idx_int = int(color_idx)
                        grams_float = float(grams)
                        spool_id_int = int(spool_id)

                        # 1. Spule aus History entfernen
                        with _history_lock:
                            history = load_history()
                            for r in history.get("records", []):
                                if r.get("id") == record_id_int:
                                    if "colors" in r:
                                        r["colors"] = [c for i, c in enumerate(r["colors"]) if i != color_idx_int]
                                    break
                            save_history(history)

                        # 1b. Job-State komplett mit neuer History synchronisieren
                        # (damit die Spule nicht beim nächsten Poll wieder rekonstruiert wird)
                        with _state_lock:
                            state = load_state()
                            jobs = state.get("jobs", {})
                            for job_key, job in jobs.items():
                                if job.get("history_id") == record_id_int:
                                    # Rebuild colors_used DIREKT aus der neuen History-JSON
                                    # (weil _write_history_record IMMER die colors aus colors_used rekonstruiert)
                                    history_reload = load_history()
                                    history_rec = next(
                                        (r for r in history_reload.get("records", []) if r.get("id") == record_id_int),
                                        None
                                    )
                                    if history_rec:
                                        new_colors_used = {}
                                        colors_list = history_rec.get("colors", [])
                                        # Rekonstruiere colors_used aus der neuen History-JSON
                                        for cidx, color_entry in enumerate(colors_list):
                                            tray_key = color_entry.get("tray_key", "tray_0")
                                            if tray_key not in new_colors_used:
                                                new_colors_used[tray_key] = []
                                            new_colors_used[tray_key].append(color_entry)
                                        job["colors_used"] = new_colors_used
                                        log.info(f"  ✅ Job-State synchronisiert: colors_used neu aus History #{record_id_int}")
                                    break
                            save_state(state)

                        # 2. Verbrauch in Spoolman erhöhen (um das gerade gelöschte Gewicht zu kompensieren)
                        try:
                            spool = sm_get(f"/spool/{spool_id_int}")
                            if spool:
                                # Reduziere den Verbrauch (erhöhe die Restmenge)
                                current_spent = spool.get("used_weight", 0)
                                new_spent = max(0, current_spent - grams_float)
                                sm_patch(f"/spool/{spool_id_int}", {"used_weight": new_spent})
                                log.info(f"✅ History #{record_id_int}: Spool {spool_id_int} gelöscht | Spoolman Verbrauch: -{grams_float:.1f}g (neu: {new_spent:.1f}g)")
                        except Exception as e:
                            log.error(f"⚠️ Spoolman-Update fehlgeschlagen: {e}")

                    except Exception as e:
                        log.error(f"manage-history/delete-spool fehlgeschlagen: {e}")
        except Exception as e:
            log.error(f"POST {self.path} fehlgeschlagen: {e}", exc_info=True)

        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def log_message(self, format, *args):
        log.info("HistoryWeb: " + (format % args))


def _set_plate_type(rec_id, plate_type):
    """Setzt/korrigiert den Druckplatten-Typ eines History-Eintrags manuell -
    z.B. um alte Eintraege ohne plate_type nachzutragen oder die neue Spalte
    an einem bestehenden Eintrag zu ueberpruefen."""
    requested = plate_type
    plate_type = normalize_plate_type(plate_type)
    if not is_known_plate_type(plate_type):
        known = ", ".join(PLATE_TYPE_LABELS)
        return f'<p style="color: red;">❌ Unbekannter plate_type "{html.escape(str(requested))}" (bekannt: {known})</p>'

    with _history_lock:
        history = load_history()
        record = next((r for r in history.get("records", []) if r.get("id") == rec_id), None)
        if not record:
            return f'<p style="color: red;">❌ History-Eintrag #{rec_id} nicht gefunden</p>'

        old_type = record.get("plate_type")
        record["plate_type"] = plate_type
        save_history(history)

    label = html.escape(PLATE_TYPE_LABELS.get(plate_type, plate_type))
    return (
        f'<div style="padding: 20px; max-width: 600px; margin: 20px auto;">'
        f'<h3>✅ Druckplatte gesetzt</h3>'
        f'<p><strong>History #{rec_id}</strong>: {html.escape(old_type or "-")} → <strong>{plate_type}</strong> ({label})</p>'
        f'<p><a href="/">Zurück zur History</a></p></div>'
    )


def _set_spool_grams(rec_id, spool_id, grams):
    """Setze grams für Spule auf exakte Menge (nicht addieren!)"""
    try:
        with _history_lock:
            history = load_history()
            record = None
            for r in history.get("records", []):
                if r.get("id") == rec_id:
                    record = r
                    break

            if not record:
                return f'<p style="color: red;">❌ History-Eintrag #{rec_id} nicht gefunden</p>'

            # Finde Spule und SETZE grams
            found = False
            old_grams = 0
            for color_entry in record.get("colors", []):
                if color_entry.get("spool_id") == spool_id:
                    old_grams = color_entry.get("grams", 0)
                    color_entry["grams"] = grams
                    found = True
                    break

            if not found:
                return f'<p style="color: red;">❌ Spule #{spool_id} nicht in History #{rec_id}</p>'

            # Speichere History
            save_history(history)

            # Berechne neue Summe
            total_grams = sum(c.get("grams", 0) for c in record.get("colors", []))
            percent = (total_grams / 580.3) * 100 if total_grams > 0 else 0

            html = f'''
            <div style="padding: 20px; max-width: 600px; margin: 20px auto;">
            <h3>✅ Menge korrigiert</h3>
            <p><strong>History #{rec_id}</strong></p>
            <ul>
            <li>Spule #{spool_id}: {old_grams:.1f}g → <strong>{grams:.1f}g</strong></li>
            </ul>
            <p><strong>Neue Summe:</strong></p>
            <ul>
            {''.join(f'<li>Spule #{c.get("spool_id")}: {c.get("grams", 0):.1f}g</li>' for c in record.get("colors", []))}
            <li style="border-top: 1px solid #ccc; padding-top: 10px; margin-top: 10px;"><strong>GESAMT: {total_grams:.1f}g / 580.3g = {percent:.1f}%</strong></li>
            </ul>
            <p><a href="/" style="color: #0ea5e9; text-decoration: none;">← Zurück zur History</a></p>
            </div>
            '''
            log.info(f"✅ History #{rec_id}: Spule #{spool_id} {old_grams:.1f}g → {grams:.1f}g")
            return html

    except Exception as e:
        log.error(f"Set-Spool-Grams fehlgeschlagen: {e}")
        return f'<p style="color: red;">❌ Fehler: {html.escape(str(e))}</p>'


def _fix_history_consumption(rec_id, spool_id_add, spool_id_from, grams):
    """Korrigiere Verbrauch in History: addiere grams zu spool_id_add UND entferne spool_id_from"""
    try:
        with _history_lock:
            history = load_history()
            record = None
            for r in history.get("records", []):
                if r.get("id") == rec_id:
                    record = r
                    break

            if not record:
                return f'<p style="color: red;">❌ History-Eintrag #{rec_id} nicht gefunden</p>'

            # 1. Addiere grams zu spool_id_add
            found_add = False
            for color_entry in record.get("colors", []):
                if color_entry.get("spool_id") == spool_id_add:
                    old_grams = color_entry.get("grams", 0)
                    color_entry["grams"] = old_grams + grams
                    found_add = True
                    break

            if not found_add:
                return f'<p style="color: red;">❌ Spule #{spool_id_add} nicht in History #{rec_id}</p>'

            # 2. Entferne spool_id_from komplett aus der History
            colors_before = len(record.get("colors", []))
            record["colors"] = [c for c in record.get("colors", []) if c.get("spool_id") != spool_id_from]
            colors_after = len(record.get("colors", []))
            removed_spool = colors_before > colors_after

            # Speichere History
            save_history(history)

            # Berechne neue Summe
            total_grams = sum(c.get("grams", 0) for c in record.get("colors", []))
            percent = (total_grams / 580.3) * 100 if total_grams > 0 else 0

            html = f'''
            <div style="padding: 20px; max-width: 600px; margin: 20px auto;">
            <h3>✅ Verbrauch korrigiert</h3>
            <p><strong>History #{rec_id}</strong></p>
            <ul>
            <li>Spule #{spool_id_add}: +{grams:.1f}g hinzugefügt</li>
            <li>Spule #{spool_id_from}: <strong>aus History gelöscht</strong></li>
            </ul>
            <p><strong>Neue Summe:</strong></p>
            <ul>
            {''.join(f'<li>Spule #{c.get("spool_id")}: {c.get("grams", 0):.1f}g</li>' for c in record.get("colors", []))}
            <li style="border-top: 1px solid #ccc; padding-top: 10px; margin-top: 10px;"><strong>GESAMT: {total_grams:.1f}g / 580.3g = {percent:.1f}%</strong></li>
            </ul>
            <p><a href="/" style="color: #0ea5e9; text-decoration: none;">← Zurück zur History</a></p>
            </div>
            '''
            log.info(f"✅ History #{rec_id} korrigiert: Spule #{spool_id_add} +{grams:.1f}g | Spule #{spool_id_from} gelöscht")
            return html

    except Exception as e:
        log.error(f"Fix-History fehlgeschlagen: {e}")
        return f'<p style="color: red;">❌ Fehler: {html.escape(str(e))}</p>'


def _render_manage_history_page(rec_id):
    """UI zum Editieren einer History-Eintrags: Spulen-Zeilen löschen und Spoolman erhöhen."""
    history = load_history()
    record = None
    for r in history.get("records", []):
        if r.get("id") == rec_id:
            record = r
            break

    if not record:
        return f'<p>History-Eintrag #{rec_id} nicht gefunden</p>'

    colors = record.get("colors", [])
    html_rows = ""
    for idx, color in enumerate(colors):
        spool_id = color.get("spool_id", "?")
        filament_name = color.get("filament_name", "?")
        grams = color.get("grams", 0)

        delete_button = f'''
        <form style="display:inline;" method="POST" action="/manage-history/delete-spool">
            <input type="hidden" name="record_id" value="{rec_id}">
            <input type="hidden" name="color_idx" value="{idx}">
            <input type="hidden" name="spool_id" value="{spool_id}">
            <input type="hidden" name="grams" value="{grams}">
            <button type="submit" style="background: #ff4444; color: white; padding: 5px 10px; cursor: pointer; border: none; border-radius: 3px;">
                ❌ Löschen & Spoolman +{grams:.1f}g
            </button>
        </form>
        '''

        html_rows += f'''
        <tr>
            <td>{spool_id}</td>
            <td>{filament_name}</td>
            <td>{grams:.1f}g</td>
            <td>{delete_button}</td>
        </tr>
        '''

    return f'''
    <html>
    <head>
        <title>History #{rec_id} - Spulen verwalten</title>
        <meta charset="utf-8">
        <style>
            body {{ font-family: Arial, sans-serif; margin: 20px; }}
            table {{ border-collapse: collapse; width: 100%; margin-top: 20px; }}
            th, td {{ border: 1px solid #ccc; padding: 10px; text-align: left; }}
            th {{ background: #4caf50; color: white; }}
            tr:hover {{ background: #f5f5f5; }}
            .header {{ margin-bottom: 20px; }}
        </style>
    </head>
    <body>
        <div class="header">
            <h2>History #{rec_id} - Spulen verwalten</h2>
            <p><strong>Job:</strong> {html.escape(record.get('gcode') or record.get('gcode_path') or '?')}</p>
            <p><a href="/">← Zurück zur History</a></p>
        </div>

        <table>
            <thead>
                <tr>
                    <th>Spule-ID</th>
                    <th>Filament</th>
                    <th>Verbrauch</th>
                    <th>Aktion</th>
                </tr>
            </thead>
            <tbody>
                {html_rows}
            </tbody>
        </table>
    </body>
    </html>
    '''


def start_history_web_server():
    try:
        server = ThreadingHTTPServer(("0.0.0.0", HISTORY_WEB_PORT), _HistoryHandler)
        log.info(f"Druckverlauf-Webseite laeuft auf Port {HISTORY_WEB_PORT}")
        server.serve_forever()
    except Exception as e:
        log.error(f"History-Webserver konnte nicht gestartet werden: {e}", exc_info=True)


def _update_version_info_in_ha(info):
    """Schreibt Version/Release/Commit in HA input_text Entities (via REST API)."""
    try:
        # Version (z.B. "v3.1.0-0-ged1aa0f" → "3.1.0")
        version_clean = info.get('version', 'unknown').split('-')[0].lstrip('v')
        commit = info.get('commit', 'unknown')
        release_date = info.get('date', 'unknown')

        # Entities aktualisieren (input_text Entities müssen in HA existieren)
        entities = {
            VERSION_TEXT_ENTITY: version_clean,
            COMMIT_TEXT_ENTITY: commit,
            RELEASE_TEXT_ENTITY: release_date,
        }

        headers = _ha_headers()
        url = f"{HA_URL}/api/services/input_text/set_value"

        for entity_id, value in entities.items():
            try:
                payload = {"entity_id": entity_id, "value": str(value)}
                response = _http.post(url, json=payload, headers=headers, timeout=5)
                if response.status_code != 200:
                    log.debug(f"  HA Entity {entity_id}: Status {response.status_code}")
            except requests.RequestException as e:
                log.debug(f"  HA Entity {entity_id} Fehler: {e}")

        log.info(f"✓ Version Info an HA geschrieben: {version_clean} ({release_date}) [{commit}]")

    except Exception as e:
        log.warning(f"Version Info konnte nicht vorbereitet werden: {e}", exc_info=True)


def main():
    info = _get_git_info()
    log.info("=" * 60)
    log.info("SpoolManSyncBambu - Verbrauchs-Job (consumption.py)")
    log.info(f"Version: {info['version']}")
    log.info(f"Commit:  {info['commit']} ({info['timestamp']})")
    log.info(f"HA:        {HA_URL}")
    log.info(f"Spoolman:  {SPOOLMAN_URL}")
    log.info(f"Intervall: {POLL_INTERVAL}s | RUN_ONCE={RUN_ONCE}")
    log.info(
        f"History: SQLite {HISTORY_DB_FILE} | Limit: "
        f"{HISTORY_MAX_RECORDS if HISTORY_MAX_RECORDS > 0 else 'unbegrenzt'}"
    )
    log.info("=" * 60)

    # Muss VOR dem Webserver laufen: beide lesen den Verlauf, und der Import
    # der Altdaten soll nur einmal und ohne parallelen Zugriff passieren.
    init_history_storage()
    try:
        backfill_title_images()
    except Exception as e:
        log.warning(f"Titelbild-Nachtrag fehlgeschlagen: {e}")

    # Erstes Backup beim Startup (min. 15 Min. Abstand)
    history_store.backup_database(HISTORY_DB_FILE)

    # Version Info an HA schreiben
    _update_version_info_in_ha(info)

    threading.Thread(target=start_history_web_server, daemon=True).start()
    threading.Thread(target=_shop_links_loop, name="shop-links", daemon=True).start()
    threading.Thread(target=_cloud_names_loop, name="cloud-names", daemon=True).start()

    while True:
        try:
            with _state_lock:
                state = load_state()
                process_once(state)
                save_state(state)

            # Stündliches Backup: min. 15 Min. Abstand, max. 100 Dateien in _backup/
            history_store.backup_database(HISTORY_DB_FILE)

            log_remaining_report()
        except requests.exceptions.ConnectionError as e:
            log.error(f"Verbindungsfehler: {e}")
        except requests.exceptions.Timeout:
            log.error("API-Timeout")
        except requests.exceptions.HTTPError as e:
            log.error(f"HTTP-Fehler: {e}")
        except Exception as e:
            log.error(f"Unerwarteter Fehler: {e}", exc_info=True)

        if RUN_ONCE:
            break
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    if not HA_TOKEN:
        log.error("HA_TOKEN nicht gesetzt! Bitte .env konfigurieren.")
        sys.exit(1)
    main()
