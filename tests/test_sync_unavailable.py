import sync as s

ok = True


def check(label, cond):
    global ok
    print(("  PASS  " if cond else "  FAIL  ") + label)
    ok = ok and cond


print("1) _is_empty: unavailable/unknown gelten NICHT mehr als leer")
check("unavailable != leer", s._is_empty("unavailable", {}) is False)
check("unknown != leer", s._is_empty("unknown", {}) is False)
check("Empty == leer", s._is_empty("Empty", {}) is True)
check("unavailable erkannt", s._is_unavailable("unavailable") is True)

print("2) process_slot: Aussetzer laesst State + Location unangetastet")
calls = []
s._set_location_to_storage = lambda e, i: calls.append(("reset_location", i))
state = {"slots": {"sensor.x_ams_1_slot_4": {"name": "Bambu PLA Silk", "spoolman_id": 20,
                                             "tray_uuid": "ABC", "location": "AMS 1 - Slot 4"}}}
slot = s._parse_slot("sensor.x_ams_1_slot_4", {"state": "unavailable", "attributes": {}})
s.process_slot("sensor.x_ams_1_slot_4", slot, state, [])
check("Zuordnung erhalten", state["slots"]["sensor.x_ams_1_slot_4"]["spoolman_id"] == 20)
check("kein Location-Reset auf 'Lager'", calls == [])

print("3) Gegenprobe: echtes 'Empty' verwirft weiterhin korrekt")
slot_empty = s._parse_slot("sensor.x_ams_1_slot_4", {"state": "Empty", "attributes": {}})
s.process_slot("sensor.x_ams_1_slot_4", slot_empty, state, [])
check("Slot geleert", "sensor.x_ams_1_slot_4" not in state["slots"])
check("Location-Reset ausgefuehrt", calls == [("reset_location", 20)])

print("\nERGEBNIS:", "ALLE TESTS PASS" if ok else "FEHLER")
raise SystemExit(0 if ok else 1)
