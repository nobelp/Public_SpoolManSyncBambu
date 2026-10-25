"""Issue #6 (Teil A): Pruefung der HA-Hilfsentities (input_*), die die Dienste benutzen,
aber nicht selbst anlegen. Kein Netzwerk."""
import logging
import os
import tempfile

tmp = tempfile.mkdtemp()
os.environ.update({
    "CONSUMPTION_STATE_FILE": os.path.join(tmp, "c.json"),
    "CONSUMPTION_LOG_FILE": os.path.join(tmp, "c.log"),
    "CONSUMPTION_HISTORY_FILE": os.path.join(tmp, "h.json"),
    "CONSUMPTION_HISTORY_DB": os.path.join(tmp, "h.db"),
    "CONSUMPTION_HISTORY_IMAGES_DIR": os.path.join(tmp, "img"),
    "WARTUNG_BACKUP_FILE": os.path.join(tmp, "w.json"),
    "CONSUMPTION_OVERRIDES_FILE": os.path.join(tmp, "o.json"),
    "FILAMENT_ENRICH_STATE_FILE": os.path.join(tmp, "e.json"),
    "FILAMENT_ENRICH_LOG_FILE": os.path.join(tmp, "e.log"),
})

import consumption as c  # noqa: E402
import entity_config as ec  # noqa: E402
import filament_enrich as fe  # noqa: E402

ok = True


def check(label, cond):
    global ok
    print(("  PASS  " if cond else "  FAIL  ") + label)
    ok = ok and bool(cond)


class Sammler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append((record.levelname, record.getMessage()))


TRAYS = {"AMS 1 Tray 1": "sensor.x_ams_1_slot_1", "AMS 1 Tray 2": "sensor.x_ams_1_slot_2",
         "External": "sensor.x_externalspool_externe_spule"}

print("1) required_helpers: vollstaendige Liste, ohne Dubletten")
helpers = c.required_helpers(TRAYS)
check("25 Wartungs-Datumshelfer", sum(h.startswith("input_datetime.p2s_") for h in helpers) == 25)
check("10 Stunden-Baselines", sum(h.startswith("input_number.p2s_rt_") for h in helpers) == 10)
check("3 Versions-Texte", {c.VERSION_TEXT_ENTITY, c.COMMIT_TEXT_ENTITY, c.RELEASE_TEXT_ENTITY} <= set(helpers))
check("Dropdown extern + je vorhandenem Slot (nur Slots, die es gibt)",
      {"input_select.p2s_slot_external", "input_select.p2s_slot_ams_1_tray_1",
       "input_select.p2s_slot_ams_1_tray_2"} <= set(helpers)
      and "input_select.p2s_slot_ams_1_tray_3" not in helpers)
check("keine Dubletten", len(helpers) == len(set(helpers)))
check("ohne Slots: nur das externe Dropdown",
      [h for h in c.required_helpers({}) if h.startswith("input_select.")] == ["input_select.p2s_slot_external"])

print("2) Meldung: einmal je Aenderung, mit allen fehlenden Namen und Verweis auf HELPERS.md")
sammler = Sammler()
c.log.addHandler(sammler)
c.log.setLevel(logging.DEBUG)
c._helper_reporter = ec.ChangeReporter()

c.report_missing_helpers({}, TRAYS)
level, msg = sammler.records[-1]
check("WARNING (Buchung haengt nicht davon ab)", level == "WARNING")
check("nennt fehlenden Helfer", "input_select.p2s_slot_ams_1_tray_2" in msg and "input_datetime.p2s_gereinigt_hotend" in msg)
check("gruppiert nach Typ", "input_datetime (25)" in msg and "input_number (10)" in msg)
check("verweist auf HELPERS.md", "HELPERS.md" in msg)
anzahl = len(sammler.records)
c.report_missing_helpers({}, TRAYS)
check("gleicher Stand -> keine neue Meldung", len(sammler.records) == anzahl)

fast_alles = {h: {} for h in helpers if h != "input_text.spoolman_commit"}
c.report_missing_helpers(fast_alles, TRAYS)
check("Stand aendert sich -> neue Meldung nur mit dem einen fehlenden Helfer",
      len(sammler.records) == anzahl + 1 and "(1)" in sammler.records[-1][1]
      and "input_text.spoolman_commit" in sammler.records[-1][1])

c.report_missing_helpers({h: {} for h in helpers}, TRAYS)
check("alles da -> Info statt Warnung", sammler.records[-1][0] == "INFO")
c.log.removeHandler(sammler)

print("3) entity_config-Hilfsfunktionen")
check("keine Meldung wenn nichts fehlt", ec.missing_helpers_message([]) is None)
check("Gruppierung", ec.format_missing_helpers(["input_text.b", "input_number.a", "input_text.c"])
      == "input_number (1): input_number.a; input_text (2): input_text.b, input_text.c")
rep = ec.ChangeReporter()
check("ChangeReporter: erster Aufruf neu, gleicher nicht, anderer wieder", rep.is_new(1) and not rep.is_new(1) and rep.is_new(2))

print("4) filament_enrich: Helfer-Pruefung")


class FakeResponse:
    def __init__(self, ids):
        self._ids = ids

    def raise_for_status(self):
        pass

    def json(self):
        return [{"entity_id": i} for i in self._ids]


calls = []
ids_in_ha = [fe.URL_ENTITY, fe.SELECT_ENTITY]
fe._http.get = lambda url, **kw: calls.append(url) or FakeResponse(ids_in_ha)
sammler = Sammler()
fe.log.addHandler(sammler)
fe.log.setLevel(logging.DEBUG)
fe._helper_check_done = False
fe.check_helpers_once()
level, msg = sammler.records[-1]
check("fehlende Helfer als ERROR gemeldet", level == "ERROR" and fe.CREATE_BUTTON_ENTITY in msg and fe.STATUS_ENTITY in msg)
check("vorhandene werden nicht gemeldet", fe.URL_ENTITY not in msg and fe.SELECT_ENTITY not in msg)
fe.check_helpers_once()
check("nur einmal geprueft (kein Dauerabruf der Statusliste)", len(calls) == 1)

fe._helper_check_done = False
ids_in_ha[:] = list(fe.REQUIRED_HELPERS)
fe.check_helpers_once()
check("alles da -> Info", sammler.records[-1][0] == "INFO")


def failing_get(url, **kw):
    import requests
    raise requests.ConnectionError("HA down")


fe._http.get = failing_get
fe._helper_check_done = False
try:
    fe.check_helpers_once()
    raised = False
except Exception:
    raised = True
check("HA nicht erreichbar -> Fehler wird weitergereicht, Pruefung bleibt offen", raised and not fe._helper_check_done)
fe.log.removeHandler(sammler)

calls_after = []
fe._http.get = lambda url, **kw: calls_after.append(url) or (_ for _ in ()).throw(__import__("requests").ConnectionError("x"))
fe._button_pressed = lambda *a, **k: False
fe._helper_check_done = False
try:
    fe.process_once({})
    survived = True
except Exception:
    survived = False
check("process_once laeuft trotz fehlgeschlagener Pruefung weiter", survived)

print("5) HELPERS.md beschreibt jeden benutzten Helfer")
doc = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "HELPERS.md"), encoding="utf-8").read()
for entity in (c.VERSION_TEXT_ENTITY, c.COMMIT_TEXT_ENTITY, c.RELEASE_TEXT_ENTITY, *fe.REQUIRED_HELPERS,
               "input_select.p2s_slot_external"):
    check(f"{entity} steht in HELPERS.md", entity in doc)
for family in ("p2s_ersetzt_", "p2s_gereinigt_", "p2s_geschmiert_", "p2s_kalibriert_full",
               "p2s_rt_ersetzen_", "p2s_rt_reinigen_", "p2s_rt_schmieren_", "p2s_slot_ams_<n>_tray_<m>"):
    check(f"Familie {family} steht in HELPERS.md", family in doc)
check("Anzahlen stimmen mit dem Code ueberein",
      f"{len(c.WARTUNG_DATE_ENTITIES)} ×" in doc and f"{len(c.WARTUNG_BASELINE_ENTITIES)} ×" in doc)

print("\nERGEBNIS:", "ALLE TESTS PASS" if ok else "FEHLER")
raise SystemExit(0 if ok else 1)
