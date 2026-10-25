# DIV_SpoolManSyncBambu

Automatische Spulen-Verwaltung und Druckverlauf für Bambu-Lab-3D-Drucker. Der Dienst verbindet Home Assistant, Spoolman und die Bambu-Lab-Integration.

**Englische Anleitung:** [README.en.md](README.en.md)

---

## Funktionen

- **Spulenerkennung:** Neue Spulen in AMS und externem Spulenhalter werden automatisch in Spoolman registriert.
- **Verbrauchs-Tracking:** Der Filamentverbrauch wird nach Druckende berechnet und in Spoolman abgezogen.
- **Standort-Tracking:** Der Spulenstandort in Spoolman folgt dem AMS-Slot.
- **Druckverlauf:** Web-Oberfläche mit Statistiken, Bildern und Filterung. Mehrfarben-Drucke werden pro Filament aufgeschlüsselt.
- **Manuelle Erfassung:** Drucke lassen sich nachträglich eintragen, z. B. wenn Home Assistant ausgefallen ist.
- **Filament-Anreicherung:** Preis, Artikelnummer und Metadaten aus einer Produkt-URL in Spoolman übernehmen.
- **Nachkaufen:** Filamente mit niedrigem Restbestand und Preisvergleich.

---

## Voraussetzungen

- Docker und Docker Compose
- **Home Assistant** mit der **Bambu-Lab-Integration**. Der Dienst liest die Drucker-Entities über die Home-Assistant-API.
- **Spoolman** (eigene Instanz, wird hier nicht mitinstalliert)
- Ein Bambu-Lab-Drucker mit AMS, der in Home Assistant eingebunden ist

---

## Installation

```bash
git clone https://github.com/nobelp/Public_SpoolManSyncBambu.git
cd Public_SpoolManSyncBambu

# Konfiguration anlegen
cp .env.example .env
```

In `.env` mindestens eintragen:

| Variable | Bedeutung |
|---|---|
| `HA_URL` | Adresse von Home Assistant, z. B. `http://homeassistant.local:8123` |
| `HA_TOKEN` | Long-Lived Access Token aus Home Assistant (Profil → Sicherheit) |
| `SPOOLMAN_URL` | Adresse der Spoolman-Instanz, z. B. `http://spoolman.local:7912` |
| `PRINTER_ENTITY_PREFIX` | Gemeinsamer Anfang der Drucker-Entities, z. B. `sensor.p2s_<seriennummer>` |

Die Entity-Namen lassen sich mit `discover_entities.py` ermitteln. Der Befehl ist rein lesend und ändert nichts. Er gibt fertige Zeilen für die `.env` aus (Präfix und alle Drucker-Entities), die du **komplett** in deine `.env` übernimmst. Ohne diese Zeilen gelten nur Platzhalter-Namen:

```bash
docker exec DIV_SpoolManSyncBambu python3 discover_entities.py
```

Alle übrigen Variablen sind optional. Die Standardwerte stehen als Kommentare in `.env.example`.

### Starten

```bash
docker compose up -d --build

# Logs
docker logs -f DIV_SpoolManSyncBambu
docker logs -f DIV_SpoolManSyncBambu_Consumption
```

### Dienste und Ports

| Service | Skript | Port | Aufgabe |
|---|---|---|---|
| Spoolman-Sync | `sync.py` | – | Erkennt neue Spulen, registriert sie, verfolgt Standorte |
| Verbrauchs-Tracking | `consumption.py` | 8100 | Reduziert Restmengen, Druckverlauf-Web-Oberfläche |
| Filament-Anreicherung | `filament_enrich.py` | 8099 | Produkt-Import, Metadaten-Befüllung, Formular |

Die Oberfläche ist unter `http://<Host>:8100` erreichbar.

---

## Sicherheit

- Die Web-Oberflächen haben **keine Anmeldung**. Die Ports 8099 und 8100 nicht ins Internet freigeben, nur im lokalen Netz betreiben.
- `.env` enthält Zugangsdaten und gehört nie ins Repository. Die Datei ist in `.gitignore` eingetragen.
- Das Verzeichnis `data/` enthält Historie und Tokens und wird ebenfalls nicht versioniert.

---

## Tests

Die Tests brauchen kein Docker, keine Home Assistant und keine Spoolman-Instanz:

```bash
pip install -r requirements.txt
bash tests/run_all.sh
```

---

## Fehlersuche

- **Keine Spulen werden erkannt:** Entity-Namen prüfen (`discover_entities.py`) und die Logs von `DIV_SpoolManSyncBambu` ansehen.
- **Verbrauch stimmt nicht:** Log-Stufe des Verbrauchs-Trackings erhöhen und die Logs von `DIV_SpoolManSyncBambu_Consumption` prüfen.
- **Drucker-Sensoren sind `unavailable`:** Die Bambu-Lab-Integration in Home Assistant prüfen. Kurze Aussetzer verwerfen keine Slot-Zuordnungen.
- **Home Assistant antwortet mit 401:** Das `HA_TOKEN` ist ungültig oder abgelaufen.

---

## Lizenz

MIT, siehe [LICENSE](LICENSE).
