"""POST /add-history ohne das Modul cgi (v4.2.5).

cgi wurde in Python 3.13 entfernt. Der Handler nutzte cgi.FieldStorage; ein Wechsel
des Container-Images haette das Speichern neuer Eintraege (Dialog "Neuer Eintrag")
gebrochen. Jetzt liest parse_form_data() das Formular mit der Standardbibliothek
(email) - gleiches Verhalten in 3.11 und 3.13+.

Dieser Test deckt ab:
- parse_form_data(): multipart und urlencoded, Umlaute/Emoji, binaere Dateien,
  leeres Dateifeld, kaputte Eingaben
- Gleichwertigkeit mit cgi.FieldStorage (nur wo cgi noch existiert, z.B. Python 3.11)
- den Endpunkt per echtem HTTP-Request: Eintrag samt Bildern, Validierung,
  Groessenlimit (413)
- ein Formular, das Node (wie ein Browser) mit FormData kodiert hat
- dass kein Produktivcode ein in Python 3.13 entferntes Modul importiert"""
import ast
import glob
import http.client
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

import booking_harness as h
from booking_harness import c, check

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 3 + b"\r\n--ende\r\n\x00\xff"
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + bytes(range(255, -1, -1)) * 2 + b"\r\n\r\n\xff\xd9"


def multipart(fields, files, boundary="----WebKitFormBoundaryTEST123abc"):
    parts = []
    for key, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode("utf-8"))
    for key, (filename, ctype, data) in files.items():
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; filename="{filename}"\r\n'
                      f"Content-Type: {ctype}\r\n\r\n").encode("utf-8") + data + b"\r\n")
    return b"".join(parts) + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


def parse(built):
    """multipart() liefert (body, content_type); parse_form_data erwartet (content_type, body)."""
    return c.parse_form_data(built[1], built[0])


COLORS = [{"spool_id": 26, "filament_name": "PLA Matte Morandi Green", "material": "PLA", "color_hex": "33586C",
           "density": 1.24, "diameter": 1.75, "grams": 43.25, "length_m": 14.5}]
FIELDS = {"name": "Testdruck äöü €", "start": "2026-10-10T15:38", "end": "2026-10-10T18:10", "status": "finish",
          "gcode": "/data/Metadata/plate_1.gcode", "colors": json.dumps(COLORS)}

# ── 1 ────────────────────────────────────────────────────────────────
print("1) parse_form_data: multipart")
body, ctype = multipart(FIELDS, {"image": ("vorschau bild.png", "image/png", PNG), "camera": ("kamera.jpg", "image/jpeg", JPEG)})
fields, files = c.parse_form_data(ctype, body)
check("alle Textfelder, inkl. Umlaute, Euro-Zeichen und JSON", fields == FIELDS, str(fields))
check("Bild bytegenau (inkl. CRLF, Nullbytes und Boundary-aehnlichem Text)", files.get("image") == ("vorschau bild.png", PNG))
check("Kamerafoto bytegenau", files.get("camera") == ("kamera.jpg", JPEG))
quoted_body, _ = multipart({"x": "1"}, {}, boundary="quoted-b")
fields, files = c.parse_form_data('multipart/form-data; boundary="quoted-b"', quoted_body)
check("Begrenzung in Anfuehrungszeichen wird erkannt", fields == {"x": "1"}, str(fields))
fields, files = parse(multipart({"name": "A"}, {"image": ("", "application/octet-stream", b"")}))
check("leeres Dateifeld (Browser ohne Auswahl) zaehlt als nicht gesendet", files == {} and fields == {"name": "A"}, str((fields, files)))
fields, files = parse(multipart({"name": "A"}, {"image": ("bild.png", "image/png", b"")}))
check("Datei mit Namen aber ohne Inhalt zaehlt als nicht gesendet", files == {}, str(files))
fields, files = parse(multipart({"leer": "", "multi": "zeile1\r\nzeile2"}, {}))
check("leeres Textfeld und mehrzeiliger Text bleiben erhalten", fields == {"leer": "", "multi": "zeile1\r\nzeile2"}, str(fields))
raw = b'--B\r\nContent-Disposition: form-data\r\n\r\nohne Namen\r\n--B\r\nContent-Disposition: form-data; name="ok"\r\n\r\nja\r\n--B--\r\n'
check("Teil ohne Feldnamen wird ignoriert", c.parse_form_data("multipart/form-data; boundary=B", raw)[0] == {"ok": "ja"})
big = os.urandom(2_000_000)
body_big, ctype_big = multipart({"a": "b"}, {"image": ("x.png", "image/png", big)})
check("grosse Binaerdatei (2 MB, Zufallsdaten) bytegenau", c.parse_form_data(ctype_big, body_big)[1]["image"][1] == big)

print("2) parse_form_data: urlencoded und Fehlerfaelle")
fields, files = c.parse_form_data("application/x-www-form-urlencoded; charset=UTF-8", urlencode({"name": "Größe", "status": "finish", "leer": ""}).encode())
check("urlencoded: Felder inkl. Umlaut und leerem Wert", fields == {"name": "Größe", "status": "finish", "leer": ""} and files == {}, str(fields))
for bad_type, bad_body, label in [("application/json", b"{}", "JSON"), ("", b"x", "kein Inhaltstyp"), (None, b"x", "None"),
                                  ("text/plain", b"name=x", "text/plain")]:
    try:
        c.parse_form_data(bad_type, bad_body)
        check(f"abgelehnt: {label}", False)
    except ValueError:
        check(f"abgelehnt: {label} (ValueError)", True)
try:
    c.parse_form_data("multipart/form-data", b"--B\r\n\r\nx")
    check("multipart ohne Begrenzung wird abgelehnt", False)
except ValueError as e:
    check("multipart ohne Begrenzung wird abgelehnt", True, str(e))
check("leerer multipart-Rumpf liefert keine Felder", c.parse_form_data("multipart/form-data; boundary=B", b"--B--\r\n") == ({}, {}))

print("3) Gleichwertigkeit mit dem alten cgi.FieldStorage (nur wo cgi noch existiert)")
try:
    import cgi
except ImportError:
    cgi = None
    print("  SKIP  cgi existiert in dieser Python-Version nicht (>= 3.13) - genau der Grund fuer diese Aenderung")
if cgi:
    import warnings
    warnings.simplefilter("ignore", DeprecationWarning)
    scenarios = {
        "Text + beide Dateien": multipart(FIELDS, {"image": ("a.png", "image/png", PNG), "camera": ("b.jpg", "image/jpeg", JPEG)}),
        "nur Text": multipart(FIELDS, {}),
        "leeres Dateifeld": multipart({"name": "A", "start": "x"}, {"image": ("", "application/octet-stream", b"")}),
        "Sonderzeichen": multipart({"name": "Zeile1\r\nZeile2 \"zitat\" <b>", "colors": "[]"}, {}),
    }
    for label, (body, ctype) in scenarios.items():
        old = cgi.FieldStorage(fp=io.BytesIO(body), headers={"content-type": ctype, "content-length": str(len(body))},
                               environ={"REQUEST_METHOD": "POST"})
        # So hat der alte Handler gelesen: Textfelder ueber getvalue(), Dateien nur mit
        # nicht leerem Dateinamen ('image' in form and form['image'].filename).
        old_fields = {k: old.getvalue(k) for k in old.keys() if old[k].filename is None}
        old_files = {k: (old[k].filename, old[k].file.read()) for k in old.keys() if old[k].filename}
        new_fields, new_files = c.parse_form_data(ctype, body)
        check(f"{label}: Felder identisch mit cgi", old_fields == new_fields, f"{old_fields} != {new_fields}")
        check(f"{label}: Dateien identisch mit cgi", old_files == new_files)

# ── 4 ────────────────────────────────────────────────────────────────
print("4) Endpunkt POST /add-history per echtem HTTP-Request")
PORT = 18177
sm = h.FakeSpoolman(h.make_spool(26))
sm.install()
os.makedirs(c.HISTORY_IMAGES_DIR, exist_ok=True)
server = ThreadingHTTPServer(("127.0.0.1", PORT), c._HistoryHandler)
threading.Thread(target=server.serve_forever, daemon=True).start()


def post(body, content_type, length=None):
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=15)
    headers = {"Content-Type": content_type} if content_type else {}
    if length is not None:
        headers["Content-Length"] = str(length)
    conn.request("POST", "/add-history", body, headers)
    resp = conn.getresponse()
    raw = resp.read().decode("utf-8", errors="replace")
    try:
        return resp.status, json.loads(raw)
    except ValueError:
        return resp.status, raw


_counter = [0]


def fresh(**override):
    """Jeder Eintrag braucht eine eigene Startzeit und einen eigenen G-Code: der Verlauf haelt
    gleiche Drucke (gleicher G-Code, Start +-90 s) fuer Duplikate und fuehrt sie zusammen."""
    _counter[0] += 1
    day = f"2026-11-{_counter[0]:02d}"
    return dict(FIELDS, start=f"{day}T15:38", end=f"{day}T18:10", gcode=f"/data/Metadata/test_{_counter[0]}.gcode", **override)


def records():
    return c.load_history()["records"]


def record_by_id(rec_id):
    return next((r for r in records() if r["id"] == rec_id), None)


try:
    before = len(records())
    body, ctype = multipart(FIELDS, {"image": ("v.png", "image/png", PNG), "camera": ("k.jpg", "image/jpeg", JPEG)})
    status, data = post(body, ctype)
    check("Eintrag mit Bildern: 200 und {ok: true, id}", status == 200 and data.get("ok") is True and isinstance(data.get("id"), int), f"{status} {data}")
    rec = record_by_id(data["id"])
    check("Eintrag steht im Verlauf (ein Eintrag mehr)", rec is not None and len(records()) == before + 1)
    check("Name (mit Umlauten), Status, G-Code gespeichert", rec["name"] == FIELDS["name"] and rec["status"] == "finish" and rec["gcode"] == FIELDS["gcode"], str(rec))
    check("Dauer aus Start und Ende berechnet (152 min)", rec["duration_min"] == 152, str(rec["duration_min"]))
    check("Spule mit Gramm gespeichert", rec["colors"][0]["spool_id"] == 26 and rec["colors"][0]["grams"] == 43.25, str(rec["colors"]))
    check("Name gilt als von Hand vergeben (wird nicht ueberschrieben)", rec["name_overridden"] is True)
    img = os.path.join(c.HISTORY_IMAGES_DIR, f"{data['id']}.png")
    cam = os.path.join(c.HISTORY_IMAGES_DIR, f"{data['id']}_camera.jpg")
    check("Vorschaubild bytegenau gespeichert", os.path.exists(img) and open(img, "rb").read() == PNG)
    check("Kamerafoto bytegenau gespeichert", os.path.exists(cam) and open(cam, "rb").read() == JPEG)
    check("Verlauf verweist auf beide Dateien", rec["image"] == f"{data['id']}.png" and rec["camera_image"] == f"{data['id']}_camera.jpg", str((rec["image"], rec["camera_image"])))

    status, data = post(*multipart(fresh(), {}))
    rec = record_by_id(data["id"])
    check("ohne Bilder: 200, keine Bilddateien", status == 200 and rec["image"] is None and rec["camera_image"] is None, f"{status} {data} {rec}")
    status, data = post(*multipart(fresh(), {"image": ("", "application/octet-stream", b"")}))
    check("leeres Dateifeld wie im Browser ohne Auswahl: 200 ohne Bild", status == 200 and record_by_id(data["id"])["image"] is None, f"{status} {data} {record_by_id(data['id'])}")

    form = urlencode(fresh()).encode()
    status, data = post(form, "application/x-www-form-urlencoded")
    check("urlencoded (ohne Bilder) funktioniert weiterhin", status == 200 and data.get("ok") is True, f"{status} {data}")
    fields_no_status = {k: v for k, v in fresh().items() if k != "status"}
    status, data = post(*multipart(fields_no_status, {}))
    check("fehlender Status gilt als 'finish' (bisheriges Verhalten)", status == 200 and record_by_id(data["id"])["status"] == "finish", f"{status} {data}")
    status, data = post(*multipart(fresh(name="N" * 500), {}))
    check("Name wird auf 200 Zeichen gekuerzt", status == 200 and len(record_by_id(data["id"])["name"]) == 200)

    print("   Validierung (je 400 mit Fehlermeldung, kein neuer Eintrag, keine Bilddateien)")
    files_before = set(os.listdir(c.HISTORY_IMAGES_DIR))
    count = len(records())
    invalid = {
        "Name fehlt": dict(FIELDS, name=""), "Start fehlt": dict(FIELDS, start=""), "Ende fehlt": dict(FIELDS, end=""),
        "Status ungueltig": dict(FIELDS, status="kaputt"),
        "colors kein JSON": dict(FIELDS, colors="{kaputt"), "colors leeres Array": dict(FIELDS, colors="[]"),
        "colors kein Array": dict(FIELDS, colors='{"a":1}'),
        "Start nach Ende": dict(FIELDS, start="2026-10-10T19:00"), "Start gleich Ende": dict(FIELDS, end=FIELDS["start"]),
        "Datum unlesbar": dict(FIELDS, start="gestern"),
    }
    for label, fields_bad in invalid.items():
        status, data = post(*multipart(fields_bad, {"image": ("v.png", "image/png", PNG)}))
        check(f"{label}: 400 mit Meldung", status == 400 and data.get("ok") is False and data.get("error"), f"{status} {data}")
    check("... kein Eintrag wurde angelegt", len(records()) == count)
    check("... und keine Bilddatei zurueckgelassen", set(os.listdir(c.HISTORY_IMAGES_DIR)) == files_before)

    print("   kaputte oder unpassende Anfragen")
    for label, args in {
        "JSON statt Formular": (b"{}", "application/json"),
        "kein Inhaltstyp": (b"name=x", None),
        "multipart ohne Begrenzung": (b"--B\r\n\r\nx", "multipart/form-data"),
        "leerer Rumpf": (b"", "application/x-www-form-urlencoded"),
    }.items():
        status, data = post(*args)
        check(f"{label}: 400", status == 400 and isinstance(data, dict) and data.get("ok") is False, f"{status} {data}")
    status, data = post(b"", "application/x-www-form-urlencoded")
    check("leerer Rumpf nennt die Ursache (nicht nur 'Pflichtfelder fehlen')", status == 400 and "Leere Anfrage" in str(data.get("error")), f"{status} {data}")
    status, data = post(b"x=1", "application/x-www-form-urlencoded", length="abc")
    check("ungueltige Content-Length: 400", status == 400, f"{status} {data}")

    print("   Groessenlimit")
    original_limit = c.MAX_FORM_BYTES
    c.MAX_FORM_BYTES = 5_000
    count = len(records())
    body, ctype = multipart(FIELDS, {"image": ("gross.png", "image/png", os.urandom(20_000))})
    try:
        status, data = post(body, ctype)
    except (ConnectionError, http.client.HTTPException, OSError) as e:
        status, data = 413, {"ok": False, "error": f"Verbindung vom Server beendet ({type(e).__name__})"}
    check("zu grosse Anfrage wird abgelehnt (413 bzw. Verbindung beendet)", status == 413, f"{status} {data}")
    check("... ohne Eintrag", len(records()) == count)
    c.MAX_FORM_BYTES = original_limit
    status, data = post(*multipart(fresh(), {}))
    check("danach funktioniert der Endpunkt normal", status == 200, f"{status} {data}")
finally:
    server.shutdown()

# ── 5 ────────────────────────────────────────────────────────────────
print("5) Formular, das Node wie ein Browser (FormData) kodiert")
NODE = shutil.which("node")
if not NODE:
    print("  SKIP  Node.js nicht gefunden")
else:
    tmp = tempfile.mkdtemp()
    png_path = os.path.join(tmp, "vorschau.png")
    open(png_path, "wb").write(PNG)
    script = r"""
const fs = require('fs');
(async () => {
  const fd = new FormData();
  const f = JSON.parse(process.argv[1]);
  for (const [k, v] of Object.entries(f)) fd.append(k, v);
  fd.append('image', new Blob([fs.readFileSync(process.argv[2])], { type: 'image/png' }), 'vorschau.png');
  fd.append('camera', new Blob([]), '');          // leeres Dateifeld wie ohne Auswahl
  const req = new Request('http://x/', { method: 'POST', body: fd });
  fs.writeFileSync(process.argv[3], Buffer.from(await req.arrayBuffer()));
  process.stdout.write(req.headers.get('content-type'));
})();
"""
    out_path = os.path.join(tmp, "body.bin")
    node_fields = fresh()
    run = subprocess.run([NODE, "-e", script, json.dumps(node_fields), png_path, out_path], capture_output=True, text=True, timeout=60)
    check("Node erzeugt das Formular", run.returncode == 0 and os.path.exists(out_path), run.stderr[-200:])
    node_ctype, node_body = run.stdout, open(out_path, "rb").read()
    fields, files = c.parse_form_data(node_ctype, node_body)
    check("Felder aus dem Browser-Formular gelesen (ein leeres Dateifeld darf als leeres Textfeld ankommen)",
          {k: v for k, v in fields.items() if k != "camera"} == node_fields and fields.get("camera", "") == "", str(fields))
    check("Bild bytegenau, leeres Kamerafeld ist keine Datei", files == {"image": ("vorschau.png", PNG)}, str({k: (v[0], len(v[1])) for k, v in files.items()}))
    server = ThreadingHTTPServer(("127.0.0.1", PORT + 1), c._HistoryHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", PORT + 1, timeout=15)
        conn.request("POST", "/add-history", node_body, {"Content-Type": node_ctype})
        resp = conn.getresponse()
        data = json.loads(resp.read())
        rec = record_by_id(data.get("id"))
        check("Endpunkt nimmt das Browser-Formular an und speichert Bild und Eintrag",
              resp.status == 200 and rec is not None and rec["image"] == f"{data['id']}.png"
              and open(os.path.join(c.HISTORY_IMAGES_DIR, f"{data['id']}.png"), "rb").read() == PNG, f"{resp.status} {data}")
    finally:
        server.shutdown()

# ── 6 ────────────────────────────────────────────────────────────────
print("6) Kein Produktivcode importiert ein in Python 3.13 entferntes Modul")
REMOVED = {"cgi", "cgitb", "imghdr", "sndhdr", "pipes", "telnetlib", "crypt", "nntplib", "smtpd", "asyncore", "asynchat", "imp",
           "distutils", "lib2to3", "uu", "xdrlib", "msilib", "nis", "ossaudiodev", "spwd", "chunk", "aifc", "audioop", "mailcap", "sunau"}
found = []
for path in sorted(glob.glob(os.path.join(ROOT, "*.py"))):
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module.split(".")[0]]
        found += [f"{os.path.basename(path)}:{node.lineno} {m}" for m in names if m in REMOVED]
check("keine Importe entfernter Standardbibliotheks-Module", not found, str(found))

h.finish()
