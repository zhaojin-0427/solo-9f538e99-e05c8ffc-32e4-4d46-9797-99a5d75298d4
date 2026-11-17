"""GTFS feed parsing and whole-feed validation.

The module is deliberately dependency-free (stdlib only) so it can also be
used as a library.  `load_feed` reads a ZIP from a directory and returns a
fully validated, normalised `FeedData` structure or raises `FeedError` with a
list of human readable problems.

Only the five files required by the task are interpreted:
stops.txt, calendar.txt, calendar_dates.txt, trips.txt, stop_times.txt.
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

REQUIRED_FILES = (
    "stops.txt",
    "calendar.txt",
    "calendar_dates.txt",
    "trips.txt",
    "stop_times.txt",
)

# GTFS spec upper bound for a time value is 47:59:59 (service next day).
MAX_TIME_SECONDS = 47 * 3600 + 59 * 60 + 59

# Guards against decompression bombs / absurd uploads.
MAX_MEMBER_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024

_TIME_RE = re.compile(r"^(\d{1,3}):([0-5]\d):([0-5]\d)$")
_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


class FeedError(Exception):
    """Raised when an uploaded feed is structurally invalid or inconsistent."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors[:20]))


@dataclass(frozen=True)
class Stop:
    stop_id: str
    stop_name: str
    stop_lat: float | None
    stop_lon: float | None


@dataclass(frozen=True)
class CalendarEntry:
    service_id: str
    weekdays: tuple[bool, ...]  # Monday .. Sunday
    start_date: date
    end_date: date


@dataclass(frozen=True)
class CalendarException:
    service_id: str
    date: date
    exception_type: int  # 1 = service added, 2 = service removed


@dataclass(frozen=True)
class Trip:
    trip_id: str
    route_id: str
    service_id: str
    headsign: str | None = None
    direction_id: int | None = None


@dataclass(frozen=True)
class StopTime:
    trip_id: str
    stop_id: str
    arrival: int  # seconds since midnight of the service day (may exceed 24h)
    departure: int
    stop_sequence: int
    stop_headsign: str | None = None
    interpolated: bool = False  # timing was inferred, not given by the operator


@dataclass
class FeedData:
    stops: dict[str, Stop] = field(default_factory=dict)
    calendars: dict[str, CalendarEntry] = field(default_factory=dict)
    exceptions: list[CalendarException] = field(default_factory=list)
    trips: dict[str, Trip] = field(default_factory=dict)
    stop_times: dict[str, list[StopTime]] = field(default_factory=dict)

    def stop_order(self, trip_id: str) -> list[StopTime]:
        return self.stop_times.get(trip_id, [])


class _ErrorCollector:
    MAX = 200

    def __init__(self) -> None:
        self.errors: list[str] = []

    def add(self, msg: str) -> None:
        if len(self.errors) < self.MAX:
            self.errors.append(msg)
        elif len(self.errors) == self.MAX:
            self.errors.append("... further errors truncated")

    def raise_if_any(self) -> None:
        if self.errors:
            raise FeedError(self.errors)


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def _read_csv(zf: zipfile.ZipFile, name: str, errors: _ErrorCollector) -> list[dict[str, str]] | None:
    try:
        raw = zf.read(name)
    except KeyError:
        errors.add(f"{name}: missing required file")
        return None
    try:
        text = _decode(raw)
    except Exception as exc:  # pragma: no cover - defensive
        errors.add(f"{name}: cannot decode file ({exc})")
        return None
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames is None:
        errors.add(f"{name}: missing header row")
        return None
    # Normalise header names: strip BOM/whitespace.
    reader.fieldnames = [f.strip().lstrip("﻿") for f in reader.fieldnames]
    rows: list[dict[str, str]] = []
    for line_no, row in enumerate(reader, start=2):
        if None in row:
            errors.add(f"{name}:{line_no}: too many fields in row")
            continue
        clean = {k: (v.strip() if isinstance(v, str) else "") for k, v in row.items()}
        if not any(clean.values()):
            continue
        clean["__line__"] = str(line_no)
        rows.append(clean)
    return rows


def _require_columns(name: str, rows: list[dict[str, str]], required: list[str], errors: _ErrorCollector) -> bool:
    if not rows:
        return True
    present = set(rows[0].keys())
    missing = [c for c in required if c not in present]
    if missing:
        errors.add(f"{name}: missing required columns: {', '.join(missing)}")
        return False
    return True


def parse_time(value: str) -> int | None:
    """Parse a GTFS HH:MM:SS time (hours may exceed 24, up to 47:59:59)."""
    m = _TIME_RE.match(value)
    if not m:
        return None
    h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
    total = h * 3600 + mi * 60 + s
    if total > MAX_TIME_SECONDS:
        return None
    return total


def parse_date(value: str) -> date | None:
    m = _DATE_RE.match(value)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _parse_int(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_bool_weekday(value: str) -> bool:
    return value.strip() in ("1", "true", "True", "TRUE")


def load_feed(path: str | Path) -> FeedData:
    """Parse and fully validate a GTFS ZIP archive.

    Returns FeedData when every reference, stop ordering and time is valid.
    Raises FeedError listing all problems otherwise.
    """
    path = Path(path)
    errors = _ErrorCollector()
    feed = FeedData()

    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise FeedError([f"archive is not a readable ZIP file: {exc}"]) from exc

    with zf:
        def member_names() -> list[str]:
            # Ignore directory entries and macOS resource-fork noise.
            return [n for n in zf.namelist()
                    if not n.endswith("/") and not n.split("/")[-1].startswith("._")
                    and "__MACOSX" not in n.split("/")]

        infos = [i for i in zf.infolist() if i.filename in member_names()]
        total = sum(i.file_size for i in infos)
        if total > MAX_TOTAL_BYTES:
            errors.add(f"archive: uncompressed size {total} exceeds limit {MAX_TOTAL_BYTES}")
            errors.raise_if_any()
        for info in infos:
            if info.file_size > MAX_MEMBER_BYTES:
                errors.add(f"archive: member {info.filename!r} exceeds size limit")
        errors.raise_if_any()
        if zf.testzip() is not None:
            raise FeedError(["archive is corrupt (CRC check failed)"])
        names: dict[str, str] = {}
        for n in member_names():
            base = n.split("/")[-1]
            if base in names:
                errors.add(f"archive: duplicate file entry {base!r} ({names[base]!r} and {n!r})")
                continue
            names[base] = n
        for required in REQUIRED_FILES:
            if required not in names:
                errors.add(f"archive: missing required file {required}")
        if errors.errors:
            errors.raise_if_any()

        stops_rows = _read_csv(zf, names["stops.txt"], errors) or []
        calendar_rows = _read_csv(zf, names["calendar.txt"], errors) or []
        exception_rows = _read_csv(zf, names["calendar_dates.txt"], errors) or []
        trip_rows = _read_csv(zf, names["trips.txt"], errors) or []
        stop_time_rows = _read_csv(zf, names["stop_times.txt"], errors) or []

    _require_columns("stops.txt", stops_rows, ["stop_id", "stop_name"], errors)
    _require_columns("calendar.txt", calendar_rows,
                     ["service_id", "monday", "tuesday", "wednesday", "thursday",
                      "friday", "saturday", "sunday", "start_date", "end_date"], errors)
    _require_columns("calendar_dates.txt", exception_rows,
                     ["service_id", "date", "exception_type"], errors)
    _require_columns("trips.txt", trip_rows, ["route_id", "service_id", "trip_id"], errors)
    _require_columns("stop_times.txt", stop_time_rows,
                     ["trip_id", "arrival_time", "departure_time", "stop_id", "stop_sequence"],
                     errors)
    errors.raise_if_any()

    # ---- stops ---------------------------------------------------------
    for row in stops_rows:
        sid = row.get("stop_id", "")
        if not sid:
            errors.add(f"stops.txt:{row['__line__']}: empty stop_id")
            continue
        if sid in feed.stops:
            errors.add(f"stops.txt:{row['__line__']}: duplicate stop_id {sid!r}")
            continue
        lat = _parse_float(row.get("stop_lat", ""))
        lon = _parse_float(row.get("stop_lon", ""))
        if row.get("stop_lat") and lat is None:
            errors.add(f"stops.txt:{row['__line__']}: invalid stop_lat {row.get('stop_lat')!r}")
        if row.get("stop_lon") and lon is None:
            errors.add(f"stops.txt:{row['__line__']}: invalid stop_lon {row.get('stop_lon')!r}")
        if lat is not None and not (-90 <= lat <= 90):
            errors.add(f"stops.txt:{row['__line__']}: stop_lat out of range")
        if lon is not None and not (-180 <= lon <= 180):
            errors.add(f"stops.txt:{row['__line__']}: stop_lon out of range")
        feed.stops[sid] = Stop(
            stop_id=sid,
            stop_name=row.get("stop_name") or sid,
            stop_lat=lat,
            stop_lon=lon,
        )

    # ---- calendar ------------------------------------------------------
    for row in calendar_rows:
        sid = row.get("service_id", "")
        if not sid:
            errors.add(f"calendar.txt:{row['__line__']}: empty service_id")
            continue
        if sid in feed.calendars:
            errors.add(f"calendar.txt:{row['__line__']}: duplicate service_id {sid!r}")
            continue
        weekdays = tuple(_parse_bool_weekday(row.get(d, "0")) for d in _WEEKDAYS)
        start = parse_date(row.get("start_date", ""))
        end = parse_date(row.get("end_date", ""))
        if start is None:
            errors.add(f"calendar.txt:{row['__line__']}: invalid start_date {row.get('start_date')!r}")
            continue
        if end is None:
            errors.add(f"calendar.txt:{row['__line__']}: invalid end_date {row.get('end_date')!r}")
            continue
        if end < start:
            errors.add(f"calendar.txt:{row['__line__']}: end_date {end} before start_date {start}")
            continue
        if not any(weekdays):
            # A service running no weekdays is legal if calendar_dates adds
            # it; that is verified once exceptions have been parsed below.
            pass
        feed.calendars[sid] = CalendarEntry(sid, weekdays, start, end)

    for row in exception_rows:
        sid = row.get("service_id", "")
        if not sid:
            errors.add(f"calendar_dates.txt:{row['__line__']}: empty service_id")
            continue
        d = parse_date(row.get("date", ""))
        if d is None:
            errors.add(f"calendar_dates.txt:{row['__line__']}: invalid date {row.get('date')!r}")
            continue
        et = _parse_int(row.get("exception_type", ""))
        if et not in (1, 2):
            errors.add(f"calendar_dates.txt:{row['__line__']}: exception_type must be 1 or 2, got {row.get('exception_type')!r}")
            continue
        feed.exceptions.append(CalendarException(sid, d, et))

    # A service_id may legitimately be defined only through calendar_dates
    # (GTFS permits omitting calendar.txt, and this task allows a service
    # that exists only via exception_type=1).  A removal (2) for a service
    # that is never defined anywhere is rejected.
    calendar_services = set(feed.calendars)
    added_services = {e.service_id for e in feed.exceptions if e.exception_type == 1}
    known_services = calendar_services | added_services
    for e in feed.exceptions:
        if e.service_id not in calendar_services and e.service_id not in added_services:
            errors.add(f"calendar_dates.txt: service_id {e.service_id!r} has only a removal exception and no calendar definition")
    seen_exc: set[tuple[str, date]] = set()
    for e in feed.exceptions:
        key = (e.service_id, e.date)
        if key in seen_exc:
            errors.add(f"calendar_dates.txt: duplicate exception for service {e.service_id!r} on {e.date}")
            continue
        seen_exc.add(key)

    # A calendar row with no weekday flags can still be legal when
    # calendar_dates adds it; one that is never added anywhere never runs.
    added_dates = {(e.service_id, e.date) for e in feed.exceptions if e.exception_type == 1}
    for sid, entry in feed.calendars.items():
        if not any(entry.weekdays) and not any(s == sid for s, _ in added_dates):
            errors.add(f"calendar.txt: service {sid!r} has no active weekday and no "
                       f"calendar_dates.txt addition, so it never runs")

    # ---- trips ---------------------------------------------------------
    for row in trip_rows:
        tid = row.get("trip_id", "")
        if not tid:
            errors.add(f"trips.txt:{row['__line__']}: empty trip_id")
            continue
        if tid in feed.trips:
            errors.add(f"trips.txt:{row['__line__']}: duplicate trip_id {tid!r}")
            continue
        service_id = row.get("service_id", "")
        if not service_id:
            errors.add(f"trips.txt:{row['__line__']}: trip {tid!r} has empty service_id")
            continue
        if service_id not in known_services:
            errors.add(f"trips.txt:{row['__line__']}: trip {tid!r} references unknown service_id {service_id!r}")
            continue
        direction = _parse_int(row.get("direction_id", "")) if row.get("direction_id") else None
        feed.trips[tid] = Trip(
            trip_id=tid,
            route_id=row.get("route_id") or "",
            service_id=service_id,
            headsign=row.get("trip_headsign") or None,
            direction_id=direction,
        )

    # ---- stop_times ----------------------------------------------------
    for row in stop_time_rows:
        tid = row.get("trip_id", "")
        if tid not in feed.trips:
            errors.add(f"stop_times.txt:{row['__line__']}: unknown trip_id {tid!r}")
            continue
        sid = row.get("stop_id", "")
        if not sid:
            errors.add(f"stop_times.txt:{row['__line__']}: empty stop_id")
            continue
        if sid not in feed.stops:
            errors.add(f"stop_times.txt:{row['__line__']}: unknown stop_id {sid!r}")
            continue
        arr_raw = row.get("arrival_time", "")
        dep_raw = row.get("departure_time", "")
        arrival = parse_time(arr_raw) if arr_raw else None
        departure = parse_time(dep_raw) if dep_raw else None
        if arr_raw and arrival is None:
            errors.add(f"stop_times.txt:{row['__line__']}: invalid arrival_time {arr_raw!r} (expected HH:MM:SS, max 47:59:59)")
            continue
        if dep_raw and departure is None:
            errors.add(f"stop_times.txt:{row['__line__']}: invalid departure_time {dep_raw!r} (expected HH:MM:SS, max 47:59:59)")
            continue
        # GTFS allows blank times at non-timepoint stops; they are filled in
        # by interpolation once the trip's rows are complete.  At least one
        # time must be given per stop row.
        if arrival is None and dep_raw:
            arrival = departure
        if departure is None and arr_raw:
            departure = arrival
        if arrival is None and departure is None and not (arr_raw or dep_raw):
            arrival = departure = -1  # sentinel: interpolate later
        if arrival is not None and departure is not None and arrival >= 0 and departure < arrival:
            errors.add(f"stop_times.txt:{row['__line__']}: departure {dep_raw} before arrival {arr_raw} for trip {tid!r}")
            continue
        seq = _parse_int(row.get("stop_sequence", ""))
        if seq is None:
            errors.add(f"stop_times.txt:{row['__line__']}: invalid stop_sequence {row.get('stop_sequence')!r}")
            continue
        if seq < 0:
            errors.add(f"stop_times.txt:{row['__line__']}: negative stop_sequence {seq}")
            continue
        feed.stop_times.setdefault(tid, []).append(
            StopTime(
                trip_id=tid,
                stop_id=sid,
                arrival=arrival,
                departure=departure,
                stop_sequence=seq,
                stop_headsign=row.get("stop_headsign") or None,
                interpolated=arrival < 0,
            )
        )

    # ---- whole-feed consistency ---------------------------------------
    for tid, trip in feed.trips.items():
        times = feed.stop_times.get(tid, [])
        if not times:
            errors.add(f"trips.txt: trip {tid!r} has no stop_times")
            continue
        times.sort(key=lambda st: st.stop_sequence)
        seqs = [st.stop_sequence for st in times]
        if len(set(seqs)) != len(seqs):
            errors.add(f"stop_times.txt: trip {tid!r} has duplicate stop_sequence values")
            continue
        timed = [i for i, st in enumerate(times) if not st.interpolated]
        if not timed:
            errors.add(f"stop_times.txt: trip {tid!r} has no timed stop at all")
            continue
        if times[0].interpolated or times[-1].interpolated:
            errors.add(f"stop_times.txt: trip {tid!r} must time its first and last stop")
            continue
        # Interpolate blank intermediate times between neighbouring timed stops.
        filled: list[StopTime] = []
        for pos, st in enumerate(times):
            if not st.interpolated:
                filled.append(st)
                continue
            prev_i = max(i for i in timed if i < pos)
            next_i = min(i for i in timed if i > pos)
            prev, nxt = times[prev_i], times[next_i]
            span = nxt.arrival - prev.departure
            steps = next_i - prev_i
            offset = int(round(span * (pos - prev_i) / steps))
            t = prev.departure + offset
            filled.append(StopTime(st.trip_id, st.stop_id, t, t, st.stop_sequence,
                                   st.stop_headsign, interpolated=True))
        times = filled
        for prev, cur in zip(times, times[1:]):
            if cur.arrival < prev.arrival or cur.arrival < prev.departure:
                errors.add(
                    f"stop_times.txt: trip {tid!r} has non-increasing times between "
                    f"sequence {prev.stop_sequence} ({_fmt(prev.arrival)}/{_fmt(prev.departure)}) and "
                    f"sequence {cur.stop_sequence} ({_fmt(cur.arrival)}/{_fmt(cur.departure)})"
                )
                break
        for st in times:
            if st.departure < st.arrival:
                errors.add(
                    f"stop_times.txt: trip {tid!r} sequence {st.stop_sequence} departs "
                    f"{_fmt(st.departure)} before arriving {_fmt(st.arrival)}"
                )
                break
        feed.stop_times[tid] = times

    errors.raise_if_any()
    return feed


def _fmt(seconds: int) -> str:
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def format_time(seconds: int) -> str:
    """Format seconds-since-service-day-start as GTFS HH:MM:SS (hours may exceed 24)."""
    if seconds < 0:
        raise ValueError("negative seconds")
    return _fmt(seconds)
