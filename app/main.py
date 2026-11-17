"""FastAPI application exposing GTFS timetable upload and daily stop queries."""
from __future__ import annotations

import hashlib
import os
import tempfile
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .gtfs import FeedError, load_feed
from .service import query_stop_day
from .store import Store, VersionNotFound

DB_PATH = os.environ.get("GTFS_DB_PATH", str(Path(__file__).resolve().parent.parent / "data" / "gtfs.db"))
MAX_UPLOAD_BYTES = int(os.environ.get("GTFS_MAX_UPLOAD_BYTES", str(256 * 1024 * 1024)))


class AppState:
    def __init__(self, db_path: str):
        self.store = Store(db_path)

    @property
    def db_path(self) -> str:
        return self.store.db_path


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.gtfs = AppState(DB_PATH)
    yield


app = FastAPI(
    title="GTFS Stop Timetable API",
    version="1.0.0",
    description=(
        "Upload GTFS timetable packages (stops, calendar, calendar_dates, trips, "
        "stop_times) and query daily scheduled arrivals/departures at a stop.\n\n"
        "**Service-day model**\n"
        "- Times may run up to `47:59:59`; a trip from operating day D with "
        "arrival >= 24:00 is attributed to the next local date.\n"
        "- A local-date query merges operating days D and D-1, applying "
        "calendar.txt weekdays and calendar_dates.txt exceptions separately.\n"
        "- Every response is read from exactly one immutable feed version; "
        "include `version_id` to re-read a historical version.\n"
        "- A new package is published atomically and only if the whole feed "
        "validates; failed uploads leave the current version untouched."
    ),
    lifespan=lifespan,
)


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------
class VersionOut(BaseModel):
    version_id: str
    uploaded_at: str
    source_name: str | None = None
    stop_count: int
    trip_count: int
    stop_time_count: int
    sha256: str
    calendar_start: str | None = None
    calendar_end: str | None = None
    is_current: bool


class StopOut(BaseModel):
    stop_id: str
    stop_name: str
    stop_lat: float | None = None
    stop_lon: float | None = None


class CallOut(BaseModel):
    trip_id: str
    route_id: str
    service_id: str
    trip_headsign: str | None = None
    stop_id: str
    stop_name: str
    stop_sequence: int
    stop_headsign: str | None = None
    arrival_time: str = Field(description="GTFS-style time, hours may exceed 24")
    departure_time: str
    arrival_at: str = Field(description="Actual local timestamp of arrival")
    departure_at: str
    service_date: str = Field(description="Operating day that produced this call")
    local_date: str
    is_after_midnight: bool
    timing_interpolated: bool = Field(
        description="True when the operator left this stop's times blank and they were interpolated"
    )
    version_id: str


class OperatingDayOut(BaseModel):
    service_date: str
    active_service_ids: list[str]
    added_by_calendar_dates: list[str]
    removed_by_calendar_dates: list[str]


class StopDayOut(BaseModel):
    stop_id: str
    stop_name: str
    local_date: str
    version_id: str
    operating_days_evaluated: list[OperatingDayOut]
    count: int
    calls: list[CallOut]


class ValidationFailure(BaseModel):
    detail: str
    errors: list[str]


def _state() -> AppState:
    st = getattr(app.state, "gtfs", None)
    if st is None:  # pragma: no cover - lifespan normally initialises this
        app.state.gtfs = st = AppState(DB_PATH)
    return st


def _no_feed() -> HTTPException:
    return HTTPException(status_code=404, detail="no feed has been published yet")


def _version_out(info: dict) -> VersionOut:
    return VersionOut(**{k: info[k] for k in VersionOut.model_fields if k in info})


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------
@app.get("/healthz", tags=["meta"])
def healthz() -> dict:
    store = _state().store
    return {"status": "ok", "current_version_id": store.current_version_id()}


@app.post(
    "/feeds",
    response_model=VersionOut,
    status_code=201,
    tags=["feeds"],
    summary="Upload and publish a GTFS ZIP package",
    responses={422: {"model": ValidationFailure, "description": "Package rejected; current version unchanged"}},
)
def upload_feed(file: UploadFile = File(..., description="ZIP containing the five required .txt files")):
    store = _state().store

    tmp_dir = Path(tempfile.mkdtemp(prefix="gtfs-upload-"))
    tmp_path = tmp_dir / "feed.zip"
    digest = hashlib.sha256()
    size = 0
    try:
        with open(tmp_path, "wb") as out:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail=f"upload exceeds {MAX_UPLOAD_BYTES} bytes")
                digest.update(chunk)
                out.write(chunk)
        if size == 0:
            raise HTTPException(status_code=422, detail="uploaded file is empty")

        try:
            feed = load_feed(tmp_path)
        except FeedError as exc:
            return JSONResponse(
                status_code=422,
                content={"detail": "GTFS package rejected; current version was not changed",
                         "errors": exc.errors},
            )
        version_id = store.publish(feed, source_name=file.filename, sha256=digest.hexdigest())
        info = store.get_version(version_id)
        assert info is not None
        return _version_out(info)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
            tmp_dir.rmdir()
        except OSError:
            pass


@app.get("/feeds", response_model=list[VersionOut], tags=["feeds"],
         summary="List all published versions (newest metadata incl. is_current)")
def list_feeds():
    return [_version_out(v) for v in _state().store.list_versions()]


@app.get("/feeds/current", response_model=VersionOut, tags=["feeds"],
         summary="Metadata of the currently published version")
def current_feed():
    info = _state().store.get_version()
    if info is None:
        raise HTTPException(status_code=404, detail="no feed has been published yet")
    return _version_out(info)


@app.get("/feeds/{version_id}", response_model=VersionOut, tags=["feeds"],
         summary="Metadata of a specific (including historical) version")
def feed_info(version_id: str):
    try:
        info = _state().store.get_version(version_id)
    except VersionNotFound:
        raise HTTPException(status_code=404, detail=f"unknown version_id {version_id!r}")
    return _version_out(info)


@app.get("/stops", response_model=list[StopOut], tags=["stops"],
         summary="List stops of the current or a specified version")
def list_stops(version_id: str | None = Query(None, description="Pin to a specific feed version")):
    store = _state().store
    try:
        with store.open_current(version_id) as reader:
            return [StopOut(**vars(s)) for s in reader.stops().values()]
    except VersionNotFound:
        if version_id is None:
            raise _no_feed()
        raise HTTPException(status_code=404, detail=f"unknown version_id {version_id!r}")


@app.get("/stops/{stop_id}/calls", response_model=StopDayOut, tags=["stops"],
         summary="Scheduled arrivals/departures at a stop on a local date")
def stop_calls(
    stop_id: str,
    date_: date = Query(..., alias="date", description="Local service date, YYYY-MM-DD"),
    version_id: str | None = Query(None, description="Pin to a specific feed version"),
):
    store = _state().store
    try:
        return query_stop_day(store, stop_id, date_, version_id)
    except VersionNotFound:
        if version_id is None:
            raise _no_feed()
        raise HTTPException(status_code=404, detail=f"unknown version_id {version_id!r}")
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown stop_id {stop_id!r}")


@app.get("/feeds/{version_id}/stops", response_model=list[StopOut], tags=["feeds"],
         summary="List stops of a historical version (explicit path form)")
def version_stops(version_id: str):
    return list_stops(version_id=version_id)
