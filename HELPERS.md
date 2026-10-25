# Benötigte Home-Assistant-Helfer

Die Dienste **legen diese Helfer nicht selbst an**. Sie müssen in Home Assistant existieren
(Einstellungen → Geräte & Dienste → Helfer, oder per YAML). Fehlt einer, schreibt der Dienst
beim Start eine Warnung mit allen fehlenden Namen ins Log (`consumption.log`,
`filament_enrich.log`) und meldet Änderungen erneut. Die Verbrauchsbuchung selbst hängt davon
nicht ab.

Der Entity-Name ergibt sich aus dem Namen des Helfers (`input_number.p2s_rt_ersetzen_nozzle`
= Helfer vom Typ „Zahl“ mit der Entity-ID `p2s_rt_ersetzen_nozzle`). Beim Anlegen in der
Oberfläche die Entity-ID nach dem Speichern prüfen und ggf. anpassen.

## Dashboard-Zuordnung der Spulen (`consumption.py`, `sync.py`)

| Entity | Typ | Einstellung |
|---|---|---|
| `input_select.p2s_slot_external` | Dropdown | beliebige Startoption (z. B. „– lade Spulen –“); die Optionen füllt `consumption.py` selbst |
| `input_select.p2s_slot_ams_<n>_tray_<m>` | Dropdown | eines je vorhandenem Slot (AMS 1: `…ams_1_tray_1` bis `…_4`); weitere AMS-Einheiten erst bei Bedarf |

## Wartung (`consumption.py`: Backup/Restore, `/restore-wartung`)

| Entities | Typ | Einstellung |
|---|---|---|
| 25 × `input_datetime.p2s_ersetzt_*`, `p2s_gereinigt_*`, `p2s_geschmiert_*`, `p2s_kalibriert_full` | Datum und/oder Uhrzeit | nur Datum (`has_date: true`, `has_time: false`) |
| 10 × `input_number.p2s_rt_ersetzen_*`, `p2s_rt_reinigen_*`, `p2s_rt_schmieren_*` | Zahl | Minimum 0, Maximum 100000, Schritt 0,01, Einheit `h`, Modus Eingabefeld |

Die vollständige Namensliste steht in `consumption.py` (`WARTUNG_DATE_ENTITIES`,
`WARTUNG_BASELINE_ENTITIES`). Die Anzeige der Wartung im Dashboard (Template-Sensoren,
Skript `p2s_wartung_erledigt`, Automationen) sind **keine Helfer** und gehören zur
HA-Konfiguration, nicht zu diesem Projekt.

## Versionsanzeige (`consumption.py`)

| Entity | Typ |
|---|---|
| `input_text.spoolman_version`, `input_text.spoolman_commit`, `input_text.spoolman_release` | Text |

## Filament-Formular (`filament_enrich.py`)

| Entity | Typ | Zweck |
|---|---|---|
| `input_text.neues_filament_url` | Text | Produkt-URL (lange URLs: maximale Länge auf 255 setzen) |
| `input_text.neues_filament_produktname` | Text | optionaler Produktname |
| `input_button.neues_filament_pruefen` | Taste | Schritt 1: suchen |
| `input_button.neues_filament_anlegen` | Taste | Schritt 2: anlegen |
| `input_select.neues_filament_auswahl` | Dropdown | Vorschläge; Optionen setzt der Dienst |
| `input_number.neues_filament_filament_id` | Zahl | optional: Spoolman-ID eines vorhandenen Filaments (0 = neues suchen); Minimum 0, Schritt 1 |
| `input_text.neues_filament_status` | Text | Rückmeldung des Dienstes (maximale Länge auf 255 setzen) |

Hinweis: Die Werte in dieser Tabelle sind aus der Verwendung im Code abgeleitet. Die
Wartungs-Helfer stimmen mit der YAML-Definition der Originalinstallation überein
(`has_date`, `min`, `max`, `step`, Einheit). Für die Helfer des Filament-Formulars und der
Versionsanzeige liegt keine YAML-Definition vor (sie wurden in der Oberfläche angelegt);
dort sind die Angaben Empfehlungen.

## Namen anpassen

Die Entity-IDs der Helfer sind fest. Die Namen der Drucker-Sensoren sind dagegen
über die `.env` konfigurierbar (siehe README, Abschnitt „Eigene HA-Entity-Namen“).
