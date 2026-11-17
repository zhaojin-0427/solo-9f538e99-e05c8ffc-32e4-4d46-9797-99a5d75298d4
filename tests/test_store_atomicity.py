"""Atomic publication / rollback guarantees at the store layer."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.gtfs import load_feed
from app.service import query_stop_day
from app.store import Store
from tests.fixtures import build_zip, default_files


def _feed(tmp_path: Path, files: dict[str, str] | None = None):
    p = tmp_path / "feed.zip"
    p.write_bytes(build_zip(files or default_files()))
    return load_feed(p)


def test_publication_failure_rolls_back_and_keeps_pointer(tmp_path):
    store = Store(tmp_path / "gtfs.db")
    first = store.publish(_feed(tmp_path), "v1.zip", "a" * 64)

    # Force a failure halfway through the inserts.
    conn = store._connect()
    conn.execute(
        "CREATE TRIGGER boom BEFORE INSERT ON stop_time BEGIN"
        " SELECT RAISE(ABORT, 'simulated failure'); END"
    )
    conn.close()

    with pytest.raises(Exception):
        store.publish(_feed(tmp_path), "v2.zip", "b" * 64)

    assert store.current_version_id() == first
    assert [v["version_id"] for v in store.list_versions()] == [first]
    assert query_stop_day(store, "S2", date(2026, 11, 16))["version_id"] == first


def test_no_pointer_before_first_publication(tmp_path):
    store = Store(tmp_path / "gtfs.db")
    assert store.current_version_id() is None
    assert store.get_version() is None
    assert store.list_versions() == []


def test_versions_accumulate_immutably(tmp_path):
    store = Store(tmp_path / "gtfs.db")
    ids = [store.publish(_feed(tmp_path), f"v{i}.zip", str(i) * 64) for i in range(3)]
    assert store.current_version_id() == ids[-1]
    assert [v["version_id"] for v in store.list_versions()] == ids
    assert [v["is_current"] for v in store.list_versions()] == [False, False, True]
    # each old version still answers with its own data and id
    for vid in ids:
        res = query_stop_day(store, "S2", date(2026, 11, 16), version_id=vid)
        assert res["version_id"] == vid
        assert res["calls"][0]["arrival_at"] == "2026-11-16T08:10:00"


def test_calendar_dates_only_service_is_queryable(tmp_path):
    files = default_files()
    files["calendar.txt"] = files["calendar.txt"].replace(
        "SPECIAL,0,0,0,0,0,0,0,20261101,20261231\n", "")
    store = Store(tmp_path / "gtfs.db")
    store.publish(_feed(tmp_path, files), "v1.zip", "c" * 64)
    on = query_stop_day(store, "S1", date(2026, 11, 17))
    assert [c["trip_id"] for c in on["calls"]] == ["T_SPECIAL_AM"]
    off = query_stop_day(store, "S1", date(2026, 11, 18))
    # 11-18 is a normal Wednesday: WEEKDAY runs again, SPECIAL (added only on
    # 11-15 and 11-17) must not.
    assert [c["trip_id"] for c in off["calls"]] == ["T_WEEKDAY_AM", "T_WEEKDAY_NIGHT"]
