#!/usr/bin/env python3
"""
DIV_SpoolManSyncBambu - title_image.py
Reine Hilfsfunktionen fuer das Titelbild (3D-Vorschau) im Druckverlauf
(GitHub Issue #33). Ohne HA-Zugriff, daher direkt testbar.

Hintergrund: Das HA-Entity image.<drucker>_titelbild hat als State den
Zeitpunkt des letzten Bildwechsels. Ein neuer Druck kann vom Dienst erkannt
werden, BEVOR die Bambu-Integration das neue Vorschaubild geladen hat - dann
zeigt das Entity noch das Bild des vorherigen Drucks. Deshalb wird ein Bild nur
uebernommen, wenn es zeitlich zum Druck passt.
"""

import re
from datetime import datetime, timedelta, timezone

# Das Bild kommt typischerweise Sekunden nach dem Start (prepare). Etwas Luft
# nach vorne fuer Uhren-/Rundungsunterschiede zwischen Drucker und HA.
FRESH_SLACK_BEFORE_S = 60
# Nach Druckende kann HA das (schon vorhandene) Bild noch liefern - ein Bild,
# das deutlich NACH dem Ende gewechselt hat, gehoert aber zum naechsten Druck.
FRESH_SLACK_AFTER_S = 120

_TOKEN_RE = re.compile(r"(token=)[^&\s'\"]+", re.IGNORECASE)


def parse_ts(value):
    """ISO-Zeit -> zeitzonenbewusstes datetime. Werte ohne Zeitzone (so liefert
    HA den State des Image-Entitys) gelten als lokale Zeit des Containers."""
    if not value or value in ("unknown", "unavailable"):
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.astimezone()


def image_fits_job(image_ts, job_start, job_end=None):
    """True, wenn das Bild zum Druck gehoert: gewechselt fruehestens kurz vor
    dem Start und (falls der Druck beendet ist) spaetestens kurz nach dem Ende.
    Unbekannte Zeiten -> False (lieber warten als ein falsches Bild speichern)."""
    img = parse_ts(image_ts) if not isinstance(image_ts, datetime) else image_ts
    start = parse_ts(job_start) if not isinstance(job_start, datetime) else job_start
    if img is None or start is None:
        return False
    if img < start - timedelta(seconds=FRESH_SLACK_BEFORE_S):
        return False
    if job_end is not None:
        end = parse_ts(job_end) if not isinstance(job_end, datetime) else job_end
        if end is not None and img > end + timedelta(seconds=FRESH_SLACK_AFTER_S):
            return False
    return True


_GENERIC_NAME = re.compile(r"(^plate_\d+$)|(\.(gcode|3mf)$)|(^auto_cali)|/", re.IGNORECASE)


def reuse_key(name):
    """Vergleichsschluessel fuer "gleicher Druck" (Nachdruck) anhand des
    Aufgabennamens - None fuer leere oder allgemeine Namen (z.B. "plate_1",
    Gcode-Pfade, Kalibrierung), damit nie Bilder verschiedener Drucke
    verwechselt werden."""
    text = re.sub(r"\s+", " ", str(name or "")).strip()
    if len(text) < 4 or text in ("?", "unknown", "unavailable") or _GENERIC_NAME.search(text):
        return None
    return text.lower()


def mask_tokens(text):
    """Zugriffstokens (z.B. HA image_proxy ?token=...) fuer Logs maskieren."""
    return _TOKEN_RE.sub(lambda m: m.group(1) + "***", str(text))


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()
