"""Regressionstest fuer zwei zusammenhaengende Bugs, die dazu fuehrten, dass
nach dem Entnehmen einer chiplosen Spule (z.B. Spule #23) automatisch wieder
auf sie gebucht wurde, obwohl laengst eine andere Spule im Slot lag:

1) save_state() persistierte user_spool_overrides nur, wenn das Dict NICHT
   leer war ("if overrides: ..."). Wurde der letzte Eintrag geloescht (z.B.
   durch _clear_stale_override(), sobald RFID wieder greift), blieb die
   veraltete Zuordnung auf der Platte stehen und kam beim naechsten
   load_state() zurueck.

2) _forget_tray_spool() (aufgerufen sobald der Slot nachweislich "Empty" ist)
   loeschte nur die RFID-Merk-Spule (last_known_tray_spools), nicht aber eine
   manuelle Zuordnung (user_spool_overrides) - die wurde nach dem naechsten,
   ebenfalls chiplosen Spuleneinlegen automatisch wiederverwendet, ohne dass
   der User das je fuer die NEUE Spule bestaetigt hat.
"""
import os
import tempfile

tmp = tempfile.mkdtemp()
os.environ.update({
    "CONSUMPTION_STATE_FILE": os.path.join(tmp, "state.json"),
    "CONSUMPTION_LOG_FILE": os.path.join(tmp, "c.log"),
    "CONSUMPTION_HISTORY_FILE": os.path.join(tmp, "h.json"),
    "CONSUMPTION_HISTORY_DB": os.path.join(tmp, "h.db"),
    "CONSUMPTION_HISTORY_IMAGES_DIR": os.path.join(tmp, "img"),
    "WARTUNG_BACKUP_FILE": os.path.join(tmp, "w.json"),
    "CONSUMPTION_OVERRIDES_FILE": os.path.join(tmp, "o.json"),
})

import consumption as c

ok = True


def check(label, cond, detail=""):
    global ok
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  → {detail}" if detail and not cond else ""))
    ok = ok and cond


SLOT_ENTITY = "sensor.p2s_test_ams_1_slot_4"
TRAY_KEY = "AMS 1 Tray 4"
SPOOLS = [{"id": 23, "filament": {"name": "PLA-Basic Orange"}}]

print("1) save_state() persistiert ein leer geraeumtes user_spool_overrides-Dict")
state = {"jobs": {}, "current_job_key": None, "user_spool_overrides": {"AMS 1 Tray 4": 23}}
c.save_state(state)
del state["user_spool_overrides"]["AMS 1 Tray 4"]
c.save_state(state)
reloaded = c.load_state()
check(
    "Override bleibt nach dem Speichern eines leeren Dicts geloescht",
    reloaded.get("user_spool_overrides", {}) == {},
    detail=str(reloaded.get("user_spool_overrides")),
)

print("2) Slot wird 'Empty' gemeldet: manuelle Zuordnung muss mit verworfen werden")
state_data = {
    "user_spool_overrides": {TRAY_KEY: 23},
    "last_known_tray_spools": {TRAY_KEY: {"spool_id": 23, "uid": "USER_x_23"}},
}
all_states = {SLOT_ENTITY: {"state": "Empty", "attributes": {}}}
tray_key_to_slot = {TRAY_KEY: SLOT_ENTITY}
orig_build = c._build_tray_key_to_slot
c._build_tray_key_to_slot = lambda _states: tray_key_to_slot
try:
    result = c.resolve_spool_for_tray(TRAY_KEY, all_states, state_data, SPOOLS)
finally:
    c._build_tray_key_to_slot = orig_build

check("Kein Spool mehr aufgeloest fuer leeren Slot", result == (None, None), detail=str(result))
check(
    "Manuelle Zuordnung wurde verworfen",
    TRAY_KEY not in state_data["user_spool_overrides"],
    detail=str(state_data["user_spool_overrides"]),
)

print("3) Neue, ebenfalls chiplose Spule danach: KEINE automatische Wiederverwendung der alten Auswahl")
all_states_new_spool = {SLOT_ENTITY: {"state": "Generic PLA", "attributes": {}}}
c._build_tray_key_to_slot = lambda _states: tray_key_to_slot
try:
    result2 = c.resolve_spool_for_tray(TRAY_KEY, all_states_new_spool, state_data, SPOOLS)
finally:
    c._build_tray_key_to_slot = orig_build
check(
    "Keine Spule automatisch zugeordnet - wartet auf User-Bestaetigung",
    result2 == (None, None),
    detail=str(result2),
)

print("\nERGEBNIS:", "ALLE TESTS PASS" if ok else "FEHLER")
raise SystemExit(0 if ok else 1)
