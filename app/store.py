"""SQLite persistence for immutable timetable versions.

Layout
------
``versions``            one row per successfully published feed version.
``feed_meta``/``stops``/``service``/``service_exception``/``trip``/
``stop_time``           feed content, keyed by ``version_id``.  Rows are only
                        ever inserted, never updated or deleted (old versions
                        remain readable after a restart and after newer
                        uploads).
``state``               pointer to the single ``current_version_id``.

Publication is one SQLite transaction: validation happens entirely before the
transaction starts, so a rejected package changes nothing.  Readers open a
short read transaction pinned to the pointer they observed, therefore a
concurrent publication cannot mix two versions inside one query.
"""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Iterator

from .gtfs import CalendarEntry, FeedData, Stop

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA busy_timeout=10000;

CREATE TABLE IF NOT EXISTS versions (
    version_id   TEXT PRIMARY KEY,
    uploaded_at  TEXT NOT NULL,
    source_name  TEXT,
    stop_count   INTEGER NOT NULL,
    trip_count   INTEGER NOT NULL,
    stop_time_count INTEGER NOT NULL,
    sha256       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feed_meta (
    version_id        TEXT PRIMARY KEY REFERENCES versions(version_id),
    calendar_start    TEXT,
    calendar_end      TEXT
);

CREATE TABLE IF NOT EXISTS stops (
    version_id TEXT NOT NULL REFERENCES versions(version_id),
    stop_id    TEXT NOT NULL,
    stop_name  TEXT NOT NULL,
    stop_lat   REAL,
    stop_lon   REAL,
    PRIMARY KEY (version_id, stop_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS service (
    version_id TEXT NOT NULL REFERENCES versions(version_id),
    service_id TEXT NOT NULL,
    mon INTEGER NOT NULL,
    tue INTEGER NOT NULL,
    wed INTEGER NOT NULL,
    thu INTEGER NOT NULL,
    fri INTEGER NOT NULL,
    sat INTEGER NOT NULL,
    sun INTEGER NOT NULL,
    start_date TEXT NOT NULL,
    end_date   TEXT NOT NULL,
    PRIMARY KEY (version_id, service_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS service_exception (
    version_id   TEXT NOT NULL REFERENCES versions(version_id),
    service_id   TEXT NOT NULL,
    date         TEXT NOT NULL,
    exception_type INTEGER NOT NULL,
    PRIMARY KEY (version_id, service_id, date, exception_type)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS trip (
    version_id TEXT NOT NULL REFERENCES versions(version_id),
    trip_id    TEXT NOT NULL,
    route_id   TEXT NOT NULL,
    service_id TEXT NOT NULL,
    headsign   TEXT,
    direction_id INTEGER,
    PRIMARY KEY (version_id, trip_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS stop_time (
    version_id  TEXT NOT NULL REFERENCES versions(version_id),
    trip_id     TEXT NOT NULL,
    stop_sequence INTEGER NOT NULL,
    stop_id     TEXT NOT NULL,
    arrival     INTEGER NOT NULL,
    departure   INTEGER NOT NULL,
    stop_headsign TEXT,
    interpolated INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (version_id, trip_id, stop_sequence)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_stop_time_origin
    ON stop_time(version_id, stop_id, arrival);

CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class VersionNotFound(Exception):
    pass


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            self._migrate(conn)
        finally:
            conn.close()

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add columns introduced after a database was first created."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(stop_time)")}
        if "interpolated" not in cols:
            conn.execute("ALTER TABLE stop_time ADD COLUMN interpolated INTEGER NOT NULL DEFAULT 0")

    # -- connections -----------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    # -- pointer ---------------------------------------------------------
    def current_version_id(self) -> str | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT value FROM state WHERE key='current_version_id'").fetchone()
            return row["value"] if row else None
        finally:
            conn.close()

    def list_versions(self) -> list[dict]:
        conn = self._connect()
        try:
            current = self.current_version_id()
            rows = conn.execute(
                "SELECT * FROM versions ORDER BY uploaded_at ASC, rowid ASC"
            ).fetchall()
            return [{**dict(r), "is_current": r["version_id"] == current} for r in rows]
        finally:
            conn.close()

    def get_version(self, version_id: str | None = None) -> dict | None:
        vid = version_id or self.current_version_id()
        if vid is None:
            return None
        conn = self._connect()
        try:
            r = conn.execute("SELECT * FROM versions WHERE version_id = ?", (vid,)).fetchone()
            if r is None:
                if version_id is not None:
                    raise VersionNotFound(version_id)
                return None
            meta = conn.execute(
                "SELECT calendar_start, calendar_end FROM feed_meta WHERE version_id = ?", (vid,)
            ).fetchone()
            out = dict(r)
            out["calendar_start"] = meta["calendar_start"] if meta else None
            out["calendar_end"] = meta["calendar_end"] if meta else None
            out["is_current"] = vid == self.current_version_id()
            return out
        finally:
            conn.close()

    # -- publication -----------------------------------------------------
    def publish(self, feed: FeedData, source_name: str | None, sha256: str) -> str:
        """Atomically publish a fully validated feed as a new version.

        The caller must have validated the feed (``load_feed``).  Any failure
        while writing rolls the transaction back; the pointer is updated last
        in the same transaction.
        """
        version_id = f"v-{uuid.uuid4().hex}"
        uploaded_at = _now_iso()
        starts = [c.start_date for c in feed.calendars.values()]
        ends = [c.end_date for c in feed.calendars.values()]

        with self._write_lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO versions(version_id, uploaded_at, source_name, stop_count,"
                    " trip_count, stop_time_count, sha256) VALUES(?,?,?,?,?,?,?)",
                    (
                        version_id,
                        uploaded_at,
                        source_name,
                        len(feed.stops),
                        len(feed.trips),
                        sum(len(v) for v in feed.stop_times.values()),
                        sha256,
                    ),
                )
                conn.execute(
                    "INSERT INTO feed_meta(version_id, calendar_start, calendar_end) VALUES(?,?,?)",
                    (
                        version_id,
                        min(starts).isoformat() if starts else None,
                        max(ends).isoformat() if ends else None,
                    ),
                )
                conn.executemany(
                    "INSERT INTO stops(version_id, stop_id, stop_name, stop_lat, stop_lon)"
                    " VALUES(?,?,?,?,?)",
                    [
                        (version_id, s.stop_id, s.stop_name, s.stop_lat, s.stop_lon)
                        for s in feed.stops.values()
                    ],
                )
                conn.executemany(
                    "INSERT INTO service(version_id, service_id, mon, tue, wed, thu, fri, sat,"
                    " sun, start_date, end_date) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        (
                            version_id,
                            c.service_id,
                            *[1 if d else 0 for d in c.weekdays],
                            c.start_date.isoformat(),
                            c.end_date.isoformat(),
                        )
                        for c in feed.calendars.values()
                    ],
                )
                conn.executemany(
                    "INSERT INTO service_exception(version_id, service_id, date, exception_type)"
                    " VALUES(?,?,?,?)",
                    [
                        (version_id, e.service_id, e.date.isoformat(), e.exception_type)
                        for e in feed.exceptions
                    ],
                )
                conn.executemany(
                    "INSERT INTO trip(version_id, trip_id, route_id, service_id, headsign,"
                    " direction_id) VALUES(?,?,?,?,?,?)",
                    [
                        (version_id, t.trip_id, t.route_id, t.service_id, t.headsign,
                         t.direction_id)
                        for t in feed.trips.values()
                    ],
                )
                rows = (
                    (version_id, t.trip_id, st.stop_sequence, st.stop_id, st.arrival,
                     st.departure, st.stop_headsign, int(st.interpolated))
                    for t in feed.trips.values()
                    for st in feed.stop_times.get(t.trip_id, [])
                )
                conn.executemany(
                    "INSERT INTO stop_time(version_id, trip_id, stop_sequence, stop_id,"
                    " arrival, departure, stop_headsign, interpolated)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    rows,
                )
                conn.execute(
                    "INSERT INTO state(key, value) VALUES('current_version_id', ?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (version_id,),
                )
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                conn.close()
        return version_id

    # -- feed access for readers ----------------------------------------
    @contextmanager
    def open_current(self, version_id: str | None = None) -> Iterator["VersionReader"]:
        """Open one immutable snapshot of the requested (or current) version.

        When ``version_id`` is omitted the state pointer is read inside the
        same transaction as the feed data, so a publication that commits
        concurrently can never produce a half-old/half-new answer: the query
        sees exactly one version.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            if version_id is None:
                row = conn.execute(
                    "SELECT value FROM state WHERE key='current_version_id'"
                ).fetchone()
                version_id = row["value"] if row else None
                if version_id is None:
                    raise VersionNotFound("<none>")
            row = conn.execute(
                "SELECT 1 FROM versions WHERE version_id = ?", (version_id,)
            ).fetchone()
            if row is None:
                raise VersionNotFound(version_id)
            yield VersionReader(conn, version_id)
        finally:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            conn.close()


class VersionReader:
    """Read-only access to one pinned feed version."""

    def __init__(self, conn: sqlite3.Connection, version_id: str):
        self.conn = conn
        self.version_id = version_id

    def stops(self) -> dict[str, Stop]:
        rows = self.conn.execute(
            "SELECT stop_id, stop_name, stop_lat, stop_lon FROM stops WHERE version_id=?",
            (self.version_id,),
        ).fetchall()
        return {
            r["stop_id"]: Stop(r["stop_id"], r["stop_name"], r["stop_lat"], r["stop_lon"])
            for r in rows
        }

    def stop(self, stop_id: str) -> Stop | None:
        r = self.conn.execute(
            "SELECT stop_id, stop_name, stop_lat, stop_lon FROM stops"
            " WHERE version_id=? AND stop_id=?",
            (self.version_id, stop_id),
        ).fetchone()
        if r is None:
            return None
        return Stop(r["stop_id"], r["stop_name"], r["stop_lat"], r["stop_lon"])

    def services(self) -> dict[str, CalendarEntry]:
        rows = self.conn.execute(
            "SELECT * FROM service WHERE version_id=?", (self.version_id,)
        ).fetchall()
        out: dict[str, CalendarEntry] = {}
        for r in rows:
            out[r["service_id"]] = CalendarEntry(
                service_id=r["service_id"],
                weekdays=(bool(r["mon"]), bool(r["tue"]), bool(r["wed"]), bool(r["thu"]),
                          bool(r["fri"]), bool(r["sat"]), bool(r["sun"])),
                start_date=date.fromisoformat(r["start_date"]),
                end_date=date.fromisoformat(r["end_date"]),
            )
        return out

    def exceptions(self, dates: list[str]) -> dict[tuple[str, str], int]:
        """exception_type keyed by (service_id, ISO date) for the given dates."""
        if not dates:
            return {}
        placeholders = ",".join("?" for _ in dates)
        rows = self.conn.execute(
            f"SELECT service_id, date, exception_type FROM service_exception"
            f" WHERE version_id=? AND date IN ({placeholders})",
            (self.version_id, *dates),
        ).fetchall()
        return {(r["service_id"], r["date"]): r["exception_type"] for r in rows}

    def calls_at_stop(self, stop_id: str, max_arrival: int) -> list[dict]:
        """All calls at one stop whose arrival is <= ``max_arrival`` seconds.

        ``max_arrival`` is inclusive and may exceed 24h; callers narrow the
        result to the local date they want.
        """
        rows = self.conn.execute(
            "SELECT st.trip_id, st.stop_sequence, st.stop_id, st.arrival, st.departure,"
            " st.stop_headsign, st.interpolated, t.route_id, t.service_id,"
            " t.headsign AS trip_headsign, t.direction_id"
            " FROM stop_time st JOIN trip t"
            "   ON t.version_id = st.version_id AND t.trip_id = st.trip_id"
            " WHERE st.version_id=? AND st.stop_id=? AND st.arrival <= ?"
            " ORDER BY st.arrival, st.trip_id, st.stop_sequence",
            (self.version_id, stop_id, max_arrival),
        ).fetchall()
        return [dict(r) for r in rows]

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
