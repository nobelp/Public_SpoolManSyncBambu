"""Tests fuer die zur Laufzeit umschaltbare Log-Stufe (v4.2.2).

- Auswahl oben rechts im Druckverlauf (POST /log-level), wirkt sofort, ohne
  Container-Neustart, bleibt ueber Neustarts erhalten (data/log_level.json)
- "Aus" bedeutet nur Fehler (nie komplett stumm)
- Die Warnzeile "Tray-Gewichte fehlen ..." (bei reinen Extern-Drucken der
  Normalfall, jede Minute) ist nur noch auf Debug-Stufe sichtbar"""
import http.client
import json
import logging
import threading
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

import booking_harness as h
from booking_harness import c, check

PORT = 18175
root = logging.getLogger()


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def messages(self, level=None):
        return [r.getMessage() for r in self.records if level is None or r.levelno == level]


capture = Capture()
root.addHandler(capture)

print("1) Standard ohne gespeicherte Datei: Normal (INFO)")
check("Start-Stufe INFO", root.level == logging.INFO and c.current_log_level() == "normal", str(root.level))
check("Datei existiert noch nicht", not __import__("os").path.exists(c.LOG_LEVEL_FILE))

print("2) Umschalten wirkt sofort und wird gespeichert")
c.set_log_level("debug")
check("Root-Logger auf DEBUG", root.level == logging.DEBUG and c.current_log_level() == "debug")
check("Stufe in Datei gespeichert", json.load(open(c.LOG_LEVEL_FILE))["level"] == "debug")
check("_load_log_level liest sie zurueck (Neustart-Simulation)", c._load_log_level() == "debug")

print("3) 'Aus' = nur Fehler; die Umschaltung selbst bleibt im Log sichtbar")
capture.records.clear()
c.set_log_level("off")
check("Root-Logger auf ERROR", root.level == logging.ERROR and c.current_log_level() == "off")
check("Umschaltung wurde trotz 'nur Fehler' protokolliert",
      any("Log-Stufe geaendert" in m for m in capture.messages(logging.ERROR)), str(capture.messages()))
capture.records.clear()
c.log.warning("Test-Warnung")
c.log.error("Test-Fehler")
check("Warnungen sind unterdrueckt, Fehler erscheinen",
      "Test-Warnung" not in capture.messages() and "Test-Fehler" in capture.messages(), str(capture.messages()))

print("4) Ungueltige/defekte Eingaben")
try:
    c.set_log_level("verbose")
    check("unbekannte Stufe wird abgelehnt", False)
except ValueError:
    check("unbekannte Stufe wird abgelehnt (ValueError)", True)
check("Stufe bleibt unveraendert", c.current_log_level() == "off")
open(c.LOG_LEVEL_FILE, "w").write("{kaputt")
check("defekte Datei -> Standardstufe", c._load_log_level() == "normal")
open(c.LOG_LEVEL_FILE, "w").write(json.dumps({"level": "hacker"}))
check("unbekannter Wert in der Datei -> Standardstufe", c._load_log_level() == "normal")
c.set_log_level("normal")
good_path = c.LOG_LEVEL_FILE
c.LOG_LEVEL_FILE = "/nicht/vorhanden/log_level.json"
capture.records.clear()
c.set_log_level("debug")
check("Speichern schlaegt fehl: Stufe gilt trotzdem sofort", c.current_log_level() == "debug")
check("... und der Fehler steht im Log", any("nicht gespeichert werden" in m for m in capture.messages(logging.ERROR)),
      str(capture.messages()))
c.LOG_LEVEL_FILE = good_path
c.set_log_level("normal")

print("5) Seite und POST per echtem HTTP")
sm = h.FakeSpoolman(h.make_spool(26))
sm.install()
server = ThreadingHTTPServer(("127.0.0.1", PORT), c._HistoryHandler)
threading.Thread(target=server.serve_forever, daemon=True).start()


def request(method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    conn.request(method, path, body, {"Content-Type": "application/x-www-form-urlencoded"} if body else {})
    resp = conn.getresponse()
    return resp.status, resp.read().decode("utf-8", errors="replace")


try:
    status, page = request("GET", "/")
    check("Seite laedt (200)", status == 200, str(status))
    check("Auswahl mit Label und drei Optionen vorhanden",
          'for="log-level"' in page and page.count('<option value="off"') == 1
          and 'value="normal" selected' in page and 'value="debug"' in page)
    status, _ = request("POST", "/log-level", urlencode({"level": "debug"}))
    check("POST debug -> 303", status == 303, str(status))
    check("Stufe sofort wirksam und gespeichert", c.current_log_level() == "debug" and c._load_log_level() == "debug")
    check("Seite zeigt jetzt Debug vorausgewaehlt", 'value="debug" selected' in request("GET", "/")[1])
    status, _ = request("POST", "/log-level", urlencode({"level": "kaputt"}))
    check("ungueltige Stufe -> 400 und unveraendert", status == 400 and c.current_log_level() == "debug", str(status))
    status, _ = request("POST", "/log-level", "")
    check("fehlender Parameter -> 400", status == 400, str(status))
    status, _ = request("POST", "/log-level", urlencode({"level": "normal"}))
    check("zurueck auf Normal", status == 303 and c.current_log_level() == "normal")
finally:
    server.shutdown()

print("6) Warnzeile 'Tray-Gewichte fehlen' nur noch auf Debug-Stufe")


def run_external_print(start):
    sm = h.FakeSpoolman(h.make_spool(26))
    sm.install()
    state, printer = {"user_spool_overrides": {"External": 26}}, h.Printer(start)
    capture.records.clear()
    for progress in (10, 30, 60):
        h.poll(state, printer.states("running", progress, 43.25, external=True))
    return capture.messages(logging.WARNING), capture.messages(logging.DEBUG)


c.set_log_level("normal")
warnings, _ = run_external_print("2026-10-20T10:00:00+00:00")
check("Normal: keine Warnzeile", not any("Tray-Gewichte fehlen" in m for m in warnings), str(warnings))
c.set_log_level("debug")
_, debugs = run_external_print("2026-10-21T10:00:00+00:00")
check("Debug: Zeile weiterhin vorhanden (als DEBUG)", any("Tray-Gewichte fehlen" in m for m in debugs), str(debugs[:3]))
c.set_log_level("normal")

h.finish()
