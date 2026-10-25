"""reconcile_locations.py laeuft nur einmalig beim allerersten Start (siehe
entrypoint-sync.sh) und hatte dadurch unbemerkt eine eigene, veraltete Kopie
von _get_location_from_entity behalten ("Externer Slot 5" statt "P2S -
Extern", das sync.py laengst verwendet). Dieser Test prueft direkt gegen
sync.py's Version, damit ein erneutes Auseinanderlaufen der beiden Kopien
sofort auffaellt."""
import os

os.environ.setdefault("STATE_FILE", "/tmp/test_reconcile_sync_state.json")
os.environ.setdefault("LOG_FILE", "/tmp/test_reconcile_sync.log")

import reconcile_locations as rl
import sync as s

ok = True


def check(label, cond, detail=""):
    global ok
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  → {detail}" if detail and not cond else ""))
    ok = ok and cond


CASES = [
    "sensor.p2s_testserial0001_externalspool_externe_spule",
    "sensor.p2s_testserial0001_ams_1_slot_1",
    "sensor.p2s_testserial0001_ams_1_slot_4",
    "sensor.p2s_testserial0001_ams_2_slot_3",
]

print("1) reconcile_locations._get_location_from_entity stimmt mit sync.py ueberein")
for entity_id in CASES:
    rl_result = rl._get_location_from_entity(entity_id)
    sync_result = s._get_location_from_entity(entity_id)
    check(f"{entity_id} -> gleiche Location", rl_result == sync_result,
          detail=f"reconcile_locations={rl_result!r} sync={sync_result!r}")

print("2) Externe Spule liefert konkret 'P2S - Extern' (nicht die alte 'Externer Slot 5')")
check("Wert ist 'P2S - Extern'",
      rl._get_location_from_entity("sensor.p2s_testserial0001_externalspool_externe_spule") == "P2S - Extern")

print("\nERGEBNIS:", "ALLE TESTS PASS" if ok else "FEHLER")
raise SystemExit(0 if ok else 1)
