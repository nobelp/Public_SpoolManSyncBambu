"""Issue #6: HA-Entity-Namen kommen aus der .env, Standardwerte bleiben die alten.

Kein Netzwerk. Die Module lesen die Namen beim Import, daher werden die
Ueberschreibungen vor dem Import von consumption/sync gesetzt.
"""
import contextlib
import logging
import os

import entity_config as ec

ok = True


def check(label, cond):
    global ok
    print(("  PASS  " if cond else "  FAIL  ") + label)
    ok = ok and bool(cond)


ENV_VARS = ("PRINTER_ENTITY_PREFIX", "EXTERNAL_SPOOL_ENTITY", "EXTERNAL_ACTIVE_ENTITY",
            "PRINT_STATUS_ENTITY")


@contextlib.contextmanager
def env(**values):
    saved = {k: os.environ.get(k) for k in ENV_VARS}
    for k in ENV_VARS:
        os.environ.pop(k, None)
    os.environ.update(values)
    try:
        yield
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def raises_value_error(func, *needles):
    try:
        func()
    except ValueError as e:
        return all(n in str(e) for n in needles)
    return False


OLD_SLOTS = [
    "sensor.bambu_p2s_ams_1_slot_1",
    "sensor.bambu_p2s_ams_1_slot_2",
    "sensor.bambu_p2s_ams_1_slot_3",
    "sensor.bambu_p2s_ams_1_slot_4",
    "sensor.bambu_p2s_externalspool_externe_spule",
]

print("1) Ohne Eintrag gelten die Standardnamen")
with env():
    check("Slot-Liste identisch zu vorher", ec.slot_entities() == OLD_SLOTS)
    check("Praefix", ec.printer_prefix() == "sensor.bambu_p2s")
    check("Extern-aktiv", ec.external_active_entity() == "binary_sensor.bambu_p2s_externalspool_aktiv")
    check("Extern-Spule", ec.external_spool_entity() == OLD_SLOTS[-1])
    check("Entity ohne Variable -> Standardwert", ec.entity_from_env("PRINT_STATUS_ENTITY", "sensor.a") == "sensor.a")

print("2) Eigener Drucker-Praefix leitet alle Slot-Namen ab")
with env(PRINTER_ENTITY_PREFIX="sensor.x1c_01s00c123456789"):
    slots = ec.slot_entities()
    check("AMS 1 Slot 1..4", slots[:4] == [f"sensor.x1c_01s00c123456789_ams_1_slot_{n}" for n in (1, 2, 3, 4)])
    check("externe Spule folgt dem Praefix", slots[4] == "sensor.x1c_01s00c123456789_externalspool_externe_spule")
    check("Extern-aktiv folgt dem Praefix",
          ec.external_active_entity() == "binary_sensor.x1c_01s00c123456789_externalspool_aktiv")

print("3) Einzelne Entity ueberschreiben, auch die externe Spule")
with env(EXTERNAL_SPOOL_ENTITY="sensor.drucker_extern_spule", EXTERNAL_ACTIVE_ENTITY="binary_sensor.drucker_extern_aktiv"):
    check("Slot-Liste enthaelt eigenen Namen", ec.slot_entities()[-1] == "sensor.drucker_extern_spule")
    check("eigener Name wird als extern erkannt", ec.is_external_spool_entity("sensor.drucker_extern_spule"))
    check("Extern-aktiv ueberschrieben", ec.external_active_entity() == "binary_sensor.drucker_extern_aktiv")
    check("AMS-Slot ist nicht extern", not ec.is_external_spool_entity(ec.ams_slot_entity("sensor.p", 1, 2)))
check("Standardname 'externalspool' wird weiter erkannt", ec.is_external_spool_entity(OLD_SLOTS[-1]))
check("leere/None-Entity ist nicht extern", not ec.is_external_spool_entity("") and not ec.is_external_spool_entity(None))

print("4) Leerer Wert zaehlt als nicht gesetzt; ungueltige Werte brechen klar ab")
with env(PRINT_STATUS_ENTITY="   "):
    check("nur Leerzeichen -> Standardwert", ec.entity_from_env("PRINT_STATUS_ENTITY", "sensor.a") == "sensor.a")
for bad in ("Sensor.Status", "druckstatus", "sensor.a b", "sensor.", "sensor.x;rm"):
    with env(PRINT_STATUS_ENTITY=bad):
        check(f"'{bad}' -> ValueError mit Variablenname",
              raises_value_error(lambda: ec.entity_from_env("PRINT_STATUS_ENTITY", "sensor.a"), "PRINT_STATUS_ENTITY"))
with env(PRINTER_ENTITY_PREFIX="binary_sensor.x"):
    check("Praefix muss mit 'sensor.' beginnen", raises_value_error(ec.printer_prefix, "sensor."))

print("5) find_missing")
check("fehlende in Reihenfolge", ec.find_missing({"a": 1, "c": 1}, ["a", "b", "c", "d"]) == ["b", "d"])
check("nichts fehlt", ec.find_missing({"a": 1}, ["a"]) == [])

# --- Ab hier: echte Module mit eigenen Namen importieren ---------------------------
CUSTOM = {
    "PRINTER_ENTITY_PREFIX": "sensor.x1c_01s00c123456789",
    "PRINT_STATUS_ENTITY": "sensor.mein_drucker_print_status",
    "PRINT_PROGRESS_ENTITY": "sensor.mein_drucker_print_progress",
    "PRINT_WEIGHT_ENTITY": "sensor.mein_drucker_print_weight",
    "EXTERNAL_SPOOL_ENTITY": "sensor.mein_drucker_extern",
}
os.environ.update(CUSTOM)
os.environ.setdefault("STATE_FILE", os.path.join(os.environ.get("TMPDIR", "/tmp"), "t_entity_state.json"))
os.environ.setdefault("LOG_FILE", os.path.join(os.environ.get("TMPDIR", "/tmp"), "t_entity_sync.log"))

import consumption as c  # noqa: E402
import sync as s  # noqa: E402

print("6) consumption.py uebernimmt die .env-Werte")
check("Status-Entity", c.PRINT_STATUS_ENTITY == CUSTOM["PRINT_STATUS_ENTITY"])
check("Fortschritt-Entity", c.PRINT_PROGRESS_ENTITY == CUSTOM["PRINT_PROGRESS_ENTITY"])
check("Gewicht-Entity", c.PRINT_WEIGHT_ENTITY == CUSTOM["PRINT_WEIGHT_ENTITY"])
check("Praefix", c.PRINTER_ENTITY_PREFIX == CUSTOM["PRINTER_ENTITY_PREFIX"])
check("Extern-aktiv aus Praefix abgeleitet",
      c.EXTERNAL_ACTIVE_ENTITY == "binary_sensor.x1c_01s00c123456789_externalspool_aktiv")
states = {f"sensor.x1c_01s00c123456789_ams_1_slot_{n}": {} for n in (1, 2)} | {CUSTOM["EXTERNAL_SPOOL_ENTITY"]: {}}
mapping = c._build_tray_key_to_slot(states)
check("Tray-Mapping nutzt Praefix + eigene Extern-Entity",
      mapping == {"AMS 1 Tray 1": "sensor.x1c_01s00c123456789_ams_1_slot_1",
                  "AMS 1 Tray 2": "sensor.x1c_01s00c123456789_ams_1_slot_2",
                  "External": CUSTOM["EXTERNAL_SPOOL_ENTITY"]})

print("7) sync.py / reconcile_locations.py uebernehmen sie ebenfalls")
check("SLOT_ENTITIES in sync.py", s.SLOT_ENTITIES[0] == "sensor.x1c_01s00c123456789_ams_1_slot_1"
      and s.SLOT_ENTITIES[-1] == CUSTOM["EXTERNAL_SPOOL_ENTITY"])
check("eigene Extern-Entity -> Location 'P2S - Extern'",
      s._get_location_from_entity(CUSTOM["EXTERNAL_SPOOL_ENTITY"]) == "P2S - Extern")
check("AMS-Slot -> Location", s._get_location_from_entity(s.SLOT_ENTITIES[1]) == "AMS 1 - Slot 2")

print("8) Fehlende Entities werden gemeldet - einmal je Aenderung, nicht jede Minute")


class Sammler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append((record.levelname, record.getMessage()))


sammler = Sammler()
c.log.addHandler(sammler)
c.log.setLevel(logging.DEBUG)
c._last_reported_missing = None

c.report_missing_entities({})
check("Pflicht fehlt -> ERROR mit allen drei Namen",
      any(lvl == "ERROR" and all(e in msg for e in c.REQUIRED_ENTITIES) for lvl, msg in sammler.records))
check("ERROR verweist auf discover_entities.py",
      any("discover_entities.py" in msg for lvl, msg in sammler.records if lvl == "ERROR"))
check("Optionale fehlen -> WARNING", any(lvl == "WARNING" for lvl, _ in sammler.records))
anzahl = len(sammler.records)
c.report_missing_entities({})
check("zweiter Poll mit gleichem Stand schweigt", len(sammler.records) == anzahl)
c.report_missing_entities({e: {} for e in c.REQUIRED_ENTITIES})
check("Stand aendert sich (Pflicht da, Optionale fehlen) -> neue Meldung, kein ERROR mehr",
      len(sammler.records) > anzahl and sammler.records[-1][0] == "WARNING")
c.report_missing_entities({e: {} for e in c.REQUIRED_ENTITIES + c.OPTIONAL_ENTITIES})
check("alles da -> Info", sammler.records[-1] == ("INFO", "Alle konfigurierten HA-Entities sind vorhanden"))
c.log.removeHandler(sammler)

print("9) discover_entities.py: Vorschlaege fuer die .env")
import discover_entities as d  # noqa: E402

REAL = [
    "sensor.bambu_p2s_ams_1_slot_1", "sensor.bambu_p2s_ams_1_slot_2",
    "sensor.bambu_p2s_externalspool_externe_spule",
    "sensor.bambu_p2s_druckstatus", "sensor.bambu_p2s_druckfortschritt",
    "sensor.bambu_p2s_gewicht_des_drucks", "sensor.bambu_p2s_gcode_dateiname",
    "sensor.bambu_p2s_startzeit", "sensor.bambu_p2s_name_der_aufgabe",
    "image.bambu_p2s_titelbild", "camera.bambu_p2s_kamera",
    "sensor.bambu_p2s_druckbett_typ", "light.irgendwas",
]
lines, notes = d.build_suggestions(REAL)
check("liefert genau die Standardnamen",
      sorted(lines) == sorted([
          "PRINTER_ENTITY_PREFIX=sensor.bambu_p2s",
          "PRINT_STATUS_ENTITY=sensor.bambu_p2s_druckstatus",
          "PRINT_PROGRESS_ENTITY=sensor.bambu_p2s_druckfortschritt",
          "PRINT_WEIGHT_ENTITY=sensor.bambu_p2s_gewicht_des_drucks",
          "GCODE_ENTITY=sensor.bambu_p2s_gcode_dateiname",
          "STARTTIME_ENTITY=sensor.bambu_p2s_startzeit",
          "TASK_NAME_ENTITY=sensor.bambu_p2s_name_der_aufgabe",
          "TITLE_IMAGE_ENTITY=image.bambu_p2s_titelbild",
          "LIVE_CAMERA_ENTITY=camera.bambu_p2s_kamera",
          "PLATE_TYPE_ENTITY=sensor.bambu_p2s_druckbett_typ"]))
check("keine Hinweise bei vollstaendiger Installation", notes == [])

ENGLISH = ["sensor.x1c_abc_ams_1_slot_1", "sensor.x1c_abc_print_status", "sensor.x1c_abc_print_progress",
           "sensor.x1c_abc_print_weight"]
lines, notes = d.build_suggestions(ENGLISH)
check("englische Namen werden erkannt", "PRINT_STATUS_ENTITY=sensor.x1c_abc_print_status" in lines)
check("abweichender Praefix wird hervorgehoben", any("weicht vom Standard ab" in n for n in notes))
check("fehlende Entities werden gemeldet, nicht geraten",
      any(n.startswith("GCODE_ENTITY: nichts gefunden") for n in notes)
      and not any(l.startswith("GCODE_ENTITY=") for l in lines))

lines, notes = d.build_suggestions(ENGLISH + ["sensor.zweit_drucker_print_status"])
check("mehrdeutiger Treffer wird nicht geraten",
      not any(l.startswith("PRINT_STATUS_ENTITY=") for l in lines)
      and any(n.startswith("PRINT_STATUS_ENTITY: mehrdeutig") for n in notes))

lines, notes = d.build_suggestions(ENGLISH + ["sensor.y_ams_1_slot_1"])
check("mehrere Drucker -> kein Praefix geraten",
      not any(l.startswith("PRINTER_ENTITY_PREFIX=") for l in lines)
      and any("Mehrere Drucker" in n for n in notes))
check("kein Treffer fuer Suffix-Ende mitten im Namen",
      d.match_by_suffix(["sensor.a_print_status_extra", "sensor.a_print_status"], "sensor", ("print_status",))
      == ["sensor.a_print_status"])

print("\nERGEBNIS:", "ALLE TESTS PASS" if ok else "FEHLER")
raise SystemExit(0 if ok else 1)
