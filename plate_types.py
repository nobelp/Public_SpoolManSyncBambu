#!/usr/bin/env python3
"""
DIV_SpoolManSyncBambu - plate_types.py
Einheitliche Druckplatten-Typen fuer den Druckverlauf (GitHub Issue #32).

Die Bambu-Lab-Integration (ha-bambulab) fuellt den Sensor "Druckbett Typ" aus
zwei Quellen mit unterschiedlichem Format:
  - Bambu-Cloud-Auftragsdaten ("bedType")        -> Schluessel, z.B. "textured_plate"
  - 3MF vom Drucker per FTP (plate_N.json)        -> Bambu-Studio-Text, z.B. "Textured PEI Plate"
Welche Quelle greift, haengt vom Druckweg ab. Diese Funktion bildet beides auf
dieselben Schluessel ab, damit Anzeige und Auswertung nicht vom Druckweg abhaengen.
"""

import re

# Schluessel wie in ha-bambulab (translations "print_bed_type") plus die
# neuere "Textured Cool Plate" aus Bambu Studio.
PLATE_TYPE_LABELS = {
    "textured_plate": "Strukturierte Platte",
    "supertack_plate": "Bambu Cool Plate SuperTack",
    "cool_plate": "Kühlplatte / PLA-Platte",
    "hot_plate": "Glatte PEI-/Hochtemperaturplatte",
    "eng_plate": "Technische Platte",
    "textured_cool_plate": "Strukturierte Kühlplatte",
}

# Reihenfolge ist wichtig: spezifische Merkmale vor allgemeinen
# ("Textured Cool Plate" enthaelt sowohl "textured" als auch "cool").
_RULES = (
    ("supertack", "supertack_plate"),
    (("textured", "cool"), "textured_cool_plate"),
    ("textured", "textured_plate"),
    ("eng", "eng_plate"),
    ("hightemp", "hot_plate"),
    ("hotplate", "hot_plate"),
    ("smoothpei", "hot_plate"),
    ("cool", "cool_plate"),
    ("plaplate", "cool_plate"),
)

_EMPTY = {"", "unknown", "unavailable", "none", "null", "auto"}


def normalize_plate_type(value):
    """Liefert den einheitlichen Schluessel (z.B. "textured_plate"), None fuer
    leere/unbekannte Sensorwerte, oder den bereinigten Originaltext, wenn das
    Format unbekannt ist (bleibt so sichtbar statt verloren zu gehen)."""
    if value is None:
        return None
    text = str(value).strip()
    compact = re.sub(r"[^a-z0-9]", "", text.lower())
    if compact in _EMPTY:
        return None
    if text in PLATE_TYPE_LABELS:
        return text
    for needle, key in _RULES:
        needles = needle if isinstance(needle, tuple) else (needle,)
        if all(n in compact for n in needles):
            return key
    return text[:60]


def is_known_plate_type(value):
    return value in PLATE_TYPE_LABELS


def plate_label(value):
    """Anzeigetext; unbekannte Werte erscheinen als ihr Originaltext."""
    key = normalize_plate_type(value)
    if key is None:
        return None
    return PLATE_TYPE_LABELS.get(key, key)
