# GTFS Stop Timetable API

FastAPI + SQLite service that ingests a GTFS timetable package (ZIP with
`stops.txt`, `calendar.txt`, `calendar_dates.txt`, `trips.txt`,
`stop_times.txt`) and answers "which scheduled arrivals/departures does stop X
have on local date D?".

## Quick start

```bash
docker compose up --build -d
open http://localhost:8000/docs        # interactive Swagger UI
curl -F "file=@demo/gtfs_demo.zip" http://localhost:8000/feeds
curl "http://localhost:8000/stops/S2/calls?date=2026-11-17"
```

Data (all published versions plus the current pointer) lives in the
`gtfs-data` volume at `/data/gtfs.db`, so a container restart keeps every
version re-readable by `version_id`.

Local (no Docker):

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest httpx
uvicorn app.main:app --reload
pytest -q
```

## API

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/feeds` | Upload a ZIP; validate the whole package and publish it atomically |
| GET | `/feeds` | List every published version (`is_current` marks the live one) |
| GET | `/feeds/current` | Metadata of the current version |
| GET | `/feeds/{version_id}` | Metadata of any historical version |
| GET | `/stops` | Stops of the current (or `version_id`-pinned) feed |
| GET | `/feeds/{version_id}/stops` | Stops of a historical version (path form) |
| GET | `/stops/{stop_id}/calls?date=YYYY-MM-DD[&version_id=...]` | Daily arrivals/departures |
| GET | `/healthz` | Liveness + current version id |

### Upload validation (all-or-nothing)

`POST /feeds` rejects the package with HTTP 422 and a list of `errors`
(no version is created, the current version is untouched) when any of these
hold:

* a required file/column is missing, the ZIP is unreadable, or a file appears
  twice;
* a `stop_times.stop_id` / `trips.service_id` / `stop_times.trip_id` reference
  does not exist in the archive;
* a trip has no stop times, duplicate `stop_sequence`, non-increasing times,
  or a stop whose departure precedes its arrival;
* a time is not `HH:MM:SS` or exceeds `47:59:59` (blank intermediate times are
  interpolated; first/last stops must be timed);
* a calendar date is malformed or `end_date < start_date`, or an exception
  uses `exception_type` other than 1/2.

Every problem found is reported, up to 200 entries.

### Query semantics

* Times are interpreted on the **operating day** recorded in `service_date`.
  A trip on 2026-11-16 with `24:10:00` runs at 2026-11-17 00:10 local.
* A call is attributed to local date **D** when its *arrival* timestamp falls
  on D. `arrival_time`/`departure_time` keep the GTFS representation (`24:10:00`);
  `arrival_at`/`departure_at` are real local timestamps and `service_date` is
  the operating day.
* Each query merges operating days **D** (same-day calls) and **D-1**
  (after-midnight calls, times >= 24:00:00 up to 47:59:59). Each operating day
  independently applies `calendar.txt` weekday flags + date range and then
  `calendar_dates.txt` exceptions: type 1 adds a service, type 2 removes it.
* The response echoes, per operating day, which service ids were active,
  added and removed, so the merge is auditable.

### Version consistency

* A publication writes all feed rows and flips the `current_version_id`
  pointer inside one SQLite transaction, and only after validation passes.
* A query reads inside one read transaction, resolving the pointer and the
  feed data from the same snapshot; concurrent uploads can never yield a
  mixed-version answer. Every call carries the `version_id` it came from.
* Any version id can be queried again later, including after a restart.

## Demo package

`demo/gtfs_demo.zip` (regenerate with `python3 demo/generate_demo.py`) covers a
weekday service, a weekend service, a `calendar_dates`-only special service, a
removal exception, and after-midnight trips at 24:10, 25:00 and 47:59:59.

## Tests

```bash
python3 -m pytest -q
```

56 tests cover validation failures, reference/ordering/time checks,
service-day attribution across midnight (including the 47:59:59 boundary),
exception add/remove on either operating day, historical-version reads,
restart persistence, transactional rollback on a mid-publication failure, and
concurrent upload/query snapshot isolation.

## Known limitations

* Route metadata (`routes.txt`, `agency.txt`) is not ingested; only the five
  files in the task are read. A `route_id` absent from other files is kept as
  an opaque string.
* Times are treated as local wall-clock; no agency timezone conversion is
  applied (GTFS agency time differs from "local date" only across timezones).
* Old versions are retained forever; there is no pruning/retention endpoint.
* The compose file runs a single uvicorn worker; SQLite serialises writes, so
  scale-out would require moving the store to a server database.
