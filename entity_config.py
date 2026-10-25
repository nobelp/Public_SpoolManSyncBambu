"""Home-Assistant-Entity-Namen aus der Umgebung (.env) statt fest im Code (Issue #6).

Ohne Eintrag in der .env gelten die bisherigen Namen (Standardwerte), das Verhalten
bleibt also unveraendert. Wer andere Entity-Namen hat (andere Sprache, anderer
Drucker, anderer Anzeigename), traegt sie in der .env ein - ohne den Code zu
aendern. Vorschlaege fuer die eigene Installation liefert discover_entities.py.

Die Funktionen lesen os.environ erst beim Aufruf. Die aufrufenden Module laden ihre
.env deshalb VOR dem Import-Zeitpunkt der Konstanten.
"""
import os
import re

DEFAULT_PRINTER_PREFIX = "sensor.bambu_p2s"

_ENTITY_ID_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")

# Bambu-Lab-Integration: Zahl der AMS-Einheiten x Slots, die abgefragt werden.
# Mehr als eine AMS-Einheit erkennt consumption.py selbst (siehe _build_tray_key_to_slot).
AMS_SLOTS_PER_UNIT = 4


def entity_from_env(var_name, default):
    """Entity-ID aus der Umgebungsvariable lesen. Leer/fehlend -> Standardwert.
    Ungueltige Werte brechen den Start mit klarer Meldung ab: ein Tippfehler darf
    nicht still dazu fuehren, dass Verbrauch gar nicht oder falsch gebucht wird."""
    value = os.environ.get(var_name, "").strip()
    if not value:
        return default
    if not _ENTITY_ID_RE.match(value):
        raise ValueError(
            f"{var_name}='{value}' ist keine gueltige Entity-ID "
            f"(erwartet: <domain>.<name> in Kleinbuchstaben, z.B. {default})"
        )
    return value


def printer_prefix():
    """Gemeinsamer Anfang der Entity-IDs des Druckers, z.B. 'sensor.p2s_<seriennummer>'."""
    prefix = entity_from_env("PRINTER_ENTITY_PREFIX", DEFAULT_PRINTER_PREFIX)
    if not prefix.startswith("sensor."):
        raise ValueError(f"PRINTER_ENTITY_PREFIX='{prefix}' muss mit 'sensor.' beginnen")
    return prefix


def ams_slot_entity(prefix, ams_number, slot_number):
    return f"{prefix}_ams_{ams_number}_slot_{slot_number}"


def external_spool_entity(prefix=None):
    """Sensor der externen Spule; ableitbar aus dem Praefix, einzeln ueberschreibbar."""
    prefix = prefix or printer_prefix()
    return entity_from_env("EXTERNAL_SPOOL_ENTITY", f"{prefix}_externalspool_externe_spule")


def external_active_entity(prefix=None):
    """Binary-Sensor 'externe Spule druckt gerade'; gleicher Geraetename wie der Praefix."""
    prefix = prefix or printer_prefix()
    device_part = prefix[len("sensor."):]
    return entity_from_env("EXTERNAL_ACTIVE_ENTITY", f"binary_sensor.{device_part}_externalspool_aktiv")


def slot_entities(prefix=None):
    """Slot-Sensoren, die sync.py und reconcile_locations.py abfragen (AMS 1 + extern)."""
    prefix = prefix or printer_prefix()
    ams = [ams_slot_entity(prefix, 1, n) for n in range(1, AMS_SLOTS_PER_UNIT + 1)]
    return ams + [external_spool_entity(prefix)]


def is_external_spool_entity(entity_id):
    """True fuer den Sensor der externen Spule - auch bei selbst gewaehltem Namen."""
    if not entity_id:
        return False
    return entity_id == external_spool_entity() or "externalspool" in entity_id.lower()


def find_missing(states, entity_ids):
    """Welche der Entity-IDs fehlen in den HA-States? (Reihenfolge bleibt erhalten.)"""
    return [e for e in entity_ids if e not in states]


HELPER_REFERENCE = "HELPERS.md"


def format_missing_helpers(missing):
    """Fehlende HA-Hilfsentities (input_*) lesbar nach Typ gruppiert, z.B.
    'input_datetime (2): input_datetime.a, input_datetime.b; input_text (1): ...'."""
    by_domain = {}
    for entity_id in missing:
        by_domain.setdefault(entity_id.split(".", 1)[0], []).append(entity_id)
    return "; ".join(f"{domain} ({len(ids)}): {', '.join(ids)}"
                     for domain, ids in sorted(by_domain.items()))


def missing_helpers_message(missing):
    """Logmeldung fuer fehlende Hilfsentities, None wenn nichts fehlt. Die Dienste
    legen diese Helfer nicht selbst an; sie muessen in HA existieren (siehe HELPERS.md)."""
    if not missing:
        return None
    return (f"HA-Hilfsentities fehlen ({len(missing)}) - zugehoerige Funktionen sind "
            f"eingeschraenkt: {format_missing_helpers(missing)}. "
            f"Anlegen in HA, Beschreibung: {HELPER_REFERENCE}")


class ChangeReporter:
    """Merkt sich den zuletzt gemeldeten Stand, damit ein Dauerzustand (fehlende
    Entities) nicht jede Minute neu ins Log geschrieben wird."""

    def __init__(self):
        self._last = None

    def is_new(self, state):
        """True beim ersten Aufruf und immer dann, wenn sich der Stand aendert."""
        if state == self._last:
            return False
        self._last = state
        return True
