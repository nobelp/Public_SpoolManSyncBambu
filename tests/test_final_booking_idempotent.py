"""Regressionstests fuer Issue #49: Doppelbuchung bei Spoolman-Timeout.

Spoolman kann eine Buchung (PUT /spool/<id>/use) anwenden und trotzdem nicht
rechtzeitig antworten. Der Client sieht einen Timeout, die Buchung ist aber
schon drin - ein blindes Wiederholen bucht doppelt (Spule #31: 5 g zu viel).
Die Endbuchung verifiziert deshalb vor jeder Wiederholung used_weight.

Zusaetzlich: Einheitentests der reinen Hilfsfunktionen (Segmente, Zuordnung,
Bestaetigungsregel, Job-Schluessel)."""
import booking_harness as h
from booking_harness import c, check

PLAN = 100.0
_n = [0]


def setup(spool=None):
    _n[0] += 1
    sm = h.FakeSpoolman(spool or h.make_spool(31, tag_uid="AAAA111122223333"))
    sm.install()
    printer = h.Printer(f"2026-10-1{_n[0]}T08:00:00+00:00")
    ams = {"AMS 1 Tray 2": ("AAAA111122223333", PLAN)}
    return sm, printer, ams, {}


def to_finish(state, printer, ams):
    h.poll(state, printer.states("running", 50, PLAN, ams=ams))
    h.poll(state, printer.states("finish", 100, PLAN, ams=ams))


# ── 1 ────────────────────────────────────────────────────────────────
print("1) Timeout NACH angewendeter Buchung: naechster Poll verifiziert und bucht NICHT erneut")
sm, printer, ams, state = setup()
sm.fail_use = ["after_apply"]
to_finish(state, printer, ams)
check("Buchung wurde einmal angewendet", sm.applied == [(31, PLAN)], str(sm.applied))
item = h.job_of(state, printer)["final_booking"]["items"][0]
check("Posten bleibt offen (Antwort fehlte)", item["status"] == c.ITEM_OPEN, item["status"])
check("u0 und attempted wurden VOR dem PUT festgehalten", item.get("attempted") and item.get("u0") == 0.0, str(item))
h.poll(state, printer.states("finish", 100, PLAN, ams=ams))
check("keine zweite Buchung (Issue #49)", sm.applied == [(31, PLAN)] and len(sm.use_calls) == 1, str(sm.use_calls))
check("Posten ist jetzt verbucht (verifiziert)", item["status"] == c.ITEM_BOOKED and item.get("verified_after_error"), str(item))
check("Spoolman-Stand = genau ein Plangewicht", sm.used(31) == PLAN, str(sm.used(31)))

# ── 2 ────────────────────────────────────────────────────────────────
print("2) Timeout VOR dem Anwenden: naechster Poll bucht erneut - genau einmal")
sm, printer, ams, state = setup()
sm.fail_use = ["before_apply"]
to_finish(state, printer, ams)
check("noch nichts angewendet", sm.applied == [])
h.poll(state, printer.states("finish", 100, PLAN, ams=ams))
check("Wiederholung bucht genau einmal", sm.applied == [(31, PLAN)], str(sm.applied))
h.poll(state, printer.states("finish", 100, PLAN, ams=ams))
check("danach nichts mehr", len(sm.use_calls) == 2 and sm.used(31) == PLAN, str(sm.use_calls))

# ── 3 ────────────────────────────────────────────────────────────────
print("3) Mehrere Timeouts hintereinander (erst angewendet, dann nicht, dann ok)")
sm, printer, ams, state = setup()
sm.fail_use = ["after_apply"]
to_finish(state, printer, ams)
sm.down = True
h.poll(state, printer.states("finish", 100, PLAN, ams=ams))
check("waehrend Spoolman down ist: kein PUT-Versuch (Verifikation nicht moeglich)",
      len(sm.use_calls) == 1, str(sm.use_calls))
sm.down = False
h.poll(state, printer.states("finish", 100, PLAN, ams=ams))
check("nach Wiederkehr: verifiziert, keine Doppelbuchung", sm.used(31) == PLAN and len(sm.use_calls) == 1, str(sm.use_calls))

# ── 4 ────────────────────────────────────────────────────────────────
print("4) Container-Neustart zwischen angewendeter Buchung und Antwort")
sm, printer, ams, state = setup()
sm.fail_use = ["after_apply"]
to_finish(state, printer, ams)
restarted = c.load_state()           # Zustand frisch von der Platte, wie nach docker restart
check("attempted/u0 wurden persistiert", restarted["jobs"][f"{printer.gcode}|{printer.start}"]["final_booking"]["items"][0].get("attempted"))
h.poll(restarted, printer.states("idle", 0, 0.0))
check("nach Neustart keine Doppelbuchung", sm.used(31) == PLAN and len(sm.use_calls) == 1, str(sm.use_calls))

# ── 5 ────────────────────────────────────────────────────────────────
print("5) Andere Buchungen auf derselben Spule zwischen Timeout und Wiederholung")
print("   (bekannte Grenze: eine Fremdbuchung >= der eigenen Menge laesst sich nicht unterscheiden)")
sm, printer, ams, state = setup()
sm.fail_use = ["before_apply"]
to_finish(state, printer, ams)
sm.spools[31]["used_weight"] += 40.0          # jemand bucht manuell 40 g, unsere Buchung kam NICHT an
h.poll(state, printer.states("finish", 100, PLAN, ams=ams))
check("kleinere Fremdbuchung (40 g < 100 g) wird nicht mit unserer verwechselt: wir buchen trotzdem 100 g",
      sm.applied == [(31, PLAN)] and sm.used(31) == 140.0, f"{sm.applied} {sm.used(31)}")

# ── 6 ────────────────────────────────────────────────────────────────
print("6) PUT /use verwendet den grosszuegigen Timeout (Spoolman antwortet unter NAS-Last langsam)")


class _Resp:
    def raise_for_status(self):
        pass

    def json(self):
        return {}


seen = {}


class _Http:
    def put(self, url, **kwargs):
        seen.update(kwargs)
        return _Resp()


original_http = c._http
c._http = _Http()
h.REAL_SM_USE(31, 1.0)
c._http = original_http
check("Timeout fuer PUT /use = SM_USE_TIMEOUT_S (30 s)", seen.get("timeout") == c.SM_USE_TIMEOUT_S == 30.0, str(seen))

# ── 7 ────────────────────────────────────────────────────────────────
print("7) Einheitentests: Zuordnungsregel, Segmente, Job-Schluessel")
check("AMS-Slot braucht nie eine Bestaetigung", not c._needs_user_confirmation("AMS 1 Tray 3", None))
check("Externe Spule mit RFID: automatisch", not c._needs_user_confirmation("External", "ABCD1234"))
check("Externe Spule aus Dashboard-Auswahl (USER_): automatisch", not c._needs_user_confirmation("External", "USER_20261007_062949_26"))
check("Externe Spule mit MANUAL_-Fallback-UID: Bestaetigung", c._needs_user_confirmation("External", "MANUAL_1_ext"))
check("Externe Spule ohne UID: Bestaetigung", c._needs_user_confirmation("External", None))

job = {"max_progress": 0.0, "segments": {}, "tray_weights": {"T": 100.0}}
spool_a, spool_b = h.make_spool(1, "A"), h.make_spool(2, "B")
c._track_tray_spool(job, "T", None, None)
check("Platzhalter ohne Spule angelegt", job["segments"]["T"][0]["spool_id"] is None)
job["max_progress"] = 0.2
c._track_tray_spool(job, "T", spool_a, "UID_A")
check("Platzhalter wird rueckwirkend gefuellt (kein neuer Abschnitt)",
      len(job["segments"]["T"]) == 1 and job["segments"]["T"][0]["spool_id"] == 1)
job["max_progress"] = 0.5
c._track_tray_spool(job, "T", None, None)
check("nicht erkannte Spule (Sensor-Aussetzer) behaelt die letzte", job["segments"]["T"][-1]["spool_id"] == 1)
c._track_tray_spool(job, "T", spool_b, "UID_B")
segs = job["segments"]["T"]
check("Spulenwechsel schliesst den Abschnitt bei 50 % und oeffnet einen neuen",
      len(segs) == 2 and segs[0]["to"] == 0.5 and segs[1]["from"] == 0.5 and segs[1]["to"] is None)
check("Gramm je Abschnitt: 50 g und (offen bis 80 %) 30 g",
      c._segment_grams(job, "T", segs[0], 0.8) == 50.0 and c._segment_grams(job, "T", segs[1], 0.8) == 30.0)

jobs = {"g|2026-10-07T06:29:00+00:00": {"gcode": "g", "start": "2026-10-07T06:29:00+00:00", "done": False},
        "g|2026-10-06T06:29:00+00:00": {"gcode": "g", "start": "2026-10-06T06:29:00+00:00", "done": True}}
check("Startzeit +49 s: laufender Job wird weiterverwendet",
      c._resolve_job_key(jobs, "g", "2026-10-07T06:29:49+00:00") == "g|2026-10-07T06:29:00+00:00")
check("exakter Treffer hat Vorrang", c._resolve_job_key(jobs, "g", "2026-10-06T06:29:00+00:00") == "g|2026-10-06T06:29:00+00:00")
jobs["g|2026-10-07T06:29:00+00:00"]["done"] = True
check("abgeschlossener Job wird NIE unscharf gematcht (neuer Druck binnen 90 s)",
      c._resolve_job_key(jobs, "g", "2026-10-07T06:29:49+00:00") == "g|2026-10-07T06:29:49+00:00")
check("anderer gcode: neuer Job", c._resolve_job_key({"a|2026-10-07T06:29:00+00:00": {"gcode": "a", "start": "2026-10-07T06:29:00+00:00", "done": False}},
                                                      "b", "2026-10-07T06:29:10+00:00") == "b|2026-10-07T06:29:10+00:00")

# ── 8 ────────────────────────────────────────────────────────────────
print("8) Wiederholung erst nach der Mindestwartezeit (Spoolman kann den PUT nach dem Client-Timeout noch verarbeiten)")
sm, printer, ams, state = setup()
sm.fail_use = ["before_apply"]
to_finish(state, printer, ams)
c.BOOKING_RETRY_MIN_S = 3600.0
h.poll(state, printer.states("finish", 100, PLAN, ams=ams))
check("vor Ablauf der Wartezeit wird NICHT erneut gesendet", len(sm.use_calls) == 1, str(sm.use_calls))
c.BOOKING_RETRY_MIN_S = 0.0
h.poll(state, printer.states("finish", 100, PLAN, ams=ams))
check("danach genau eine Wiederholung", sm.applied == [(31, PLAN)] and len(sm.use_calls) == 2, str(sm.use_calls))

# ── 9 ────────────────────────────────────────────────────────────────
print("9) Zwei Posten auf DERSELBEN Spule: ein ungeklaerter Posten sperrt den zweiten")
sm = h.FakeSpoolman(h.make_spool(31, tag_uid="AAAA111122223333"))
sm.install()
printer = h.Printer("2026-10-19T08:00:00+00:00")
two = {"AMS 1 Tray 1": ("AAAA111122223333", 30.0), "AMS 1 Tray 2": ("AAAA111122223333", 50.0)}
state = {}
sm.fail_use = ["before_apply"]           # erster PUT (30 g) kommt nicht an
h.poll(state, printer.states("running", 50, 80.0, ams=two))
h.poll(state, printer.states("finish", 100, 80.0, ams=two))
check("der zweite Posten (50 g) wurde NICHT gesendet, solange der erste ungeklaert ist",
      sm.use_calls == [(31, 30.0)], str(sm.use_calls))
h.poll(state, printer.states("finish", 100, 80.0, ams=two))
check("danach beide genau einmal: 30 g und 50 g", sorted(sm.applied) == [(31, 30.0), (31, 50.0)] and sm.used(31) == 80.0, f"{sm.applied} {sm.used(31)}")

sm = h.FakeSpoolman(h.make_spool(31, tag_uid="AAAA111122223333"))
sm.install()
printer = h.Printer("2026-10-20T08:00:00+00:00")
state = {}
sm.fail_use = ["after_apply"]            # erster PUT kommt an, Antwort fehlt
h.poll(state, printer.states("running", 50, 80.0, ams=two))
h.poll(state, printer.states("finish", 100, 80.0, ams=two))
h.poll(state, printer.states("finish", 100, 80.0, ams=two))
check("auch mit angewendetem, unbeantwortetem ersten PUT: jeder Posten genau einmal, Summe 80 g",
      sm.used(31) == 80.0 and sorted(sm.applied) == [(31, 30.0), (31, 50.0)], f"{sm.applied} {sm.used(31)}")

h.finish()
