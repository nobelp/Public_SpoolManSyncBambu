"""Prueft das Titelbild im Druckverlauf (Issue #33): Frische-Pruefung (kein
Bild des vorherigen Drucks), Retry bei HA-Fehlern inkl. Nachholen nach
Druckende, keine Schein-Dateinamen, maskierte Tokens, Anzeige.
Ohne HA - HTTP-Antworten und HA-States werden simuliert."""
import logging
import os
from datetime import datetime, timedelta, timezone

import consumption as c
import title_image as ti

ok = True


def check(label, cond):
    global ok
    print(("  PASS  " if cond else "  FAIL  ") + label)
    ok = ok and cond


class FakeResp:
    def __init__(self, status, content=b"PNGDATA"):
        self.status_code, self.content = status, content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"{self.status_code} Server Error for url: http://ha/api/image_proxy/x?token=GEHEIM123")


calls = []


def fake_get(status):
    def get(url, headers=None, timeout=None):
        calls.append(url)
        return FakeResp(status)
    return get


def states(image_ts, token="tok"):
    return {c.TITLE_IMAGE_ENTITY: {"state": image_ts, "attributes": {"access_token": token}}}


UTC = timezone.utc
START = datetime(2026, 9, 26, 12, 48, 0, tzinfo=UTC)
local = lambda dt: dt.astimezone().replace(tzinfo=None).isoformat()  # so liefert HA den State

print("1) Frische-Pruefung (reine Funktion)")
check("Bild 53 s nach Start passt", ti.image_fits_job(local(START + timedelta(seconds=53)), START.isoformat()))
check("Bild des vorherigen Drucks (89 s vor Start) passt nicht",
      not ti.image_fits_job(local(START - timedelta(seconds=89)), START.isoformat()))
check("Bild lange nach Druckende passt nicht",
      not ti.image_fits_job(local(START + timedelta(hours=2)), START.isoformat(), (START + timedelta(minutes=43)).isoformat()))
check("Bild kurz nach Ende passt noch",
      ti.image_fits_job(local(START + timedelta(minutes=44)), START.isoformat(), (START + timedelta(minutes=43)).isoformat()))
check("unbekannte Zeiten -> nicht uebernehmen", not ti.image_fits_job(None, START.isoformat())
      and not ti.image_fits_job("unknown", START.isoformat()))
check("Token maskiert", ti.mask_tokens("http://x/api/image_proxy/i?token=abc123&y=1") == "http://x/api/image_proxy/i?token=***&y=1")

print("2) Speichern: veraltetes Bild wird nicht geholt, frisches schon")
c.init_history_storage()
os.makedirs(c.HISTORY_IMAGES_DIR, exist_ok=True)
job = {"history_id": 159, "start": START.isoformat()}
calls.clear(); c._http.get = fake_get(200)
check("veraltet -> None, kein HTTP-Abruf", c._save_title_image(states(local(START - timedelta(seconds=89))), job) is None and not calls)
fresh = local(START + timedelta(seconds=53))
fn = c._save_title_image(states(fresh), job)
path = os.path.join(c.HISTORY_IMAGES_DIR, "159.png")
check("frisch -> gespeichert", fn == "159.png" and os.path.exists(path) and job["image_ts"] == fresh)
check("kein .tmp-Rest (atomar geschrieben)", not os.path.exists(path + ".tmp"))

print("3) HA-Fehler (HTTP 500): None, Fehler maskiert gemerkt, Log ohne Token")
records = []


class Grab(logging.Handler):
    def emit(self, record):
        records.append(record.getMessage())


c.log.addHandler(Grab())
job2 = {"history_id": 157, "start": START.isoformat()}
c._http.get = fake_get(500)
check("500 -> None", c._save_title_image(states(fresh), job2) is None)
check("letzter Fehler ohne Token", "GEHEIM123" not in job2.get("image_last_error", "") and "token=***" in job2["image_last_error"])
check("Log ohne Token", records and all("GEHEIM123" not in r for r in records))
n = len(records)
c._save_title_image(states(fresh), job2)
check("wiederholter Fehler wird gedrosselt geloggt", len(records) == n)

print("4) Nachholen nach Druckende")
rec = {"id": 157, "name": "Tight hole", "start": START.isoformat(), "end": None, "status": "failed",
       "image": None, "camera_image": None, "colors": []}
hist = c.load_history(); hist["records"] = [rec]; hist["next_id"] = 158; c.save_history(hist)
ended = (datetime.now(UTC) - timedelta(minutes=3)).isoformat()
st = {"jobs": {"k": {"history_id": 157, "start": START.isoformat(), "ended_at": ended, "history_finalized": True}}}
c._http.get = fake_get(500)
c._retry_missing_title_images(st, states(fresh))
check("HA noch fehlerhaft -> weiter offen", not st["jobs"]["k"].get("image_file") and not st["jobs"]["k"].get("image_given_up"))
c._http.get = fake_get(200)
c._retry_missing_title_images(st, states(fresh))
saved = next(r for r in c.load_history()["records"] if r["id"] == 157)
check("HA wieder ok -> Bild nachgetragen im Datensatz", st["jobs"]["k"].get("image_file") == "157.png"
      and saved.get("image") == "157.png" and saved.get("title_image_ts") == fresh)
st2 = {"jobs": {"k": {"history_id": 156, "start": START.isoformat(), "history_finalized": True,
                      "ended_at": (datetime.now(UTC) - timedelta(minutes=11)).isoformat()}}}
c._retry_missing_title_images(st2, states(fresh))
check("nach 10 min aufgegeben", st2["jobs"]["k"].get("image_given_up") is True)
st3 = {"jobs": {"k": {"history_id": 155, "start": START.isoformat(), "history_finalized": True}}}
calls.clear(); c._retry_missing_title_images(st3, states(fresh))
check("Altbestand ohne Endzeit: kein rueckwirkender Abruf", st3["jobs"]["k"].get("image_given_up") and not calls)
nxt = local(datetime.now(UTC) + timedelta(minutes=1))  # Bild des naechsten Drucks
st4 = {"jobs": {"k": {"history_id": 154, "start": START.isoformat(), "history_finalized": True,
                      "ended_at": (START + timedelta(minutes=43)).isoformat()}}}
calls.clear(); c._retry_missing_title_images(st4, states(nxt))
check("Bild des naechsten Drucks wird nicht nachgetragen", not st4["jobs"]["k"].get("image_file") and not calls)

print("5) Keine Schein-Dateinamen, Anzeige prueft Datei")
hist = c.load_history()
hist["records"] = [{"id": 900, "name": "ohne Bild", "start": START.isoformat(), "status": "finish",
                    "image": None, "camera_image": None, "colors": []}]
c.save_history(hist)
r900 = next(r for r in c.load_history()["records"] if r["id"] == 900)
check("ohne Datei kein automatischer Name", not r900.get("image") and not r900.get("camera_image"))
row = c._render_history_rows([{"id": 901, "name": "x", "image": "901.png", "status": "finish", "colors": []}])
check("Schein-Dateiname ohne Datei -> '–' statt kaputtem Bild", 'src="/image/901' not in row)
row = c._render_history_rows([{"id": 159, "name": "x", "image": "159.png", "status": "finish", "colors": [],
                               "title_image_ts": fresh}])
check("vorhandene Datei mit Versions-Parameter", 'src="/image/159?v=' in row)

print("6) Nachdruck: Titelbild des letzten gleichnamigen Drucks uebernehmen")
check("allgemeine Namen nie wiederverwenden", all(ti.reuse_key(n) is None for n in
      ("", "?", "plate_1", "/data/Metadata/plate_2.gcode", "Model.3mf", "auto_cali_for_user_param.gcode")))
check("echter Name -> Schluessel", ti.reuse_key("  Sturdy  Modular Rack ") == "sturdy modular rack")
with open(os.path.join(c.HISTORY_IMAGES_DIR, "160.png"), "wb") as f:
    f.write(b"BILD160")
hist = c.load_history()
hist["records"] = [
    {"id": 158, "name": "BIGGER HOLE", "status": "failed", "image": None, "colors": []},
    {"id": 160, "name": "Sturdy Modular Rack", "status": "finish", "image": "160.png", "colors": []},
    {"id": 161, "name": "Sturdy Modular Rack", "status": "finish", "image": None, "colors": []},
    {"id": 162, "name": "/data/Metadata/plate_1.gcode", "status": "finish", "image": None, "colors": []},
]
c.save_history(hist)
check("gleichnamiger frueherer Druck gefunden", c._find_reusable_title_image("Sturdy Modular Rack", 161) == 160)
check("kein spaeterer Druck als Quelle", c._find_reusable_title_image("Sturdy Modular Rack", 160) is None)
filled = c.backfill_title_images()
r161 = next(r for r in c.load_history()["records"] if r["id"] == 161)
check("Nachtrag beim Start: #161 <- #160", filled == ["#161<-#160"] and r161["title_image_source"] == "#160"
      and open(os.path.join(c.HISTORY_IMAGES_DIR, "161.png"), "rb").read() == b"BILD160")
check("allgemeiner Name (#162) nicht befuellt", not os.path.exists(os.path.join(c.HISTORY_IMAGES_DIR, "162.png")))
check("zweiter Start: nichts mehr zu tun", c.backfill_title_images() == [])

job6 = {"history_id": 163, "task_name": "Sturdy Modular Rack",
        "start": (datetime.now(UTC) - timedelta(minutes=3)).isoformat()}
check("waehrend des Drucks: uebernommen, wenn HA nichts liefert", c._reuse_title_image(job6, "Test")
      and job6["image_file"] == "163.png" and job6["image_source"] == "#161")  # neuester gleichnamiger mit Bild
c._http.get = fake_get(200)
new_img = local(datetime.now(UTC) - timedelta(minutes=1))
check("echtes HA-Bild ersetzt die Uebernahme", c._save_title_image(states(new_img), job6) == "163.png"
      and "image_source" not in job6 and open(os.path.join(c.HISTORY_IMAGES_DIR, "163.png"), "rb").read() == b"PNGDATA")

print()
print("ALLE TESTS OK" if ok else "FEHLER")
raise SystemExit(0 if ok else 1)
