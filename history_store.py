#!/usr/bin/env python3
"""
DIV_SpoolManSyncBambu - history_store.py

SQLite-Speicher fuer den Druckverlauf (vormals print_history.json).

WARUM SQLite:
  Die JSON-Datei wurde bei jedem Poll komplett gelesen und komplett neu
  geschrieben. Das hatte drei Nachteile:
  - Kein Transaktionsschutz: brach der Schreibvorgang ab, war die Datei kaputt
    bzw. halb geschrieben.
  - Kein sinnvolles Abfragen: "alle Drucke mit Spule #20" ging nur, indem die
    komplette Datei geladen und in Python gefiltert wurde.
  - Das Sperren (_history_lock) musste den gesamten Lese-Aendern-Schreib-Zyklus
    umfassen, weil ein paralleler Schreiber sonst Aenderungen verworfen haette.

BEWUSSTE ENTSCHEIDUNG - gleiche Datenform nach aussen:
  load()/save() arbeiten mit genau derselben Struktur wie die JSON-Datei:
      {"next_id": <int>, "records": [ {..., "colors": [ {...} ]} ]}
  Damit bleibt der gesamte aufrufende Code (Web-UI, /fix-history, Statistik,
  Dedup) unveraendert. Der Umstieg ist ein Austausch der Speicherschicht,
  keine Umstellung der Anwendungslogik.

BEWUSSTE ENTSCHEIDUNG - extra_json:
  Felder, die das Schema (noch) nicht kennt, werden in extra_json mitgefuehrt
  statt verworfen. Ein spaeter im Code ergaenztes Feld geht so nicht still
  verloren, auch wenn hier keine Spalte dafuer existiert.

Nur die Standardbibliothek (sqlite3) - keine zusaetzliche Abhaengigkeit.
"""

import json
import logging
import os
import shutil
import sqlite3
from datetime import datetime

log = logging.getLogger("consumption")

# Spalten, die als echte Felder gespeichert werden. Alles andere landet in
# extra_json. Reihenfolge = Reihenfolge im INSERT.
RECORD_COLUMNS = (
    "gcode", "name", "name_overridden", "start", "end",
    "duration_min", "status", "image", "camera_image",
)
COLOR_COLUMNS = (
    "spool_id", "tray_key", "filament_name", "material", "color_hex",
    "density", "diameter", "grams", "length_m", "last_update",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS print_history (
    id              INTEGER PRIMARY KEY,
    gcode           TEXT,
    name            TEXT,
    name_overridden INTEGER NOT NULL DEFAULT 0,
    start           TEXT,
    end             TEXT,
    duration_min    INTEGER,
    status          TEXT,
    image           TEXT,
    camera_image    TEXT,
    extra_json      TEXT
);

CREATE TABLE IF NOT EXISTS print_colors (
    color_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    history_id    INTEGER NOT NULL,
    position      INTEGER NOT NULL,
    spool_id      INTEGER,
    tray_key      TEXT,
    filament_name TEXT,
    material      TEXT,
    color_hex     TEXT,
    density       REAL,
    diameter      REAL,
    grams         REAL,
    length_m      REAL,
    last_update   TEXT,
    extra_json    TEXT,
    FOREIGN KEY (history_id) REFERENCES print_history(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS history_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Genau die Abfragen, um die es in Issue #7 geht:
-- "alle Drucke mit Spule #20", Sortierung nach Datum, Filter nach Status.
CREATE INDEX IF NOT EXISTS idx_colors_spool   ON print_colors (spool_id);
CREATE INDEX IF NOT EXISTS idx_colors_history ON print_colors (history_id, position);
CREATE INDEX IF NOT EXISTS idx_history_start  ON print_history (start);
CREATE INDEX IF NOT EXISTS idx_history_status ON print_history (status);
"""


def connect(db_file):
    """Verbindung mit den fuer diesen Einsatz passenden PRAGMAs.

    Pro Aufruf eine eigene Verbindung: consumption.py betreibt neben der
    Poll-Schleife einen Webserver-Thread, und sqlite3-Verbindungen sind nicht
    thread-sicher. Bei dieser Datenmenge ist der Verbindungsaufbau
    vernachlaessigbar und deutlich robuster als eine geteilte Verbindung.

    WAL: erlaubt Lesen waehrend geschrieben wird (Webserver-Thread liest,
    waehrend die Poll-Schleife schreibt).
    busy_timeout: wartet statt sofort mit 'database is locked' zu scheitern.
    """
    con = sqlite3.connect(db_file, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=10000")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init_schema(db_file):
    os.makedirs(os.path.dirname(db_file) or ".", exist_ok=True)
    with connect(db_file) as con:
        con.executescript(SCHEMA)

    # Migriere nach Schema mit strikten Datentypen
    _migrate_to_typed_schema(db_file)


def _migrate_to_typed_schema(db_file):
    """Migriert die DB auf echte Datentypen (REAL/INTEGER statt TEXT).

    Verhindert Fehler durch manuelle Edits in DBViewer, die Zahlen als Strings speichern.
    Idempotent: läuft nur wenn nötig."""
    con = None
    try:
        con = connect(db_file)

        # Prüfe ob Spalte grams existiert und welchen Typ sie hat
        cursor = con.execute("PRAGMA table_info(print_colors)")
        cols = {row[1]: row[2] for row in cursor}  # {name: type}

        # Wenn grams schon REAL ist, nichts zu tun
        if cols.get("grams") == "REAL":
            log.debug("DB-Schema ist bereits aktuell (grams=REAL)")
            con.close()
            return

        log.info("DB-Migration: Konvertiere Spalten zu strikten Datentypen...")

        # Backup der alten Daten
        con.execute("BEGIN")

        # print_history: duration_min sollte INTEGER sein
        con.execute("""
            ALTER TABLE print_history
            RENAME TO print_history_old
        """)

        con.execute("""
            CREATE TABLE print_history (
                id              INTEGER PRIMARY KEY,
                gcode           TEXT,
                name            TEXT,
                name_overridden INTEGER NOT NULL DEFAULT 0,
                start           TEXT,
                end             TEXT,
                duration_min    INTEGER NOT NULL DEFAULT 0,
                status          TEXT,
                image           TEXT,
                camera_image    TEXT,
                extra_json      TEXT
            )
        """)

        # Migriere mit Type-Casting
        con.execute("""
            INSERT INTO print_history
            SELECT
                id, gcode, name, name_overridden, start, end,
                CAST(COALESCE(duration_min, 0) AS INTEGER),
                status, image, camera_image, extra_json
            FROM print_history_old
        """)

        con.execute("DROP TABLE print_history_old")

        # print_colors: grams und length_m sollten REAL sein
        con.execute("ALTER TABLE print_colors RENAME TO print_colors_old")

        con.execute("""
            CREATE TABLE print_colors (
                color_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                history_id    INTEGER NOT NULL,
                position      INTEGER NOT NULL,
                spool_id      INTEGER,
                tray_key      TEXT,
                filament_name TEXT,
                material      TEXT,
                color_hex     TEXT,
                density       REAL,
                diameter      REAL,
                grams         REAL NOT NULL DEFAULT 0,
                length_m      REAL NOT NULL DEFAULT 0,
                last_update   TEXT,
                extra_json    TEXT,
                FOREIGN KEY (history_id) REFERENCES print_history(id) ON DELETE CASCADE
            )
        """)

        con.execute("""
            INSERT INTO print_colors
            SELECT
                color_id, history_id, position, spool_id, tray_key, filament_name,
                material, color_hex, density, diameter,
                CAST(COALESCE(grams, 0) AS REAL),
                CAST(COALESCE(length_m, 0) AS REAL),
                last_update, extra_json
            FROM print_colors_old
        """)

        con.execute("DROP TABLE print_colors_old")

        # Indizes neu erstellen
        con.execute("CREATE INDEX IF NOT EXISTS idx_colors_spool ON print_colors (spool_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_colors_history ON print_colors (history_id, position)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_history_start ON print_history (start)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_history_status ON print_history (status)")

        con.execute("COMMIT")
        con.close()

        log.info("✓ DB-Migration abgeschlossen: Datentypen sind nun strikt (INTEGER/REAL)")

    except Exception as e:
        if con:
            con.execute("ROLLBACK")
            con.close()
        log.error(f"DB-Migration fehlgeschlagen: {e}", exc_info=True)


def count_records(db_file):
    with connect(db_file) as con:
        return con.execute("SELECT COUNT(*) FROM print_history").fetchone()[0]


def _split_known(source, columns):
    """Trennt bekannte Felder von unbekannten (-> extra_json)."""
    known = {c: source.get(c) for c in columns}
    extra = {k: v for k, v in source.items()
             if k not in columns and k not in ("id", "colors")}
    return known, (json.dumps(extra, ensure_ascii=False) if extra else None)


def _row_to_dict(row, columns):
    out = {c: row[c] for c in columns}
    if row["extra_json"]:
        try:
            out.update(json.loads(row["extra_json"]))
        except (ValueError, TypeError):
            log.warning("History: extra_json nicht lesbar - Feld uebersprungen")
    return out


def load(db_file):
    """Liest den kompletten Verlauf in der bekannten JSON-Form."""
    with connect(db_file) as con:
        records = []
        by_id = {}
        for row in con.execute(
            "SELECT * FROM print_history ORDER BY id"
        ):
            rec = _row_to_dict(row, RECORD_COLUMNS)
            rec["id"] = row["id"]
            # In JSON war das ein bool - Form exakt erhalten, damit sich
            # aufrufender Code (und Vergleiche in Tests) nicht aendert.
            rec["name_overridden"] = bool(row["name_overridden"])
            rec["colors"] = []
            records.append(rec)
            by_id[row["id"]] = rec

        for row in con.execute(
            "SELECT * FROM print_colors ORDER BY history_id, position, color_id"
        ):
            rec = by_id.get(row["history_id"])
            if rec is not None:
                rec["colors"].append(_row_to_dict(row, COLOR_COLUMNS))

        meta = con.execute(
            "SELECT value FROM history_meta WHERE key = 'next_id'"
        ).fetchone()

    if meta is not None:
        next_id = int(meta["value"])
    else:
        # Kein Meta-Eintrag (frische DB): aus den vorhandenen IDs ableiten,
        # damit keine ID doppelt vergeben wird.
        next_id = max((r["id"] for r in records), default=0) + 1

    return {"next_id": next_id, "records": records}


def save(db_file, history):
    """Schreibt den Verlauf in EINER Transaktion.

    Vorhandene Records werden aktualisiert, fehlende entfernt - dadurch wirken
    auch Loeschungen aus dem Web-UI. Faellt der Prozess mitten im Schreiben
    aus, ist der vorherige Stand unveraendert erhalten (das war mit der
    JSON-Datei nicht gegeben)."""
    records = history.get("records") or []
    with connect(db_file) as con:
        con.execute("BEGIN")
        keep_ids = []
        for rec in records:
            rec_id = rec.get("id")
            if rec_id is None:
                log.warning(f"History: Record ohne id uebersprungen: {rec.get('name')!r}")
                continue
            keep_ids.append(rec_id)
            known, extra = _split_known(rec, RECORD_COLUMNS)
            # name_overridden ist NOT NULL: fehlt das Feld (aeltere bzw. per
            # /add-history angelegte Records), waere der Wert None und der
            # gesamte Speichervorgang wuerde scheitern.
            known["name_overridden"] = int(bool(known.get("name_overridden")))
            # Keine automatischen Dateinamen mehr ({id}.png / {id}_camera.jpg):
            # sie behaupteten Bilder, die es nicht gibt (Issue #33). Die Anzeige
            # prueft die Existenz der Datei selbst.
            con.execute(
                f"""INSERT INTO print_history
                        (id, {', '.join(RECORD_COLUMNS)}, extra_json)
                    VALUES (?{', ?' * (len(RECORD_COLUMNS) + 1)})
                    ON CONFLICT(id) DO UPDATE SET
                        {', '.join(f'{c}=excluded.{c}' for c in RECORD_COLUMNS)},
                        extra_json=excluded.extra_json""",
                (rec_id, *(known[c] for c in RECORD_COLUMNS), extra),
            )
            # Farben werden pro Record komplett neu geschrieben: sie haben keine
            # stabile eigene ID, und ihre Reihenfolge ist bedeutungstragend
            # (bei einem Spulenwechsel wird angehaengt).
            con.execute("DELETE FROM print_colors WHERE history_id = ?", (rec_id,))
            for position, color in enumerate(rec.get("colors") or []):
                c_known, c_extra = _split_known(color, COLOR_COLUMNS)
                con.execute(
                    f"""INSERT INTO print_colors
                            (history_id, position, {', '.join(COLOR_COLUMNS)}, extra_json)
                        VALUES (?, ?{', ?' * (len(COLOR_COLUMNS) + 1)})""",
                    (rec_id, position, *(c_known[c] for c in COLOR_COLUMNS), c_extra),
                )

        if keep_ids:
            placeholders = ",".join("?" * len(keep_ids))
            con.execute(
                f"DELETE FROM print_history WHERE id NOT IN ({placeholders})", keep_ids
            )
        else:
            con.execute("DELETE FROM print_history")
        con.execute("DELETE FROM print_colors WHERE history_id NOT IN (SELECT id FROM print_history)")

        next_id = history.get("next_id")
        if next_id is not None:
            con.execute(
                "INSERT INTO history_meta (key, value) VALUES ('next_id', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(next_id),),
            )
        con.execute("COMMIT")


def import_from_json(db_file, json_file):
    """Einmaliger Import der bestehenden JSON-Datei.

    Idempotent: laeuft nur, wenn die DB noch leer ist. Ein erneuter Aufruf
    (z.B. bei jedem Containerstart) macht nichts. Gibt die Anzahl importierter
    Records zurueck, oder None wenn nichts zu tun war.

    Priorisiert .pre_sqlite (Sicherung vom letzten Import vor der Umstellung
    auf SQLite) vor der aktuellen .json (die nur den aktuellen Druck enthaelt)."""
    if count_records(db_file) > 0:
        return None

    # Zuerst .pre_sqlite versuchen (hat die 75+ historischen Records)
    pre_sqlite = f"{json_file}.pre_sqlite"
    source_file = None
    if os.path.exists(pre_sqlite):
        source_file = pre_sqlite
    elif os.path.exists(json_file):
        source_file = json_file
    else:
        return None

    try:
        with open(source_file, encoding="utf-8") as f:
            history = json.load(f)
    except Exception as e:
        log.error(f"History-Import: {source_file} nicht lesbar: {e}")
        return None

    records = history.get("records") or []
    if not records:
        return None

    # Sicherungskopie vor dem ersten Import - unabhaengig davon, dass die
    # JSON-Datei waehrend der Uebergangsphase weiter mitgeschrieben wird.
    backup = f"{json_file}.pre_sqlite"
    if not os.path.exists(backup):
        try:
            with open(backup, "w", encoding="utf-8") as f:
                json.dump(history, f, indent=2, ensure_ascii=False)
            log.info(f"History-Import: Sicherung angelegt: {backup}")
        except Exception as e:
            log.warning(f"History-Import: Sicherung fehlgeschlagen: {e}")

    save(db_file, history)
    imported = count_records(db_file)
    colors = 0
    with connect(db_file) as con:
        colors = con.execute("SELECT COUNT(*) FROM print_colors").fetchone()[0]
    log.info(
        f"History-Import: {imported} Records und {colors} Farb-Eintraege aus "
        f"{json_file} in die SQLite-DB uebernommen (next_id="
        f"{history.get('next_id')})"
    )
    return imported


def find_prints_by_spool(db_file, spool_id):
    """Beispiel fuer den eigentlichen Gewinn: gezielte Abfrage statt die
    komplette Datei zu laden und in Python zu filtern. Wird von /api/spool-prints
    genutzt."""
    with connect(db_file) as con:
        rows = con.execute(
            """SELECT h.id, h.name, h.start, h.status,
                      c.grams, c.length_m, c.tray_key
                 FROM print_colors c
                 JOIN print_history h ON h.id = c.history_id
                WHERE c.spool_id = ?
                ORDER BY h.start DESC""",
            (spool_id,),
        ).fetchall()
    return [dict(r) for r in rows]


# Globaler Timestamp des letzten Backups (für Abstand-Kontrolle)
_last_backup_time = None


def backup_database(db_file, min_interval_minutes=15, max_backups=100):
    """Erstellt stündliche Backups der DB mit min. 15-Minuten-Abstand.
    Behält max. 100 letzte Backups im _backup-Verzeichnis.

    Backups: /data/_backup/print_history.db.backup-YYYY-MM-DD-HH-MM-SS.db
    """
    global _last_backup_time

    if not os.path.exists(db_file):
        return False

    # Prüfe ob min_interval_minutes vergangen sind
    now = datetime.now()
    if _last_backup_time is not None:
        if (now - _last_backup_time).total_seconds() < (min_interval_minutes * 60):
            return False  # Zu früh für nächstes Backup

    backup_dir = os.path.join(os.path.dirname(db_file), "_backup")
    os.makedirs(backup_dir, exist_ok=True)

    try:
        # Backup-Dateiname mit Timestamp
        timestamp = now.strftime("%Y-%m-%d-%H-%M-%S")
        backup_file = os.path.join(backup_dir, f"print_history.db.backup-{timestamp}.db")

        # WAL-Files checkoponten, bevor wir backupen
        con = connect(db_file)
        con.execute("PRAGMA wal_checkpoint(RESTART)")
        con.close()

        # Backup erstellen
        shutil.copy2(db_file, backup_file)
        _last_backup_time = now
        log.info(f"✓ Stündliches DB-Backup erstellt: {backup_file}")

        # Alte Backups löschen (behält max_backups)
        backups = sorted([f for f in os.listdir(backup_dir) if f.startswith("print_history.db.backup-")])
        if len(backups) > max_backups:
            for old_backup in backups[:-max_backups]:
                try:
                    os.remove(os.path.join(backup_dir, old_backup))
                    log.debug(f"Altes Backup gelöscht (>100): {old_backup}")
                except Exception as e:
                    log.debug(f"Konnte Backup nicht löschen: {e}")

        return True

    except Exception as e:
        log.warning(f"DB-Backup fehlgeschlagen: {e}")
        return False
