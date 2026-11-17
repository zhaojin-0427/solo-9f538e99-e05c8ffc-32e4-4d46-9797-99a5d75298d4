"""Generate demo/gtfs_demo.zip with a feed exercising every query rule.

Run:  python3 demo/generate_demo.py
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

STOPS = """stop_id,stop_name,stop_lat,stop_lon
S1,Central Station,31.2304,121.4737
S2,Market Square,31.2350,121.4800
S3,Airport Terminal,31.2000,121.8000
"""

CALENDAR = """service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date
WEEKDAY,1,1,1,1,1,0,0,20261101,20261231
WEEKEND,0,0,0,0,0,1,1,20261101,20261231
HOLIDAY,0,0,0,0,0,0,0,20261101,20261231
"""

CALENDAR_DATES = """service_id,date,exception_type
HOLIDAY,20261117,1
WEEKDAY,20261117,2
WEEKEND,20261122,2
"""

TRIPS = """route_id,service_id,trip_id,trip_headsign,direction_id
R1,WEEKDAY,T_WD_MORNING,Airport,0
R1,WEEKDAY,T_WD_NIGHT,Airport,0
R1,WEEKDAY,T_WD_LAST,Airport,0
R2,WEEKEND,T_WE_MORNING,Central,1
R3,HOLIDAY,T_HOL_NOON,Market,0
"""

STOP_TIMES = """trip_id,arrival_time,departure_time,stop_id,stop_sequence
T_WD_MORNING,08:00:00,08:00:00,S1,1
T_WD_MORNING,08:10:00,08:11:00,S2,2
T_WD_MORNING,08:30:00,08:30:00,S3,3
T_WD_NIGHT,23:50:00,23:50:00,S1,1
T_WD_NIGHT,24:10:00,24:10:00,S2,2
T_WD_NIGHT,24:40:00,24:40:00,S3,3
T_WD_LAST,25:00:00,25:00:00,S2,1
T_WD_LAST,47:59:59,47:59:59,S3,2
T_WE_MORNING,10:00:00,10:00:00,S2,1
T_WE_MORNING,10:30:00,10:30:00,S3,2
T_HOL_NOON,12:00:00,12:00:00,S1,1
T_HOL_NOON,12:20:00,12:20:00,S2,2
"""

FILES = {
    "stops.txt": STOPS,
    "calendar.txt": CALENDAR,
    "calendar_dates.txt": CALENDAR_DATES,
    "trips.txt": TRIPS,
    "stop_times.txt": STOP_TIMES,
}


def main() -> None:
    out = Path(__file__).resolve().parent / "gtfs_demo.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in FILES.items():
            zf.writestr(name, text)
    out.write_bytes(buf.getvalue())
    print(f"wrote {out} ({len(buf.getvalue())} bytes)")


if __name__ == "__main__":
    main()
