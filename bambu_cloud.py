#!/usr/bin/env python3
"""
DIV_SpoolManSyncBambu - bambu_cloud.py
Ersetzt Platzhalter-Namen im Druckverlauf durch den Modelltitel aus der
Bambu Cloud (GitHub Issue #50).

WARUM: Der Drucker meldet per MQTT nur den Namen der gesendeten Aufgabe
(subtask_name). Der ist oft ein G-Code-Pfad ("/data/Metadata/plate_1.gcode")
oder ein Slicer-Profil ("0.2mm layer, 2 walls, 15% infill"). Bambu Studio und
MakerWorld zeigen dagegen den Modelltitel (designTitle).

Endpunkt (inoffiziell, https://github.com/coelacant1/Bambu-Lab-Cloud-API):
    GET https://api.bambulab.com/v1/user-service/my/tasks   (Bearer-Token)
Das Token ist der Cookie "token" von makerworld.com und laeuft nach rund
90 Tagen ab. Es wird ueber die Webseite /cloud-token eingetragen und nie
angezeigt, geloggt oder ueber die Seite wieder ausgegeben.

Grundsaetze:
  - Nur Platzhalter-Namen werden ersetzt. Von Hand umbenannte Eintraege
    (name_overridden) und bereits ersetzte (cloud_name_applied) bleiben.
  - Zuordnung ueber die Startzeit (+-3 min) und nur bei eindeutigem Treffer.
  - Ohne Token, bei abgelaufenem Token oder Cloud-Fehler bleibt der lokale
    Name unveraendert - es geht nie etwas kaputt, es wird nur nichts ersetzt.
  - Die Cloud wird nur gefragt, wenn es Eintraege zum Ersetzen gibt.
Netzwerk (http_get) und Zeit (now) werden uebergeben - ohne Netzwerk testbar.
"""

import html
import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger("consumption")

TASKS_URL = "https://api.bambulab.com/v1/user-service/my/tasks"
TOKEN_FILE = os.environ.get("BAMBU_CLOUD_TOKEN_FILE", "/app/data/bambu_cloud_token")
REQUEST_TIMEOUT_S = 15
TASK_LIMIT = 100
MATCH_TOLERANCE = timedelta(minutes=3)
MAX_AGE = timedelta(days=int(os.environ.get("BAMBU_CLOUD_NAMES_MAX_AGE_DAYS", "14")))
RUN_INTERVAL_S = int(os.environ.get("BAMBU_CLOUD_NAMES_INTERVAL_S", "900"))
FIRST_RUN_DELAY_S = 120

TERMINAL_STATUSES = ("finish", "failed", "cancelled")

# Alles, was nach G-Code-Pfad, Slicer-Profil, Dateiname oder "unbekannt" aussieht.
PLACEHOLDER_PATTERN = re.compile(
    r"^/"                                  # G-Code-Pfad
    r"|\d(\.\d+)?\s*mm\s*(layer|nozzle|Schicht)"  # Slicer-Profil
    r"|^\S+_\S+$"                          # Dateiname_mit_Unterstrichen
    r"|^\?$|^$",                           # Fallback "?" / leer
    re.IGNORECASE,
)
# Cookie-Wert: nur URL-/Base64-Zeichen. Schuetzt vor Header-Injection und Tippfehlern.
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_\-.=+/]{20,1000}")

STATE_NO_TOKEN = "no_token"
STATE_UNCHECKED = "unchecked"
STATE_OK = "ok"
STATE_EXPIRED = "expired"
STATE_ERROR = "error"

_token_lock = threading.Lock()
_status_lock = threading.Lock()
_status = {"state": STATE_UNCHECKED, "checked_at": None, "detail": ""}


class CloudError(Exception):
    """Cloud nicht nutzbar (Netzwerk, Sperre, unerwartete Antwort)."""


class CloudAuthError(CloudError):
    """Token abgelehnt (abgelaufen oder ungueltig)."""


# --- Token-Speicher ---------------------------------------------------------

def is_valid_token(token):
    return bool(TOKEN_PATTERN.fullmatch(token or ""))


def clean_token(raw):
    """Entfernt Leerraum und ein versehentlich mitkopiertes 'Bearer '."""
    token = (raw or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token


def load_token():
    with _token_lock:
        try:
            with open(TOKEN_FILE, encoding="utf-8") as f:
                token = f.read().strip()
        except FileNotFoundError:
            return None
    return token if is_valid_token(token) else None


def save_token(token):
    """Atomar und nur fuer den Container-Benutzer lesbar (0600)."""
    with _token_lock:
        tmp = TOKEN_FILE + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token)
        os.replace(tmp, TOKEN_FILE)
    _set_status(STATE_UNCHECKED, "")


def delete_token():
    with _token_lock:
        try:
            os.remove(TOKEN_FILE)
        except FileNotFoundError:
            pass
    _set_status(STATE_NO_TOKEN, "")


def token_saved_at():
    try:
        return datetime.fromtimestamp(os.path.getmtime(TOKEN_FILE), tz=timezone.utc)
    except OSError:
        return None


# --- Status -----------------------------------------------------------------

def _set_status(state, detail):
    with _status_lock:
        _status.update(state=state, detail=detail,
                       checked_at=datetime.now(timezone.utc) if state != STATE_UNCHECKED else None)


def get_status():
    with _status_lock:
        status = dict(_status)
    if load_token() is None:
        status["state"] = STATE_NO_TOKEN
    return status


# --- Cloud-Abruf ------------------------------------------------------------

def fetch_tasks(token, http_get=None):
    """Liefert die letzten Cloud-Druckjobs (neueste zuerst) als Liste."""
    http_get = http_get or requests.get
    try:
        resp = http_get(TASKS_URL, params={"limit": TASK_LIMIT},
                        headers={"Authorization": f"Bearer {token}"}, timeout=REQUEST_TIMEOUT_S)
    except requests.RequestException as e:
        raise CloudError(f"Cloud nicht erreichbar ({type(e).__name__})") from None
    if resp.status_code == 401:
        raise CloudAuthError("Token abgelehnt (HTTP 401)")
    if resp.status_code != 200:
        raise CloudError(f"Cloud antwortet mit HTTP {resp.status_code}")
    try:
        hits = resp.json().get("hits")
    except (ValueError, AttributeError):
        raise CloudError("Antwort ist kein JSON (evtl. Sperre durch Cloudflare)") from None
    if not isinstance(hits, list):
        raise CloudError("Antwort ohne Druckjob-Liste (API geaendert?)")
    return hits


def check_token(token, http_get=None):
    """Prueft ein Token mit einem echten Abruf. Wirft CloudError/CloudAuthError."""
    fetch_tasks(token, http_get)


# --- Zuordnung --------------------------------------------------------------

def is_placeholder(name):
    return bool(PLACEHOLDER_PATTERN.search((name or "").strip()))


def _parse_utc(iso):
    dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def find_title(tasks, start_iso):
    """Modelltitel des Cloud-Jobs, der zur Startzeit passt - sonst None.
    Mehrere Jobs mit verschiedenen Titeln im Zeitfenster gelten als nicht eindeutig."""
    try:
        start = _parse_utc(start_iso)
    except (TypeError, ValueError):
        return None
    titles = set()
    for task in tasks:
        try:
            task_start = _parse_utc(task.get("startTime"))
        except (TypeError, ValueError):
            continue
        if abs(task_start - start) <= MATCH_TOLERANCE:
            titles.add((task.get("designTitle") or "").strip())
    titles.discard("")
    return titles.pop() if len(titles) == 1 else None


def needs_cloud_name(record, now):
    if record.get("status") not in TERMINAL_STATUSES:
        return False
    if record.get("name_overridden") or record.get("cloud_name_applied"):
        return False
    if not is_placeholder(record.get("name")):
        return False
    try:
        return now - _parse_utc(record.get("start")) <= MAX_AGE
    except (TypeError, ValueError):
        return False


def apply_cloud_names(history, tasks, now):
    """Ersetzt Platzhalter-Namen in history['records'] (in place).
    Gibt [(id, alter_name, neuer_name)] zurueck."""
    changes = []
    for record in history.get("records", []):
        if not needs_cloud_name(record, now):
            continue
        title = find_title(tasks, record.get("start"))
        if not title:
            continue
        changes.append((record.get("id"), record.get("name"), title))
        record["name"] = title
        record["cloud_name_applied"] = True
    return changes


# --- Ein Lauf ---------------------------------------------------------------

def run_once(load_history, save_history, history_lock, now=None, http_get=None):
    """Ein Durchlauf. Gibt die Zahl ersetzter Namen zurueck, None ohne Token.
    Fehler der Cloud werden als Status festgehalten, nicht geworfen."""
    token = load_token()
    if token is None:
        _set_status(STATE_NO_TOKEN, "")
        return None
    now = now or datetime.now(timezone.utc)

    with history_lock:
        pending = any(needs_cloud_name(r, now) for r in load_history().get("records", []))
    if not pending:
        return 0

    try:
        tasks = fetch_tasks(token, http_get)
    except CloudAuthError as e:
        _set_status(STATE_EXPIRED, str(e))
        log.warning(f"Cloud-Namen: {e} - Token unter /cloud-token neu eintragen")
        return 0
    except CloudError as e:
        _set_status(STATE_ERROR, str(e))
        log.warning(f"Cloud-Namen: {e}")
        return 0
    _set_status(STATE_OK, "")

    # Verlauf erst NACH dem Abruf (Netzwerk) neu laden: so geht kein paralleles
    # Schreiben des Verbrauchs-Jobs verloren.
    with history_lock:
        history = load_history()
        changes = apply_cloud_names(history, tasks, now)
        if changes:
            save_history(history)
    for record_id, old, new in changes:
        log.info(f"Cloud-Name: #{record_id} '{old}' -> '{new}'")
    return len(changes)


# --- Einstellungsseite /cloud-token -----------------------------------------

_STATE_TEXT = {
    STATE_NO_TOKEN: "Kein Token hinterlegt - Namen werden nicht aus der Cloud geholt.",
    STATE_UNCHECKED: "Token hinterlegt, noch nicht geprueft.",
    STATE_OK: "Token funktioniert.",
    STATE_EXPIRED: "Token abgelehnt - vermutlich abgelaufen. Bitte neu eintragen.",
    STATE_ERROR: "Cloud derzeit nicht nutzbar.",
}
PAGE_MESSAGES = {
    "saved": "Token geprueft und gespeichert.",
    "deleted": "Token geloescht.",
}


def render_page(message="", error=""):
    status = get_status()
    saved_at = token_saved_at()
    checked_at = status["checked_at"]
    rows = [f"<li>Status: <strong>{html.escape(_STATE_TEXT[status['state']])}</strong></li>"]
    if status["detail"]:
        rows.append(f"<li>Detail: {html.escape(status['detail'])}</li>")
    if saved_at and status["state"] != STATE_NO_TOKEN:
        rows.append(f"<li>Token gespeichert am: {saved_at.astimezone().strftime('%Y-%m-%d %H:%M')}</li>")
    if checked_at:
        rows.append(f"<li>Letzte Pruefung: {checked_at.astimezone().strftime('%Y-%m-%d %H:%M')}</li>")
    banner = ""
    if error:
        banner = f'<p class="msg msg-error" role="alert">Fehler: {html.escape(error)}</p>'
    elif message:
        banner = f'<p class="msg msg-ok" role="status">{html.escape(message)}</p>'
    delete_form = ""
    if status["state"] != STATE_NO_TOKEN:
        delete_form = ('<form method="post" action="/cloud-token/delete">'
                       '<button type="submit">Token loeschen</button></form>')
    return f"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cloud-Token - Druckverlauf</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: sans-serif; max-width: 42rem; margin: 0 auto; padding: 1.2em; line-height: 1.5; }}
  label {{ display: block; font-weight: bold; margin: 1em 0 0.3em; }}
  input[type=password] {{ width: 100%; padding: 0.5em; font-size: 1em; }}
  button {{ padding: 0.5em 1em; font-size: 1em; margin: 0.8em 0.5em 0 0; cursor: pointer; }}
  :focus-visible {{ outline: 3px solid #0ea5e9; outline-offset: 2px; }}
  .msg {{ padding: 0.6em 0.8em; border: 2px solid; border-radius: 4px; }}
  .msg-ok {{ border-color: #16a34a; }}
  .msg-error {{ border-color: #dc2626; }}
</style>
</head>
<body>
<main>
<p><a href="/">&larr; Zurueck zum Druckverlauf</a></p>
<h1>Bambu-Cloud-Token</h1>
{banner}
<ul>{''.join(rows)}</ul>
<p>Mit dem Token holt der Dienst nach Druckende den Modelltitel aus der Bambu Cloud
und ersetzt damit Platzhalter-Namen (G-Code-Pfad, Slicer-Profil). Das Token wird
nie angezeigt.</p>
<form method="post" action="/cloud-token">
  <label for="token">Neues Token (Cookie <code>token</code> von makerworld.com)</label>
  <input id="token" name="token" type="password" autocomplete="off" required
         aria-describedby="token-hint" minlength="20" maxlength="1000">
  <p id="token-hint">Anleitung: siehe README, Abschnitt &quot;Bambu-Cloud-Namen&quot;.
  Das Token ist rund 90 Tage gueltig.</p>
  <button type="submit">Pruefen und speichern</button>
</form>
{delete_form}
</main>
</body>
</html>
"""
