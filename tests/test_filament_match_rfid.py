"""Regressionstest Issue #51: neue Original-Bambu-Spule legte eine Filament-Dublette an.

Echter Fall (Log 2026-10-10 13:47): Der AMS meldete 'PLA' statt 'Bambu PLA Pure'.
Der Sync suchte nach Name+Material+Farbe, fand 'Bambu PLA Pure Milky Pink' (#F7CED7)
nicht und legte ein neues Filament 'PLA' ohne Hersteller an.
Kein Netzwerk: sm_get/sm_post/ensure_vendor werden durch Attrappen ersetzt.
"""
import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ.setdefault("STATE_FILE", os.path.join(_tmp, "state.json"))
os.environ.setdefault("LOG_FILE", os.path.join(_tmp, "sync.log"))

import sync as s

ok = True


def check(label, cond):
    global ok
    print(("  PASS  " if cond else "  FAIL  ") + label)
    ok = ok and cond


def fil(fid, name, material, hex_, vendor_id=1):
    return {"id": fid, "name": name, "material": material, "color_hex": hex_,
            "vendor": {"id": vendor_id, "name": "Bambu Lab"}}


def slot(name, color, material="PLA", **extra):
    return {"name": name, "type": material, "color": color, "tray_weight": 1000,
            "tray_uuid": "06B677BDA2EF4271B22EA12847868FE4", "remain": 100,
            "nozzle_temp_max": 0, "bed_temp": 0, "filament_id": "", **extra}


class SpoolmanAttrappe:
    """Merkt sich, welche Filamente/Hersteller angelegt wurden."""

    def __init__(self, filaments):
        self.filaments = list(filaments)
        self.created = []
        self.vendor_requests = []
        self.spools = []

    def install(self):
        s.sm_get = lambda path, params=None, timeout=10: list(self.filaments)
        s.sm_post = self._post
        s.ensure_vendor = self._vendor

    def _vendor(self, name):
        self.vendor_requests.append(name)
        return 1 if name else None

    def _post(self, path, data):
        if path == "/filament":
            new = {"id": 900 + len(self.created), **data}
            self.created.append(new)
            return new
        if path == "/spool":
            self.spools.append(data)
            return {"id": 34, **data}
        raise AssertionError(f"unerwarteter POST {path}")


MILKY_PINK = fil(67, "Bambu PLA Pure Milky Pink", "PLA", "F7CED7")
BASIC_WHITE = fil(10, "Bambu PLA Basic Jade White", "PLA", "FFFFFF")
PURE_WHITE = fil(11, "Bambu PLA Pure Pure White", "PLA", "FFFFFF")
MATTE_WHITE = fil(12, "Bambu PLA Matte Ivory White", "PLA", "FFFFFF")

print("1) Der gemeldete Fall: AMS-Name 'PLA' -> bestehendes Filament, KEINE Dublette")
sm = SpoolmanAttrappe([MILKY_PINK, BASIC_WHITE])
sm.install()
fid = s.find_or_create_filament(slot("PLA", "#F7CED7FF"), vendor_id=1)
check("bestehende ID 67 zurueckgegeben", fid == 67)
check("nichts neu angelegt", sm.created == [])

print("2) create_spool: RFID-Spule ohne 'Bambu' im Namen bekommt Hersteller 'Bambu Lab'")
sm = SpoolmanAttrappe([MILKY_PINK])
sm.install()
s.create_spool(slot("PLA", "#F7CED7FF"), "5E4163BF00000100", "AMS 1 - Slot 2")
check("Hersteller 'Bambu Lab' angefragt", sm.vendor_requests == ["Bambu Lab"])
check("Spule haengt am bestehenden Filament 67", sm.spools[0]["filament_id"] == 67)
check("kein neues Filament", sm.created == [])

print("3) Gegenprobe: Farbe unbekannt -> es wird weiterhin neu angelegt")
sm = SpoolmanAttrappe([MILKY_PINK])
sm.install()
s.find_or_create_filament(slot("PLA", "#123456FF"), vendor_id=1)
check("genau ein neues Filament", len(sm.created) == 1)
check("mit Farbe 123456", sm.created[0]["color_hex"] == "123456")

print("4) Gegenprobe: gleiche Farbe, anderes Material -> kein Treffer")
sm = SpoolmanAttrappe([fil(70, "Bambu PETG HF Milky Pink", "PETG", "F7CED7")])
sm.install()
s.find_or_create_filament(slot("PLA", "#F7CED7FF"), vendor_id=1)
check("PLA legt neu an (PETG wird nicht verwendet)", len(sm.created) == 1)

print("5) Gleiche Farbe in mehreren Linien: AMS meldet 'Bambu PLA Pure' -> Pure-Filament")
sm = SpoolmanAttrappe([BASIC_WHITE, PURE_WHITE, MATTE_WHITE])
sm.install()
fid = s.find_or_create_filament(slot("Bambu PLA Pure", "#FFFFFFFF"), vendor_id=1)
check("Pure White -> ID 11", fid == 11)
check("nichts neu angelegt", sm.created == [])

print("5b) Exakter Namenstreffer (Name+Material+Farbe) hat Vorrang vor dem Fallback")
sm = SpoolmanAttrappe([fil(5, "Bambu PLA Pure Milky Pink", "PLA", "F7CED7"),
                       fil(2, "Bambu PLA Milky Pink Sondereditions", "PLA", "F7CED7")])
sm.install()
check("exakter Name -> ID 5 (nicht die kleinere ID 2)",
      s.find_or_create_filament(slot("Bambu PLA Pure Milky Pink", "#F7CED7FF"), vendor_id=1) == 5)

print("6) Produktlinie bekannt: 'Bambu PLA Matte' faellt NICHT auf 'Basic' gleicher Farbe")
sm = SpoolmanAttrappe([BASIC_WHITE])
sm.install()
s.find_or_create_filament(slot("Bambu PLA Matte", "#FFFFFFFF"), vendor_id=1)
check("neu angelegt statt falscher Zuordnung", len(sm.created) == 1)

print("7) Produktlinie bekannt und passendes Filament vorhanden -> wiederverwendet")
sm = SpoolmanAttrappe([BASIC_WHITE, MATTE_WHITE])
sm.install()
fid = s.find_or_create_filament(slot("Bambu PLA Matte", "#FFFFFFFF"), vendor_id=1)
check("Matte Ivory White -> ID 12", fid == 12)

print("8) Mehrdeutig (AMS nur 'PLA', Farbe in mehreren Linien) -> aeltestes, nie neu anlegen")
sm = SpoolmanAttrappe([PURE_WHITE, BASIC_WHITE])
sm.install()
fid = s.find_or_create_filament(slot("PLA", "#FFFFFFFF"), vendor_id=1)
check("kleinste ID (10)", fid == 10)
check("nichts neu angelegt", sm.created == [])

print("9) Gross-/Kleinschreibung bei Hex und Name ist egal")
sm = SpoolmanAttrappe([fil(67, "Bambu PLA Pure Milky Pink", "PLA", "f7ced7")])
sm.install()
check("Hex klein gespeichert wird gefunden",
      s.find_or_create_filament(slot("PLA", "#f7ced7ff"), vendor_id=1) == 67)

print("10) Fremdhersteller ohne RFID-Annahme: _detect_vendor bleibt unveraendert")
check("'PLA' allein -> kein Hersteller", s._detect_vendor("PLA") is None)
check("'Bambu PLA Basic' -> Bambu Lab", s._detect_vendor("Bambu PLA Basic") == "Bambu Lab")

print("\nERGEBNIS:", "ALLE TESTS PASS" if ok else "FEHLER")
raise SystemExit(0 if ok else 1)
