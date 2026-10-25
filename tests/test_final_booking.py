"""Regressionstests fuer die Endbuchung (Issue #43, v4.2.0).

Hintergrund: Bis v4.1.x wurde waehrend des Drucks teilweise gebucht. Bestaetigte
der Nutzer die externe Spule mitten im Druck, setzte /confirm-consumption den
Fortschritt auf 100 %; der BACKWARD-Reset setzte ihn beim naechsten Poll auf 0
zurueck und buchte alles Bisherige ein zweites Mal (Spule #26, Druecke #210,
#211, #213, #214: insgesamt rund 361 g zu viel). Druck #208 wurde nie gebucht,
weil niemand bestaetigte.

Neu: waehrend des Drucks nur beobachten, genau einmal bei FERTIG/FEHLER/
Abbruch buchen. Jedes Szenario hier laeuft end-to-end durch process_once()
gegen einen simulierten Drucker und ein simuliertes Spoolman."""
import booking_harness as h
from booking_harness import c, check

PLAN = 173.01
GCODE = h.GCODE
_counter = [0]


def new_print(start=None):
    _counter[0] += 1
    # {:02d}: ab dem 10. Aufruf sonst ungueltige Daten wie '2026-10-018' (nicht lesbarer Start)
    return h.Printer(start or f"2026-10-{_counter[0]:02d}T06:29:49+00:00")


def external_state():
    """Zustand mit der im Dashboard gewaehlten externen Spule #26."""
    return {"user_spool_overrides": {"External": 26}}


def run_external(sm, state, printer, progresses, final_status="finish", final_progress=100, plan=PLAN):
    for p in progresses:
        h.poll(state, printer.states("running", p, plan, external=True))
    h.poll(state, printer.states(final_status, final_progress, plan, external=True))


# ── 1 ────────────────────────────────────────────────────────────────
print("1) Normaler externer Druck: waehrend des Drucks KEINE Buchung, bei FERTIG genau eine")
sm = h.FakeSpoolman(h.make_spool(26))
sm.install()
state, printer = external_state(), new_print()
for p in (5, 30, 60, 90):
    h.poll(state, printer.states("running", p, PLAN, external=True))
job = h.job_of(state, printer)
check("waehrend des Drucks wurde nichts bei Spoolman gebucht", sm.use_calls == [], str(sm.use_calls))
check("Spoolman-Stand unveraendert", sm.used(26) == 0.0)
est = h.history_record(job["history_id"])["colors"][0]["grams"]
check("History zeigt die Schaetzung Plan x Fortschritt (90 %)", h.approx(est, PLAN * 0.9), str(est))
check("History-Status waehrend des Drucks: started", h.history_record(job["history_id"])["status"] == "started")
h.poll(state, printer.states("finish", 100, PLAN, external=True))
check("genau EINE Buchung ueber das volle Plangewicht", sm.applied == [(26, PLAN)], str(sm.applied))
check("History zeigt gebuchte Menge", h.approx(h.history_record(job["history_id"])["colors"][0]["grams"], PLAN))
before = list(sm.use_calls)
for _ in range(3):
    h.poll(state, printer.states("finish", 100, PLAN, external=True))
check("weitere Polls im Zustand 'finish' buchen nichts mehr", sm.use_calls == before, str(sm.use_calls))

# ── 2 ────────────────────────────────────────────────────────────────
print("2) Kern von #43: Bestaetigung mitten im Druck + Fortschritts-Aussetzer => keine Doppelbuchung")
sm = h.FakeSpoolman(h.make_spool(26))
sm.install()
state, printer = external_state(), new_print()
h.poll(state, printer.states("running", 40, PLAN, external=True))
h.poll(state, printer.states("running", 82, PLAN, external=True))
job = h.job_of(state, printer)
key = f"{printer.gcode}|{printer.start}"
ok, msg = c.confirm_final_booking(state, key, 26)
check("Bestaetigen mitten im Druck wird abgelehnt (nichts ausstehend)", ok is False, msg)
check("... und bucht nichts", sm.use_calls == [], str(sm.use_calls))
h.poll(state, printer.states("running", None, PLAN, external=True))     # Sensor 'unavailable'
h.poll(state, printer.states("running", 0, PLAN, external=True))        # Sensor springt auf 0
check("max_progress faellt bei Sensor-Aussetzern nicht", h.approx(job["max_progress"], 0.82, 0.001), str(job["max_progress"]))
h.poll(state, printer.states("running", 97, PLAN, external=True))
h.poll(state, printer.states("finish", 100, PLAN, external=True))
check("am Ende genau EINE Buchung = Plangewicht (nicht 1,8-fach)", sm.applied == [(26, PLAN)], str(sm.applied))

# ── 3 ────────────────────────────────────────────────────────────────
print("3) FEHLER bei 40 %: gebucht wird Plan x hoechster Fortschritt, auch wenn der Sensor am Ende 0 meldet")
sm = h.FakeSpoolman(h.make_spool(26))
sm.install()
state, printer = external_state(), new_print()
h.poll(state, printer.states("running", 20, PLAN, external=True))
h.poll(state, printer.states("running", 40, PLAN, external=True))
h.poll(state, printer.states("failed", 0, PLAN, external=True))
check("Buchung = 40 % des Plangewichts", sm.applied == [(26, round(PLAN * 0.4, 2))], str(sm.applied))
job = h.job_of(state, printer)
check("History-Status failed", h.history_record(job["history_id"])["status"] == "failed")

# ── 4 ────────────────────────────────────────────────────────────────
print("4) Abbruch (Status wechselt auf idle, 2x bestaetigt): Plan x hoechster Fortschritt")
sm = h.FakeSpoolman(h.make_spool(26))
sm.install()
state, printer = external_state(), new_print()
h.poll(state, printer.states("running", 30, PLAN, external=True))
h.poll(state, printer.states("running", 63, PLAN, external=True))
h.poll(state, printer.states("idle", 0, 0.0))
check("nach dem ersten idle-Poll noch keine Buchung (Debounce)", sm.use_calls == [], str(sm.use_calls))
h.poll(state, printer.states("idle", 0, 0.0))
check("nach dem zweiten idle-Poll: Buchung = 63 %", sm.applied == [(26, round(PLAN * 0.63, 2))], str(sm.applied))
job = h.job_of(state, printer)
check("History-Status cancelled", h.history_record(job["history_id"])["status"] == "cancelled")
h.poll(state, printer.states("idle", 0, 0.0))
check("weitere idle-Polls buchen nichts mehr", len(sm.applied) == 1, str(sm.applied))

# ── 5 ────────────────────────────────────────────────────────────────
print("5) AMS-Spule mit RFID: automatische Endbuchung ohne Bestaetigung")
sm = h.FakeSpoolman(h.make_spool(33, "Bambu PLA Basic Black", tag_uid="ABCD1234ABCD1234"))
sm.install()
state, printer = {}, new_print()
ams = {"AMS 1 Tray 3": ("ABCD1234ABCD1234", 95.5)}
h.poll(state, printer.states("running", 20, 95.5, ams=ams))
h.poll(state, printer.states("running", 70, 95.5, ams=ams))
check("waehrend des Drucks keine Buchung", sm.use_calls == [])
h.poll(state, printer.states("finish", 100, 95.5, ams=ams))
check("Endbuchung 95.5 g auf Spule 33", sm.applied == [(33, 95.5)], str(sm.applied))

# ── 6 ────────────────────────────────────────────────────────────────
print("6) Spulenwechsel mitten im Druck (AMS): Verbrauch bleibt der jeweiligen Spule zugeordnet")
sm = h.FakeSpoolman(h.make_spool(5, "Alt", tag_uid="AAAA000000000001"),
                    h.make_spool(6, "Neu", tag_uid="BBBB000000000002"))
sm.install()
state, printer = {}, new_print()
old_ams = {"AMS 1 Tray 1": ("AAAA000000000001", 100.0)}
new_ams = {"AMS 1 Tray 1": ("BBBB000000000002", 100.0)}
h.poll(state, printer.states("running", 10, 100.0, ams=old_ams))
h.poll(state, printer.states("running", 50, 100.0, ams=old_ams))
h.poll(state, printer.states("running", 60, 100.0, ams=new_ams))
h.poll(state, printer.states("finish", 100, 100.0, ams=new_ams))
check("Spule 5 bekommt 50 g, Spule 6 bekommt 50 g",
      sorted(sm.applied) == [(5, 50.0), (6, 50.0)], str(sm.applied))

# ── 7 ────────────────────────────────────────────────────────────────
print("7) Externe Spule ohne Zuordnung: Druck endet -> wartet auf Bestaetigung, dann genau eine Buchung (#208)")
sm = h.FakeSpoolman(h.make_spool(26))
sm.install()
state, printer = {}, new_print()          # KEIN Override, kein RFID
h.poll(state, printer.states("running", 50, 21.63, external=True))
h.poll(state, printer.states("finish", 100, 21.63, external=True))
job = h.job_of(state, printer)
items = job["final_booking"]["items"]
check("Posten wartet auf den Nutzer (awaiting_user)", [i["status"] for i in items] == [c.ITEM_AWAITING_USER], str(items))
check("keine Buchung ohne Bestaetigung", sm.use_calls == [])
check("History zeigt den Verbrauch bereits (nicht 0.0 g)",
      h.approx(h.history_record(job["history_id"])["colors"][0]["grams"], 21.63))
for _ in range(3):
    h.poll(state, printer.states("finish", 100, 21.63, external=True))
check("Polls ohne Bestaetigung buchen weiterhin nichts, der Posten bleibt erhalten",
      sm.use_calls == [] and items[0]["status"] == c.ITEM_AWAITING_USER)
end_before = h.history_record(job["history_id"])["end"]
key = f"{printer.gcode}|{printer.start}"
ok, msg = c.confirm_final_booking(state, key, 26)
check("Bestaetigen bucht", ok and sm.applied == [(26, 21.63)], f"{ok} {msg} {sm.applied}")
rec = h.history_record(job["history_id"])
check("History zeigt Spule 26", rec["colors"][0]["spool_id"] == 26, str(rec["colors"]))
check("Druckende bleibt beim Bestaetigen unveraendert", rec["end"] == end_before, f"{end_before} -> {rec['end']}")
ok2, _ = c.confirm_final_booking(state, key, 26)
check("zweites Bestaetigen bucht nicht erneut", ok2 is False and sm.applied == [(26, 21.63)], str(sm.applied))

# ── 8 ────────────────────────────────────────────────────────────────
print("8) Spoolman beim Druckende nicht erreichbar: Buchung wird spaeter nachgeholt (auch bei idle)")
sm = h.FakeSpoolman(h.make_spool(26))
sm.install()
state, printer = external_state(), new_print()
h.poll(state, printer.states("running", 80, PLAN, external=True))
sm.down = True
h.poll(state, printer.states("finish", 100, PLAN, external=True))
check("nichts gebucht, solange Spoolman down ist", sm.applied == [])
job = h.job_of(state, printer)
check("Posten bleibt offen", job["final_booking"]["items"][0]["status"] == c.ITEM_OPEN)
sm.down = False
h.poll(state, printer.states("idle", 0, 0.0))     # Drucker laengst idle
check("beim naechsten Poll nachgeholt, genau einmal", sm.applied == [(26, PLAN)], str(sm.applied))

# ── 9 ────────────────────────────────────────────────────────────────
print("9) Startzeit springt in der ersten Minute (06:29:00 -> 06:29:49) und der Sensor liefert zuerst das Gewicht des Vorgaengers")
sm = h.FakeSpoolman(h.make_spool(26))
sm.install()
state = external_state()
printer = h.Printer("2026-10-07T06:29:00+00:00")
h.poll(state, printer.states("running", 0, 151.47, external=True))                           # Wert des Vorgaengerdrucks
h.poll(state, printer.states("running", 1, 173.11, external=True, start="2026-10-07T06:29:49+00:00"))
h.poll(state, printer.states("running", 50, 173.11, external=True, start="2026-10-07T06:29:49+00:00"))
check("nur EIN Job fuer den Druck", len(state["jobs"]) == 1, str(list(state["jobs"])))
h.poll(state, printer.states("finish", 100, 173.11, external=True, start="2026-10-07T06:29:49+00:00"))
check("gebucht wird der korrigierte Planwert 173.11 g (nicht 151.47)", sm.applied == [(26, 173.11)], str(sm.applied))
records = [r for r in c.load_history()["records"] if r["gcode"] == GCODE and r["start"].startswith("2026-10-07T06:29")]
check("genau ein History-Eintrag", len(records) == 1, str(len(records)))

# ── 10 ───────────────────────────────────────────────────────────────
print("10) Zustand verloren, Drucker meldet noch 'finish' des alten Drucks: nicht erneut buchen")
sm = h.FakeSpoolman(h.make_spool(26))
sm.install()
printer = new_print()
state = external_state()
run_external(sm, state, printer, [30, 70])
check("Ausgangslage: einmal gebucht", sm.applied == [(26, PLAN)])
state_lost = external_state()                       # consumption_state.json weg
h.poll(state_lost, printer.states("finish", 100, PLAN, external=True))
check("keine zweite Buchung trotz verlorenem Zustand", sm.applied == [(26, PLAN)], str(sm.applied))

# ── 11 ───────────────────────────────────────────────────────────────
print("11) Update waehrend eines laufenden Drucks (Job im alten Format): bereits Gebuchtes wird nicht erneut gebucht")
sm = h.FakeSpoolman(h.make_spool(26, used=69.2))     # alte Version hat 40 % schon gebucht
sm.install()
printer = new_print()
key = f"{printer.gcode}|{printer.start}"
with c._history_lock:
    history = c.load_history()
    history_id = history["next_id"]
    history["next_id"] += 1
    c.save_history(history)
state = {
    "user_spool_overrides": {"External": 26},
    "external_seen_jobs": {key: True},
    "jobs": {key: {
        "gcode": printer.gcode, "start": printer.start, "done": False, "history_id": history_id,
        "task_name": "Altjob", "external_seen": True,
        "tray_weights": {"External": 173.0}, "tray_fractions": {"External": 0.4},
        "colors_used": {"External": [dict(c._new_color_entry(26, sm.spools[26]["filament"], "External"), grams=69.2)]},
        "pending_consumption": [],
    }},
}
h.poll(state, printer.states("running", 40, 173.0, external=True))
h.poll(state, printer.states("running", 80, 173.0, external=True))
h.poll(state, printer.states("finish", 100, 173.0, external=True))
check("nur die restlichen 60 % werden gebucht", sm.applied == [(26, round(173.0 * 0.6, 2))], str(sm.applied))
check("History zeigt die Gesamtmenge 173 g",
      h.approx(h.history_record(history_id)["colors"][0]["grams"], 173.0), str(h.history_record(history_id)["colors"]))

# ── 12 ───────────────────────────────────────────────────────────────
print("12) Aufraeumen ueber process_once: Job mit wartender Buchung ueberlebt die 50-Job-Grenze")
sm = h.FakeSpoolman(h.make_spool(26))
sm.install()
state = {"jobs": {"alt|1": {"done": True, "gcode": "alt", "start": "1", "final_booking": {
    "status": "finish", "fraction": 1.0, "items": [
        {"tray_key": "External", "segment": 0, "spool_id": None, "grams": 5.0,
         "status": c.ITEM_AWAITING_USER}]}}}}
for i in range(60):
    state["jobs"][f"neu|{i}"] = {"done": True, "gcode": "neu", "start": str(i)}
printer = new_print()
h.poll(state, printer.states("running", 10, PLAN, external=True))
check("Job mit wartendem Posten wurde NICHT entfernt", "alt|1" in state["jobs"])
check("gebuchte/leere alte Jobs wurden aufgeraeumt", "neu|0" not in state["jobs"] and len(state["jobs"]) <= 52,
      str(len(state["jobs"])))

# ── 13 ───────────────────────────────────────────────────────────────
print("13) Das Dashboard verwirft die Zuordnung der externen Spule, sobald sie inaktiv ist - die Buchung bleibt auf der im Druck erkannten Spule")
sm = h.FakeSpoolman(h.make_spool(26))
sm.install()
state, printer = external_state(), new_print()
h.poll(state, printer.states("running", 40, PLAN, external=True))
h.poll(state, printer.states("running", 95, PLAN, external=True))
state["user_spool_overrides"] = {}                     # wie _sync_dashboard_selects nach Druckende
h.poll(state, printer.states("finish", 100, 
                             PLAN, external=False, ams={}))
check("trotzdem automatisch auf Spule 26 gebucht", sm.applied == [(26, PLAN)], str(sm.applied))

# ── 14 ───────────────────────────────────────────────────────────────
print("14) Veralteter Start-Sensor beim naechsten Druck: der abgeschlossene Job wird nicht veraendert")
sm = h.FakeSpoolman(h.make_spool(26))
sm.install()
state, printer = external_state(), new_print()
run_external(sm, state, printer, [50])
job = h.job_of(state, printer)
weights_before = dict(job["tray_weights"])
h.poll(state, printer.states("running", 3, 999.0, external=True))   # naechster Druck, Start-Sensor noch alt
check("Planwerte des fertigen Jobs unveraendert", job["tray_weights"] == weights_before, str(job["tray_weights"]))
check("keine zusaetzliche Buchung", sm.applied == [(26, PLAN)], str(sm.applied))

# ── 15 ───────────────────────────────────────────────────────────────
print("15) Fehler bei der Nachbereitung (Leer-Warnung) gefaehrdet die Buchung nicht")
sm = h.FakeSpoolman(h.make_spool(26, used=950.0, initial=1000.0))     # wird mit dieser Buchung leer
sm.install()
original_warn = c._warn_if_spool_empty
c._warn_if_spool_empty = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("Benachrichtigung kaputt"))
state, printer = external_state(), new_print()
run_external(sm, state, printer, [50], plan=80.0)
c._warn_if_spool_empty = original_warn
check("Buchung steht trotzdem als gebucht", h.job_of(state, printer)["final_booking"]["items"][0]["status"] == c.ITEM_BOOKED)
check("genau eine Buchung", sm.applied == [(26, 80.0)], str(sm.applied))

# ── 16 ───────────────────────────────────────────────────────────────
print("16) Spulenerkennung scheitert den ganzen Druck ueber: der Verbrauch geht nicht still verloren")
sm = h.FakeSpoolman(h.make_spool(26))
sm.install()
state, printer = external_state(), new_print()
original_resolve = c.resolve_spool_for_tray
c.resolve_spool_for_tray = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("Spoolman weg"))
h.poll(state, printer.states("running", 30, PLAN, external=True))
h.poll(state, printer.states("running", 80, PLAN, external=True))
h.poll(state, printer.states("finish", 100, PLAN, external=True))
c.resolve_spool_for_tray = original_resolve
items = h.job_of(state, printer)["final_booking"]["items"]
check("Posten existiert und wartet auf die Spulenwahl", [(i["status"], i["grams"]) for i in items] == [(c.ITEM_AWAITING_USER, PLAN)], str(items))
key = f"{printer.gcode}|{printer.start}"
ok, _ = c.confirm_final_booking(state, key, 26)
check("nach Bestaetigung genau eine Buchung", ok and sm.applied == [(26, PLAN)], str(sm.applied))

# ── 17 ───────────────────────────────────────────────────────────────
print("17) HA faellt mitten im Druck aus (alle Sensoren unavailable), danach idle: der Druck wird nicht vergessen")
sm = h.FakeSpoolman(h.make_spool(26))
sm.install()
state, printer = external_state(), new_print()
h.poll(state, printer.states("running", 30, PLAN, external=True))
h.poll(state, printer.states("running", 60, PLAN, external=True))
gone = {c.PRINT_STATUS_ENTITY: {"state": "unavailable"}, c.GCODE_ENTITY: {"state": "unavailable"},
        c.STARTTIME_ENTITY: {"state": "unavailable"}}
h.poll(state, gone)
h.poll(state, gone)
check("waehrend des Ausfalls keine Buchung", sm.use_calls == [])
h.poll(state, printer.states("idle", 0, 0.0))
h.poll(state, printer.states("idle", 0, 0.0))
check("nach idle: Abbruch gebucht = 60 % (nicht verloren)", sm.applied == [(26, round(PLAN * 0.6, 2))], str(sm.applied))

# ── 18 ───────────────────────────────────────────────────────────────
print("18) Folgedruck startet, bevor der idle-Abbruch bestaetigt wurde: der alte Druck wird trotzdem gebucht")
sm = h.FakeSpoolman(h.make_spool(26))
sm.install()
state = external_state()
first, second = new_print(), new_print()
h.poll(state, first.states("running", 45, PLAN, external=True))
h.poll(state, first.states("idle", 0, 0.0))                              # 1/2
h.poll(state, second.states("running", 2, 50.0, external=True))           # neuer Druck
check("alter Druck wurde bei Start des neuen abgeschlossen und gebucht",
      sm.applied == [(26, round(PLAN * 0.45, 2))], str(sm.applied))
h.poll(state, second.states("running", 90, 50.0, external=True))
h.poll(state, second.states("finish", 100, 50.0, external=True))
check("neuer Druck separat gebucht", sm.applied[-1] == (26, 50.0) and len(sm.applied) == 2, str(sm.applied))

# ── 19 ───────────────────────────────────────────────────────────────
print("19) Sensor zeigt direkt nach Druckstart noch die 100 % des Vorgaengers: Altwert wird verworfen")
sm = h.FakeSpoolman(h.make_spool(26))
sm.install()
state, printer = external_state(), new_print()
h.poll(state, printer.states("prepare", 100, PLAN, external=True))
h.poll(state, printer.states("running", 3, PLAN, external=True))
h.poll(state, printer.states("failed", 3, PLAN, external=True))
check("gebucht werden 3 % (nicht 100 %)", sm.applied == [(26, round(PLAN * 0.03, 2))], str(sm.applied))

# ── 20 ───────────────────────────────────────────────────────────────
print("20) Fehler beim Abschluss des Verlaufseintrags blockiert die Buchung nicht und wird nachgeholt")
sm = h.FakeSpoolman(h.make_spool(26))
sm.install()
state, printer = external_state(), new_print()
h.poll(state, printer.states("running", 70, PLAN, external=True))
real_write, calls = c._write_history_record, {"n": 0}


def flaky_write(job, status):
    if status == "finish" and calls["n"] == 0:
        calls["n"] += 1
        raise RuntimeError("database is locked (Test)")
    return real_write(job, status)


c._write_history_record = flaky_write
h.poll(state, printer.states("finish", 100, PLAN, external=True))
check("Buchung steht trotz Verlaufsfehler", sm.applied == [(26, PLAN)], str(sm.applied))
job = h.job_of(state, printer)
check("Verlaufseintrag ist noch nicht abgeschlossen", not job.get("history_finalized"))
h.poll(state, printer.states("finish", 100, PLAN, external=True))
c._write_history_record = real_write
check("naechster Poll holt den Abschluss nach", job.get("history_finalized") is True)
check("History-Status finish", h.history_record(job["history_id"])["status"] == "finish")
check("keine zweite Buchung", sm.applied == [(26, PLAN)], str(sm.applied))

# ── 21 ───────────────────────────────────────────────────────────────
print("21) Zwei wartende Posten (externe Spule + AMS-Slot ohne Erkennung): jeder wird einzeln zugeordnet")
sm = h.FakeSpoolman(h.make_spool(26), h.make_spool(33, "Bambu Black"))
sm.install()
state, printer = {}, new_print()
mixed = {"AMS 1 Tray 1": (h.EMPTY_UID, 40.0)}
h.poll(state, printer.states("running", 50, 100.0, ams=mixed))   # AMS ohne RFID/Zuordnung
job = h.job_of(state, printer)
job["external_seen"] = True
state.setdefault("external_seen_jobs", {})[f"{printer.gcode}|{printer.start}"] = True
h.poll(state, printer.states("running", 60, 100.0, external=True))
h.poll(state, printer.states("finish", 100, 100.0, external=True))
items = {i["tray_key"]: i for i in job["final_booking"]["items"]}
check("beide Posten warten", {k: v["status"] for k, v in items.items()} == {
    "AMS 1 Tray 1": c.ITEM_AWAITING_USER, "External": c.ITEM_AWAITING_USER}, str({k: v["status"] for k, v in items.items()}))
key = f"{printer.gcode}|{printer.start}"
ok, _ = c.confirm_final_booking(state, key, 26, tray_key="External")
check("nur der externe Posten wird gebucht", ok and sm.applied == [(26, 60.0)]
      and items["AMS 1 Tray 1"]["status"] == c.ITEM_AWAITING_USER, f"{sm.applied}")
ok, _ = c.confirm_final_booking(state, key, 33, tray_key="AMS 1 Tray 1")
check("der AMS-Posten geht auf die andere Spule", ok and sorted(sm.applied) == [(26, 60.0), (33, 40.0)], str(sm.applied))

# ── 22 ───────────────────────────────────────────────────────────────
print("22) Komplett wie die Hauptschleife (Zustand je Poll laden/speichern): Ablauf inkl. Neustart und Timeout")
sm = h.FakeSpoolman(h.make_spool(26))
sm.install()
printer = new_print()
state0 = external_state()
c.save_state(state0)
for p in (10, 50, 90):
    h.poll_persisted(printer.states("running", p, PLAN, external=True))
check("nach 3 Polls (mit Neuladen) nichts gebucht", sm.use_calls == [])
sm.fail_use = ["after_apply"]
h.poll_persisted(printer.states("finish", 100, PLAN, external=True))
h.poll_persisted(printer.states("finish", 100, PLAN, external=True))
h.poll_persisted(printer.states("idle", 0, 0.0))
check("trotz Timeout und Neuladen genau eine Buchung", sm.used(26) == PLAN and len(sm.use_calls) == 1, f"{sm.use_calls} {sm.used(26)}")
final = c.load_state()["jobs"][f"{printer.gcode}|{printer.start}"]
check("persistierter Zustand: Posten gebucht, Job abgeschlossen",
      final["done"] and final["final_booking"]["items"][0]["status"] == c.ITEM_BOOKED)

# ── 23 ───────────────────────────────────────────────────────────────
print("23) idle-Abbruch erfasst jeden offenen Job, auch wenn current_job_key nicht mehr auf ihn zeigt")
sm = h.FakeSpoolman(h.make_spool(26))
sm.install()
state, printer = external_state(), new_print()
h.poll(state, printer.states("running", 55, PLAN, external=True))
state["current_job_key"] = "verwaist|verwaist"          # Verweis ging verloren
h.poll(state, printer.states("idle", 0, 0.0))
h.poll(state, printer.states("idle", 0, 0.0))
check("offener Job wurde trotzdem abgebrochen gebucht (55 %)", sm.applied == [(26, round(PLAN * 0.55, 2))], str(sm.applied))

# ── 24 ───────────────────────────────────────────────────────────────
print("24) Druck #215: Aktiv-Sensor der externen Spule steht auf 'on', gedruckt wird aber ueber den AMS - keine Buchung auf die externe Spule")
sm = h.FakeSpoolman(h.make_spool(26), h.make_spool(2, "Bambu PLA Matte Desert Tan", tag_uid="4C102E6927F347E6A8DBBE31DBF5BD03"))
sm.install()
state, printer = external_state(), new_print()
a4 = {"AMS 1 Tray 4": ("4C102E6927F347E6A8DBBE31DBF5BD03", 14.95)}
h.poll(state, printer.states("running", 1, 14.95, external=True))                 # AMS-Gewichte noch nicht da
job = h.job_of(state, printer)
check("anfangs steht das ganze Gewicht als 'extern' da (Zwischenzustand)", job["tray_weights"] == {"External": 14.95}, str(job["tray_weights"]))
h.poll(state, printer.states("running", 10, 14.95, ams=a4, ext_active=True))      # jetzt kommen die AMS-Gewichte
check("sobald die AMS-Gewichte da sind, entfaellt der externe Anteil", job["tray_weights"] == {"AMS 1 Tray 4": 14.95}, str(job["tray_weights"]))
h.poll(state, printer.states("running", 60, 14.95, ams=a4, ext_active=True))
h.poll(state, printer.states("finish", 100, 14.95, ams=a4, ext_active=True))
check("gebucht wird NUR auf Spule 2 (nicht zusaetzlich auf die externe Spule 26)", sm.applied == [(2, 14.95)], str(sm.applied))
colors = h.history_record(job["history_id"])["colors"]
check("History zeigt genau eine Spule (#2)", [(col["spool_id"], col["tray_key"]) for col in colors] == [(2, "AMS 1 Tray 4")], str(colors))

# ── 25 ───────────────────────────────────────────────────────────────
print("25) Gemischter Druck (AMS + externe Spule): der externe Anteil bleibt erhalten")
sm = h.FakeSpoolman(h.make_spool(26), h.make_spool(2, "Desert Tan", tag_uid="4C102E6927F347E6A8DBBE31DBF5BD03"))
sm.install()
state, printer = external_state(), new_print()
h.poll(state, printer.states("running", 5, 100.0, ams={"AMS 1 Tray 4": ("4C102E6927F347E6A8DBBE31DBF5BD03", 40.0)}))
h.poll(state, printer.states("running", 40, 100.0, external=True))                # extern aktiv: Sensor zeigt nur Gesamtgewicht
job = h.job_of(state, printer)
check("AMS 40 g + extern 60 g", job["tray_weights"] == {"AMS 1 Tray 4": 40.0, "External": 60.0}, str(job["tray_weights"]))
h.poll(state, printer.states("finish", 100, 100.0, external=True))
check("beide Spulen gebucht: 40 g auf #2, 60 g auf #26", sorted(sm.applied) == [(2, 40.0), (26, 60.0)], str(sm.applied))

h.finish()
