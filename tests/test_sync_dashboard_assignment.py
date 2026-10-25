"""Regressionstest: Dashboard-Zuordnung einer nicht erkannten Spule muss in
Spoolman den AMS-Slot-Standort setzen, und der Standort muss bei Wechsel,
Entfernen der Zuordnung oder Entnahme wieder auf 'Lager' zurueckgehen.

Vorher las sync.py die Zuordnung aus consumption_state.json, wo sie seit dem
Umzug in consumption_overrides.json nicht mehr steht - der Standort blieb daher
auf 'Lager'.
"""
import json
import os
import tempfile

tmp = tempfile.mkdtemp()
OVERRIDES_PATH = os.path.join(tmp, "consumption_overrides.json")
os.environ["CONSUMPTION_OVERRIDES_FILE"] = OVERRIDES_PATH

import sync as s

ok = True


def check(label, cond, detail=""):
    global ok
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  → {detail}" if detail and not cond else ""))
    ok = ok and cond


ENTITY = "sensor.p2s_test_ams_1_slot_2"
TRAY_KEY = "AMS 1 Tray 2"

location_calls = []
reconcile_calls = []
s._set_location_to_storage = lambda entity_id, spool_id: location_calls.append(spool_id)
s._reconcile_spool_metadata = lambda entity_id, spool_id, location: reconcile_calls.append((spool_id, location))
s.find_spool_by_bambu_uid = lambda uid, spools=None: None
s.find_spool_by_tray_uuid = lambda uuid, spools=None: None

SPOOLS = [{"id": 50}, {"id": 51}, {"id": 7}, {"id": 60}]


def write_overrides(overrides):
    with open(OVERRIDES_PATH, "w", encoding="utf-8") as f:
        json.dump(overrides, f)


def run_poll(state, attributes, entity_state="PLA"):
    slot = s._parse_slot(ENTITY, {"state": entity_state, "attributes": attributes})
    s.process_slot(ENTITY, slot, state, SPOOLS)
    return state["slots"].get(ENTITY)


def reset_calls():
    location_calls.clear()
    reconcile_calls.clear()


NON_RFID = {"name": "PLA-Matte Morandi Green", "tray_uuid": "", "tag_uid": ""}
RFID = {"name": "PLA Basic", "tray_uuid": "A" * 32, "tag_uid": "B1C2D3E4F5A60708"}

print("1) Nicht erkannte Spule wird im Dashboard gewählt → Slot-Standort gesetzt")
reset_calls()
write_overrides({TRAY_KEY: 50})
state = {"slots": {}}
entry = run_poll(state, NON_RFID)
check("Spule #50 im Slot-Zustand", entry["spoolman_id"] == 50, detail=str(entry))
check("Standort 'AMS 1 - Slot 2' gesetzt", (50, "AMS 1 - Slot 2") in reconcile_calls, detail=str(reconcile_calls))
check("assigned_by = dashboard", entry.get("assigned_by") == "dashboard")

print("2) Gleiche Auswahl bleibt bestehen (Fall a: Spule bleibt)")
reset_calls()
entry = run_poll(state, NON_RFID)
check("Spule #50 bleibt", entry["spoolman_id"] == 50)
check("kein Lager-Reset", location_calls == [], detail=str(location_calls))

print("3) Umbelegung im Dashboard auf andere Spule (b1 / Wechsel)")
reset_calls()
write_overrides({TRAY_KEY: 51})
entry = run_poll(state, NON_RFID)
check("Spule #51 übernimmt den Slot", entry["spoolman_id"] == 51)
check("alte Spule #50 → Lager", location_calls == [50], detail=str(location_calls))
check("neue Spule #51 bekommt Slot-Standort", (51, "AMS 1 - Slot 2") in reconcile_calls)

print("4) Auswahl im Dashboard entfernt (b2) → Spule wieder auf Lager")
reset_calls()
write_overrides({})
entry = run_poll(state, NON_RFID)
check("kein Spule mehr im Slot-Zustand", entry["spoolman_id"] is None, detail=str(entry))
check("Spule #51 → Lager", location_calls == [51], detail=str(location_calls))
check("kein Slot-Standort mehr gesetzt", all(loc != "AMS 1 - Slot 2" or sid is None for sid, loc in reconcile_calls),
      detail=str(reconcile_calls))

print("5) RFID-Spule im Slot: Dashboard-Auswahl wird ignoriert")
reset_calls()
state = {"slots": {ENTITY: {"spoolman_id": 7, "tray_uuid": RFID["tray_uuid"], "tag_uid": RFID["tag_uid"],
                            "bambu_uid": RFID["tag_uid"], "uid_source": "RFID", "assigned_by": None}}}
write_overrides({TRAY_KEY: 60})
entry = run_poll(state, RFID)
check("RFID-Spule #7 bleibt", entry["spoolman_id"] == 7, detail=str(entry))
check("Dashboard-Spule #60 wird nicht übernommen", (60, "AMS 1 - Slot 2") not in reconcile_calls)
check("kein Lager-Reset", location_calls == [], detail=str(location_calls))
check("assigned_by = None (RFID-Pfad)", entry.get("assigned_by") is None)

print("6) Slot wird leer geräumt → Spule auf Lager (bestehendes Verhalten)")
reset_calls()
state = {"slots": {ENTITY: {"spoolman_id": 50, "tray_uuid": "", "assigned_by": "dashboard"}}}
write_overrides({TRAY_KEY: 50})
slot = s._parse_slot(ENTITY, {"state": "Empty", "attributes": {}})
s.process_slot(ENTITY, slot, state, SPOOLS)
check("Slot-Zustand entfernt", ENTITY not in state["slots"])
check("Spule #50 → Lager", location_calls == [50], detail=str(location_calls))

print("7) Zuordnung zeigt auf eine in Spoolman nicht vorhandene Spule")
reset_calls()
state = {"slots": {ENTITY: {"spoolman_id": 51, "tray_uuid": "", "assigned_by": "dashboard"}}}
write_overrides({TRAY_KEY: 999})
entry = run_poll(state, NON_RFID)
check("keine Spule übernommen", entry["spoolman_id"] is None, detail=str(entry))
check("bisherige Spule #51 → Lager", location_calls == [51], detail=str(location_calls))
check("kein Standort für Spule #999 gesetzt", all(sid != 999 for sid, _ in reconcile_calls))

print("8) _spool_exists: 404 = nicht vorhanden, anderer Fehler wird weitergereicht")
import requests


def fake_response(status):
    r = requests.Response()
    r.status_code = status
    return r


def sm_get_raising(status):
    def _get(path, params=None, timeout=10):
        raise requests.exceptions.HTTPError(f"{status} error", response=fake_response(status))
    return _get


original_sm_get = s.sm_get
try:
    s.sm_get = sm_get_raising(404)
    check("404 → nicht vorhanden", s._spool_exists(999) is False)
    s.sm_get = sm_get_raising(500)
    raised = False
    try:
        s._spool_exists(999)
    except requests.exceptions.HTTPError:
        raised = True
    check("500 → Fehler wird weitergereicht (kein Lager-Reset)", raised)
    s.sm_get = lambda path, params=None, timeout=10: {"id": 50}
    check("vorhandene Spule → True", s._spool_exists(50) is True)
finally:
    s.sm_get = original_sm_get

print("\nERGEBNIS:", "ALLE TESTS PASS" if ok else "FEHLER")
raise SystemExit(0 if ok else 1)
