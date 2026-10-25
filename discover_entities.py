#!/usr/bin/env python3
"""Schlaegt die Entity-Namen fuer die .env vor (Issue #6). Nur lesend: fragt Home
Assistant ab, gibt Zeilen fuer die .env aus und aendert nichts.

Aufruf (im Projektverzeichnis oder im Container):
    python3 discover_entities.py

Die Zuordnung geschieht ueber das Namensende (z.B. '..._druckstatus' oder
'..._print_status'). Mehrdeutige oder fehlende Treffer werden NICHT geraten,
sondern gemeldet - dann die Entity in HA nachsehen und von Hand eintragen.
"""
import os
import re
import sys

import requests

from entity_config import DEFAULT_PRINTER_PREFIX

_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "")

# env-Variable -> (Domain, moegliche Namensenden: deutsch, englisch)
# Die englischen Enden entsprechen den Standardnamen der Bambu-Lab-Integration;
# nicht jede Installation hat jede Entity - fehlende werden gemeldet, nicht geraten.
PRINTER_ENTITY_SUFFIXES = {
    "PRINT_STATUS_ENTITY":   ("sensor", ("druckstatus", "print_status")),
    "PRINT_PROGRESS_ENTITY": ("sensor", ("druckfortschritt", "print_progress")),
    "PRINT_WEIGHT_ENTITY":   ("sensor", ("gewicht_des_drucks", "print_weight")),
    "GCODE_ENTITY":          ("sensor", ("gcode_dateiname", "gcode_file")),
    "STARTTIME_ENTITY":      ("sensor", ("startzeit", "start_time")),
    "TASK_NAME_ENTITY":      ("sensor", ("name_der_aufgabe", "task_name")),
    "TITLE_IMAGE_ENTITY":    ("image", ("titelbild", "cover_image")),
    "LIVE_CAMERA_ENTITY":    ("camera", ("kamera", "camera")),
    "PLATE_TYPE_ENTITY":     ("sensor", ("druckbett_typ", "bed_type")),
}

_SLOT_RE = re.compile(r"^(sensor\.[a-z0-9_]+)_ams_\d+_slot_\d+$")


def fetch_entity_ids():
    response = requests.get(f"{HA_URL}/api/states",
                            headers={"Authorization": f"Bearer {HA_TOKEN}"}, timeout=15)
    response.raise_for_status()
    return sorted(item["entity_id"] for item in response.json())


def find_printer_prefixes(entity_ids):
    """Eindeutige Drucker-Praefixe aus den AMS-Slot-Sensoren ('sensor.p2s_<serial>')."""
    return sorted({m.group(1) for e in entity_ids if (m := _SLOT_RE.match(e))})


def match_by_suffix(entity_ids, domain, suffixes):
    """Alle Entities der Domain, deren Name auf eines der Enden endet."""
    return [e for e in entity_ids
            if e.startswith(f"{domain}.") and any(e.endswith("_" + s) for s in suffixes)]


def build_suggestions(entity_ids):
    """-> (zeilen_fuer_env, hinweise). Nur eindeutige Treffer werden vorgeschlagen."""
    lines, notes = [], []
    prefixes = find_printer_prefixes(entity_ids)
    if len(prefixes) == 1:
        lines.append(f"PRINTER_ENTITY_PREFIX={prefixes[0]}")
        if prefixes[0] != DEFAULT_PRINTER_PREFIX:
            notes.append("Drucker-Praefix weicht vom Standard ab - Zeile unbedingt uebernehmen.")
    elif not prefixes:
        notes.append("Kein AMS-Slot-Sensor gefunden (sensor.<drucker>_ams_<n>_slot_<m>). "
                     "Ist die Bambu-Lab-Integration geladen?")
    else:
        notes.append(f"Mehrere Drucker gefunden ({', '.join(prefixes)}) - bitte "
                     "PRINTER_ENTITY_PREFIX selbst waehlen; mehrere Drucker werden nicht unterstuetzt.")

    for var, (domain, suffixes) in PRINTER_ENTITY_SUFFIXES.items():
        hits = match_by_suffix(entity_ids, domain, suffixes)
        if len(hits) == 1:
            lines.append(f"{var}={hits[0]}")
        elif not hits:
            notes.append(f"{var}: nichts gefunden (gesucht: ...{' / ...'.join(suffixes)}) - von Hand eintragen.")
        else:
            notes.append(f"{var}: mehrdeutig ({', '.join(hits)}) - bitte eine waehlen.")
    return lines, notes


def main():
    if not HA_TOKEN:
        sys.exit("HA_TOKEN fehlt (in der .env oder als Umgebungsvariable).")
    try:
        entity_ids = fetch_entity_ids()
    except requests.HTTPError as e:
        sys.exit(f"Home Assistant ({HA_URL}) hat die Abfrage abgelehnt: HTTP {e.response.status_code} "
                 "(401 = HA_TOKEN ungueltig)")
    except requests.RequestException as e:
        sys.exit(f"Home Assistant ({HA_URL}) nicht erreichbar: {type(e).__name__}")
    lines, notes = build_suggestions(entity_ids)
    print("# Vorschlag fuer die .env (nichts wurde geaendert):")
    print("\n".join(lines))
    if notes:
        print("\n# Hinweise:")
        print("\n".join(f"# {n}" for n in notes))


if __name__ == "__main__":
    main()
