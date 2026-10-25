"""Die Inline-Skripte der Verlaufsseite muessen im Browser laufen.

Hintergrund: Das Skript des Dialogs "Neuer Eintrag" war seit dem ersten Commit
kaputt und fiel nie auf, weil es nur im Browser ausgefuehrt wird:
  1. In einem normalen Python-String wird \\' zu ' - der Browser bekam
     onchange="updateSpoolInfo('' + rowId + '')" (ungueltiges JavaScript).
  2. Die Klammern wurden verdoppelt ({{ }}), obwohl der Text als Wert (nicht als
     Template) eingesetzt wird - aus colors.push({ ... }) wurde {{ ... }}.
  3. Das Skript steht im <head> und griff sofort auf das Formular zu, das erst
     weiter unten im Dokument steht (null.addEventListener).

Der Test rendert die echte Seite, prueft ALLE Inline-Skripte mit dem
JavaScript-Parser von Node und fuehrt den Dialog gegen eine DOM-Attrappe aus
(oeffnen, Zeile anlegen, Pflichtfeldpruefung, Absenden inkl. gesendeter Daten).
Ohne Node wird uebersprungen (Node gehoert nicht ins Container-Image)."""
import json
import os
import re
import shutil
import subprocess
import tempfile

import booking_harness as h
from booking_harness import c, check

NODE = shutil.which("node")
if not NODE:
    print("  SKIP  Node.js nicht gefunden - JavaScript-Pruefung uebersprungen")
    raise SystemExit(0)

tmp = tempfile.mkdtemp()
c.sm_get = lambda path, params=None: []
html = c._render_history_page()
scripts = re.findall(r"<script(?![^>]*src=)[^>]*>(.*?)</script>", html, re.S)

print("1) Syntax aller Inline-Skripte (echter JavaScript-Parser)")
check("die Seite enthaelt Inline-Skripte", len(scripts) >= 3, str(len(scripts)))
for index, js in enumerate(scripts):
    path = os.path.join(tmp, f"script{index}.js")
    open(path, "w", encoding="utf-8").write(js)
    result = subprocess.run([NODE, "--check", path], capture_output=True, text=True)
    check(f"Skript {index} ({js.strip()[:35]!r}...) ist gueltiges JavaScript", result.returncode == 0,
          (result.stderr.strip().splitlines() or [""])[-1])

print("   weitere Seite: AMS-Spulen (/slots)")
c.ha_states = lambda: {}
slots_html = c._render_slots_page()
slots_scripts = re.findall(r"<script(?![^>]*src=)[^>]*>(.*?)</script>", slots_html, re.S)
check("die Spulen-Seite enthaelt Inline-Skripte", len(slots_scripts) >= 1, str(len(slots_scripts)))
for index, js in enumerate(slots_scripts):
    path = os.path.join(tmp, f"slots{index}.js")
    open(path, "w", encoding="utf-8").write(js)
    result = subprocess.run([NODE, "--check", path], capture_output=True, text=True)
    check(f"/slots Skript {index} ist gueltiges JavaScript", result.returncode == 0, (result.stderr.strip().splitlines() or [""])[-1])

print("2) Keine verdoppelten Klammern im Dialog-Skript")
add_entry = next(js for js in scripts if "function openAddEntryModal" in js)
check("kein '{{' und kein '}}' im ausgelieferten Skript (ausser in Strings)",
      "{{" not in add_entry.replace("'{{'", "") and "}}" not in add_entry.replace("'}}'", ""))
check("Objekt-Literal colors.push({ ... }) ist intakt", "colors.push({\n" in add_entry)

print("3) Dialog gegen DOM-Attrappe ausfuehren")
HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const code = fs.readFileSync(process.argv[2], 'utf8');
const log = { alerts: [], xhr: [], reloads: 0, listeners: {}, domReady: [] };

function element(id) {
  const el = {
    id, style: {}, value: '', files: [], children: [], attrs: {}, _q: {},
    _html: '',
    set innerHTML(v) { this._html = v; if (v === '') this.children = []; },
    get innerHTML() { return this._html; },
    appendChild(child) { this.children.push(child); },
    setAttribute(k, v) { this.attrs[k] = v; },
    getAttribute(k) { return this.attrs[k]; },
    remove() { this.removed = true; },
    reset() { log.reset = true; },
    addEventListener(type, fn) { log.listeners[id + ':' + type] = fn; },
    querySelector(sel) { return this._q[sel] || (this._q[sel] = { value: '' }); },
  };
  return el;
}

const byId = {};
['add-entry-modal', 'spools-container', 'add-entry-form', 'entry-name', 'entry-start', 'entry-end',
 'entry-status', 'entry-gcode', 'entry-image', 'entry-camera'].forEach(id => {
  // WICHTIG: Beim Laden des Skripts im <head> gibt es noch KEIN Formular im Dokument
  byId[id] = element(id);
});
let documentReady = false;
const document = {
  getElementById(id) { return documentReady ? (byId[id] || null) : null; },
  createElement(tag) { return element('neu-' + tag); },
  querySelectorAll(sel) { return byId['spools-container'].children; },
  addEventListener(type, fn) { if (type === 'DOMContentLoaded') log.domReady.push(fn); },
};

class FakeXHR {
  open(method, url, async) { this.method = method; this.url = url; log.xhr.push(this); }
  send(body) { this.body = body; }
}

const context = {
  document, console, Math, Date, parseInt, parseFloat, JSON, FormData,
  alert: (m) => log.alerts.push(m),
  location: { reload: () => { log.reloads++; } },
  XMLHttpRequest: FakeXHR,
  SPOOLS: { 26: 'PLA Matte Morandi Green' },
  SPOOLS_DATA: { 26: { filament: { name: 'PLA Matte Morandi Green', material: 'PLA', color_hex: '33586C', density: 1.24, diameter: 1.75 } } },
};
vm.createContext(context);
const out = { errors: [] };

try { vm.runInContext(code, context); out.loadedWithoutError = true; }
catch (e) { out.loadedWithoutError = false; out.errors.push('Laden: ' + e.message); }

documentReady = true;
out.domReadyHandlers = log.domReady.length;
try { log.domReady.forEach(fn => fn()); } catch (e) { out.errors.push('DOMContentLoaded: ' + e.message); }
out.submitListener = typeof log.listeners['add-entry-form:submit'];

try {
  vm.runInContext('openAddEntryModal()', context);
  const container = byId['spools-container'];
  out.rowsAfterOpen = container.children.length;
  out.modalDisplay = byId['add-entry-modal'].style.display;
  out.startFilled = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(byId['entry-start'].value);
  const row = container.children[0];
  out.rowHtml = row._html;
  out.rowId = row.id;

  // Zeile ausfuellen und absenden
  row._q['.spool-select'] = { value: '26' };
  row._q['.spool-weight'] = { value: '43.25' };
  row._q['.spool-length'] = { value: '14.50' };
  byId['entry-name'].value = '  Testdruck  ';
  byId['entry-start'].value = '2026-10-10T15:38';
  byId['entry-end'].value = '2026-10-10T18:10';
  byId['entry-status'].value = 'finish';
  byId['entry-gcode'].value = '/data/Metadata/plate_1.gcode';
  const submit = log.listeners['add-entry-form:submit'];
  let prevented = false;
  submit({ preventDefault() { prevented = true; } });
  out.prevented = prevented;
  out.xhrCount = log.xhr.length;
  const x = log.xhr[0];
  out.xhrMethod = x.method; out.xhrUrl = x.url;
  out.sent = {};
  for (const [k, v] of x.body.entries()) out.sent[k] = v;
  x.status = 200; x.onload();
  out.alertsAfterSave = log.alerts.slice();
  out.reloads = log.reloads;

  // Pflichtfeld fehlt -> Hinweis, kein Versand
  log.alerts.length = 0;
  byId['entry-name'].value = '';
  submit({ preventDefault() {} });
  out.xhrCountAfterInvalid = log.xhr.length;
  out.invalidAlert = log.alerts[0];

  // Keine Spule mit Gewicht -> Hinweis, kein Versand
  log.alerts.length = 0;
  byId['entry-name'].value = 'X';
  row._q['.spool-weight'] = { value: '' };
  submit({ preventDefault() {} });
  out.xhrCountAfterNoSpool = log.xhr.length;
  out.noSpoolAlert = log.alerts[0];

  // Zeile entfernen und Info aktualisieren
  vm.runInContext("removeSpoolRow('" + row.id + "')", context);
  out.removedCalled = true;
} catch (e) { out.errors.push('Ausfuehrung: ' + e.message); }
console.log(JSON.stringify(out));
"""
harness_path = os.path.join(tmp, "harness.js")
open(harness_path, "w", encoding="utf-8").write(HARNESS)
script_path = os.path.join(tmp, "add_entry.js")
open(script_path, "w", encoding="utf-8").write(add_entry)
run = subprocess.run([NODE, harness_path, script_path], capture_output=True, text=True, timeout=60)
check("Node-Lauf ohne Absturz", run.returncode == 0, run.stderr[-300:])
out = json.loads(run.stdout.strip().splitlines()[-1]) if run.stdout.strip() else {}
check("keine Fehler beim Laden, bei DOMContentLoaded und bei der Ausfuehrung", out.get("errors") == [], str(out.get("errors")))
check("Skript laedt im <head> OHNE auf das (noch nicht vorhandene) Formular zuzugreifen", out.get("loadedWithoutError") is True)
check("Formular-Listener wird erst nach DOMContentLoaded registriert",
      out.get("domReadyHandlers") == 1 and out.get("submitListener") == "function", str(out))
check("Dialog oeffnen: sichtbar, genau eine Spulenzeile, Startzeit gesetzt",
      out.get("modalDisplay") == "flex" and out.get("rowsAfterOpen") == 1 and out.get("startFilled") is True, str(out))
row_html = out.get("rowHtml", "")
row_id = out.get("rowId", "")
check("Spulenauswahl enthaelt gueltigen onchange-Aufruf mit Zeilen-ID",
      f"""onchange="updateSpoolInfo('{row_id}')\"""" in row_html, row_html[:300])
check("Entfernen-Knopf enthaelt gueltigen onclick-Aufruf mit Zeilen-ID",
      f"""onclick="removeSpoolRow('{row_id}')\"""" in row_html, row_html[-300:])
check("Spule aus der Liste ist waehlbar", '<option value="26">PLA Matte Morandi Green (#26)</option>' in row_html)
check("Absenden: Standard-Aktion verhindert, genau eine Anfrage POST /add-history",
      out.get("prevented") is True and out.get("xhrCount") == 1 and out.get("xhrMethod") == "POST" and out.get("xhrUrl") == "/add-history", str(out))
sent = out.get("sent", {})
check("gesendet: Name (getrimmt), Zeiten, Status, G-Code",
      sent.get("name") == "Testdruck" and sent.get("start") == "2026-10-10T15:38" and sent.get("end") == "2026-10-10T18:10"
      and sent.get("status") == "finish" and sent.get("gcode") == "/data/Metadata/plate_1.gcode", str(sent))
colors = json.loads(sent.get("colors", "[]"))
check("gesendet: Spule 26 mit 43.25 g, 14.5 m, Filamentdaten",
      len(colors) == 1 and colors[0]["spool_id"] == 26 and colors[0]["grams"] == 43.25 and colors[0]["length_m"] == 14.5
      and colors[0]["material"] == "PLA" and colors[0]["color_hex"] == "33586C" and colors[0]["density"] == 1.24, str(colors))
check("Erfolg: Meldung und Seite wird neu geladen", out.get("reloads") == 1 and any("erfolgreich" in a for a in out.get("alertsAfterSave", [])), str(out))
check("Pflichtfeld fehlt: Hinweis und KEIN Versand", out.get("xhrCountAfterInvalid") == 1 and "Pflichtfelder" in (out.get("invalidAlert") or ""), str(out))
check("Keine Spule mit Gewicht: Hinweis und KEIN Versand", out.get("xhrCountAfterNoSpool") == 1 and "mindestens eine Spule" in (out.get("noSpoolAlert") or ""), str(out))

print("4) Vertrag Skript -> Server: genau die Daten, die das Skript sendet, werden vom Endpunkt angenommen")
import http.client
import threading
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

sm = h.FakeSpoolman(h.make_spool(26))
sm.install()
os.makedirs(c.HISTORY_IMAGES_DIR, exist_ok=True)
server = ThreadingHTTPServer(("127.0.0.1", 18178), c._HistoryHandler)
threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    conn = http.client.HTTPConnection("127.0.0.1", 18178, timeout=15)
    conn.request("POST", "/add-history", urlencode(sent).encode(), {"Content-Type": "application/x-www-form-urlencoded"})
    response = conn.getresponse()
    reply = json.loads(response.read())
    check("Endpunkt nimmt die vom Skript gesendeten Felder an (200, ok)", response.status == 200 and reply.get("ok") is True, f"{response.status} {reply}")
    saved = next((r for r in c.load_history()["records"] if r["id"] == reply.get("id")), None)
    check("gespeichert: Name, G-Code, Status, Spule mit Gramm",
          saved is not None and saved["name"] == "Testdruck" and saved["gcode"] == "/data/Metadata/plate_1.gcode"
          and saved["status"] == "finish" and saved["colors"][0]["spool_id"] == 26 and saved["colors"][0]["grams"] == 43.25, str(saved))
finally:
    server.shutdown()

h.finish()
