"""Tests fuer Issue #50 (bambu_cloud.py): Platzhalter-Namen im Druckverlauf werden
durch den Modelltitel aus der Bambu Cloud ersetzt. Ohne Netzwerk - der Cloud-Abruf
wird durch eine Fake-Funktion ersetzt. Prueft die Zuordnung, den kompletten Lauf
gegen eine echte (temporaere) SQLite-Historie und die Seite /cloud-token per
echtem HTTP-Request."""
import http.client
import os
import stat
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

tmp = tempfile.mkdtemp()
TOKEN_FILE = os.path.join(tmp, "cloud_token")
PORT = 18174
GOOD_TOKEN = "AQ" + "x" * 60
os.environ.update({
    "BAMBU_CLOUD_TOKEN_FILE": TOKEN_FILE,
    "CONSUMPTION_STATE_FILE": os.path.join(tmp, "state.json"),
    "CONSUMPTION_LOG_FILE": os.path.join(tmp, "c.log"),
    "CONSUMPTION_HISTORY_FILE": os.path.join(tmp, "h.json"),
    "CONSUMPTION_HISTORY_DB": os.path.join(tmp, "h.db"),
    "CONSUMPTION_HISTORY_IMAGES_DIR": os.path.join(tmp, "img"),
    "WARTUNG_BACKUP_FILE": os.path.join(tmp, "w.json"),
    "CONSUMPTION_OVERRIDES_FILE": os.path.join(tmp, "o.json"),
    "CONSUMPTION_HISTORY_WEB_PORT": str(PORT),
})

import bambu_cloud as bc
import consumption as c

ok = True


def check(label, cond, detail=""):
    global ok
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  → {detail}" if detail and not cond else ""))
    ok = ok and cond


NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.isoformat()


def record(rec_id, name, hours_ago, status="finish", **extra):
    start = NOW - timedelta(hours=hours_ago)
    return {"id": rec_id, "gcode": "/data/Metadata/plate_1.gcode", "name": name, "name_overridden": False,
            "start": iso(start), "end": iso(start + timedelta(hours=1)), "duration_min": 60,
            "status": status, "colors": [], **extra}


def task(hours_ago, title, seconds_off=0):
    start = NOW - timedelta(hours=hours_ago) + timedelta(seconds=seconds_off)
    return {"startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "designTitle": title}


# --- Platzhalter-Erkennung ---------------------------------------------------
for name, expected in [
    ("/data/Metadata/plate_1.gcode", True),
    ("0.2mm layer, 2 walls, 15% infill", True),
    ("Tight hole - 0.2mm layer, 2 walls, 15% infill", True),
    ("Sturdy_Modular_Filament_Spool_Rack", True),
    ("Brotkorb, 1 Platte, 0.2mm Schicht", True),
    ("?", True), ("", True), (None, True),
    ("Moderne Lamellenlaterne", False),
    ("Simple Cabinet Shelf", False),
]:
    check(f"is_placeholder({name!r}) == {expected}", bc.is_placeholder(name) is expected)

# --- Zuordnung ---------------------------------------------------------------
check("Treffer innerhalb der Toleranz", bc.find_title([task(2, "Modell A", 90)], iso(NOW - timedelta(hours=2))) == "Modell A")
check("Kein Treffer ausserhalb der Toleranz", bc.find_title([task(2, "Modell A", 600)], iso(NOW - timedelta(hours=2))) is None)
check("Mehrdeutig (zwei Titel im Fenster) -> None",
      bc.find_title([task(2, "A", 30), task(2, "B", 60)], iso(NOW - timedelta(hours=2))) is None)
check("Gleicher Titel doppelt im Fenster ist eindeutig",
      bc.find_title([task(2, "A", 30), task(2, "A", 60)], iso(NOW - timedelta(hours=2))) == "A")
check("Cloud-Job ohne Titel -> None", bc.find_title([task(2, "  ")], iso(NOW - timedelta(hours=2))) is None)
check("Kaputter Zeitstempel -> None", bc.find_title([{"startTime": "kaputt", "designTitle": "A"}], iso(NOW)) is None)

history = {"records": [
    record(1, "/data/Metadata/plate_1.gcode", 2),
    record(2, "Moderne Lamellenlaterne", 3),                      # kein Platzhalter
    record(3, "0.2mm layer, 2 walls", 4, name_overridden=True),   # von Hand benannt
    record(4, "/data/Metadata/plate_1.gcode", 5, cloud_name_applied=True),
    record(5, "/data/Metadata/plate_1.gcode", 24 * 30),           # zu alt
    record(6, "/data/Metadata/plate_1.gcode", 1, status="started"),  # laeuft noch
    record(7, "/data/Metadata/plate_1.gcode", 8),                 # kein Cloud-Treffer
]}
tasks = [task(h, f"Cloud {h}") for h in (1, 2, 3, 4, 5, 24 * 30)]
changes = bc.apply_cloud_names(history, tasks, NOW)
by_id = {r["id"]: r for r in history["records"]}
check("Nur Eintrag 1 geaendert", [ch[0] for ch in changes] == [1], str(changes))
check("Platzhalter ersetzt + Flag gesetzt", by_id[1]["name"] == "Cloud 2" and by_id[1]["cloud_name_applied"] is True)
check("Eigener Name bleibt", by_id[2]["name"] == "Moderne Lamellenlaterne")
check("Von Hand umbenannter Eintrag bleibt", by_id[3]["name"] == "0.2mm layer, 2 walls")
check("Bereits ersetzter Eintrag bleibt", by_id[4]["name"] == "/data/Metadata/plate_1.gcode")
check("Zu alter Eintrag bleibt", by_id[5]["name"] == "/data/Metadata/plate_1.gcode")
check("Laufender Druck bleibt", by_id[6]["name"] == "/data/Metadata/plate_1.gcode")
check("Ohne Cloud-Treffer bleibt der Name", by_id[7]["name"] == "/data/Metadata/plate_1.gcode")

# --- Token-Speicher ----------------------------------------------------------
check("Ohne Datei: kein Token", bc.load_token() is None)
check("clean_token entfernt 'Bearer ' und Leerraum", bc.clean_token("  Bearer " + GOOD_TOKEN + " \n") == GOOD_TOKEN)
check("Zu kurzes Token ungueltig", not bc.is_valid_token("abc"))
check("Token mit Leerzeichen/Zeilenumbruch ungueltig", not bc.is_valid_token(GOOD_TOKEN + "\nX-Evil: 1"))
bc.save_token(GOOD_TOKEN)
check("Token gespeichert und lesbar", bc.load_token() == GOOD_TOKEN)
check("Token-Datei nur fuer Besitzer (0600)", stat.S_IMODE(os.stat(TOKEN_FILE).st_mode) == 0o600)
bc.delete_token()
check("Token geloescht", bc.load_token() is None and not os.path.exists(TOKEN_FILE))

# --- Cloud-Abruf: Fehlerfaelle -----------------------------------------------


class FakeResponse:
    def __init__(self, status, payload=None, json_error=False):
        self.status_code, self._payload, self._json_error = status, payload, json_error

    def json(self):
        if self._json_error:
            raise ValueError("kein JSON")
        return self._payload


def raises(exc, fn):
    try:
        fn()
    except exc as e:
        return e
    except Exception as e:  # falscher Typ
        return None
    return None


check("HTTP 401 -> CloudAuthError", raises(bc.CloudAuthError, lambda: bc.fetch_tasks("t", lambda *a, **k: FakeResponse(401))) is not None)
e = raises(bc.CloudError, lambda: bc.fetch_tasks("t", lambda *a, **k: FakeResponse(403)))
check("HTTP 403 -> CloudError (nicht 'abgelaufen')", e is not None and not isinstance(e, bc.CloudAuthError))
check("Kein JSON (Cloudflare-Seite) -> CloudError", raises(bc.CloudError, lambda: bc.fetch_tasks("t", lambda *a, **k: FakeResponse(200, json_error=True))) is not None)
check("Antwort ohne 'hits' -> CloudError", raises(bc.CloudError, lambda: bc.fetch_tasks("t", lambda *a, **k: FakeResponse(200, {"x": 1}))) is not None)
check("Gueltige Antwort -> Liste", bc.fetch_tasks("t", lambda *a, **k: FakeResponse(200, {"hits": [1]})) == [1])


def timeout_get(*a, **k):
    raise bc.requests.Timeout("Token=" + GOOD_TOKEN)


e = raises(bc.CloudError, lambda: bc.fetch_tasks(GOOD_TOKEN, timeout_get))
check("Timeout -> CloudError ohne Token im Text", e is not None and GOOD_TOKEN not in str(e))

# --- Kompletter Lauf gegen echte SQLite-Historie -----------------------------
c.init_history_storage()
calls = []


def fake_get_factory(response):
    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(headers["Authorization"])
        return response
    return fake_get


def seed(records):
    with c._history_lock:
        h = c.load_history()
        h["records"] = records
        h["next_id"] = max(r["id"] for r in records) + 1
        c.save_history(h)


def names():
    with c._history_lock:
        return {r["id"]: r["name"] for r in c.load_history()["records"]}


def run(response):
    return bc.run_once(c.load_history, c.save_history, c._history_lock, now=NOW, http_get=fake_get_factory(response))


seed([record(1, "/data/Metadata/plate_1.gcode", 2), record(2, "Eigener Name", 3)])
check("Ohne Token: Lauf liefert None, kein Netzwerkaufruf", run(FakeResponse(200, {"hits": []})) is None and calls == [])

bc.save_token(GOOD_TOKEN)
seed([record(2, "Eigener Name", 3)])
check("Ohne Kandidaten: kein Netzwerkaufruf", run(FakeResponse(200, {"hits": []})) == 0 and calls == [])

seed([record(1, "/data/Metadata/plate_1.gcode", 2), record(2, "Eigener Name", 3)])
check("Lauf ersetzt 1 Namen", run(FakeResponse(200, {"hits": [task(2, "Cloud Titel")]})) == 1)
check("Token als Bearer gesendet", calls == ["Bearer " + GOOD_TOKEN])
check("Name in DB ersetzt, anderer unveraendert", names() == {1: "Cloud Titel", 2: "Eigener Name"}, str(names()))
with c._history_lock:
    persisted = {r["id"]: r for r in c.load_history()["records"]}
check("Flag cloud_name_applied bleibt in der DB erhalten", persisted[1].get("cloud_name_applied") is True)
check("Status 'ok' nach erfolgreichem Lauf", bc.get_status()["state"] == bc.STATE_OK)

calls.clear()
check("Zweiter Lauf: nichts mehr zu tun, kein Netzwerkaufruf", run(FakeResponse(200, {"hits": []})) == 0 and calls == [])

# Regression: der laufende Verbrauchs-Job darf den Cloud-Namen nicht zurueckschreiben
job = {"history_id": 1, "gcode": "/data/Metadata/plate_1.gcode", "task_name": "/data/Metadata/plate_1.gcode",
       "start": persisted[1]["start"], "colors_used": {}}
c._write_history_record(job, "finish")
check("_write_history_record ueberschreibt den Cloud-Namen nicht", names()[1] == "Cloud Titel", str(names()))

seed([record(1, "/data/Metadata/plate_1.gcode", 2)])
check("Token abgelehnt: 0 Aenderungen", run(FakeResponse(401)) == 0 and names()[1] == "/data/Metadata/plate_1.gcode")
check("Status 'expired' bei 401", bc.get_status()["state"] == bc.STATE_EXPIRED)
check("Cloud-Sperre (403): 0 Aenderungen, Status 'error'",
      run(FakeResponse(403)) == 0 and bc.get_status()["state"] == bc.STATE_ERROR)
check("Fehlerfall: Token bleibt gespeichert", bc.load_token() == GOOD_TOKEN)

# --- Webseite /cloud-token (echter HTTP-Server) ------------------------------
server = ThreadingHTTPServer(("127.0.0.1", PORT), c._HistoryHandler)
threading.Thread(target=server.serve_forever, daemon=True).start()


def request(method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    headers = {"Content-Type": "application/x-www-form-urlencoded"} if body is not None else {}
    conn.request(method, path, body=urlencode(body) if body is not None else None, headers=headers)
    resp = conn.getresponse()
    data = resp.read().decode("utf-8")
    conn.close()
    return resp.status, resp.getheader("Location"), data


def fake_fetch_ok(token, http_get=None):
    return []


bc.delete_token()
status, _, page = request("GET", "/cloud-token")
check("GET /cloud-token: 200, Formular, 'Kein Token'", status == 200 and 'name="token"' in page and "Kein Token" in page)
check("Seite hat lang=de und Label zum Feld", '<html lang="de">' in page and 'for="token"' in page)
check("Kopfzeile des Verlaufs verlinkt auf /cloud-token", 'href="/cloud-token"' in request("GET", "/")[2])

status, _, page = request("POST", "/cloud-token", {"token": "zu kurz"})
check("POST mit Unsinn -> 400, nichts gespeichert", status == 400 and bc.load_token() is None)

orig_fetch = bc.fetch_tasks
bc.fetch_tasks = lambda token, http_get=None: (_ for _ in ()).throw(bc.CloudAuthError("Token abgelehnt (HTTP 401)"))
status, _, page = request("POST", "/cloud-token", {"token": GOOD_TOKEN})
check("POST mit abgelehntem Token -> 400, nichts gespeichert", status == 400 and bc.load_token() is None)
check("Abgelehntes Token steht nicht in der Antwort", GOOD_TOKEN not in page)

bc.fetch_tasks = lambda token, http_get=None: (_ for _ in ()).throw(bc.CloudError("Cloud nicht erreichbar (Timeout)"))
status, _, page = request("POST", "/cloud-token", {"token": GOOD_TOKEN})
check("POST bei Cloud-Fehler -> 502, nichts gespeichert", status == 502 and bc.load_token() is None)

bc.fetch_tasks = fake_fetch_ok
status, location, _ = request("POST", "/cloud-token", {"token": "Bearer " + GOOD_TOKEN})
check("POST mit gueltigem Token -> 303 auf /cloud-token", status == 303 and location == "/cloud-token?msg=saved")
check("Token gespeichert (ohne 'Bearer ')", bc.load_token() == GOOD_TOKEN)
status, _, page = request("GET", "/cloud-token?msg=saved")
check("Seite zeigt Meldung, aber nie das Token", status == 200 and "gespeichert" in page and GOOD_TOKEN not in page)
check("Meldung kommt nur aus fester Liste (keine Reflexion)", "<script>" not in request("GET", "/cloud-token?msg=<script>")[2])

status, _, _ = request("POST", "/cloud-token", {"token": "x" * 5000})
check("Zu grosser Body -> 413", status == 413)

status, location, _ = request("POST", "/cloud-token/delete", {})
check("Loeschen -> 303, Token weg", status == 303 and bc.load_token() is None)

bc.fetch_tasks = orig_fetch
server.shutdown()

with open(os.environ["CONSUMPTION_LOG_FILE"], encoding="utf-8") as f:
    log_text = f.read()
check("Token taucht nirgends im Log auf", GOOD_TOKEN not in log_text)

print("\nOK" if ok else "\nFEHLER")
raise SystemExit(0 if ok else 1)
