"""Builders for in-memory GTFS ZIP fixtures used by the test suite."""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

STOPS = """stop_id,stop_name,stop_lat,stop_lon
S1,Central,31.2304,121.4737
S2,Market,31.2350,121.4800
S3,Airport,31.2000,121.8000
"""

CALENDAR = """service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date
WEEKDAY,1,1,1,1,1,0,0,20261101,20261231
WEEKEND,0,0,0,0,0,1,1,20261101,20261231
SPECIAL,0,0,0,0,0,0,0,20261101,20261231
"""

CALENDAR_DATES = """service_id,date,exception_type
SPECIAL,20261115,1
SPECIAL,20261117,1
WEEKDAY,20261117,2
WEEKEND,20261122,2
"""

TRIPS = """route_id,service_id,trip_id,trip_headsign,direction_id
R1,WEEKDAY,T_WEEKDAY_AM,Airport,0
R1,WEEKDAY,T_WEEKDAY_NIGHT,Airport,0
R1,WEEKDAY,T_WEEKDAY_LATE,Airport,0
R2,WEEKEND,T_WEEKEND_AM,Central,1
R3,SPECIAL,T_SPECIAL_AM,Market,0
"""

STOP_TIMES = """trip_id,arrival_time,departure_time,stop_id,stop_sequence
T_WEEKDAY_AM,08:00:00,08:00:00,S1,1
T_WEEKDAY_AM,08:10:00,08:11:00,S2,2
T_WEEKDAY_AM,08:30:00,08:30:00,S3,3
T_WEEKDAY_NIGHT,23:50:00,23:50:00,S1,1
T_WEEKDAY_NIGHT,24:10:00,24:10:00,S2,2
T_WEEKDAY_NIGHT,24:40:00,24:40:00,S3,3
T_WEEKDAY_LATE,25:00:00,25:00:00,S2,1
T_WEEKDAY_LATE,25:30:00,25:30:00,S3,2
T_WEEKEND_AM,10:00:00,10:00:00,S2,1
T_WEEKEND_AM,10:30:00,10:30:00,S3,2
T_SPECIAL_AM,12:00:00,12:00:00,S1,1
T_SPECIAL_AM,12:20:00,12:20:00,S2,2
"""


def build_zip(files: dict[str, str] | None = None) -> bytes:
    if files is None:
        files = {
            "stops.txt": STOPS,
            "calendar.txt": CALENDAR,
            "calendar_dates.txt": CALENDAR_DATES,
            "trips.txt": TRIPS,
            "stop_times.txt": STOP_TIMES,
        }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buf.getvalue()


def default_files() -> dict[str, str]:
    return {
        "stops.txt": STOPS,
        "calendar.txt": CALENDAR,
        "calendar_dates.txt": CALENDAR_DATES,
        "trips.txt": TRIPS,
        "stop_times.txt": STOP_TIMES,
    }


def write_zip(path: Path, files: dict[str, str] | None = None) -> Path:
    path.write_bytes(build_zip(files))
    return path
