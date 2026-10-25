#!/usr/bin/env python3
"""
DIV_SpoolManSyncBambu - filament_enrich.py v1.5.1
Reichert ein Spoolman-Filament mit Stammdaten aus einer manuell eingefuegten
Produkt-URL an - im Normalfall wird dabei ein komplett neues Filament
angelegt (die ID vergibt Spoolman selbst, nicht der User).

Zweistufiger Ablauf ueber Home-Assistant-Helper (per Dashboard befuellt):
  - input_text.neues_filament_url             Produkt-URL (z.B. Amazon-Link)
  - input_button.neues_filament_pruefen       Schritt 1: suchen
  - input_select.neues_filament_auswahl       Vorschlaege aus Spoolmans externer
    Datenbank (Hersteller+Material passend zur URL) - manuell auswaehlen
  - input_button.neues_filament_anlegen       Schritt 2: mit Auswahl anlegen
  - input_text.neues_filament_status          Rueckmeldung (wird von hier beschrieben)
  - input_number.neues_filament_filament_id   optional/fortgeschritten: >0 reichert
    beim "pruefen"-Klick direkt ein bestehendes Filament an statt zu suchen
    (nicht im Dashboard, nur fuer manuelle Nutzung ueber Entwicklerwerkzeuge)

Warum zweistufig: Spoolmans externe Datenbank (~7000 Eintraege) hat oft viele
Farben pro Hersteller+Material (z.B. 28 SUNLU-PETG-Farben). Der Seitentitel
allein reicht meist nicht, um die exakte Farbe sicher zu bestimmen - also
schlaegt Schritt 1 alle passenden Kandidaten vor, der User waehlt die richtige
Farbe aus, Schritt 2 legt dann mit exakt bekannten Werten an (keine geratene
Farbe mehr).

Verarbeitung ist an die Button-Klicks gebunden (Zeitstempel-Vergleich), nicht
an eine Werte-Aenderung der URL - sonst wuerde ein zweiter Versuch mit exakt
derselben URL fuer immer stillschweigend ignoriert, weil "schon mal gesehen".

Deterministisch, kein LLM: es werden nur strukturierte Seiten-Metadaten
(schema.org/JSON-LD, Open Graph) sowie ein paar eng gefasste Regex-Fallbacks
(Amazon-ASIN aus der URL, Material-/Hersteller-/Gewichts-Keywords im Titel)
ausgewertet. Nichts wird erfunden - findet sich ein Feld nicht in einer dieser
Quellen, bleibt es leer.

Bot-Schutz-Erkennung: liefert eine Shop-Seite nur einen generischen Titel
(z.B. blockierte Amazon-Seite zeigt nur "Amazon.de" statt Produktname), wird
NICHTS vorgeschlagen/angelegt - lieber ein klarer Fehler als Datenmuell.

Erweiterbar: neue Felder = neuer Eintrag in EXTRACTORS + FIELD_TO_SPOOLMAN,
keine Strukturaenderung noetig.
"""

import html
import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

import safe_fetch
from entity_config import find_missing, missing_helpers_message

# Wiederverwendete Session statt requests.get/post/patch(): haelt die
# TCP/TLS-Verbindung zu HA und Spoolman offen (Keep-Alive-Connection-Pooling)
# statt bei jedem einzelnen API-Call neu zu verbinden.
_http = requests.Session()

# Versions-Info auslesen (aus Git tags)
def _get_git_version():
    """Liest die aktuelle Version aus git tags, Fallback zu 1.5"""
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
        return version if version else "1.5"
    except Exception:
        return "1.5"


def _get_git_info():
    """Liest Git-Info aus Build-Zeit-Dateien (Container-safe)"""
    try:
        app_dir = os.path.dirname(os.path.abspath(__file__))

        version = "1.5"
        commit = "unknown"
        timestamp = ""

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

        return {
            "version": version,
            "commit": commit,
            "timestamp": timestamp,
        }
    except Exception:
        return {
            "version": "1.5",
            "commit": "unknown",
            "timestamp": "",
        }


GIT_VERSION = _get_git_version()

# .env laden (gleiches Pattern wie sync.py/consumption.py)
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

HA_URL        = os.environ.get("HA_URL", "http://homeassistant.local:8123")
HA_TOKEN      = os.environ.get("HA_TOKEN", "")
SPOOLMAN_URL  = os.environ.get("SPOOLMAN_URL", "http://spoolman.local:7912").rstrip("/")
POLL_INTERVAL = int(os.environ.get("FILAMENT_ENRICH_INTERVAL", "30"))
STATE_FILE    = os.environ.get("FILAMENT_ENRICH_STATE_FILE", "/app/data/filament_enrich_state.json")
LOG_FILE      = os.environ.get("FILAMENT_ENRICH_LOG_FILE", "/app/logs/filament_enrich.log")
MAX_BYTES     = int(os.environ.get("FILAMENT_ENRICH_MAX_BYTES", str(5_000_000)))
WEB_PORT      = int(os.environ.get("FILAMENT_ENRICH_WEB_PORT", "8099"))
MAX_PASTE_BYTES = int(os.environ.get("FILAMENT_ENRICH_MAX_PASTE_BYTES", str(200_000)))

URL_ENTITY           = "input_text.neues_filament_url"
NAME_OVERRIDE_ENTITY = "input_text.neues_filament_produktname"
SEARCH_BUTTON_ENTITY = "input_button.neues_filament_pruefen"
CREATE_BUTTON_ENTITY = "input_button.neues_filament_anlegen"
SELECT_ENTITY        = "input_select.neues_filament_auswahl"
ID_ENTITY            = "input_number.neues_filament_filament_id"
STATUS_ENTITY        = "input_text.neues_filament_status"

# Diese Helfer legt der Dienst NICHT selbst an; sie muessen in HA existieren
# (Beschreibung: HELPERS.md). Fehlen sie, funktioniert das Formular nicht.
REQUIRED_HELPERS = (URL_ENTITY, NAME_OVERRIDE_ENTITY, SEARCH_BUTTON_ENTITY, CREATE_BUTTON_ENTITY,
                    SELECT_ENTITY, ID_ENTITY, STATUS_ENTITY)
_helper_check_done = False


def check_helpers_once():
    """Beim ersten erfolgreichen Poll melden, welche Helfer in HA fehlen. Scheitert
    die Abfrage (HA nicht erreichbar), wird beim naechsten Poll erneut geprueft."""
    global _helper_check_done
    if _helper_check_done:
        return
    r = _http.get(f"{HA_URL}/api/states", headers=_ha_headers(), timeout=15)
    r.raise_for_status()
    present = {item["entity_id"] for item in r.json()}
    _helper_check_done = True
    message = missing_helpers_message(find_missing(present, REQUIRED_HELPERS))
    if message:
        log.error(message)
    else:
        log.info("Alle benoetigten HA-Helfer fuer das Filament-Formular sind vorhanden")


PLACEHOLDER_OPTION = "– bitte zuerst pruefen –"
GENERIC_OPTION      = "Generisch anlegen (kein Datenbank-Treffer/keine passende Farbe)"
MAX_SUGGESTIONS     = 35

FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}

# Extrahiertes Feld -> Spoolman-API-Feld. Neue Felder hier + in einem
# Extractor ergaenzen, sonst nichts anfassen.
FIELD_TO_SPOOLMAN = {
    "price": "price",
    "article_number": "article_number",
    "comment": "comment",
    "spool_weight": "spool_weight",
    "settings_extruder_temp": "settings_extruder_temp",
    "settings_bed_temp": "settings_bed_temp",
    "density": "density",
    "diameter": "diameter",
}

# Fuer's Anlegen neuer Filamente (Ziel-ID=0): Material/Hersteller werden nur
# aus Titel/Name per Keyword erkannt - reicht das nicht, bleiben die Felder
# leer statt geraten zu werden (Spoolman verlangt nur density+diameter).
MATERIAL_KEYWORDS = [
    "PETG-CF", "PETG-HF", "PETG", "PLA-CF", "PLA-GF", "PLA",
    "ABS-GF", "ABS", "ASA-CF", "ASA", "TPU-95A", "TPU-90A", "TPU",
    "PA6-CF", "PA-CF", "PA6", "PA", "PC-ABS", "PC", "PVA", "HIPS",
]
DENSITY_BY_MATERIAL = {
    "PLA": 1.24, "PLA-CF": 1.28, "PETG": 1.27, "PETG-CF": 1.30, "PETG-HF": 1.27,
    "ABS": 1.05, "ABS-GF": 1.28, "ASA": 1.07, "ASA-CF": 1.22,
    "TPU": 1.21, "TPU-90A": 1.21, "TPU-95A": 1.21,
    "PA": 1.15, "PA6": 1.15, "PA-CF": 1.22, "PA6-CF": 1.22,
    "PC": 1.20, "PC-ABS": 1.15, "PVA": 1.19, "HIPS": 1.06,
}
VENDOR_KEYWORDS = {
    "bambu": "Bambu Lab", "prusament": "Prusa", "polymaker": "Polymaker",
    "esun": "eSUN", "sunlu": "SUNLU", "hatchbox": "Hatchbox", "overture": "Overture",
}
DEFAULT_DIAMETER = 1.75
DEFAULT_WEIGHT_G = 1000.0
DEFAULT_DENSITY = 1.24

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("filament_enrich")


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning(f"State konnte nicht geladen werden: {e}")
    return {"last_search_press": None, "last_create_press": None, "pending": None}


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.error(f"State konnte nicht gespeichert werden: {e}")


# ─────────────────────────────────────────────────────────────────
# HOME ASSISTANT
# ─────────────────────────────────────────────────────────────────
def _ha_headers():
    return {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}


def ha_get_state(entity_id):
    r = _http.get(f"{HA_URL}/api/states/{entity_id}", headers=_ha_headers(), timeout=15)
    r.raise_for_status()
    return r.json()


def ha_set_input_text(entity_id, value):
    r = _http.post(
        f"{HA_URL}/api/services/input_text/set_value",
        headers=_ha_headers(),
        json={"entity_id": entity_id, "value": value[:255]},
        timeout=15,
    )
    r.raise_for_status()


def ha_set_input_number(entity_id, value):
    r = _http.post(
        f"{HA_URL}/api/services/input_number/set_value",
        headers=_ha_headers(),
        json={"entity_id": entity_id, "value": value},
        timeout=15,
    )
    r.raise_for_status()


def ha_set_select_options(entity_id, options):
    r = _http.post(
        f"{HA_URL}/api/services/input_select/set_options",
        headers=_ha_headers(),
        json={"entity_id": entity_id, "options": options},
        timeout=15,
    )
    r.raise_for_status()


def ha_select_option(entity_id, option):
    r = _http.post(
        f"{HA_URL}/api/services/input_select/select_option",
        headers=_ha_headers(),
        json={"entity_id": entity_id, "option": option},
        timeout=15,
    )
    r.raise_for_status()


def _set_status(message):
    try:
        ha_set_input_text(STATUS_ENTITY, message)
    except Exception as e:
        log.error(f"Status-Update fehlgeschlagen: {e}")


def _reset_after_enrich(status_message):
    """Nach dem alten Einzelschritt-Pfad (bestehendes Filament anreichern)."""
    try:
        ha_set_input_text(STATUS_ENTITY, status_message)
        ha_set_input_text(URL_ENTITY, "")
        ha_set_input_number(ID_ENTITY, 0)
    except Exception as e:
        log.error(f"HA-Reset fehlgeschlagen: {e}")


def _reset_after_create(status_message):
    """Nach erfolgreichem Neuanlegen (Schritt 2)."""
    try:
        ha_set_input_text(STATUS_ENTITY, status_message)
        ha_set_input_text(URL_ENTITY, "")
        ha_set_input_text(NAME_OVERRIDE_ENTITY, "")
        ha_set_select_options(SELECT_ENTITY, [PLACEHOLDER_OPTION])
        ha_select_option(SELECT_ENTITY, PLACEHOLDER_OPTION)
    except Exception as e:
        log.error(f"HA-Reset fehlgeschlagen: {e}")


# ─────────────────────────────────────────────────────────────────
# SPOOLMAN
# ─────────────────────────────────────────────────────────────────
_SM = {"Content-Type": "application/json"}


def sm_get(path):
    r = _http.get(f"{SPOOLMAN_URL}/api/v1{path}", headers=_SM, timeout=10)
    r.raise_for_status()
    return r.json()


def sm_patch(path, data):
    r = _http.patch(f"{SPOOLMAN_URL}/api/v1{path}", headers=_SM, json=data, timeout=10)
    r.raise_for_status()
    return r.json()


def sm_post(path, data):
    r = _http.post(f"{SPOOLMAN_URL}/api/v1{path}", headers=_SM, json=data, timeout=10)
    r.raise_for_status()
    return r.json()


def ensure_vendor(name):
    """Lieferant holen oder anlegen. None bei unbekanntem/fehlendem Namen."""
    if not name:
        return None
    vendors = sm_get("/vendor")
    for v in vendors:
        if v["name"].lower() == name.lower():
            return v["id"]
    result = sm_post("/vendor", {"name": name})
    log.info(f"Lieferant angelegt: '{name}' (ID {result['id']})")
    return result["id"]


_EXTERNAL_DB_CACHE = {"data": None}


def _get_external_filaments():
    """Spoolmans eingebaute externe Datenbank (~7000 echte Hersteller-Produkte),
    pro Prozess einmal geladen und gecacht."""
    if _EXTERNAL_DB_CACHE["data"] is None:
        try:
            _EXTERNAL_DB_CACHE["data"] = sm_get("/external/filament")
        except Exception as e:
            log.warning(f"Externe Spoolman-Datenbank nicht erreichbar: {e}")
            _EXTERNAL_DB_CACHE["data"] = []
    return _EXTERNAL_DB_CACHE["data"]


def _candidate_color_word(name):
    """Letztes Wort des DB-Farbnamens (i.d.R. die eigentliche Farbe, auch bei
    mehrteiligen Namen wie 'High Speed Matte PETG - Black' -> 'black')."""
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    return words[-1] if words else ""


def _search_external_candidates(vendor_name, material, guess_text):
    """Sucht in Spoolmans externer DB nach Hersteller+Material. Kommt eine
    erkennbare Farbe im Text vor (z.B. "(Black)"), werden NUR die dazu
    passenden Kandidaten zurueckgegeben - nicht alle Farben der Produktlinie
    (sonst zeigt das Dropdown bei eindeutig "Black" im Titel trotzdem Blau/
    Gruen/... an, was verwirrend und falsch waere). Ohne Farbtreffer im Text:
    komplette Liste als Fallback, damit man trotzdem manuell waehlen kann."""
    if not vendor_name or not material:
        return []
    candidates = [
        f for f in _get_external_filaments()
        if f.get("manufacturer", "").lower() == vendor_name.lower()
        and f.get("material", "").upper() == material.upper()
    ]
    if not candidates:
        return []

    low_text = (guess_text or "").lower()

    def _color_matches(c):
        word = _candidate_color_word(c.get("name"))
        return bool(word) and re.search(r"\b" + re.escape(word) + r"\b", low_text)

    matches = sorted(
        (c for c in candidates if _color_matches(c)), key=lambda c: c.get("name", "")
    )
    if matches:
        return matches

    return sorted(candidates, key=lambda c: c.get("name", ""))


def _looks_like_real_title(title, url):
    """Erkennt generische Bot-Schutz-/Blockseiten (Titel = nur der Domainname
    o.ae.), um kein Datenmuell-Filament aus einer blockierten Seite anzulegen."""
    if not title or len(title.strip()) < 15:
        return False
    host = urlparse(url).netloc.lower().replace("www.", "")
    # "www." auch im Titel ignorieren: Bot-Schutzseiten liefern oft den Host
    # samt Praefix als Titel ("www.shop.example")
    bare_title = re.sub(r"[^a-z0-9]", "", title.lower().replace("www.", ""))
    bare_host = re.sub(r"[^a-z0-9]", "", host)
    if bare_host and bare_title == bare_host:
        return False
    return True


# ─────────────────────────────────────────────────────────────────
# EXTRAKTOREN (jeweils best effort, liefern {feld: (wert, confidence, quelle)})
# ─────────────────────────────────────────────────────────────────
def _find_jsonld_product(soup):
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        flat = []
        for item in items:
            if isinstance(item, dict) and "@graph" in item:
                flat.extend(item["@graph"])
            else:
                flat.append(item)
        for item in flat:
            if not isinstance(item, dict):
                continue
            types = item.get("@type", "")
            types = types if isinstance(types, list) else [types]
            if "Product" in types:
                return item
    return None


def _extract_jsonld(soup, url):
    fields = {}
    product = _find_jsonld_product(soup)
    if not product:
        return fields

    if product.get("name"):
        fields["name"] = (str(product["name"]).strip(), "hoch", "JSON-LD name")

    for gtin_key in ("gtin13", "gtin12", "gtin8", "gtin"):
        if product.get(gtin_key):
            fields["article_number"] = (str(product[gtin_key]), "hoch", f"JSON-LD {gtin_key}")
            break
    if "article_number" not in fields and product.get("mpn"):
        fields["article_number"] = (str(product["mpn"]), "hoch", "JSON-LD mpn")
    if "article_number" not in fields and product.get("sku"):
        fields["article_number"] = (str(product["sku"]), "mittel", "JSON-LD sku")

    offers = product.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if isinstance(offers, dict) and offers.get("price"):
        try:
            fields["price"] = (float(offers["price"]), "hoch", "JSON-LD offers.price")
        except (TypeError, ValueError):
            pass

    return fields


def _extract_opengraph(soup, url):
    fields = {}

    def meta(prop):
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        return tag.get("content") if tag and tag.get("content") else None

    price = meta("product:price:amount") or meta("og:price:amount")
    if price:
        try:
            fields["price"] = (float(str(price).replace(",", ".")), "mittel", "Open Graph price")
        except ValueError:
            pass

    return fields


def _extract_title_comment(soup, url):
    """Liefert immer einen Kommentar mit Quelle - nie erfunden, nur Herkunft."""
    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else ""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    comment = f"Importiert von {url} am {now}"
    if title:
        comment += f" – \"{title[:120]}\""
    fields = {"comment": (comment, "hoch", "Seitentitel + URL")}
    if title:
        # Voller Titel hier (nicht auf 64 Zeichen kuerzen!) - wird sowohl als
        # Name-Fallback als auch als Text fuer Material-/Hersteller-/Gewichts-
        # Erkennung genutzt (create_new_filament kuerzt erst beim Schreiben).
        fields["name"] = (title, "niedrig", "Seitentitel (Fallback)")
    return fields


def _extract_amazon_asin(soup, url):
    fields = {}
    m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", url)
    if m:
        fields["article_number"] = (m.group(1), "hoch", "Amazon-ASIN aus URL")
    return fields


# Reihenfolge = Prioritaet: erster Treffer pro Feld gewinnt.
EXTRACTORS = [
    _extract_jsonld,
    _extract_opengraph,
    _extract_amazon_asin,
    _extract_title_comment,
]


def gather_fields(html, url):
    soup = BeautifulSoup(html, "html.parser")
    merged = {}
    for extractor in EXTRACTORS:
        try:
            result = extractor(soup, url)
        except Exception as e:
            log.warning(f"Extraktor {extractor.__name__} fehlgeschlagen: {e}")
            continue
        for key, value in result.items():
            merged.setdefault(key, value)
    return merged


# ─────────────────────────────────────────────────────────────────
# GUESSER fuer's Neuanlegen (nur Keyword-Matching, nichts Statistisches)
# ─────────────────────────────────────────────────────────────────
def _guess_material(text):
    if not text:
        return None
    for kw in MATERIAL_KEYWORDS:
        if re.search(r"\b" + re.escape(kw) + r"\b", text, re.IGNORECASE):
            return kw.upper()
    return None


def _guess_vendor(text):
    if not text:
        return None
    low = text.lower()
    for kw, vendor in VENDOR_KEYWORDS.items():
        if kw in low:
            return vendor
    return None


def _guess_weight_g(text):
    if not text:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(kg|g)\b", text, re.IGNORECASE)
    if not m:
        return None
    value = float(m.group(1).replace(",", "."))
    return value * 1000 if m.group(2).lower() == "kg" else value


# ─────────────────────────────────────────────────────────────────
# VERARBEITUNG
# ─────────────────────────────────────────────────────────────────
def _candidate_label(c):
    parts = [c["manufacturer"], c["material"], c["name"]]
    if c.get("color_hex"):
        parts.append(f"#{c['color_hex']}")
    return " ".join(str(p) for p in parts if p)[:100]


def create_new_filament_from_choice(fields, chosen):
    """Legt ein neues Filament an - chosen ist entweder ein Eintrag aus
    Spoolmans externer Datenbank (vom User in Schritt 2 explizit ausgewaehlt,
    also Farbe/Specs sicher korrekt) oder None (generisch, Branchen-Standard-
    Defaults). Nur density+diameter sind fuer Spoolman zwingend."""
    guess_text = fields.get("_search_text") or fields.get("name", (None,))[0] or ""
    name_text = fields.get("name", (None,))[0] or ""
    material = _guess_material(guess_text)
    vendor_name = _guess_vendor(guess_text)
    vendor_id = ensure_vendor(vendor_name)

    if chosen:
        density = chosen.get("density") or DEFAULT_DENSITY
        diameter = chosen.get("diameter") or DEFAULT_DIAMETER
        weight = chosen.get("weight") or _guess_weight_g(guess_text) or DEFAULT_WEIGHT_G
        spool_weight = chosen.get("spool_weight")
        extruder_temp = chosen.get("extruder_temp")
        bed_temp = chosen.get("bed_temp")
        color_hex = chosen.get("color_hex")
        source_note = f"Spoolman-Datenbank: {_candidate_label(chosen)}"
    else:
        density = DENSITY_BY_MATERIAL.get(material, DEFAULT_DENSITY)
        diameter = DEFAULT_DIAMETER
        weight = _guess_weight_g(guess_text) or DEFAULT_WEIGHT_G
        spool_weight = extruder_temp = bed_temp = color_hex = None
        source_note = "generisch angelegt (kein Datenbank-Treffer/keine Auswahl)"

    data = {
        "name": (name_text or guess_text)[:64] or "Unbekanntes Filament",
        "density": density,
        "diameter": diameter,
        "weight": weight,
    }
    if material:
        data["material"] = material
    if vendor_id:
        data["vendor_id"] = vendor_id
    if spool_weight:
        data["spool_weight"] = spool_weight
    if extruder_temp:
        data["settings_extruder_temp"] = extruder_temp
    if bed_temp:
        data["settings_bed_temp"] = bed_temp
    if color_hex:
        data["color_hex"] = color_hex
    for extracted_key in ("price", "article_number", "comment"):
        if extracted_key in fields:
            data[FIELD_TO_SPOOLMAN[extracted_key]] = fields[extracted_key][0]

    result = sm_post("/filament", data)
    log.info(f"Neues Filament angelegt: ID={result['id']} '{data['name']}' ({source_note})")
    return result, source_note


def process_enrich_existing(url, filament_id):
    """Alter Einzelschritt-Pfad: bestehendes Filament anreichern (Ziel-ID > 0,
    nur ueber Entwicklerwerkzeuge/manuell genutzt, nicht im Dashboard)."""
    log.info(f"Reichere Filament ID={filament_id} an <- {url}")

    try:
        filament = sm_get(f"/filament/{filament_id}")
    except Exception as e:
        return f"Fehler: Filament {filament_id} nicht gefunden ({e})"

    try:
        page_text = safe_fetch.fetch_text(url, headers=FETCH_HEADERS, max_bytes=MAX_BYTES)
    except safe_fetch.UnsafeURLError as e:
        log.warning(f"Abruf abgelehnt (Filament ID={filament_id}): {e}")
        return f"Fehler: {e}"
    except Exception as e:
        return f"Fehler beim Abrufen der Seite (evtl. Bot-Schutz): {e}"

    fields = gather_fields(page_text, url)

    updates = {}
    applied = []
    skipped_existing = []
    for extracted_key, (value, confidence, source) in fields.items():
        sm_key = FIELD_TO_SPOOLMAN.get(extracted_key)
        if not sm_key:
            continue
        current = filament.get(sm_key)
        if current not in (None, "", 0):
            skipped_existing.append(sm_key)
            continue
        updates[sm_key] = value
        applied.append(f"{sm_key}={value} ({confidence}, {source})")

    if not updates:
        return "Keine neuen Angaben gefunden (oder alle Felder bereits gesetzt)."

    try:
        sm_patch(f"/filament/{filament_id}", updates)
    except Exception as e:
        return f"Fehler beim Schreiben nach Spoolman: {e}"

    log.info(f"Filament ID={filament_id}: {', '.join(applied)}")
    msg = f"OK: {', '.join(sorted(updates.keys()))} gesetzt"
    if skipped_existing:
        msg += f" (uebersprungen, schon gesetzt: {', '.join(sorted(set(skipped_existing)))})"
    return msg


def process_search(url, name_override=None):
    """Schritt 1: Seite laden (best effort, nur wenn url gesetzt), Hersteller+
    Material raten, passende Kandidaten aus Spoolmans externer Datenbank
    suchen. Legt noch NICHTS an - gibt (status_message, pending_dict_oder_None)
    zurueck.

    name_override: optionaler, manuell eingegebener Produktname ODER kompletter
    per Volltext-Formular eingefuegter Seitentext. Macht uns unabhaengig von
    Bot-Schutz (z.B. Amazon) - die Seite wird trotzdem bestmoeglich fuer Preis/
    Artikelnummer/Kommentar abgerufen, ein Fehlschlag dabei blockiert die Suche
    dann aber nicht mehr. url ist optional (leer = nur name_override nutzen).
    """
    url = (url or "").strip()
    fields = {}
    page_fetch_error = None

    if url:
        if not url.lower().startswith(("http://", "https://")):
            return "Fehler: keine gueltige http(s)-URL", None
        try:
            fields = gather_fields(safe_fetch.fetch_text(url, headers=FETCH_HEADERS, max_bytes=MAX_BYTES), url)
        except safe_fetch.UnsafeURLError as e:
            # Interne/lokale Ziele werden nie abgerufen - auch nicht mit manuellem
            # Text als Fallback, und die URL landet nicht im Kommentar (Issue #46).
            log.warning(f"Abruf abgelehnt: {e}")
            return f"Fehler: {e}", None
        except Exception as e:
            page_fetch_error = str(e)
            log.warning(f"Seite nicht abrufbar ({e}) - {'nutze manuellen Produktnamen/-text' if name_override else 'kein Fallback'}")
        # Amazon-ASIN kommt nur aus der URL selbst, unabhaengig vom Seitenabruf.
        if "article_number" not in fields:
            fields.update(_extract_amazon_asin(None, url))

    name_override = (name_override or "").strip()
    scraped_title = fields.get("name", (None,))[0] or ""

    if name_override:
        title = name_override
        fields["name"] = (name_override[:64], "hoch", "Manuell eingegeben/eingefuegt")
        if "comment" not in fields:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            comment = f"Manuell importiert am {now}"
            if url:
                comment = f"Importiert von {url} am {now} (Produktname/-text manuell)"
            fields["comment"] = (comment, "hoch", "Manuell")
    elif not url:
        return "Fehler: weder Produkt-URL noch Produktname/-text angegeben", None
    elif page_fetch_error:
        return f"Fehler beim Abrufen der Seite (evtl. Bot-Schutz): {page_fetch_error} - alternativ Produktname/-text manuell eintragen.", None
    elif not _looks_like_real_title(scraped_title, url):
        return (
            "Fehler: Seite liefert keinen echten Produkttitel (Bot-Schutz/"
            "Cookie-Wall?) - bitte Produktname/-text manuell eintragen und nochmal pruefen.",
            None,
        )
    else:
        title = scraped_title

    # Fuer Material-/Hersteller-/Farberkennung den VOLLEN Text nutzen (auch bei
    # langen Volltext-Pasten aus dem Webformular), nicht nur den 64-Zeichen-
    # gekuerzten Namen, der als Spoolman-Filamentname vorgesehen ist. Separat
    # abgelegt (kein Spoolman-Feld), damit create_new_filament_from_choice
    # spaeter (Schritt 2, z.B. fuer die Gewichtserkennung) denselben vollen
    # Text nutzen kann statt nur den gekuerzten Namen.
    search_text = name_override or title
    fields["_search_text"] = search_text

    material = _guess_material(search_text)
    vendor_name = _guess_vendor(search_text)
    candidates = _search_external_candidates(vendor_name, material, search_text)[:MAX_SUGGESTIONS]

    candidate_map = {}
    options = []
    for c in candidates:
        label = _candidate_label(c)
        base, n = label, 2
        while label in candidate_map:
            label = f"{base} #{n}"
            n += 1
        candidate_map[label] = c
        options.append(label)
    options.append(GENERIC_OPTION)
    candidate_map[GENERIC_OPTION] = None

    pending = {"url": url, "fields": fields, "candidate_map": candidate_map}

    try:
        ha_set_select_options(SELECT_ENTITY, options)
        ha_select_option(SELECT_ENTITY, options[0])
    except Exception as e:
        log.error(f"Konnte Vorschlagsliste nicht setzen: {e}")

    if candidates:
        status = f"{len(candidates)} Vorschlaege gefunden - auswaehlen und 'Anlegen' klicken"
    else:
        status = (
            "Kein Datenbank-Treffer fuer Hersteller/Material erkannt - "
            "'Generisch anlegen' auswaehlen und 'Anlegen' klicken"
        )
    return status, pending


def _button_pressed(state, entity_id, state_key):
    """True (und Zeitstempel gemerkt) wenn der Button seit dem letzten Mal neu
    gedrueckt wurde. Trigger ist der Klick-Zeitstempel, NICHT eine Werte-
    Aenderung der URL - sonst wuerde ein Retry mit exakt derselben URL fuer
    immer stillschweigend ignoriert."""
    ts = ha_get_state(entity_id).get("state")
    if not ts or ts in ("unknown", "unavailable"):
        return False
    if ts == state.get(state_key):
        return False
    state[state_key] = ts
    return True


def process_once(state):
    try:
        check_helpers_once()
    except requests.RequestException as e:
        log.warning(f"Helfer-Pruefung uebersprungen (HA-Abfrage fehlgeschlagen): {e}")
    if _button_pressed(state, SEARCH_BUTTON_ENTITY, "last_search_press"):
        url = (ha_get_state(URL_ENTITY).get("state") or "").strip()
        try:
            filament_id = max(0, int(float(ha_get_state(ID_ENTITY).get("state") or 0)))
        except (TypeError, ValueError):
            filament_id = 0

        name_override = (ha_get_state(NAME_OVERRIDE_ENTITY).get("state") or "").strip()
        if name_override.lower() in ("unknown", "unavailable"):
            name_override = ""

        if not url or url.lower() in ("unknown", "unavailable"):
            _set_status("Fehler: keine Produkt-URL eingetragen")
        elif filament_id > 0:
            # Fortgeschrittener Pfad: bestehendes Filament direkt anreichern,
            # keine Auswahl noetig (Farbe/Material sind ja schon bekannt).
            result = process_enrich_existing(url, filament_id)
            _reset_after_enrich(result)
        else:
            status, pending = process_search(url, name_override)
            state["pending"] = pending
            _set_status(status)

    if _button_pressed(state, CREATE_BUTTON_ENTITY, "last_create_press"):
        pending = state.get("pending")
        if not pending:
            _set_status("Fehler: bitte zuerst 'Pruefen' klicken")
            return

        selection = ha_get_state(SELECT_ENTITY).get("state")
        candidate_map = pending.get("candidate_map", {})
        if selection not in candidate_map:
            _set_status(f"Fehler: Auswahl '{selection}' nicht mehr gueltig - bitte erneut pruefen")
            return

        try:
            new_filament, source_note = create_new_filament_from_choice(
                pending["fields"], candidate_map[selection]
            )
        except Exception as e:
            _set_status(f"Fehler beim Anlegen des Filaments: {e}")
            return

        state["pending"] = None
        _reset_after_create(
            f"OK: Neues Filament ID={new_filament['id']} angelegt "
            f"('{new_filament.get('name')}') - {source_note}"
        )


# ─────────────────────────────────────────────────────────────────
# WEB-FORMULAR (Volltext-Import, umgeht HAs 255-Zeichen-Limit fuer input_text)
# ─────────────────────────────────────────────────────────────────
_FORM_PAGE = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<title>Neues Filament - Volltext-Import</title>
<style>
  body {{ font-family: sans-serif; max-width: 700px; margin: 1.5em auto; padding: 0 1em;
         background: #1c1c1c; color: #eee; }}
  label {{ display: block; margin-top: 1em; font-weight: bold; }}
  textarea {{ width: 100%; height: 320px; font-family: monospace; box-sizing: border-box;
              background: #2b2b2b; color: #eee; border: 1px solid #555; }}
  input[type=text] {{ width: 100%; padding: 0.4em; box-sizing: border-box;
                       background: #2b2b2b; color: #eee; border: 1px solid #555; }}
  button {{ padding: 0.6em 1.2em; margin-top: 1em; cursor: pointer; }}
  .msg {{ padding: 1em; margin-top: 1em; border-radius: 4px; }}
  .ok {{ background: #1e3d24; border: 1px solid #4caf50; }}
  .err {{ background: #3d1e1e; border: 1px solid #f44336; }}
  a {{ color: #8ab4f8; }}
</style>
</head>
<body>
<h2>Filament per Volltext suchen</h2>
<p>Ganze Produktseite hier reinkopieren (Titel, Beschreibung, technische Daten) -
umgeht Home Assistants 255-Zeichen-Limit fuer Textfelder. Weiter geht's danach im
Home-Assistant-Dashboard "Neues Filament" (Vorschlag auswaehlen, "Anlegen" klicken).</p>
<form method="post">
  <label for="url">Produkt-URL (optional, fuer Artikelnummer/Preis/Kommentar)</label>
  <input type="text" id="url" name="url" value="{url}">
  <label for="text">Produkttext (komplette Produktseite reinkopieren)</label>
  <textarea id="text" name="text" placeholder="Titel, Beschreibung, technische Daten ...">{text}</textarea>
  <button type="submit">Suchen</button>
</form>
{message}
</body>
</html>
"""


class _FilamentFormHandler(BaseHTTPRequestHandler):
    def _send_html(self, body_html):
        body = body_html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _render(self, url="", text="", message=""):
        self._send_html(_FORM_PAGE.format(url=html.escape(url), text=html.escape(text), message=message))

    def do_GET(self):
        self._render()

    def do_POST(self):
        try:
            length = min(int(self.headers.get("Content-Length", 0)), MAX_PASTE_BYTES + 10_000)
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            params = parse_qs(raw)
            url = (params.get("url", [""])[0]).strip()
            text = (params.get("text", [""])[0]).strip()[:MAX_PASTE_BYTES]
        except Exception as e:
            self._render(message=f'<div class="msg err">Fehler beim Lesen des Formulars: {html.escape(str(e))}</div>')
            return

        if not text:
            self._render(url, text, '<div class="msg err">Bitte Produkttext einfuegen.</div>')
            return

        try:
            status, pending = process_search(url, name_override=text)
        except Exception as e:
            log.error(f"Web-Formular: Fehler bei der Suche: {e}", exc_info=True)
            self._render(url, text, f'<div class="msg err">Fehler: {html.escape(str(e))}</div>')
            return

        try:
            state = load_state()
            state["pending"] = pending
            save_state(state)
        except Exception as e:
            log.error(f"Web-Formular: Pending-State konnte nicht gespeichert werden: {e}")

        if pending and url:
            # URL im HA-Dashboard sichtbar machen, solange die Suche noch nicht
            # final "angelegt" wurde - _reset_after_create loescht sie danach
            # automatisch (verhindert, dass eine einmal eingegebene URL fuer
            # immer im Dashboard sichtbar/erkennbar bleibt).
            try:
                ha_set_input_text(URL_ENTITY, url)
            except Exception as e:
                log.warning(f"Web-Formular: URL konnte nicht ins HA-Feld uebertragen werden: {e}")

        css_class = "ok" if pending else "err"
        hint = " Weiter geht's in Home Assistant: Vorschlag auswaehlen, dann \"Anlegen\" klicken." if pending else ""
        self._render(url, text, f'<div class="msg {css_class}">{html.escape(status)}{hint}</div>')

    def log_message(self, format, *args):
        log.info("WebFormular: " + (format % args))


def start_web_server():
    try:
        server = ThreadingHTTPServer(("0.0.0.0", WEB_PORT), _FilamentFormHandler)
        log.info(f"Web-Formular (Volltext-Import) laeuft auf Port {WEB_PORT}")
        server.serve_forever()
    except Exception as e:
        log.error(f"Web-Formular konnte nicht gestartet werden: {e}", exc_info=True)


def main():
    info = _get_git_info()
    log.info("=" * 60)
    log.info("SpoolManSyncBambu - Filament-Enrichment-Job (filament_enrich.py)")
    log.info(f"Version: {info['version']}")
    log.info(f"Commit:  {info['commit']} ({info['timestamp']})")
    log.info(f"HA:        {HA_URL}")
    log.info(f"Spoolman:  {SPOOLMAN_URL}")
    log.info(f"Intervall: {POLL_INTERVAL}s")
    log.info("=" * 60)

    threading.Thread(target=start_web_server, daemon=True).start()

    state = load_state()

    while True:
        try:
            process_once(state)
            save_state(state)
        except requests.exceptions.ConnectionError as e:
            log.error(f"Verbindungsfehler: {e}")
        except requests.exceptions.Timeout:
            log.error("API-Timeout")
        except Exception as e:
            log.error(f"Unerwarteter Fehler: {e}", exc_info=True)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    if not HA_TOKEN:
        log.error("HA_TOKEN nicht gesetzt! Bitte .env konfigurieren.")
        sys.exit(1)
    main()
