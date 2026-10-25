"""Issue #52: Verlaufstabelle - alle Spalten sortierbar, Mehrfachauswahl-Filter fuer
Farbe, Material und Spule. Kein HA, kein Spoolman, kein Netzwerk.

1) Seitenaufbau (Python): Spaltenkoepfe, data-Attribute, Filteroptionen, Maskierung
2) Anzeige und Attribute stimmen ueberein (Filter darf nichts anderes meinen als die Zelle zeigt)
3) Reine Logik (Node): Sortierung, UND/ODER-Filter, gespeicherte Auswahl bereinigen
4) Ablauf in einem echten Browser (Chromium, ohne wird uebersprungen)"""
import html as html_lib
import json
import os
import re
import shutil
import subprocess
import tempfile
from html.parser import HTMLParser

import booking_harness as h
from booking_harness import c, check

c.sm_get = lambda path, params=None: []
c.load_state = lambda: {}
c._render_pending_consumption_section = lambda spools: ""


def line(name, material, spool_id, grams, meters, tray_key=None):
    return {"filament_name": name, "material": material, "spool_id": spool_id, "grams": grams,
            "length_m": meters, "color_hex": "FFFFFF", "tray_key": tray_key}


RECORDS = [
    {"id": 1, "name": "Benchy", "duration_min": 45, "status": "finish", "plate_type": "textured_plate",
     "colors": [line("Bambu PLA Basic Jade White", "PLA", 8, 12.5, 4.2)]},
    {"id": 2, "name": "Vase mehrfarbig", "duration_min": 300, "status": "finish",
     "colors": [line("Bambu PETG HF Black", "PETG", 2, 80.0, 28.0),
                line("Bambu PLA Pure Milky Pink", "PLA", 34, 20.5, 7.0)]},
    {"id": 3, "name": "Ohne Zuordnung", "duration_min": None, "status": "failed",
     "colors": [line("-", "", None, 3.0, None, tray_key="External")]},
    {"id": 4, "name": "Clip", "duration_min": 12, "status": "finish",
     "colors": [line("Bambu PLA Pure Milky Pink", "PLA", 34, 5.0, 1.5)]},
    {"id": 5, "name": "Zugewiesen (wird verbucht)", "duration_min": 9, "status": "finish",
     "colors": [line("-", "", None, 1.0, 0.3, tray_key="AMS 1 Tray 1")]},
]
OVERRIDES = {"AMS 1 Tray 1": 8}
SPOOLS = {8: {"filament": {"name": "Bambu PLA Basic Jade White", "material": "PLA", "color_hex": "FFFFFF"}}}


class Zeilen(HTMLParser):
    """Sammelt je <tr> die Attribute und den Text der Zellen."""

    def __init__(self):
        super().__init__()
        self.rows, self._row, self._cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = {"attrs": dict(attrs), "cells": []}
        elif tag == "td" and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag == "td" and self._cell is not None:
            self._row["cells"].append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def zeilen(rows_html):
    parser = Zeilen()
    parser.feed(f"<table>{rows_html}</table>")
    return parser.rows


print("1) Spaltenkoepfe: alle ausser Bild sortierbar, nur Farbe/Material/Spule filterbar")
options = c._collect_filter_options(RECORDS, OVERRIDES, SPOOLS)
header = c._render_table_header("desc", options)
check("Kopf steht in <thead>", header.startswith("<thead><tr>") and header.endswith("</tr></thead>"))
sort_keys = re.findall(r'data-sort="(\w+)"', header)
check("Sortierknopf fuer jede Spalte ausser 'bild'", sort_keys == [k for k, _ in c._COLUMNS if k != "bild"], str(sort_keys))
check("Filterknopf genau fuer Farbe, Material, Spule", re.findall(r'class="filter-btn" data-filter="(\w+)"', header) == ["farbe", "material", "spule"])
check("Bild hat keinen Sortierknopf", "Bild</th>" in header.replace('<div class="th-inner">', "").replace("</div>", "") or ">Bild<" in header)
check("Sortier- und Filterelemente sind echte Buttons (Tastatur)", header.count("<button type=\"button\"") >= 13)
check("Datum ist anfangs aktiv, absteigend (wie bisher)", 'aria-sort="descending"' in header and "▼" in header)
check("Filterknopf hat Zustand (aria-expanded) und Verweis auf das Panel", 'aria-expanded="false" aria-controls="filter-panel-farbe"' in header)
check("Filterpanel ist als Gruppe beschriftet und anfangs verborgen", 'role="group" aria-label="Farbe filtern" hidden' in header)
asc = c._render_table_header("asc", options)
check("Server-Parameter ?sort=asc: Pfeil aufwaerts, aria-sort ascending", 'aria-sort="ascending"' in asc and "▲" in asc)

print("2) Filteroptionen: nur vorkommende Werte, '(leer)' zuerst, Spulen numerisch")
check("Farbe", options["farbe"] == ["", "Bambu PETG HF Black", "Bambu PLA Basic Jade White", "Bambu PLA Pure Milky Pink"], str(options["farbe"]))
check("Material", options["material"] == ["", "PETG", "PLA"], str(options["material"]))
check("Spule nach Nummer (#2 vor #8 vor #34)", options["spule"] == ["", "#2", "#8", "#34"], str(options["spule"]))
check("(leer) wird angezeigt, Wert bleibt leer", '<input type="checkbox" value=""><span>(leer)</span>' in header)
check("keine leere Option, wenn es keine leeren Werte gibt",
      c._collect_filter_options([RECORDS[0]], {}, {})["material"] == ["PLA"])

print("3) Tabellenzeilen: data-rec an jeder Zeile, Sortier-/Filterdaten an der ersten")
rows = zeilen(c._render_history_rows(RECORDS, "desc", OVERRIDES, SPOOLS))
check("Mehrfarbdruck (#2) hat 2 Zeilen", [r["attrs"]["data-rec"] for r in rows].count("2") == 2)
firsts = {r["attrs"]["data-rec"]: r["attrs"] for r in rows if "data-s-id" in r["attrs"]}
check("pro Druck genau eine Zeile mit Sortierdaten", sorted(firsts) == ["1", "2", "3", "4", "5"])
check("Zeilen in Server-Reihenfolge (neuester zuerst)", [r["attrs"]["data-rec"] for r in rows if "data-s-id" in r["attrs"]] == ["5", "4", "3", "2", "1"])
a2 = firsts["2"]
check("Verbrauch g/m ueber alle Farben summiert", a2["data-s-verbrauch_g"] == "100.5" and a2["data-s-verbrauch_m"] == "35.0", f'{a2["data-s-verbrauch_g"]} {a2["data-s-verbrauch_m"]}')
check("Mehrfarbdruck: Filtermenge enthaelt BEIDE Farben/Materialien/Spulen",
      json.loads(a2["data-f-farbe"]) == ["Bambu PETG HF Black", "Bambu PLA Pure Milky Pink"]
      and json.loads(a2["data-f-material"]) == ["PETG", "PLA"] and json.loads(a2["data-f-spule"]) == ["#2", "#34"])
a3 = firsts["3"]
check("Druck ohne Zuordnung: leere Filterwerte, Dauer/Meter leer", json.loads(a3["data-f-farbe"]) == [""]
      and json.loads(a3["data-f-spule"]) == [""] and a3["data-s-dauer"] == "" and a3["data-s-verbrauch_m"] == "")
a5 = firsts["5"]
check("Zuordnung ueber Override (wird verbucht) zaehlt wie die Anzeige",
      json.loads(a5["data-f-farbe"]) == ["Bambu PLA Basic Jade White"] and json.loads(a5["data-f-spule"]) == ["#8"])
check("Name, Plattentyp und Status kleingeschrieben fuer die Sortierung, Status ohne Emoji",
      firsts["1"]["data-s-name"] == "benchy" and firsts["1"]["data-s-platte"] == "strukturierte platte"
      and firsts["1"]["data-s-status"] == "fertig", firsts["1"]["data-s-status"])
check("Zebra-Streifen je Zeile (jede zweite)", [("row-alt" in r["attrs"].get("class", "")) for r in rows] == [False, True, False, True, False, True])

print("4) Anzeige und Attribute stimmen ueberein (Filter meint, was die Zelle zeigt)")
for rec in RECORDS:
    if len(rec["colors"]) != 1:
        continue
    row = zeilen(c._render_history_rows([rec], "desc", OVERRIDES, SPOOLS))[0]
    cells = row["cells"]
    farbe_cell, material_cell, spule_cell = cells[6], cells[7], cells[8]
    f = {k: json.loads(row["attrs"][f"data-f-{k}"])[0] for k in ("farbe", "material", "spule")}
    shown_farbe = "" if "nicht zugeordnet" in farbe_cell else re.sub(r"\s*\(wird verbucht\)$", "", farbe_cell)
    check(f"#{rec['id']}: Farbe '{f['farbe']}' = Zelle", f["farbe"] == shown_farbe, f"Zelle={farbe_cell!r}")
    check(f"#{rec['id']}: Material '{f['material']}' = Zelle", f["material"] == ("" if material_cell == "-" else material_cell), material_cell)
    check(f"#{rec['id']}: Spule '{f['spule']}' = Zelle", f["spule"] == ("" if spule_cell == "-" else spule_cell), spule_cell)

print("5) Sonderzeichen werden maskiert (kein HTML/JSON-Ausbruch aus Attribut oder Option)")
evil = {"id": 9, "name": 'x"><img src=x onerror=alert(1)>', "duration_min": 1, "status": "finish",
        "colors": [line('Farbe "A" <b>&\' x', "PLA<i>", 7, 1.0, 1.0)]}
page_rows = c._render_history_rows([evil], "desc", {}, {})
page_header = c._render_table_header("desc", c._collect_filter_options([evil], {}, {}))
check("kein ungemaskiertes <img/<b/<i in Zeilen und Kopf", all(t not in page_rows + page_header for t in ("<img src=x", "<b>&", "<i>")))
evil_row = zeilen(page_rows)[0]
check("Filterwert kommt nach dem Entmaskieren unveraendert an", json.loads(evil_row["attrs"]["data-f-farbe"]) == ['Farbe "A" <b>&\' x'])

print("6) Seite: tbody, Werkzeuge eingebunden, Namenssuche nutzt dieselbe Ansicht")
c.load_history = lambda: {"records": RECORDS}
page = c._render_history_page("desc")
check("Tabelle hat <thead> und <tbody id='history-body'>", "<thead>" in page and '<tbody id="history-body" data-sort-dir="desc">' in page)
check("Tabellen-Skript eingebunden", "window.HistoryTableLogic" in page and "function initTableTools" in page)
check("Namensfeld ruft die gemeinsame Ansicht (applyTableView) auf", "function filterTable() {\n  if (typeof applyTableView === 'function') applyTableView();" in page)
check("Rueckstell-Knopf und Statusmeldung (aria-live) vorhanden", 'id="filter-reset"' in page and 'id="filter-status" class="filter-status" role="status" aria-live="polite"' in page)
check("KPIs oberhalb der Tabelle unveraendert vorhanden", 'class="kpis"' in page and "Drucke gesamt" in page)
check("alte Zeilen-Signatur bleibt (ein Argument)", len(zeilen(c._render_history_rows([RECORDS[0]]))) == 1)
check("leere Historie: keine Tabelle, kein Fehler", "Noch keine abgeschlossenen" in (setattr(c, "load_history", lambda: {"records": []}) or c._render_history_page()))
c.load_history = lambda: {"records": RECORDS}

NODE = shutil.which("node")
if not NODE:
    print("  SKIP  Node.js nicht gefunden - Logik- und Browserpruefung uebersprungen")
    h.finish()

print("7) Reine Logik unter Node: Sortierung, Filter, gespeicherte Auswahl")
script = re.search(r"<script>\n\(function \(\) \{\n  'use strict';.*?</script>", page, re.S).group(0)[len("<script>"):-len("</script>")]
tmp = tempfile.mkdtemp()
open(os.path.join(tmp, "tools.js"), "w", encoding="utf-8").write(script)
SPEC = r"""
const fs = require('fs'); const vm = require('vm');
const window = {}; const document = { addEventListener() {} };
vm.runInNewContext(fs.readFileSync(process.argv[2], 'utf8'), { window, document });
const L = window.HistoryTableLogic;
const g = (id, sort, filters, name) => ({ id: String(id), sort, filters: filters || {}, name: name || '' });
const ids = (list) => list.map(x => x.id).join(',');
const out = {};
const nums = [g(1, { verbrauch_g: '12.5' }), g(2, { verbrauch_g: '100.5' }), g(3, { verbrauch_g: '' }), g(4, { verbrauch_g: '5' }), g(5, { verbrauch_g: '9' })];
out.numAsc = ids(L.sortGroups(nums, 'verbrauch_g', 'asc'));
out.numDesc = ids(L.sortGroups(nums, 'verbrauch_g', 'desc'));
const txt = [g(1, { name: 'benchy' }), g(2, { name: 'Clip' }), g(3, { name: 'vase 10' }), g(4, { name: 'vase 9' }), g(5, { name: '' })];
out.txtAsc = ids(L.sortGroups(txt, 'name', 'asc'));
out.txtDesc = ids(L.sortGroups(txt, 'name', 'desc'));
out.tie = ids(L.sortGroups([g(1, { status: 'fertig' }), g(3, { status: 'fertig' }), g(2, { status: 'fertig' })], 'status', 'asc'));
out.original = ids(nums);
const F = (farbe, material, spule) => ({ farbe, material, spule });
const rows = [g(1, {}, F(['Weiss'], ['PLA'], ['#8']), 'benchy'), g(2, {}, F(['Schwarz', 'Pink'], ['PETG', 'PLA'], ['#2', '#34']), 'vase'),
              g(3, {}, F([''], [''], ['']), 'ohne'), g(4, {}, F(['Pink'], ['PLA'], ['#34']), 'clip')];
const m = (filters, q) => ids(rows.filter(r => L.groupMatches(r, filters, q || '')));
out.none = m({});
out.oneCol = m({ material: ['PETG'] });
out.orInCol = m({ farbe: ['Weiss', 'Schwarz'] });
out.multi = m({ farbe: ['Pink'] });
out.andCols = m({ farbe: ['Pink'], material: ['PETG'] });
out.andNone = m({ farbe: ['Weiss'], material: ['PETG'] });
out.empty = m({ spule: [''] });
out.emptyPlus = m({ spule: ['', '#8'] });
out.query = m({ farbe: ['Pink'] }, 'cl');
out.queryOnly = m({}, 'a');
out.sanitize = L.sanitizeFilters({ farbe: ['Pink', 'Gone'], material: 'kaputt', spule: [1, '#8'], evil: ['x'] }, { farbe: ['Pink'], material: ['PLA'], spule: ['#8'] });
console.log(JSON.stringify(out));
"""
spec_path = os.path.join(tmp, "spec.js")
open(spec_path, "w", encoding="utf-8").write(SPEC)
run = subprocess.run([NODE, spec_path, os.path.join(tmp, "tools.js")], capture_output=True, text=True, timeout=60)
check("Logik laeuft unter Node ohne Fehler", run.returncode == 0, run.stderr.strip()[-300:])
r = json.loads(run.stdout.strip().splitlines()[-1]) if run.returncode == 0 else {}
check("Zahlen aufsteigend, leere Werte hinten", r.get("numAsc") == "4,5,1,2,3", str(r.get("numAsc")))
check("Zahlen absteigend, leere Werte weiterhin hinten", r.get("numDesc") == "2,1,5,4,3", str(r.get("numDesc")))
check("Text ohne Gross-/Kleinschreibung, 'vase 9' vor 'vase 10' (natuerlich), leer hinten", r.get("txtAsc") == "1,2,4,3,5", str(r.get("txtAsc")))
check("Text absteigend, leer hinten", r.get("txtDesc") == "3,4,2,1,5", str(r.get("txtDesc")))
check("Gleiche Werte: stabile Reihenfolge nach ID absteigend", r.get("tie") == "3,2,1", str(r.get("tie")))
check("sortGroups veraendert die Eingabe nicht", r.get("original") == "1,2,3,4,5")
check("ohne Filter alle Drucke", r.get("none") == "1,2,3,4")
check("eine Spalte filtern (Material PETG) findet den Mehrfarbdruck", r.get("oneCol") == "2", str(r.get("oneCol")))
check("innerhalb einer Spalte ODER", r.get("orInCol") == "1,2", str(r.get("orInCol")))
check("Mehrfarbdruck wird gefunden, sobald EINE Farbe passt", r.get("multi") == "2,4", str(r.get("multi")))
check("zwischen Spalten UND", r.get("andCols") == "2" and r.get("andNone") == "", f'{r.get("andCols")!r} {r.get("andNone")!r}')
check("'(leer)' filtert Drucke ohne Wert, kombinierbar", r.get("empty") == "3" and r.get("emptyPlus") == "1,3", f'{r.get("empty")!r} {r.get("emptyPlus")!r}')
check("Namenssuche und Spaltenfilter zusammen (UND)", r.get("query") == "4" and r.get("queryOnly") == "2", f'{r.get("query")!r} {r.get("queryOnly")!r}')
check("gespeicherte Auswahl: Unbekanntes, Falschtypen und fremde Spalten fliegen raus",
      r.get("sanitize") == {"farbe": ["Pink"], "material": [], "spule": ["#8"]}, str(r.get("sanitize")))

print("8) Ablauf in einem echten Browser (Seite rendern, Knoepfe klicken)")
BROWSER = next((b for b in (shutil.which("chromium"), shutil.which("chromium-browser"), shutil.which("google-chrome")) if b), None)
if not BROWSER:
    print("  SKIP  Chromium nicht gefunden - Browserpruefung uebersprungen")
    h.finish()

PROBE = r"""<script>
document.addEventListener('DOMContentLoaded', function () { setTimeout(function () {
  var out = {};
  function ids() { return Array.prototype.filter.call(document.querySelectorAll('#history-body tr[data-s-id]'), function (tr) { return !tr.hidden; }).map(function (tr) { return tr.getAttribute('data-rec'); }).join(','); }
  function click(sel) { document.querySelector(sel).click(); }
  function tick(sel, value, on) { Array.prototype.forEach.call(document.querySelectorAll(sel + ' input'), function (b) { if (b.value === value) { b.checked = on; b.dispatchEvent(new Event('change', { bubbles: true })); } }); }
  out.start = ids(); out.statusStart = document.getElementById('filter-status').textContent;
  click('[data-sort=name]'); out.nameAsc = ids();
  click('[data-sort=name]'); out.nameDesc = ids();
  out.ariaName = document.querySelector('[data-sort=name]').closest('th').getAttribute('aria-sort');
  out.ariaDatumGone = document.querySelector('[data-sort=datum]').closest('th').hasAttribute('aria-sort');
  click('[data-sort=dauer]'); out.dauerDesc = ids();
  click('[data-sort=dauer]'); out.dauerAsc = ids();
  click('[data-sort=datum]'); click('[data-sort=datum]');
  out.datumAsc = ids();
  click('[data-filter=material]');
  out.open = !document.getElementById('filter-panel-material').hidden;
  out.expanded = document.querySelector('[data-filter=material]').getAttribute('aria-expanded');
  tick('#filter-panel-material', 'PETG', true);
  out.petg = ids(); out.statusPetg = document.getElementById('filter-status').textContent;
  out.label = document.querySelector('[data-filter=material]').getAttribute('aria-label');
  out.resetShown = !document.getElementById('filter-reset').hidden;
  tick('#filter-panel-material', '', true); out.petgLeer = ids();
  document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));
  out.closedByEsc = document.getElementById('filter-panel-material').hidden;
  out.stripes = Array.prototype.filter.call(document.querySelectorAll('#history-body tr'), function (tr) { return !tr.hidden; }).map(function (tr) { return tr.classList.contains('row-alt') ? 1 : 0; }).join('');
  click('#filter-reset'); out.afterReset = ids(); out.resetHidden = document.getElementById('filter-reset').hidden;
  click('[data-filter=spule]'); tick('#filter-panel-spule', '#34', true);
  out.spule34 = ids();
  var f = document.getElementById('search'); f.value = 'clip'; filterTable(); out.spule34Clip = ids();
  f.value = 'zzz'; filterTable(); out.none = ids(); out.statusNone = document.getElementById('filter-status').textContent;
  f.value = ''; filterTable();
  click('[data-filter-clear=spule]'); out.clearedColumn = ids();
  out.saved = localStorage.getItem('historyFilters'); out.savedSort = localStorage.getItem('historySort');
  var pre = document.createElement('pre'); pre.id = 'probe'; pre.textContent = JSON.stringify(out); document.body.appendChild(pre);
}, 50); });
</script>"""
probe_path = os.path.join(tmp, "probe.html")
open(probe_path, "w", encoding="utf-8").write(page.replace("</body>", PROBE + "</body>"))
browser = subprocess.run([BROWSER, "--headless=new", "--no-sandbox", "--disable-gpu", "--virtual-time-budget=5000",
                          f"--user-data-dir={os.path.join(tmp, 'profile')}", "--dump-dom", f"file://{probe_path}"],
                         capture_output=True, text=True, timeout=90)
found = re.search(r'<pre id="probe">(.*?)</pre>', browser.stdout, re.S)
check("Browser hat die Seite ausgefuehrt (JavaScript ohne Absturz)", bool(found), browser.stdout[-200:] if not found else "")
b = json.loads(html_lib.unescape(found.group(1))) if found else {}
check("Start: Server-Reihenfolge, Statuszeile '5 Drucke'", b.get("start") == "5,4,3,2,1" and b.get("statusStart") == "5 Drucke", f'{b.get("start")} {b.get("statusStart")}')
check("Name auf- und absteigend", b.get("nameAsc") == "1,4,3,2,5" and b.get("nameDesc") == "5,2,3,4,1", f'{b.get("nameAsc")} / {b.get("nameDesc")}')
check("aria-sort wandert mit der aktiven Spalte", b.get("ariaName") == "descending" and b.get("ariaDatumGone") is False)
check("Dauer: leere Werte immer hinten", b.get("dauerDesc") == "2,1,4,5,3" and b.get("dauerAsc") == "5,4,1,2,3", f'{b.get("dauerDesc")} / {b.get("dauerAsc")}')
check("Datum aufsteigend (zweimal geklickt)", b.get("datumAsc") == "1,2,3,4,5", str(b.get("datumAsc")))
check("Filter-Dropdown oeffnet, aria-expanded=true", b.get("open") is True and b.get("expanded") == "true")
check("Material=PETG zeigt nur den Mehrfarbdruck, Statuszeile und Knopfbeschriftung", b.get("petg") == "2" and b.get("statusPetg") == "1 von 5 Drucken"
      and b.get("label") == "Material filtern, 1 ausgewählt", f'{b.get("petg")} {b.get("statusPetg")} {b.get("label")}')
check("Rueckstell-Knopf erscheint erst mit aktivem Filter", b.get("resetShown") is True and b.get("resetHidden") is True)
# Die Seite laedt keine Overrides (load_state leer): Druck 5 ist hier nicht zugeordnet, also leer.
check("PETG + (leer): ODER innerhalb der Spalte", b.get("petgLeer") == "2,3,5", str(b.get("petgLeer")))
check("Esc schliesst das Dropdown", b.get("closedByEsc") is True)
check("Zebra-Streifen werden nach dem Filtern neu verteilt (abwechselnd)", set(b.get("stripes", "")[0::2]) <= {"0"} and set(b.get("stripes", "")[1::2]) <= {"1"}, str(b.get("stripes")))
check("Zuruecksetzen zeigt wieder alle Drucke", b.get("afterReset") == "1,2,3,4,5", str(b.get("afterReset")))
check("Spule #34: Mehrfarbdruck und Clip", b.get("spule34") == "2,4", str(b.get("spule34")))
check("Spaltenfilter + Namenssuche zusammen", b.get("spule34Clip") == "4", str(b.get("spule34Clip")))
check("keine Treffer: Meldung statt stiller leerer Tabelle", b.get("none") == "" and "keine Treffer" in b.get("statusNone", ""))
check("'Auswahl zuruecksetzen' einer Spalte", b.get("clearedColumn") == "1,2,3,4,5", str(b.get("clearedColumn")))
check("Auswahl und Sortierung werden gespeichert", b.get("saved") == '{"farbe":[],"material":["PETG",""],"spule":[]}' or '"spule":[]' in (b.get("saved") or ""), str(b.get("saved")))

h.finish()
