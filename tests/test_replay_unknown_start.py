"""Regressionstest Issue #53: Phantom-Duplikat und Doppelbuchung nach HA-Neustart.

Echter Fall (Druck #225/#226, 2026-10-12): #225 endete um 13:14 und wurde korrekt
gebucht (57,67 g auf Spule #35). HA startete neu; danach meldete der Drucker weiter
'finish' mit demselben G-Code und Plangewicht, der Start-Sensor aber 'unknown'.
Der Job-Schluessel (G-Code|Start) war damit neu, der neue Job startete im Status
'finish' und buchte sofort ein zweites Mal (#226, ohne Start und Dauer).

Kein HA, kein Spoolman, kein Netzwerk (booking_harness)."""
import booking_harness as h
from booking_harness import c, check

PLAN = 57.67
UID = "12E6843200000100"
TRAY = "AMS 1 Tray 4"
_n = [0]


def setup(plan=PLAN):
    """Neuer Drucker mit eigenem G-Code (Historie und State bleiben je Fall getrennt)."""
    _n[0] += 1
    sm = h.FakeSpoolman(h.make_spool(35, tag_uid=UID))
    sm.install()
    printer = h.Printer(f"2026-10-12T08:{_n[0]:02d}:13+00:00", gcode=f"/data/Metadata/replay_{_n[0]}.gcode")
    return sm, printer, {TRAY: (UID, plan)}, {}


def records_for(printer):
    return [r for r in c.load_history()["records"] if r.get("gcode") == printer.gcode]


def run_print(state, printer, ams, start=None, plan=PLAN):
    """Ein kompletter Druck: running, finish - mit lesbarem Start."""
    h.poll(state, printer.states("running", 50, plan, ams=ams, start=start))
    h.poll(state, printer.states("finish", 100, plan, ams=ams, start=start))


# ── 1 ───────────────────────────────────────────────────────────────
print("1) Der gemeldete Fall: fertig + gebucht, dann HA-Neustart (Start 'unknown', Status finish)")
sm, printer, ams, state = setup()
run_print(state, printer, ams)
check("Ausgangslage: genau eine Buchung und ein Verlaufseintrag", sm.applied == [(35, PLAN)] and len(records_for(printer)) == 1,
      f"{sm.applied} {len(records_for(printer))}")
used_before = sm.used(35)
for _ in range(3):
    h.poll(state, printer.states("finish", 100, PLAN, ams=ams, start="unknown"))
check("KEINE zweite Buchung (Issue #53)", sm.applied == [(35, PLAN)] and sm.used(35) == used_before, str(sm.applied))
check("KEIN zweiter Verlaufseintrag (#226 entsteht nicht)", len(records_for(printer)) == 1, str(len(records_for(printer))))
check("kein neuer Job angelegt", len(state["jobs"]) == 1 and f"{printer.gcode}|unknown" not in state["jobs"], str(list(state["jobs"])))
check("Entscheidung im State vermerkt (nur zum einmaligen Loggen)", state.get("ignored_replays") == {f"{printer.gcode}|unknown": f"{printer.gcode}|{printer.start}"},
      str(state.get("ignored_replays")))

print("2) Gleiches bei 'unavailable', '?' und leerem Start, auch nach Status 'failed'")
for variant, status in (("unavailable", "finish"), ("?", "finish"), ("", "finish"), ("unknown", "failed")):
    sm, printer, ams, state = setup()
    run_print(state, printer, ams)
    h.poll(state, printer.states(status, 100, PLAN, ams=ams, start=variant))
    check(f"Start {variant!r}, Status {status}: keine zweite Buchung", sm.applied == [(35, PLAN)] and len(records_for(printer)) == 1,
          f"{sm.applied} {len(records_for(printer))}")

print("3) Zustand ueberlebt einen Neustart des Dienstes (von der Platte geladen)")
sm, printer, ams, state = setup()
run_print(state, printer, ams)
c.save_state(state)
state = h.poll_persisted(printer.states("finish", 100, PLAN, ams=ams, start="unknown"))
state = h.poll_persisted(printer.states("finish", 100, PLAN, ams=ams, start="unknown"))
check("nach Laden/Speichern weiterhin keine zweite Buchung", sm.applied == [(35, PLAN)] and len(records_for(printer)) == 1, str(sm.applied))

# ── Gegenproben: ein echter neuer Druck muss weiter gebucht werden ──────────
print("4) Gegenprobe: derselbe G-Code noch einmal gedruckt (running -> finish, lesbarer Start)")
sm, printer, ams, state = setup()
run_print(state, printer, ams)
h.poll(state, printer.states("finish", 100, PLAN, ams=ams, start="unknown"))      # Replay wird ignoriert
second_start = "2026-10-12T15:00:00+00:00"
run_print(state, printer, ams, start=second_start)
check("zweiter, echter Druck wird gebucht", sm.applied == [(35, PLAN), (35, PLAN)], str(sm.applied))
check("zwei Verlaufseintraege", len(records_for(printer)) == 2, str(len(records_for(printer))))
h.poll(state, printer.states("finish", 100, PLAN, ams=ams, start="unknown"))
check("ein Replay des ZWEITEN Drucks wird ebenfalls ignoriert", sm.applied == [(35, PLAN), (35, PLAN)] and len(records_for(printer)) == 2,
      str(sm.applied))

print("5) Gegenprobe: anderer G-Code mit unlesbarem Start und Status finish -> wird gebucht (kein Replay)")
sm, printer, ams, state = setup()
run_print(state, printer, ams)
other = h.Printer("2026-10-12T16:00:00+00:00", gcode="/data/Metadata/other_print.gcode")
h.poll(state, other.states("finish", 100, PLAN, ams=ams, start="unknown"))
check("anderer Druck wird erfasst und einmal gebucht", sm.applied == [(35, PLAN), (35, PLAN)], str(sm.applied))
check("eigener Job fuer den anderen G-Code", "/data/Metadata/other_print.gcode|unknown" in state["jobs"], str(list(state["jobs"])))

print("6) Gegenprobe: gleicher G-Code, aber anderes Plangewicht -> kein Replay")
sm, printer, ams, state = setup()
run_print(state, printer, ams)
h.poll(state, printer.states("finish", 100, 80.0, ams={TRAY: (UID, 80.0)}, start="unknown"))
check("abweichendes Plangewicht wird als neuer Druck gebucht", sm.applied == [(35, PLAN), (35, 80.0)], str(sm.applied))

print("7) Gegenprobe: Status running mit unlesbarem Start ist nie ein Replay")
sm, printer, ams, state = setup()
run_print(state, printer, ams)
h.poll(state, printer.states("running", 10, PLAN, ams=ams, start="unknown"))
check("laufender Druck legt seinen Job an (kein Buchen vor Druckende)", f"{printer.gcode}|unknown" in state["jobs"] and sm.applied == [(35, PLAN)],
      f"{list(state['jobs'])} {sm.applied}")

# ── Start-Sensor verliert mitten im Druck seinen Wert ────────────────
print("8) Start-Sensor wird mitten im Druck 'unknown': derselbe Job laeuft weiter, genau eine Buchung")
sm, printer, ams, state = setup()
h.poll(state, printer.states("running", 30, PLAN, ams=ams))
h.poll(state, printer.states("running", 60, PLAN, ams=ams, start="unknown"))
check("kein zweiter Job, der den ersten abbricht", len(state["jobs"]) == 1 and sm.applied == [], f"{list(state['jobs'])} {sm.applied}")
h.poll(state, printer.states("finish", 100, PLAN, ams=ams, start="unknown"))
check("Druckende bucht den Plan genau einmal", sm.applied == [(35, PLAN)], str(sm.applied))
check("ein Verlaufseintrag, mit lesbarem Start", len(records_for(printer)) == 1 and records_for(printer)[0]["start"] == printer.start,
      str(records_for(printer)))

# ── Einheitentests ───────────────────────────────────────────────────
print("9) Hilfsfunktionen")
check("_is_unreadable_start", all(c._is_unreadable_start(v) for v in ("unknown", "unavailable", "?", "", None))
      and not c._is_unreadable_start("2026-10-12T08:34:13+00:00"))
check("_same_tray_plan: auf 0,01 g genau", c._same_tray_plan({TRAY: 57.67}, {TRAY: 57.675}) and not c._same_tray_plan({TRAY: 57.67}, {TRAY: 57.7})
      and not c._same_tray_plan({TRAY: 57.67}, {"AMS 1 Tray 1": 57.67}) and c._same_tray_plan({}, None))
done = {"gcode": "g", "start": "2026-10-12T08:34:13+00:00", "tray_weights": {TRAY: 57.67}, "done": True,
        "ended_at": "2026-10-12T11:14:15+00:00", "segments": {}}
older = dict(done, ended_at="2026-10-11T11:14:15+00:00", gcode="x")
jobs = {"g|2026-10-12T08:34:13+00:00": done, "x|2026-10-11T08:00:00+00:00": older}
check("Replay wird erkannt (zuletzt beendeter Job)", c._find_replayed_job_key(jobs, "g", "unknown", {TRAY: 57.67}, "finish") == "g|2026-10-12T08:34:13+00:00")
check("nur der ZULETZT beendete Job zaehlt (aelterer mit gleichem G-Code reicht nicht)",
      c._find_replayed_job_key({"x|1": older, "g|2": done}, "x", "unknown", {TRAY: 57.67}, "finish") is None)
check("lesbarer Start -> kein Replay", c._find_replayed_job_key(jobs, "g", "2026-10-12T09:00:00+00:00", {TRAY: 57.67}, "finish") is None)
check("Status running/prepare/idle -> kein Replay", all(c._find_replayed_job_key(jobs, "g", "unknown", {TRAY: 57.67}, s) is None for s in ("running", "prepare", "idle")))
check("G-Code '?' (Sensor fehlt) -> kein Replay", c._find_replayed_job_key(jobs, "?", "unknown", {TRAY: 57.67}, "finish") is None)
check("offener Job (neues Format) -> kein Replay", c._find_replayed_job_key({**jobs, "g|3": {"gcode": "g", "done": False, "segments": {}}}, "g", "unknown", {TRAY: 57.67}, "finish") is None)
check("verwaister offener Job aelterer Version (ohne 'segments') blockiert nicht",
      c._find_replayed_job_key({**jobs, "old|1": {"gcode": "old", "done": False}}, "g", "unknown", {TRAY: 57.67}, "finish") == "g|2026-10-12T08:34:13+00:00")
check("_resolve_job_key: genau ein offener Job gleichen G-Codes wird weiterverwendet",
      c._resolve_job_key({"g|S": {"gcode": "g", "start": "S", "done": False}}, "g", "unknown") == "g|S")
check("_resolve_job_key: zwei offene Jobs -> nicht raten", c._resolve_job_key(
    {"g|A": {"gcode": "g", "done": False}, "g|B": {"gcode": "g", "done": False}}, "g", "unknown") == "g|unknown")
check("_resolve_job_key: fertiger Job wird nie unscharf gematcht (wie bisher)",
      c._resolve_job_key({"g|S": {"gcode": "g", "start": "S", "done": True}}, "g", "unknown") == "g|unknown")
check("_resolve_job_key: anderer G-Code -> eigener Schluessel", c._resolve_job_key({"g|S": {"gcode": "g", "done": False}}, "h", "unknown") == "h|unknown")

print("10) Anzeige: Eintrag ohne lesbaren Start sagt es, statt 'unknown' zu zeigen")
rec = {"id": 900, "name": "x", "start": "unknown", "duration_min": None, "status": "finish", "colors": []}
row_html = c._render_history_rows([rec])
check("'Start unbekannt' statt 'unknown'", "Start unbekannt" in row_html and ">unknown<" not in row_html)
check("Dauer bleibt '?'", ">?<" in row_html)
rec_ok = dict(rec, id=901, start="2026-10-12T08:34:13+00:00", duration_min=160)
ok_html = c._render_history_rows([rec_ok])
check("normaler Eintrag zeigt Datum und Dauer wie bisher", "2026-10-12 10:34" in ok_html and "160 min" in ok_html and "Start unbekannt" not in ok_html)

h.finish()
