#!/usr/bin/env python3
"""
Einmalig Locations für alle bestehenden Spulen setzen.
Wird von entrypoint-sync.sh automatisch beim allerersten Start des
spoolman-sync-Containers ausgefuehrt (wenn state.json noch fehlt), um
Spoolman zu initialisieren.
"""

import os
import re
import sys

import requests

from entity_config import is_external_spool_entity, slot_entities

# .env laden
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    for _line in open(_env_path):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

HA_URL = os.environ.get("HA_URL", "http://homeassistant.local:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
SPOOLMAN_URL = os.environ.get("SPOOLMAN_URL", "http://spoolman.local:7912").rstrip("/")

SLOT_ENTITIES = slot_entities()

EMPTY_TAG_UID = "0000000000000000"
EMPTY_TRAY_UUID = "00000000000000000000000000000000"

_SM = {"Content-Type": "application/json"}


def _ha_headers():
    return {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}


def ha_get_states_batch(entity_ids):
    r = requests.get(f"{HA_URL}/api/states", headers=_ha_headers(), timeout=15)
    r.raise_for_status()
    all_states = {s["entity_id"]: s for s in r.json()}
    return {eid: all_states[eid] for eid in entity_ids if eid in all_states}


def sm_get(path, params=None):
    r = requests.get(f"{SPOOLMAN_URL}/api/v1{path}", headers=_SM, params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def sm_patch(path, data):
    r = requests.patch(f"{SPOOLMAN_URL}/api/v1{path}", headers=_SM, json=data, timeout=10)
    r.raise_for_status()
    return r.json()


def _normalize_uid(value):
    if isinstance(value, str):
        return value.strip('"').strip()
    return str(value).strip('"').strip()


def _get_location_from_entity(entity_id):
    if not entity_id:
        return None
    if is_external_spool_entity(entity_id):
        return "P2S - Extern"
    m = re.search(r"ams_(\d+)_slot_(\d+)", entity_id.lower())
    if m:
        ams_num, slot_num = m.groups()
        return f"AMS {ams_num} - Slot {slot_num}"
    return None


def main():
    if not HA_TOKEN:
        print("FEHLER: HA_TOKEN nicht gesetzt!")
        sys.exit(1)

    print("=" * 70)
    print("Location-Reconciliation für bestehende Spulen")
    print("=" * 70)

    # Alle Slot-States aus HA laden
    print("\n1. Slot-Status von Home Assistant lesen...")
    entity_states = ha_get_states_batch(SLOT_ENTITIES)

    slot_by_uid = {}
    for entity_id in SLOT_ENTITIES:
        entity_state = entity_states.get(entity_id)
        if not entity_state:
            print(f"   ⚠ {entity_id} nicht in HA gefunden")
            continue

        attrs = entity_state.get("attributes", {})
        state_str = entity_state.get("state", "")

        # Empty checken
        if state_str in ("Empty", "unavailable", "unknown") or attrs.get("empty", False):
            continue

        tag_uid = attrs.get("tag_uid", EMPTY_TAG_UID)
        tray_uuid = attrs.get("tray_uuid", EMPTY_TRAY_UUID)

        if tag_uid and tag_uid != EMPTY_TAG_UID:
            slot_by_uid[tag_uid] = {
                "entity_id": entity_id,
                "location": _get_location_from_entity(entity_id),
                "name": attrs.get("name", "?"),
            }
            print(f"   ✓ Slot {entity_id[-20:]}: '{attrs.get('name', '?')}' @ {_get_location_from_entity(entity_id)}")
        elif tray_uuid and tray_uuid != EMPTY_TRAY_UUID:
            slot_by_uid[f"NOTAG_{tray_uuid}"] = {
                "entity_id": entity_id,
                "location": _get_location_from_entity(entity_id),
                "name": attrs.get("name", "?"),
            }
            print(f"   ✓ Slot {entity_id[-20:]}: '{attrs.get('name', '?')}' (no RFID) @ {_get_location_from_entity(entity_id)}")

    # Alle Spulen von Spoolman laden
    print("\n2. Spulen von Spoolman laden...")
    spools = sm_get("/spool")
    print(f"   {len(spools)} Spulen geladen")

    # Für jede Spule: Location aktualisieren wenn möglich
    print("\n3. Locations aktualisieren...")
    updated = 0
    for spool in spools:
        spool_id = spool["id"]
        extra = spool.get("extra", {})
        bambu_uid = _normalize_uid(extra.get("bambu_tag_uid", ""))
        current_location = spool.get("location", "")

        slot_info = slot_by_uid.get(bambu_uid)
        if slot_info:
            new_location = slot_info["location"]
            if new_location and current_location.lower() != new_location.lower():
                try:
                    sm_patch(f"/spool/{spool_id}", {"location": new_location})
                    print(f"   ✓ Spule {spool_id}: Location → '{new_location}'")
                    updated += 1
                except Exception as e:
                    print(f"   ✗ Spule {spool_id}: FEHLER: {e}")
            elif new_location and current_location.lower() == new_location.lower():
                print(f"   - Spule {spool_id}: Location bereits korrekt ('{current_location}')")
        else:
            if not current_location:
                # Spule nicht erkannt → auf Lager setzen
                try:
                    sm_patch(f"/spool/{spool_id}", {"location": "Lager"})
                    print(f"   ✓ Spule {spool_id}: Location → 'Lager' (nicht erkannt)")
                    updated += 1
                except Exception as e:
                    print(f"   ✗ Spule {spool_id}: FEHLER: {e}")

    # 4. Prüfe ob "P2S - Extern" initialisiert ist
    print("\n4. Externe Rolle & Lager prüfen...")
    try:
        external_slot_spools = [s for s in spools if s.get("location") == "P2S - Extern"]
        if external_slot_spools:
            print(f"   ✓ Externe Rolle bereits initialisiert ({len(external_slot_spools)} Spulen)")
        else:
            print("   - Externe Rolle: noch keine Spulen erkannt (OK, wird auto-erkannt wenn Spule dort eingelegt wird)")

        storage_spools = [s for s in spools if s.get("location") == "Lager"]
        print(f"   ✓ Lager: {len(storage_spools)} Spulen")
    except Exception as e:
        print(f"   ⚠ Prüfung fehlgeschlagen: {e}")

    print("\n" + "=" * 70)
    print(f"Fertig! {updated} Spulen aktualisiert.")
    print("=" * 70)


if __name__ == "__main__":
    main()
