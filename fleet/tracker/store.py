"""
The tracker's local SQLite cache (data/tracker.db), plus the two things this tool
owns outright: my notes and the written morning summaries.

Why a local cache at all. Every view in the tracker is a question over a few weeks
of takes, check-outs and device events. Asking production each time would mean
scanning `recordings` (whose `data` JSON is TOASTed and costs seconds per thousand
rows) on every page load. Instead sync.py pulls a narrow, normalised copy every ten
minutes, and every GET reads this file, which answers in milliseconds and keeps
working when the laptop is offline.

Rules this file keeps:

- Times are float epoch seconds (UTC). Days and Cairo time belong to clock.py.
- Loaders return plain dicts whose keys are exactly the columns in SPEC §2, with
  booleans as bool (None stays None when production didn't say) and JSON columns
  (`codes`) decoded.
- Cache tables are written only by sync.py. `notes` and `summaries` are never
  synced anywhere and survive `clear_cache()`.
- One connection, one RLock around every call, WAL, and a 15 s busy timeout, so the
  always-on service and a one-shot `--sync-once` / `--rebuild` from the terminal can
  share the file. Write transactions are short: one per table per step, and bulk
  upserts commit every few thousand rows.
"""
import json
import sqlite3
import threading
import time
from pathlib import Path

import config

BATCH = 2000                 # rows per write transaction in bulk upserts
BUSY_TIMEOUT_MS = 15000

# --------------------------------------------------------------------------- #
# schema (SPEC §2). Column order here is the order loaders return keys in.
# --------------------------------------------------------------------------- #
TABLES = {
    "takes": [
        ("id", "TEXT PRIMARY KEY"), ("cpu_id", "TEXT"), ("trip_item_id", "TEXT"), ("trip_id", "TEXT"),
        ("created_at", "REAL"), ("updated_at", "REAL"), ("first_copied_at", "REAL"),
        ("uploaded_at", "REAL"), ("started_at", "REAL"), ("clock_fixed", "INTEGER"),
        ("boot_id", "TEXT"), ("software_version", "TEXT"), ("board", "TEXT"),
        ("camera_unit", "TEXT"), ("usb_path", "TEXT"), ("recorded_s", "REAL"), ("kept_s", "REAL"),
        ("settled", "INTEGER"), ("readiness_status", "TEXT"), ("codes", "TEXT"),
        ("fault_kind", "TEXT"), ("nominal_fps", "INTEGER"), ("fps", "REAL"), ("p99_dev_ms", "REAL"),
        ("out_of_band_pct", "REAL"), ("score", "REAL"), ("drop_pct", "REAL"), ("sheet_path", "TEXT"),
        ("sheet_grid", "TEXT"),
    ],
    "items": [
        ("id", "TEXT PRIMARY KEY"), ("trip_id", "TEXT"), ("cpu_id", "TEXT"), ("camera_id", "TEXT"),
        ("checkout_at", "REAL"), ("returned_at", "REAL"), ("missing", "INTEGER"),
        ("task", "TEXT"), ("destination", "TEXT"),
    ],
    "starcaps": [
        ("cpu_id", "TEXT PRIMARY KEY"), ("name", "TEXT"), ("camera_id", "TEXT"), ("board", "TEXT"),
        ("created_at", "REAL"),
    ],
    "cameras": [
        ("id", "TEXT PRIMARY KEY"), ("status", "TEXT"), ("retired", "INTEGER"), ("created_at", "REAL"),
    ],
    "cal_versions": [
        ("id", "TEXT PRIMARY KEY"), ("camera_id", "TEXT"), ("camera_key", "TEXT"), ("rev", "INTEGER"),
        ("method", "TEXT"), ("calibrated_at", "REAL"), ("uploaded_at", "REAL"),
        ("superseded_at", "REAL"), ("verdict", "TEXT"), ("evidence_status", "TEXT"),
        ("rmse_left", "REAL"), ("rmse_right", "REAL"), ("rmse_stereo", "REAL"), ("baseline_mm", "REAL"),
    ],
    "cal_current": [
        ("id", "TEXT PRIMARY KEY"), ("camera_id", "TEXT"), ("kind", "TEXT"), ("rev", "INTEGER"),
        ("calibrated_at", "REAL"), ("method", "TEXT"), ("evidence_status", "TEXT"),
        ("verdict", "TEXT"), ("fit_digest", "TEXT"),
    ],
    "camera_events": [
        ("id", "TEXT PRIMARY KEY"), ("at", "REAL"), ("action", "TEXT"), ("camera", "TEXT"),
        ("headset", "TEXT"), ("replaced", "TEXT"), ("source", "TEXT"),
    ],
    "tickets": [
        ("id", "TEXT PRIMARY KEY"), ("subject", "TEXT"), ("headset", "TEXT"), ("camera", "TEXT"),
        ("category", "TEXT"), ("critical", "INTEGER"), ("description", "TEXT"),
        ("reporter_name", "TEXT"), ("status", "TEXT"), ("created_at", "REAL"),
        ("resolved_at", "REAL"), ("resolution_note", "TEXT"), ("replacement_camera", "TEXT"),
        ("photos", "INTEGER"),
    ],
    "devices": [
        ("device_id", "TEXT PRIMARY KEY"), ("cpu_id", "TEXT"), ("name", "TEXT"), ("last_seen", "REAL"),
        ("software_version", "TEXT"), ("board", "TEXT"), ("status", "TEXT"), ("recording", "INTEGER"),
        ("disk_free", "INTEGER"), ("disk_total", "INTEGER"), ("connection_type", "TEXT"),
        ("camera", "TEXT"), ("clock_synced", "INTEGER"),
    ],
    "device_events": [
        ("device_id", "TEXT NOT NULL"), ("seq", "INTEGER NOT NULL"), ("ts", "REAL"), ("type", "TEXT"),
        ("msg", "TEXT"),
    ],
    "kv": [("key", "TEXT PRIMARY KEY"), ("value", "TEXT"), ("updated_at", "REAL")],
    "qc_runs": [
        ("id", "TEXT PRIMARY KEY"), ("cpu_id", "TEXT"), ("name", "TEXT"), ("model", "TEXT"),
        ("camera_id", "TEXT"), ("version", "TEXT"), ("verdict", "TEXT"), ("started_at", "REAL"),
        ("finished_at", "REAL"), ("headline", "TEXT"), ("failed_steps", "TEXT"), ("station", "TEXT"),
    ],
}
ADDED_IN = {"qc_runs": 2}                # tables created by a later migration, not by v1
COLUMNS_ADDED_IN = {("takes", "sheet_path"): 3, ("takes", "sheet_grid"): 3}   # ALTERed in later
PRIMARY_KEYS = {t: ("device_id", "seq") if t == "device_events" else (TABLES[t][0][0],) for t in TABLES}
BOOL_COLS = {
    "takes": {"clock_fixed", "settled"}, "items": {"missing"}, "cameras": {"retired"},
    "tickets": {"critical"}, "devices": {"recording", "clock_synced"},
}
JSON_COLS = {"takes": {"codes", "sheet_grid"}, "qc_runs": {"failed_steps"}}
JSON_NULL_IS_NONE = {"sheet_grid"}   # an absent object stays None; an absent list reads as []
CACHE_TABLES = tuple(TABLES)          # what clear_cache() empties (kv included)

NOTE_SUBJECTS = ("cap", "camera")
NOTE_STATUSES = ("ok", "watch", "repair", "dead", "lost")
NOTE_TEXT_MAX = 4000


def _create(table, version):
    """CREATE TABLE as of schema `version`: columns added later come from their own ALTER."""
    body = [f"{name} {kind}" for name, kind in TABLES[table]
            if COLUMNS_ADDED_IN.get((table, name), 0) <= version]
    if table == "device_events":
        # The composite primary key IS the (device_id, seq) index SPEC asks for.
        body.append("PRIMARY KEY (device_id, seq)")
    return f"CREATE TABLE IF NOT EXISTS {table} ({', '.join(body)})"


def _ddl():
    """Schema v1, as shipped on 2026-10-12 (tables added later have their own migration)."""
    parts = [_create(t, 1) for t in TABLES if t not in ADDED_IN]
    parts += [
        # what the loaders filter on
        "CREATE INDEX IF NOT EXISTS takes_trip_item ON takes(trip_item_id)",
        "CREATE INDEX IF NOT EXISTS takes_created ON takes(created_at)",
        "CREATE INDEX IF NOT EXISTS takes_camera_unit ON takes(camera_unit, created_at)",
        "CREATE INDEX IF NOT EXISTS items_checkout ON items(checkout_at)",
        "CREATE INDEX IF NOT EXISTS items_returned ON items(returned_at)",
        "CREATE INDEX IF NOT EXISTS camera_events_at ON camera_events(at)",
        "CREATE INDEX IF NOT EXISTS device_events_ts ON device_events(ts)",
        """CREATE TABLE IF NOT EXISTS notes (
               id INTEGER PRIMARY KEY AUTOINCREMENT, subject_type TEXT NOT NULL,
               subject_id TEXT NOT NULL, created_at REAL NOT NULL, text TEXT NOT NULL,
               status TEXT, deleted_at REAL)""",
        "CREATE INDEX IF NOT EXISTS notes_subject ON notes(subject_type, subject_id, created_at)",
        "CREATE TABLE IF NOT EXISTS summaries (date TEXT PRIMARY KEY, text TEXT NOT NULL, written_at REAL NOT NULL)",
    ]
    return parts


# Each entry upgrades the file by one PRAGMA user_version. Append; never edit one
# that has shipped.
MIGRATIONS = [
    _ddl(),
    [_create("qc_runs", 2), "CREATE INDEX IF NOT EXISTS qc_runs_cpu ON qc_runs(cpu_id, finished_at)"],
    # each take's contact sheet and its tile layout; dropping the takes cursor makes the next
    # sync re-pull the whole history, so takes synced before these columns get them too
    ["ALTER TABLE takes ADD COLUMN sheet_path TEXT", "ALTER TABLE takes ADD COLUMN sheet_grid TEXT",
     "DELETE FROM kv WHERE key = 'takes_cursor'"],
]
SCHEMA_VERSION = len(MIGRATIONS)


class Store:
    def __init__(self, path=None):
        self.path = Path(path or config.DB_PATH)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # isolation_level=None: no implicit transactions; every write below opens its own
        # short BEGIN IMMEDIATE ... COMMIT, so a reader in another process never waits long.
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None,
                                   timeout=BUSY_TIMEOUT_MS / 1000)
        with self._lock:
            self._db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
            self._migrate()

    def close(self):
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------ plumbing
    def _migrate(self):
        with self._tx():
            have = self._db.execute("PRAGMA user_version").fetchone()[0]
            if have > SCHEMA_VERSION:
                raise RuntimeError(f"{self.path} is schema v{have}; this code knows v{SCHEMA_VERSION}")
            for version in range(have, SCHEMA_VERSION):
                for stmt in MIGRATIONS[version]:
                    self._db.execute(stmt)
                self._db.execute(f"PRAGMA user_version={version + 1}")

    class _Tx:
        def __init__(self, store):
            self.s = store

        def __enter__(self):
            self.s._lock.acquire()
            try:
                self.s._db.execute("BEGIN IMMEDIATE")
            except BaseException:
                self.s._lock.release()
                raise
            return self.s._db

        def __exit__(self, exc_type, exc, tb):
            try:
                self.s._db.execute("ROLLBACK" if exc_type else "COMMIT")
            finally:
                self.s._lock.release()
            return False

    def _tx(self):
        return Store._Tx(self)

    def _select(self, sql, params=(), table=None):
        with self._lock:
            cur = self._db.execute(sql, params)
            names = [d[0] for d in cur.description]
            rows = cur.fetchall()
        return [_decode(table, dict(zip(names, r))) for r in rows]

    def _encode(self, table, row):
        cols = [c for c, _ in TABLES[table]]
        unknown = set(row) - set(cols)
        if unknown:
            raise KeyError(f"{table}: unknown column(s) {sorted(unknown)}")
        out = []
        for c in cols:
            v = row.get(c)
            if c in JSON_COLS.get(table, ()):
                v = None if v is None else json.dumps(v)
            elif isinstance(v, bool):
                v = int(v)
            out.append(v)
        return out

    def _insert_sql(self, table, conflict="upsert"):
        cols = [c for c, _ in TABLES[table]]
        sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
        if conflict == "upsert":
            pk = PRIMARY_KEYS[table]
            rest = [c for c in cols if c not in pk]
            sql += f" ON CONFLICT({', '.join(pk)}) DO UPDATE SET " + ", ".join(f"{c}=excluded.{c}" for c in rest)
        return sql

    # ------------------------------------------------------------------ cache writes (sync.py)
    def upsert(self, table, rows):
        """Insert or update rows by primary key, BATCH rows per transaction. -> count."""
        sql, values = self._insert_sql(table), [self._encode(table, r) for r in rows]
        for i in range(0, len(values), BATCH):
            with self._tx() as db:
                db.executemany(sql, values[i:i + BATCH])
        return len(values)

    def replace_all(self, table, rows):
        """Make `table` exactly `rows` (small, fully-pulled tables), in one transaction."""
        return self.replace_where(table, rows, "1=1")

    def replace_where(self, table, rows, where, params=()):
        """Delete the rows matching `where`, then insert `rows`, atomically. Used for a
        window that was pulled whole (items, camera events), so rows deleted upstream
        disappear here too."""
        values = [self._encode(table, r) for r in rows]
        with self._tx() as db:
            db.execute(f"DELETE FROM {table} WHERE {where}", params)
            db.executemany(self._insert_sql(table), values)
        return len(values)

    def delete_takes_missing(self, keep_ids, t0, t1):
        """After a complete pull of created_at in [t0, t1): drop local takes in that range
        that production no longer has. -> number deleted."""
        with self._tx() as db:
            db.execute("CREATE TEMP TABLE IF NOT EXISTS _keep (id TEXT PRIMARY KEY)")
            db.execute("DELETE FROM _keep")
            db.executemany("INSERT OR IGNORE INTO _keep VALUES (?)", [(i,) for i in keep_ids])
            n = db.execute("DELETE FROM takes WHERE created_at >= ? AND created_at < ? "
                           "AND id NOT IN (SELECT id FROM _keep)", (t0, t1)).rowcount
            db.execute("DELETE FROM _keep")
        return n

    def prune(self, before):
        """Drop cached history older than `before` (epoch s). An item still out is kept
        however old it is: open_items() must keep seeing it. -> {table: rows deleted}."""
        out = {}
        with self._tx() as db:
            out["takes"] = db.execute("DELETE FROM takes WHERE created_at < ?", (before,)).rowcount
            out["items"] = db.execute("DELETE FROM items WHERE checkout_at < ? AND returned_at IS NOT NULL",
                                      (before,)).rowcount
            out["camera_events"] = db.execute("DELETE FROM camera_events WHERE at < ?", (before,)).rowcount
            out["device_events"] = db.execute("DELETE FROM device_events WHERE ts < ?", (before,)).rowcount
        return out

    def fill_starcap_boards(self):
        """starcaps.board from the newest heartbeat, else the newest take, where production
        left it empty (it only stores a board for Dragon units). -> rows filled."""
        with self._tx() as db:
            return db.execute("""
                UPDATE starcaps SET board = COALESCE(
                    (SELECT d.board FROM devices d WHERE d.cpu_id = starcaps.cpu_id AND d.board IS NOT NULL
                      ORDER BY d.last_seen DESC LIMIT 1),
                    (SELECT t.board FROM takes t WHERE t.cpu_id = starcaps.cpu_id AND t.board IS NOT NULL
                      ORDER BY t.created_at DESC LIMIT 1))
                WHERE board IS NULL""").rowcount

    def max_seq(self):
        """{device_id: highest stored seq} for every device with stored events."""
        with self._lock:
            return dict(self._db.execute("SELECT device_id, MAX(seq) FROM device_events GROUP BY device_id"))

    def count(self, table):
        if table not in TABLES and table not in ("notes", "summaries"):
            raise KeyError(table)
        with self._lock:
            return self._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def clear_cache(self):
        """Empty every synced table (and kv, so the next sync starts from scratch) but
        keep `notes` and `summaries`: a corrupt or stale cache can be rebuilt without
        touching the only data in this file that exists nowhere else."""
        with self._tx() as db:
            for table in CACHE_TABLES:
                db.execute(f"DELETE FROM {table}")

    # ------------------------------------------------------------------ loaders (api.py)
    def starcaps(self):
        return self._select("SELECT * FROM starcaps ORDER BY name", table="starcaps")

    def cameras(self):
        return self._select("SELECT * FROM cameras ORDER BY id", table="cameras")

    def cal_versions(self):
        return self._select("SELECT * FROM cal_versions ORDER BY camera_key, calibrated_at", table="cal_versions")

    def cal_current(self):
        return self._select("SELECT * FROM cal_current ORDER BY camera_id, kind", table="cal_current")

    def tickets(self):
        return self._select("SELECT * FROM tickets ORDER BY created_at", table="tickets")

    def qc_runs(self):
        """Every StarCap QC report, oldest first."""
        return self._select("SELECT * FROM qc_runs ORDER BY finished_at", table="qc_runs")

    def devices(self):
        return self._select("SELECT * FROM devices ORDER BY last_seen DESC", table="devices")

    def items(self, t0, t1):
        """Check-outs with checkout_at in [t0, t1)."""
        return self._select("SELECT * FROM items WHERE checkout_at >= ? AND checkout_at < ? ORDER BY checkout_at",
                            (t0, t1), table="items")

    def open_items(self):
        return self._select("SELECT * FROM items WHERE returned_at IS NULL ORDER BY checkout_at", table="items")

    def field_takes(self, t0, t1):
        """Takes whose trip ITEM was checked out in [t0, t1), each with the item's
        checkout_at, returned_at and camera (item_camera_id)."""
        return self._select(
            "SELECT t.*, i.checkout_at AS checkout_at, i.returned_at AS returned_at, i.camera_id AS item_camera_id "
            "FROM items i JOIN takes t ON t.trip_item_id = i.id "
            "WHERE i.checkout_at >= ? AND i.checkout_at < ? ORDER BY t.created_at", (t0, t1), table="takes")

    def takes_created(self, t0, t1):
        return self._select("SELECT * FROM takes WHERE created_at >= ? AND created_at < ? ORDER BY created_at",
                            (t0, t1), table="takes")

    def takes_off_trip(self, t0, t1):
        return self._select("SELECT * FROM takes WHERE trip_item_id IS NULL AND created_at >= ? AND created_at < ? "
                            "ORDER BY created_at", (t0, t1), table="takes")

    def takes_for_camera(self, keys, t0, t1):
        """Takes recorded with one of the camera units in `keys` ("43", "43.1"), created in [t0, t1)."""
        keys = [str(k) for k in keys]
        if not keys:
            return []
        marks = ", ".join("?" * len(keys))
        return self._select(f"SELECT * FROM takes WHERE camera_unit IN ({marks}) AND created_at >= ? "
                            f"AND created_at < ? ORDER BY created_at", (*keys, t0, t1), table="takes")

    def camera_events(self, t0, t1):
        return self._select("SELECT * FROM camera_events WHERE at >= ? AND at < ? ORDER BY at", (t0, t1),
                            table="camera_events")

    def device_events(self, t0, t1, device_ids=None, types=None):
        """Recorder events with ts in [t0, t1), oldest first, optionally narrowed to some
        devices and/or event types."""
        sql, params = "SELECT * FROM device_events WHERE ts >= ? AND ts < ?", [t0, t1]
        for col, values in (("device_id", device_ids), ("type", types)):
            if values is not None:
                values = list(values)
                if not values:
                    return []
                sql += f" AND {col} IN ({', '.join('?' * len(values))})"
                params += values
        return self._select(sql + " ORDER BY ts, device_id, seq", params, table="device_events")

    # ------------------------------------------------------------------ kv
    def kv_get(self, key, default=None):
        with self._lock:
            row = self._db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        if row is None or row[0] is None:
            return default
        try:
            return json.loads(row[0])
        except ValueError:
            return default

    def kv_set(self, key, value):
        with self._tx() as db:
            db.execute("INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE "
                       "SET value = excluded.value, updated_at = excluded.updated_at",
                       (key, json.dumps(value), time.time()))

    # ------------------------------------------------------------------ my notes
    def notes(self, subject_type=None, subject_id=None, limit=200):
        """Notes, newest first; soft-deleted ones are hidden."""
        sql, params = "SELECT * FROM notes WHERE deleted_at IS NULL", []
        if subject_type is not None:
            sql += " AND subject_type = ?"
            params.append(subject_type)
        if subject_id is not None:
            sql += " AND subject_id = ?"
            params.append(str(subject_id))
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(int(limit))
        return [_note(r) for r in self._select(sql, params)]

    def add_note(self, subject_type, subject_id, text, status=None):
        """Save a note about a cap or a camera. `status` (ok, watch, repair, dead, lost)
        is optional; a note may be only a status change, but not empty of both."""
        if subject_type not in NOTE_SUBJECTS:
            raise ValueError(f"subject_type must be one of {', '.join(NOTE_SUBJECTS)}")
        subject_id = str(subject_id or "").strip()
        if not subject_id:
            raise ValueError("subject_id is required")
        status = (status or None) and str(status).strip().lower() or None
        if status is not None and status not in NOTE_STATUSES:
            raise ValueError(f"status must be one of {', '.join(NOTE_STATUSES)}, or empty")
        text = str(text or "").strip()
        if not text and status is None:
            raise ValueError("a note needs text or a status")
        if len(text) > NOTE_TEXT_MAX:
            raise ValueError(f"note is longer than {NOTE_TEXT_MAX} characters")
        now = time.time()
        with self._tx() as db:
            note_id = db.execute("INSERT INTO notes (subject_type, subject_id, created_at, text, status) "
                                 "VALUES (?, ?, ?, ?, ?)", (subject_type, subject_id, now, text, status)).lastrowid
        return {"id": note_id, "subject_type": subject_type, "subject_id": subject_id,
                "created_at": now, "text": text, "status": status}

    def delete_note(self, note_id):
        """Soft delete. -> True if a visible note was hidden."""
        with self._tx() as db:
            return db.execute("UPDATE notes SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
                              (time.time(), int(note_id))).rowcount == 1

    def current_statuses(self, subject_type=None):
        """Each unit's current status: its newest non-deleted note that carries a status,
        unless that status is 'ok' (which clears it). -> [{subject_type, subject_id,
        status, since, text}], newest first."""
        sql = """SELECT n.subject_type, n.subject_id, n.status, n.created_at AS since, n.text
                   FROM notes n
                  WHERE n.deleted_at IS NULL AND n.status IS NOT NULL
                    AND n.id = (SELECT m.id FROM notes m
                                 WHERE m.subject_type = n.subject_type AND m.subject_id = n.subject_id
                                   AND m.deleted_at IS NULL AND m.status IS NOT NULL
                                 ORDER BY m.created_at DESC, m.id DESC LIMIT 1)"""
        params = []
        if subject_type is not None:
            sql += " AND n.subject_type = ?"
            params.append(subject_type)
        rows = self._select(sql + " ORDER BY n.created_at DESC, n.id DESC", params)
        return [r for r in rows if r["status"] != "ok"]

    def current_status(self, subject_type, subject_id):
        """One unit's current status dict (see current_statuses), or None."""
        for r in self.current_statuses(subject_type):
            if r["subject_id"] == str(subject_id):
                return r
        return None

    # ------------------------------------------------------------------ summaries
    def summary_get(self, date):
        rows = self._select("SELECT date, text, written_at FROM summaries WHERE date = ?", (date,))
        return rows[0] if rows else None

    def summary_put(self, date, text):
        now = time.time()
        with self._tx() as db:
            db.execute("INSERT INTO summaries (date, text, written_at) VALUES (?, ?, ?) ON CONFLICT(date) "
                       "DO UPDATE SET text = excluded.text, written_at = excluded.written_at", (date, text, now))
        return {"date": date, "text": text, "written_at": now}


def _decode(table, row):
    if table is None:
        return row
    for c in BOOL_COLS.get(table, ()):
        if c in row and row[c] is not None:
            row[c] = bool(row[c])
    for c in JSON_COLS.get(table, ()):
        if c in row:
            empty = None if c in JSON_NULL_IS_NONE else []
            try:
                row[c] = json.loads(row[c]) if row[c] is not None else empty
            except ValueError:
                row[c] = empty
    return row


def _note(row):
    """The API's note shape (SPEC §4). deleted_at is left out: notes() never returns a
    deleted note, so it would always be null."""
    return {k: row[k] for k in ("id", "subject_type", "subject_id", "created_at", "text", "status")}
