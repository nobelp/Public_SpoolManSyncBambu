"""Regressionstest fuer die Bestaetigung per Dashboard (POST /confirm-consumption).

Historie: Bug vom 2026-09-29 (Druck #175) - die Bestaetigung buchte bei Spoolman,
trug die Menge aber nicht im Druckverlauf nach (Anzeige blieb 0.0 g). Seit
v4.2.0 (Endbuchung, Issue #43) wartet nach Druckende ein Posten auf die
Spulenwahl; die Bestaetigung bucht ihn genau einmal, die History zeigt Spule
und Menge, und Ende/Dauer des Drucks bleiben unveraendert.

Ende-zu-Ende ueber den echten HTTP-Handler (ohne HA/Spoolman)."""
import http.client
import threading
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

import booking_harness as h
from booking_harness import c, check

PORT = 18173
sm = h.FakeSpoolman(h.make_spool(26, used=100.0))
sm.install()

printer = h.Printer("2026-09-29T11:41:33+00:00")
state = {}                                    # keine Zuordnung -> Druck wartet auf Bestaetigung
h.poll(state, printer.states("running", 50, 4.51, external=True))
h.poll(state, printer.states("finish", 100, 4.51, external=True))
c.save_state(state)
job_key = f"{printer.gcode}|{printer.start}"
job = h.job_of(state, printer)
history_id = job["history_id"]
end_before = h.history_record(history_id)["end"]

check("Ausgangslage: Posten wartet, nichts gebucht",
      [i["status"] for i in job["final_booking"]["items"]] == [c.ITEM_AWAITING_USER] and sm.use_calls == [])
check("Ausgangslage: History zeigt schon 4.51 g ohne Spule",
      h.approx(h.history_record(history_id)["colors"][0]["grams"], 4.51))

server = ThreadingHTTPServer(("127.0.0.1", PORT), c._HistoryHandler)
threading.Thread(target=server.serve_forever, daemon=True).start()


def post(body):
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
    conn.request("POST", "/confirm-consumption", urlencode(body),
                 {"Content-Type": "application/x-www-form-urlencoded"})
    resp = conn.getresponse()
    resp.read()
    return resp.status


try:
    check("POST ohne Spulen-ID wird abgelehnt, nichts gebucht",
          post({"job_key": job_key}) == 303 and sm.use_calls == [])
    check("POST /confirm-consumption antwortet mit Redirect (303)",
          post({"job_key": job_key, "spool_id": "26"}) == 303)
    check("Spoolman: genau eine Buchung ueber 4.51 g auf Spule 26", sm.applied == [(26, 4.51)], str(sm.applied))

    stored = c.load_state()["jobs"][job_key]
    check("Posten ist als gebucht gespeichert", stored["final_booking"]["items"][0]["status"] == c.ITEM_BOOKED)
    check("Zuordnung der externen Spule wurde gemerkt", c.load_state()["user_spool_overrides"].get("External") == 26)

    rec = h.history_record(history_id)
    check("History zeigt Spule 26 mit 4.51 g (Kern von #175)",
          rec["colors"][0]["spool_id"] == 26 and h.approx(rec["colors"][0]["grams"], 4.51), str(rec["colors"]))
    check("Druckende unveraendert", rec["end"] == end_before, f"{end_before} -> {rec['end']}")

    check("zweites Bestaetigen (Doppelklick) bucht nicht erneut",
          post({"job_key": job_key, "spool_id": "26"}) == 303 and sm.applied == [(26, 4.51)], str(sm.applied))
finally:
    server.shutdown()

h.finish()
