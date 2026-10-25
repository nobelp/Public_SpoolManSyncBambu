"""Prueft die Druckplatten-Normalisierung (Issue #32) und ihre Einbindung in
consumption.py: Erfassung vom Sensor, Anzeige im Druckverlauf, manuelle
Korrektur. Ohne HA/Spoolman - Sensorwerte werden als states-dict simuliert."""
import consumption as c
from plate_types import normalize_plate_type, plate_label

ok = True


def check(label, cond):
    global ok
    print(("  PASS  " if cond else "  FAIL  ") + label)
    ok = ok and cond


print("1) Normalisierung: Cloud-Schluessel und Bambu-Studio-Texte")
cases = {
    "textured_plate": "textured_plate",
    "Textured PEI Plate": "textured_plate",       # beobachtet bei #160 (lokaler Druck)
    "textured pei plate": "textured_plate",
    "  Textured PEI Plate  ": "textured_plate",
    "supertack_plate": "supertack_plate",
    "Cool Plate (SuperTack)": "supertack_plate",
    "Bambu Cool Plate SuperTack": "supertack_plate",
    "cool_plate": "cool_plate",
    "Cool Plate": "cool_plate",
    "PLA Plate": "cool_plate",
    "hot_plate": "hot_plate",
    "High Temp Plate": "hot_plate",
    "Smooth PEI Plate": "hot_plate",
    "eng_plate": "eng_plate",
    "Engineering Plate": "eng_plate",
    "Textured Cool Plate": "textured_cool_plate",  # spezifischer als "textured"/"cool"
}
for raw, expected in cases.items():
    check(f"{raw!r} -> {expected}", normalize_plate_type(raw) == expected)

print("2) Leere/unbekannte Werte")
for raw in (None, "", "unknown", "unavailable", "None", "auto"):
    check(f"{raw!r} -> None", normalize_plate_type(raw) is None)
check("unbekanntes Format bleibt als Text", normalize_plate_type("Mystery Plate X") == "Mystery Plate X")
check("Label unbekannt = Originaltext", plate_label("Mystery Plate X") == "Mystery Plate X")
check("Label bekannt", plate_label("Textured PEI Plate") == "Strukturierte Platte")

print("3) Erfassung vom Sensor")
S = lambda v: {c.PLATE_TYPE_ENTITY: {"state": v}}
check("Sensor 'Textured PEI Plate' -> textured_plate", c._read_plate_type(S("Textured PEI Plate")) == "textured_plate")
check("Sensor 'unavailable' -> None", c._read_plate_type(S("unavailable")) is None)
check("Sensor fehlt -> None", c._read_plate_type({}) is None)
check("unbekanntes Format wird gespeichert (nicht verworfen)", c._read_plate_type(S("Mystery Plate X")) == "Mystery Plate X")

print("4) Anzeige im Druckverlauf (auch Altbestand ohne Datenaenderung)")


def plate_cell(value):
    rec = {"id": 160, "gcode": "/data/Metadata/plate_2.gcode", "name": "Test", "start": "2026-09-26T16:15:27+00:00",
           "end": "2026-09-26T18:03:41+00:00", "duration_min": 108, "status": "finish",
           "image": None, "camera_image": None, "plate_type": value, "colors": []}
    return c._render_history_rows([rec])


html = plate_cell("Textured PEI Plate")
check("#160-Format zeigt Bild der strukturierten Platte", 'src="/plate-image/textured_plate"' in html
      and 'alt="Strukturierte Platte"' in html)
check("Cloud-Schluessel weiterhin mit Bild", 'src="/plate-image/textured_plate"' in plate_cell("textured_plate"))
check("bekannter Typ ohne Bild als Text", "Technische Platte" in plate_cell("Engineering Plate"))
unk = plate_cell("Mystery <Plate>")
check("unbekannter Typ als Text (escaped) mit Hinweis", "Mystery &lt;Plate&gt;" in unk and "Unbekannter Plattentyp" in unk)

print("5) Manuelle Korrektur akzeptiert Studio-Text, lehnt Unsinn ab")
c.init_history_storage()  # leere Test-DB anlegen (Pfade kommen aus run_all.sh)
check("unbekannter Typ abgelehnt", "Unbekannter plate_type" in c._set_plate_type(999999, "gibtsnicht"))
check("Studio-Text wird normalisiert (Eintrag fehlt -> 'nicht gefunden', nicht 'unbekannt')",
      "nicht gefunden" in c._set_plate_type(999999, "Textured PEI Plate"))

print()
print("ALLE TESTS OK" if ok else "FEHLER")
raise SystemExit(0 if ok else 1)
