"""Service-day query semantics (unit level, against a temp SQLite store)."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.gtfs import load_feed
from app.service import query_stop_day
from app.store import Store
from tests import extra_fixtures
from tests.fixtures import build_zip, default_files


def _store(tmp_path: Path) -> Store:
    files = default_files()
    files["trips.txt"] = extra_fixtures.TRIPS
    files["stop_times.txt"] = extra_fixtures.STOP_TIMES
    p = tmp_path / "feed.zip"
    p.write_bytes(build_zip(files))
    store = Store(tmp_path / "gtfs.db")
    feed = load_feed(p)
    store.publish(feed, source_name="f.zip", sha256="0" * 64)
    return store


def test_same_day_call(tmp_path):
    store = _store(tmp_path)
    res = query_stop_day(store, "S2", date(2026, 11, 16))
    assert res["count"] == 1
    call = res["calls"][0]
    assert call["trip_id"] == "T_WEEKDAY_AM"
    assert call["arrival_time"] == "08:10:00"
    assert call["arrival_at"] == "2026-11-16T08:10:00"
    assert call["service_date"] == "2026-11-16"
    assert call["is_after_midnight"] is False


def test_after_midnight_attributed_to_next_local_date(tmp_path):
    store = _store(tmp_path)
    res = query_stop_day(store, "S2", date(2026, 11, 17))
    ids = [(c["trip_id"], c["arrival_at"], c["service_date"]) for c in res["calls"]]
    assert ids == [
        ("T_WEEKDAY_NIGHT", "2026-11-17T00:10:00", "2026-11-16"),
        ("T_WEEKDAY_LATE", "2026-11-17T01:00:00", "2026-11-16"),
        ("T_SPECIAL_AM", "2026-11-17T12:20:00", "2026-11-17"),
    ]
    assert res["calls"][0]["arrival_time"] == "24:10:00"
    assert res["calls"][0]["is_after_midnight"] is True


def test_next_day_query_does_not_repeat_night_call(tmp_path):
    store = _store(tmp_path)
    res = query_stop_day(store, "S2", date(2026, 11, 16))
    assert [c["trip_id"] for c in res["calls"]] == ["T_WEEKDAY_AM"]
    assert "T_WEEKDAY_NIGHT" not in [c["trip_id"] for c in res["calls"]]


def test_max_time_boundary_lands_on_next_local_date(tmp_path):
    store = _store(tmp_path)
    res = query_stop_day(store, "S3", date(2026, 11, 17))
    calls = {c["trip_id"]: c for c in res["calls"]}
    assert set(calls) == {"T_WEEKDAY_NIGHT", "T_WEEKDAY_LATE", "T_WEEKDAY_MAX"}
    assert calls["T_WEEKDAY_MAX"]["arrival_at"] == "2026-11-17T23:59:59"
    assert calls["T_WEEKDAY_MAX"]["arrival_time"] == "47:59:59"
    assert calls["T_WEEKDAY_MAX"]["service_date"] == "2026-11-16"


def test_exception_removal_removes_whole_service(tmp_path):
    store = _store(tmp_path)
    res = query_stop_day(store, "S2", date(2026, 11, 17))
    applied = {d["service_date"]: d for d in res["operating_days_evaluated"]}
    assert applied["2026-11-17"]["removed_by_calendar_dates"] == ["WEEKDAY"]
    assert "WEEKDAY" not in applied["2026-11-17"]["active_service_ids"]
    assert "SPECIAL" in applied["2026-11-17"]["active_service_ids"]


def test_exception_addition_of_zero_weekday_service(tmp_path):
    store = _store(tmp_path)
    res = query_stop_day(store, "S1", date(2026, 11, 17))
    assert any(c["trip_id"] == "T_SPECIAL_AM" for c in res["calls"])


def test_weekend_trip_on_sunday(tmp_path):
    store = _store(tmp_path)
    res = query_stop_day(store, "S2", date(2026, 11, 29))
    assert [c["trip_id"] for c in res["calls"]] == ["T_WEEKEND_AM"]


def test_weekend_removed_via_calendar_dates(tmp_path):
    store = _store(tmp_path)
    res = query_stop_day(store, "S2", date(2026, 11, 22))
    assert res["count"] == 0
    applied = {d["service_date"]: d for d in res["operating_days_evaluated"]}
    assert applied["2026-11-22"]["removed_by_calendar_dates"] == ["WEEKEND"]


def test_previous_operating_day_uses_its_own_exceptions(tmp_path):
    """11-17 WEEKDAY is removed; its after-midnight calls must not leak to 11-18."""
    store = _store(tmp_path)
    res = query_stop_day(store, "S2", date(2026, 11, 18))
    assert [c["trip_id"] for c in res["calls"]] == ["T_WEEKDAY_AM"]


def test_outside_calendar_range_has_no_service(tmp_path):
    store = _store(tmp_path)
    res = query_stop_day(store, "S2", date(2027, 1, 5))
    assert res["count"] == 0


def test_unknown_stop_raises(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(KeyError):
        query_stop_day(store, "NOPE", date(2026, 11, 16))


def test_query_pinned_version_after_newer_publish(tmp_path):
    store = _store(tmp_path)
    first = store.current_version_id()
    second = store.publish(load_feed(_zip_only_s1(tmp_path)), source_name="v2.zip", sha256="1" * 64)
    assert second != first
    # Old version is still readable by id and shows the old content.
    res = query_stop_day(store, "S1", date(2026, 11, 16), version_id=first)
    assert res["version_id"] == first
    assert res["calls"][0]["arrival_time"] == "08:00:00"
    # New version has a different time.
    res2 = query_stop_day(store, "S1", date(2026, 11, 16))
    assert res2["version_id"] == second
    assert res2["calls"][0]["arrival_time"] == "07:55:00"


def _zip_only_s1(tmp_path: Path) -> Path:
    files = default_files()
    files["stop_times.txt"] = files["stop_times.txt"].replace(
        "T_WEEKDAY_AM,08:00:00,08:00:00,S1,1", "T_WEEKDAY_AM,07:55:00,07:55:00,S1,1")
    p = tmp_path / "v2.zip"
    p.write_bytes(build_zip(files))
    return p


def test_departure_crossing_midnight_keeps_arrival_date(tmp_path):
    """Arrival on date D, departure after midnight: row belongs to D by arrival."""
    files = default_files()
    files["stop_times.txt"] = files["stop_times.txt"].replace(
        "T_WEEKEND_AM,10:00:00,10:00:00,S2,1",
        "T_WEEKEND_AM,23:55:00,24:20:00,S2,1").replace(
        "T_WEEKEND_AM,10:30:00,10:30:00,S3,2",
        "T_WEEKEND_AM,24:50:00,24:50:00,S3,2")
    p = tmp_path / "cross.zip"
    p.write_bytes(build_zip(files))
    store = Store(tmp_path / "cross.db")
    store.publish(load_feed(p), "cross.zip", "2" * 64)

    saturday = date(2026, 11, 28)  # WEEKEND service
    res = query_stop_day(store, "S2", saturday)
    call = next(c for c in res["calls"] if c["trip_id"] == "T_WEEKEND_AM")
    assert call["arrival_at"] == "2026-11-28T23:55:00"
    assert call["departure_at"] == "2026-11-29T00:20:00"
    assert call["service_date"] == "2026-11-28"
    # The follow-up stop is attributed to the next local date (Sunday 00:50),
    # via the same Saturday operating day.
    sunday_stop3 = query_stop_day(store, "S3", date(2026, 11, 29))
    call3 = next(c for c in sunday_stop3["calls"] if c["trip_id"] == "T_WEEKEND_AM")
    assert call3["arrival_at"] == "2026-11-29T00:50:00"
    assert call3["service_date"] == "2026-11-28"


def test_exactly_24h_arrival_is_next_local_date(tmp_path):
    files = default_files()
    files["stop_times.txt"] = files["stop_times.txt"].replace(
        "T_WEEKEND_AM,10:00:00,10:00:00,S2,1",
        "T_WEEKEND_AM,24:00:00,24:00:00,S2,1").replace(
        "T_WEEKEND_AM,10:30:00,10:30:00,S3,2",
        "T_WEEKEND_AM,24:30:00,24:30:00,S3,2")
    p = tmp_path / "mid.zip"
    p.write_bytes(build_zip(files))
    store = Store(tmp_path / "mid.db")
    store.publish(load_feed(p), "mid.zip", "3" * 64)

    saturday_date = date(2026, 11, 28)  # WEEKEND operating day
    saturday = query_stop_day(store, "S2", saturday_date)
    # Only Friday's WEEKDAY after-midnight calls reach Saturday; the WEEKEND
    # trip's own 24:00 arrival does not.
    assert all(c["trip_id"] != "T_WEEKEND_AM" for c in saturday["calls"])
    # 24:00:00 on the Saturday service lands at Sunday 00:00 exactly.
    sunday = query_stop_day(store, "S2", date(2026, 11, 29))
    assert [c["arrival_at"] for c in sunday["calls"]] == ["2026-11-29T00:00:00"]
    assert sunday["calls"][0]["service_date"] == "2026-11-28"
