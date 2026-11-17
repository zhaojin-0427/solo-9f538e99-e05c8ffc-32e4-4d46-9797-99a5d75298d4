"""Feed validation tests."""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.gtfs import FeedError, load_feed, parse_time
from tests.fixtures import build_zip, default_files


def _write(tmp_path: Path, files: dict[str, str]) -> Path:
    p = tmp_path / "feed.zip"
    p.write_bytes(build_zip(files))
    return p


def test_valid_feed_loads(tmp_path):
    feed = load_feed(_write(tmp_path, default_files()))
    assert set(feed.stops) == {"S1", "S2", "S3"}
    assert set(feed.trips) == {"T_WEEKDAY_AM", "T_WEEKDAY_NIGHT", "T_WEEKDAY_LATE",
                               "T_WEEKEND_AM", "T_SPECIAL_AM"}
    assert [st.stop_id for st in feed.stop_times["T_WEEKDAY_AM"]] == ["S1", "S2", "S3"]
    assert feed.stop_times["T_WEEKDAY_NIGHT"][1].arrival == 24 * 3600 + 600


@pytest.mark.parametrize("seconds,text", [
    (0, "00:00:00"),
    (24 * 3600, "24:00:00"),
    (47 * 3600 + 59 * 60 + 59, "47:59:59"),
])
def test_parse_time_upper_bound(seconds, text):
    assert parse_time(text) == seconds


@pytest.mark.parametrize("bad", ["48:00:00", "24:60:00", "8:00", "-1:00:00", "abc", "23:59:60"])
def test_parse_time_rejects(bad):
    assert parse_time(bad) is None


def test_missing_file_rejected(tmp_path):
    files = default_files()
    del files["stop_times.txt"]
    with pytest.raises(FeedError) as exc:
        load_feed(_write(tmp_path, files))
    assert "stop_times.txt" in str(exc.value)


def test_unknown_stop_reference_rejected(tmp_path):
    files = default_files()
    files["stop_times.txt"] = files["stop_times.txt"].replace("S3,3", "NOPE,3")
    with pytest.raises(FeedError) as exc:
        load_feed(_write(tmp_path, files))
    assert "unknown stop_id" in str(exc.value)


def test_unknown_trip_reference_rejected(tmp_path):
    files = default_files()
    files["stop_times.txt"] += "GHOST,09:00:00,09:00:00,S1,1\n"
    with pytest.raises(FeedError) as exc:
        load_feed(_write(tmp_path, files))
    assert "unknown trip_id" in str(exc.value)


def test_unknown_service_reference_rejected(tmp_path):
    files = default_files()
    files["trips.txt"] = files["trips.txt"].replace("R2,WEEKEND", "R2,NOSUCH")
    with pytest.raises(FeedError) as exc:
        load_feed(_write(tmp_path, files))
    assert "unknown service_id" in str(exc.value)


def test_time_beyond_47_59_59_rejected(tmp_path):
    files = default_files()
    files["stop_times.txt"] = files["stop_times.txt"].replace(
        "T_WEEKEND_AM,10:30:00,10:30:00", "T_WEEKEND_AM,48:00:00,48:00:00")
    with pytest.raises(FeedError) as exc:
        load_feed(_write(tmp_path, files))
    assert "arrival_time" in str(exc.value)


def test_non_increasing_stop_times_rejected(tmp_path):
    files = default_files()
    files["stop_times.txt"] = files["stop_times.txt"].replace(
        "T_WEEKDAY_AM,08:30:00,08:30:00", "T_WEEKDAY_AM,08:05:00,08:05:00")
    with pytest.raises(FeedError) as exc:
        load_feed(_write(tmp_path, files))
    assert "non-increasing" in str(exc.value)


def test_duplicate_stop_sequence_rejected(tmp_path):
    files = default_files()
    files["stop_times.txt"] = files["stop_times.txt"].replace("S3,3", "S3,2", 1)
    with pytest.raises(FeedError) as exc:
        load_feed(_write(tmp_path, files))
    assert "duplicate stop_sequence" in str(exc.value)


def test_trip_without_stop_times_rejected(tmp_path):
    files = default_files()
    files["trips.txt"] += "R9,WEEKDAY,T_ORPHAN,Nowhere,0\n"
    with pytest.raises(FeedError) as exc:
        load_feed(_write(tmp_path, files))
    assert "no stop_times" in str(exc.value)


def test_departure_before_arrival_rejected(tmp_path):
    files = default_files()
    files["stop_times.txt"] = files["stop_times.txt"].replace(
        "08:10:00,08:11:00", "08:10:00,08:09:00")
    with pytest.raises(FeedError) as exc:
        load_feed(_write(tmp_path, files))
    assert "departure" in str(exc.value)


def test_not_a_zip_rejected(tmp_path):
    p = tmp_path / "x.zip"
    p.write_bytes(b"not a zip")
    with pytest.raises(FeedError):
        load_feed(p)


def test_folder_prefixed_zip_accepted(tmp_path):
    p = tmp_path / "feed.zip"
    with zipfile.ZipFile(p, "w") as zf:
        for name, text in default_files().items():
            zf.writestr(f"myfeed/{name}", text)
    feed = load_feed(p)
    assert len(feed.stops) == 3


def test_calendar_dates_only_service_accepted(tmp_path):
    files = default_files()
    files["calendar.txt"] = files["calendar.txt"].replace(
        "SPECIAL,0,0,0,0,0,0,0,20261101,20261231\n", "")
    feed = load_feed(_write(tmp_path, files))
    assert "SPECIAL" not in feed.calendars
    assert any(e.service_id == "SPECIAL" for e in feed.exceptions)


def test_extension_validation_error_reports_all_errors(tmp_path):
    files = default_files()
    files["stop_times.txt"] = files["stop_times.txt"].replace("S3,3", "NOPE,3")
    p = _write(tmp_path, files)
    with pytest.raises(FeedError) as exc:
        load_feed(p)
    assert len(exc.value.errors) >= 2  # one per affected trip row


def test_zip_with_macosx_noise_accepted(tmp_path):
    p = tmp_path / "feed.zip"
    with zipfile.ZipFile(p, "w") as zf:
        for name, text in default_files().items():
            zf.writestr(f"feed/{name}", text)
        zf.writestr("__MACOSX/feed/._stops.txt", b"\x00\x01junk")
    feed = load_feed(p)
    assert len(feed.stops) == 3


def test_zip_with_duplicate_txt_rejected(tmp_path):
    p = tmp_path / "feed.zip"
    with zipfile.ZipFile(p, "w") as zf:
        for name, text in default_files().items():
            zf.writestr(name, text)
        zf.writestr("copy/stops.txt", default_files()["stops.txt"])
    with pytest.raises(FeedError) as exc:
        load_feed(p)
    assert "duplicate file entry" in str(exc.value)
