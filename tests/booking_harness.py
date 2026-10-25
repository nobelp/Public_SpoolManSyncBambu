"""Gemeinsame Testumgebung fuer die Endbuchungs-Tests (kein HA, kein Spoolman,
kein Netzwerk, nichts in data/ wird veraendert).

WICHTIG: Dieses Modul MUSS vor `import consumption` importiert werden - es
setzt die Pfad-Umgebungsvariablen auf ein temporaeres Verzeichnis.

Bestandteile:
- FakeSpoolman: in-memory Spoolman mit used_weight und gezielten Ausfaellen
  (Timeout NACH angewendeter Buchung, Timeout VOR der Buchung, komplett down)
- Printer: baut die HA-States eines Druckers (Status, Fortschritt, Gewicht,
  AMS-Slots, externe Spule) fuer process_once()
- check()/finish(): Ausgabe im Stil der uebrigen Tests
"""
import copy
import os
import tempfile

TMP = tempfile.mkdtemp()
os.environ.update({
    "CONSUMPTION_STATE_FILE": os.path.join(TMP, "state.json"),
    "CONSUMPTION_LOG_FILE": os.path.join(TMP, "c.log"),
    "CONSUMPTION_HISTORY_FILE": os.path.join(TMP, "h.json"),
    "CONSUMPTION_HISTORY_DB": os.path.join(TMP, "h.db"),
    "CONSUMPTION_HISTORY_IMAGES_DIR": os.path.join(TMP, "img"),
    "WARTUNG_BACKUP_FILE": os.path.join(TMP, "w.json"),
    "CONSUMPTION_OVERRIDES_FILE": os.path.join(TMP, "o.json"),
})

import consumption as c  # noqa: E402

REAL_SM_USE = c.sm_use      # FakeSpoolman.install() ersetzt c.sm_use

c.init_history_storage()

_ok = True


def check(label, cond, detail=""):
    global _ok
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  → {detail}" if detail and not cond else ""))
    _ok = _ok and bool(cond)


def finish():
    print("\nERGEBNIS:", "ALLE TESTS PASS" if _ok else "FEHLER")
    raise SystemExit(0 if _ok else 1)


def approx(a, b, tol=0.011):
    return a is not None and abs(a - b) <= tol


# ─────────────────────────────────────────────────────────────────
# Spoolman
# ─────────────────────────────────────────────────────────────────
def make_spool(spool_id, name="PLA Matte Morandi Green", tag_uid=None, used=0.0, initial=1000.0):
    extra = {"bambu_tag_uid": f'"{tag_uid}"'} if tag_uid else {}
    return {
        "id": spool_id, "initial_weight": initial, "used_weight": used,
        "remaining_weight": initial - used, "extra": extra, "location": "Lager",
        "filament": {"id": 100 + spool_id, "name": name, "material": "PLA",
                     "color_hex": "33586C", "density": 1.24, "diameter": 1.75},
    }


class FakeSpoolman:
    """Spoolman mit steuerbaren Ausfaellen.

    down=True                  jeder Zugriff wirft ConnectionError
    fail_use                   Liste, wird pro PUT /use von vorn abgearbeitet:
                               "after_apply"  = Buchung wird angewendet, der
                                                Client sieht aber einen Timeout
                               "before_apply" = Timeout, Buchung NICHT angewendet
    """

    def __init__(self, *spools):
        self.spools = {s["id"]: s for s in spools}
        self.down = False
        self.fail_use = []
        self.use_calls = []     # alle Aufrufe des Clients: (spool_id, grams)
        self.applied = []       # tatsaechlich angewendete Buchungen

    def install(self):
        c.sm_get = self.get
        c.sm_use = self.use
        c.sm_patch = lambda path, data: {}

    def used(self, spool_id):
        return self.spools[spool_id]["used_weight"]

    def get(self, path, params=None):
        if self.down:
            raise ConnectionError("Spoolman down (Test)")
        if path == "/spool":
            return [copy.deepcopy(s) for s in self.spools.values()]
        return copy.deepcopy(self.spools[int(path.rsplit("/", 1)[1])])

    def use(self, spool_id, grams):
        self.use_calls.append((spool_id, round(grams, 2)))
        if self.down:
            raise ConnectionError("Spoolman down (Test)")
        mode = self.fail_use.pop(0) if self.fail_use else None
        if mode == "before_apply":
            raise TimeoutError("Read timed out (Test, nicht angewendet)")
        spool = self.spools[spool_id]
        spool["used_weight"] = round(spool["used_weight"] + round(grams, 2), 2)
        spool["remaining_weight"] = round(spool["initial_weight"] - spool["used_weight"], 2)
        self.applied.append((spool_id, round(grams, 2)))
        if mode == "after_apply":
            raise TimeoutError("Read timed out (Test, aber angewendet)")
        return copy.deepcopy(spool)


# ─────────────────────────────────────────────────────────────────
# Drucker / Home Assistant
# ─────────────────────────────────────────────────────────────────
GCODE = "/data/Metadata/plate_1.gcode"
EMPTY_UID = "0000000000000000"


class Printer:
    """Liefert die HA-States eines (simulierten) P2S.

    states(status, progress, total_g, ams, external, ext_tag_uid) ->
      status:       running | finish | failed | idle | prepare
      progress:     0..100 oder None (= Sensor 'unavailable')
      total_g:      Wert des Gewichtssensors (Plangewicht des Drucks)
      ams:          {"AMS 1 Tray 1": (tag_uid, g)} - beteiligte AMS-Slots
      external:     externe Spule ist aktiv (Sensor ersetzt die AMS-Attribute
                    durch {"External Spool": total} - wie der echte Sensor)
      ext_active:   Wert des Aktiv-Sensors unabhaengig von den Attributen
                    (None = wie `external`); der Sensor kann "on" stehen, obwohl
                    der Druck ueber den AMS laeuft (Druck #215)
      ext_tag_uid:  tag_uid der externen Spule (EMPTY_UID = ohne RFID)
    """

    def __init__(self, start, gcode=GCODE, task="Testdruck"):
        self.start = start
        self.gcode = gcode
        self.task = task

    def states(self, status, progress, total_g=0.0, ams=None, external=False,
               ext_tag_uid=EMPTY_UID, start=None, ext_active=None):
        ams = ams or {}
        attrs = {"unit_of_measurement": "g", "device_class": "weight"}
        if external:
            attrs["External Spool"] = total_g
        else:
            attrs.update({tray: grams for tray, (_, grams) in ams.items()})
        out = {
            c.PRINT_STATUS_ENTITY: {"state": status, "attributes": {}, "last_reported": c._get_iso_timestamp()},
            c.PRINT_PROGRESS_ENTITY: {"state": "unavailable" if progress is None else str(progress)},
            c.PRINT_WEIGHT_ENTITY: {"state": str(total_g), "attributes": attrs},
            c.GCODE_ENTITY: {"state": self.gcode},
            c.STARTTIME_ENTITY: {"state": start or self.start},
            c.TASK_NAME_ENTITY: {"state": self.task},
            c.EXTERNAL_ACTIVE_ENTITY: {"state": "on" if (external if ext_active is None else ext_active) else "off"},
            f"{c.PRINTER_ENTITY_PREFIX}_externalspool_externe_spule": {
                "state": "Generic PLA", "attributes": {"tag_uid": ext_tag_uid},
            },
        }
        for tray_key, (tag_uid, _) in ams.items():
            _, ams_n, _, slot_n = tray_key.split()
            out[f"{c.PRINTER_ENTITY_PREFIX}_ams_{ams_n}_slot_{slot_n}"] = {
                "state": "Bambu PLA Basic", "attributes": {"tag_uid": tag_uid},
            }
        return out


def install_stubs():
    """Alles abschalten, was Netzwerk/HA braucht und fuer die Buchung egal ist."""
    c._sync_dashboard_selects = lambda *a, **k: None
    c._backup_wartung_data = lambda *a, **k: None
    c._retry_missing_title_images = lambda *a, **k: None
    c._save_title_image = lambda *a, **k: None
    c._save_end_camera_photo = lambda *a, **k: None
    c._read_plate_type = lambda *a, **k: None
    c.ha_call_service = lambda *a, **k: None
    c.BOOKING_RETRY_MIN_S = 0.0     # Tests steuern die Wartezeit gezielt selbst


install_stubs()

_current = {}
c.ha_states = lambda: _current


def poll(state, states):
    """Ein Poll von process_once() mit den gegebenen HA-States."""
    global _current
    _current = states
    c.process_once(state)


def poll_persisted(states):
    """Ein Poll exakt wie die Hauptschleife: Zustand von der Platte laden,
    process_once, speichern. Deckt Persistenz und JSON-Tauglichkeit ab."""
    global _current
    _current = states
    state = c.load_state()
    try:
        c.process_once(state)
    finally:
        c.save_state(state)
    return state


def job_of(state, printer, start=None):
    return state["jobs"].get(f"{printer.gcode}|{start or printer.start}")


def history_record(history_id):
    return next((r for r in c.load_history()["records"] if r["id"] == history_id), None)
