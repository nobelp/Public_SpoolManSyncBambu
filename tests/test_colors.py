"""Prueft, dass die History-Farben bei Spulenwechsel/Platzhalter korrekt
gepflegt werden - insbesondere dass bereits gebuchte Gramm NIE verloren gehen."""
import consumption as c

ok = True


def check(label, cond):
    global ok
    print(("  PASS  " if cond else "  FAIL  ") + label)
    ok = ok and cond


SPOOL20 = {"id": 20, "filament": {"name": "Bambu PLA Silk", "material": "PLA",
                                  "color_hex": "7F3696", "density": 1.24, "diameter": 1.75}}
TRAY = "AMS 1 Tray 4"


def fill_placeholder_block(job, spool, tray):
    """Spiegelt den Block aus process_once (Platzhalter befuellen / anlegen)."""
    colors_used = job.setdefault("colors_used", {})
    current = colors_used.get(tray)
    if not current:
        colors_used[tray] = [c._new_color_entry(spool["id"], spool["filament"], tray)]
    elif current[-1].get("spool_id") is None:
        current[-1].update({
            "spool_id": spool["id"],
            "filament_name": spool["filament"].get("name"),
            "material": spool["filament"].get("material"),
            "color_hex": spool["filament"].get("color_hex"),
            "density": spool["filament"].get("density"),
            "diameter": spool["filament"].get("diameter"),
        })


print("1) Platzhalter (spool_id=None) wird befuellt, nicht ersetzt")
job = {"colors_used": {TRAY: [c._new_color_entry(None, {}, TRAY)]}}
job["colors_used"][TRAY][0]["grams"] = 0.0
fill_placeholder_block(job, SPOOL20, TRAY)
entries = job["colors_used"][TRAY]
check("genau 1 Eintrag", len(entries) == 1)
check("Spule gesetzt", entries[0]["spool_id"] == 20)
check("Filament-Name gesetzt", entries[0]["filament_name"] == "Bambu PLA Silk")

print("2) Bereits gebuchte Gramm bleiben erhalten (der fruehere Bug)")
job = {"colors_used": {TRAY: [c._new_color_entry(19, {"name": "SUNLU PLA Marmor"}, TRAY)]}}
job["colors_used"][TRAY][0]["grams"] = 53.0
fill_placeholder_block(job, SPOOL20, TRAY)
entries = job["colors_used"][TRAY]
check("Eintrag von #19 nicht ersetzt", len(entries) == 1 and entries[0]["spool_id"] == 19)
check("53.0g erhalten (vorher: auf 0 zurueckgesetzt)", entries[0]["grams"] == 53.0)

print("3) Mehrere Spulen pro Tray: Liste bleibt unangetastet")
job = {"colors_used": {TRAY: [
    dict(c._new_color_entry(19, {"name": "SUNLU"}, TRAY), grams=53.0),
    dict(c._new_color_entry(20, {"name": "Bambu"}, TRAY), grams=80.8),
]}}
fill_placeholder_block(job, SPOOL20, TRAY)
entries = job["colors_used"][TRAY]
check("beide Eintraege erhalten", [e["spool_id"] for e in entries] == [19, 20])
check("Summe bleibt 133.8g", round(sum(e["grams"] for e in entries), 1) == 133.8)

print("4) Noch keine Farbe vorhanden -> Eintrag wird angelegt")
job = {}
fill_placeholder_block(job, SPOOL20, TRAY)
check("Eintrag angelegt", job["colors_used"][TRAY][0]["spool_id"] == 20)
check("Zaehler startet bei 0", job["colors_used"][TRAY][0]["grams"] == 0.0)

print("\nERGEBNIS:", "ALLE TESTS PASS" if ok else "FEHLER")
raise SystemExit(0 if ok else 1)
