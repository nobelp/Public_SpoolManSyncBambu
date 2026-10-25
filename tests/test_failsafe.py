import sync as s

ok = True
reloads = []


def check(label, cond):
    global ok
    print(("  PASS  " if cond else "  FAIL  ") + label)
    ok = ok and cond


# Reload nicht echt ausfuehren, nur mitschreiben
s.ha_reload_config_entry = lambda domain: reloads.append(domain) or True

AMS = [f"sensor.p2s_testserial0001_ams_1_slot_{i}" for i in (1, 2, 3, 4)]
EXT = "sensor.p2s_testserial0001_externalspool_externe_spule"
HEALTHY = {e: {"state": "Bambu PLA Silk"} for e in AMS} | {EXT: {"state": "Empty"}}
KNOWN_SLOTS = {"slots": {e: {"spoolman_id": 20} for e in AMS}}


def fresh_state():
    import copy
    return copy.deepcopy(KNOWN_SLOTS)


print("1) Gesunde Sensoren -> kein Reload")
st = fresh_state()
s.failsafe_reload_integrations(st, HEALTHY)
check("kein Reload", reloads == [])
check("Zaehler bei 0", st["failsafe"]["unavailable_polls"] == 0)

print("2) Totalausfall: Reload erst ab 3. Poll (nicht sofort)")
st = fresh_state()
for poll in (1, 2):
    s.failsafe_reload_integrations(st, {})
    check(f"Poll {poll}: noch kein Reload", reloads == [])
s.failsafe_reload_integrations(st, {})
check("Poll 3: Reload ausgeloest", reloads == ["bambu_lab", "spoolman"])
check("Drucker VOR Spoolman neu geladen", reloads.index("bambu_lab") < reloads.index("spoolman"))

print("3) Cooldown: kein zweiter Reload direkt danach")
reloads.clear()
for _ in range(5):
    s.failsafe_reload_integrations(st, {})
check("kein Reload im Cooldown", reloads == [])

print("4) Nach Cooldown: weiterer Versuch, aber max. 3 insgesamt")
reloads.clear()
for versuch in (2, 3):
    st["failsafe"]["last_reload_ts"] = 0  # Cooldown abgelaufen simulieren
    st["failsafe"]["unavailable_polls"] = s.RELOAD_AFTER_UNAVAILABLE_POLLS - 1
    s.failsafe_reload_integrations(st, {})
    check(f"Versuch {versuch} ausgefuehrt", len(reloads) == (versuch - 1) * 2)
st["failsafe"]["last_reload_ts"] = 0
st["failsafe"]["unavailable_polls"] = s.RELOAD_AFTER_UNAVAILABLE_POLLS - 1
s.failsafe_reload_integrations(st, {})
check("4. Versuch blockiert (Limit)", len(reloads) == 4)
check("Versuchszaehler bei Limit", st["failsafe"]["reload_attempts"] == s.RELOAD_MAX_ATTEMPTS)

print("5) Erholung setzt Zaehler zurueck -> spaeterer Ausfall darf wieder reloaden")
s.failsafe_reload_integrations(st, HEALTHY)
check("Versuchszaehler zurueckgesetzt", st["failsafe"]["reload_attempts"] == 0)
reloads.clear()
st["failsafe"]["last_reload_ts"] = 0
for _ in range(s.RELOAD_AFTER_UNAVAILABLE_POLLS):
    s.failsafe_reload_integrations(st, {})
check("nach Erholung wieder Reload moeglich", reloads == ["bambu_lab", "spoolman"])

print("6) Einzelner unavailable Slot (Slot leer/traege) -> KEIN Reload")
reloads.clear()
st = fresh_state()
teilweise = dict(HEALTHY)
teilweise[AMS[0]] = {"state": "unavailable"}
for _ in range(5):
    s.failsafe_reload_integrations(st, teilweise)
check("kein Reload bei Einzelausfall", reloads == [])

print("7) Nie belegter Slot fehlt dauerhaft (kein AMS 2) -> KEIN Reload")
reloads.clear()
st = {"slots": {AMS[0]: {"spoolman_id": 20}}}
nur_slot1 = {AMS[0]: {"state": "Bambu PLA Silk"}}
for _ in range(5):
    s.failsafe_reload_integrations(st, nur_slot1)
check("kein Reload wenn bekannter Slot gesund ist", reloads == [])

print("8) Deaktivierbar per INTEGRATION_RELOAD_ENABLED=0")
reloads.clear()
s.INTEGRATION_RELOAD_ENABLED = False
st = fresh_state()
for _ in range(5):
    s.failsafe_reload_integrations(st, {})
check("kein Reload wenn deaktiviert", reloads == [])
s.INTEGRATION_RELOAD_ENABLED = True

print("\nERGEBNIS:", "ALLE TESTS PASS" if ok else "FEHLER")
raise SystemExit(0 if ok else 1)
