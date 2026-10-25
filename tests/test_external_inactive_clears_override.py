"""Regressionstest fuer: wird die externe Spule inaktiv ("Ausser Betrieb"),
muss die Dashboard-Zuordnung (input_select.p2s_slot_external) auf den
Platzhalter zurueckgesetzt UND die gespeicherte Zuordnung (user_spool_overrides)
geloescht werden - sonst bleibt z.B. Spule #26 dauerhaft zugeordnet, obwohl
laengst eine andere/keine Spule mehr extern eingelegt ist. Der Slot-Sensor
selbst meldet seinen letzten Materialnamen unveraendert weiter (kein
zuverlaessiger "leer"-Zustand), einzig der separate Aktiv-Binary-Sensor zeigt
das zuverlaessig an."""
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


ha_calls = []
c.ha_call_service = lambda domain, service, data: ha_calls.append((domain, service, data))

EXTERNAL_SLOT_ENTITY = f"{c.PRINTER_ENTITY_PREFIX}_externalspool_externe_spule"
SELECT_ENTITY = "input_select.p2s_slot_external"
SPOOLS = [{"id": 26, "filament": {"name": "PLA Matte Morandi Green"}}]
TRAY_KEY_TO_SLOT = {"External": EXTERNAL_SLOT_ENTITY}


def make_states(active):
    return {
        EXTERNAL_SLOT_ENTITY: {"state": "Generic PLA", "attributes": {}},
        c.EXTERNAL_ACTIVE_ENTITY: {"state": "on" if active else "off"},
        SELECT_ENTITY: {
            "state": "26 - PLA Matte Morandi Green",
            "attributes": {"options": [c._SELECT_PLACEHOLDER, "26 - PLA Matte Morandi Green"]},
        },
    }


print("1) Externe Spule AKTIV: Zuordnung bleibt bestehen (Kontrollfall)")
state = {"user_spool_overrides": {"External": 26}}
c._sync_dashboard_selects(make_states(active=True), state, TRAY_KEY_TO_SLOT, SPOOLS)
check("Override bleibt erhalten", state["user_spool_overrides"].get("External") == 26)
check("Kein HA-Select-Aufruf noetig (Dropdown zeigt schon das Richtige)", ha_calls == [], detail=str(ha_calls))

print("2) Externe Spule AUSSER BETRIEB: Zuordnung muss geloescht werden")
ha_calls.clear()
state = {"user_spool_overrides": {"External": 26}}
c._sync_dashboard_selects(make_states(active=False), state, TRAY_KEY_TO_SLOT, SPOOLS)
check("Override wurde geloescht", "External" not in state["user_spool_overrides"], detail=str(state["user_spool_overrides"]))
select_calls = [call for call in ha_calls if call[1] == "select_option"]
check("Dropdown wird auf Platzhalter zurueckgesetzt",
      any(call[2].get("option") == c._SELECT_EMPTY_LABEL for call in select_calls),
      detail=str(select_calls))

print("\nERGEBNIS:", "ALLE TESTS PASS" if ok else "FEHLER")
raise SystemExit(0 if ok else 1)
