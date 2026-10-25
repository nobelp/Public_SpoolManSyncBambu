"""Regressionstest (Issue #56): Ist der externe Slot laut Sensor leer, muss eine
Spule mit Standort 'P2S - Extern' auf 'Lager' gehen - auch ohne bambu_tag_uid.
Bei 'unavailable' (Aussetzer der Bambu-Integration) bleibt der Standort unverändert.
"""
import sync as s
from entity_config import external_spool_entity

ok = True


def check(label, cond, detail=""):
    global ok
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  → {detail}" if detail and not cond else ""))
    ok = ok and cond


EXT = external_spool_entity()
location_calls = []
s._set_location_to_storage = lambda entity_id, spool_id: location_calls.append(spool_id)

# Spule #1 ohne bambu_tag_uid, Standort 'P2S - Extern'; #35 im AMS darf nicht angefasst werden
SPOOLS = [
    {"id": 1, "location": "P2S - Extern", "extra": {}},
    {"id": 35, "location": "AMS 1 - Slot 3", "extra": {}},
]

print("1) Externer Slot leer → Spule #1 auf Lager")
location_calls.clear()
s._release_empty_external_spools({EXT: {"state": "Empty", "attributes": {}}}, SPOOLS)
check("Spule #1 → Lager", location_calls == [1], detail=str(location_calls))

print("2) Externer Sensor 'unavailable' → unverändert")
location_calls.clear()
s._release_empty_external_spools({EXT: {"state": "unavailable", "attributes": {}}}, SPOOLS)
check("kein Reset", location_calls == [], detail=str(location_calls))

print("3) Externe Spule eingelegt → unverändert")
location_calls.clear()
s._release_empty_external_spools(
    {EXT: {"state": "PLA Basic", "attributes": {"name": "PLA Basic", "tray_uuid": "A" * 32, "tag_uid": "B1C2D3E4F5A60708"}}},
    SPOOLS,
)
check("kein Reset", location_calls == [], detail=str(location_calls))

print("4) Sensor fehlt in HA → unverändert")
location_calls.clear()
s._release_empty_external_spools({}, SPOOLS)
check("kein Reset", location_calls == [], detail=str(location_calls))

print("\nERGEBNIS:", "ALLE TESTS PASS" if ok else "FEHLER")
raise SystemExit(0 if ok else 1)
