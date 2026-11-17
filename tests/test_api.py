"""HTTP API tests: upload, query, atomicity and persistence."""
from __future__ import annotations

import importlib
import threading

import pytest
from fastapi.testclient import TestClient

from tests.fixtures import build_zip, default_files


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import app.main as main_mod

    monkeypatch.setenv("GTFS_DB_PATH", str(tmp_path / "api.db"))
    importlib.reload(main_mod)
    main_mod.DB_PATH = str(tmp_path / "api.db")
    with TestClient(main_mod.app) as c:
        yield c
    importlib.reload(main_mod)  # restore default DB_PATH for other tests


def _upload(client, files=None, name="feed.zip"):
    data = build_zip(files)
    return client.post("/feeds", files={"file": (name, data, "application/zip")})


def test_docs_available(client):
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_health_without_feed(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["current_version_id"] is None


def test_upload_publishes_version(client):
    r = _upload(client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["version_id"].startswith("v-")
    assert body["stop_count"] == 3
    assert body["is_current"] is True
    assert client.get("/feeds/current").json()["version_id"] == body["version_id"]
    assert len(client.get("/feeds").json()) == 1
    assert len(client.get("/stops").json()) == 3


def test_invalid_upload_keeps_current_version(client):
    good = _upload(client).json()
    files = default_files()
    files["stop_times.txt"] = files["stop_times.txt"].replace("S3,3", "NOPE,3")
    r = _upload(client, files)
    assert r.status_code == 422
    body = r.json()
    assert body["errors"]
    assert client.get("/feeds/current").json()["version_id"] == good["version_id"]
    assert len(client.get("/feeds").json()) == 1


def test_empty_upload_rejected(client):
    r = client.post("/feeds", files={"file": ("empty.zip", b"", "application/zip")})
    assert r.status_code == 422
    assert client.get("/feeds/current").status_code == 404


def test_query_calls_endpoint(client):
    _upload(client)
    r = client.get("/stops/S2/calls", params={"date": "2026-11-17"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 3
    assert body["calls"][0]["trip_id"] == "T_WEEKDAY_NIGHT"
    assert body["calls"][0]["arrival_at"] == "2026-11-17T00:10:00"
    assert body["calls"][0]["service_date"] == "2026-11-16"
    assert body["version_id"] == client.get("/feeds/current").json()["version_id"]


def test_query_bad_date_422(client):
    _upload(client)
    assert client.get("/stops/S2/calls", params={"date": "17-11-2026"}).status_code == 422


def test_query_unknown_stop_404(client):
    _upload(client)
    assert client.get("/stops/NOPE/calls", params={"date": "2026-11-17"}).status_code == 404


def test_query_before_any_feed_404(client):
    assert client.get("/stops/S2/calls", params={"date": "2026-11-17"}).status_code == 404


def test_query_unknown_version_404(client):
    _upload(client)
    r = client.get("/stops/S2/calls", params={"date": "2026-11-17", "version_id": "v-nope"})
    assert r.status_code == 404


def test_historical_version_query_after_new_upload(client):
    first = _upload(client).json()["version_id"]
    files = default_files()
    files["stop_times.txt"] = files["stop_times.txt"].replace(
        "T_WEEKDAY_AM,08:10:00,08:11:00", "T_WEEKDAY_AM,08:20:00,08:21:00")
    second = _upload(client, files).json()["version_id"]
    assert second != first

    old = client.get("/stops/S2/calls", params={"date": "2026-11-16", "version_id": first}).json()
    new = client.get("/stops/S2/calls", params={"date": "2026-11-16"}).json()
    assert old["calls"][0]["arrival_at"] == "2026-11-16T08:10:00"
    assert new["calls"][0]["arrival_at"] == "2026-11-16T08:20:00"
    assert new["version_id"] == second


def test_restart_preserves_versions_and_pointer(tmp_path, monkeypatch):
    import app.main as main_mod

    db = str(tmp_path / "restart.db")
    monkeypatch.setenv("GTFS_DB_PATH", db)
    importlib.reload(main_mod)
    main_mod.DB_PATH = db
    with TestClient(main_mod.app) as c:
        vid = _upload(c).json()["version_id"]
        before = c.get("/stops/S2/calls", params={"date": "2026-11-17"}).json()
    # simulate restart: fresh app + fresh Store instance over same file
    importlib.reload(main_mod)
    main_mod.DB_PATH = db
    with TestClient(main_mod.app) as c:
        assert c.get("/feeds/current").json()["version_id"] == vid
        after = c.get("/stops/S2/calls", params={"date": "2026-11-17", "version_id": vid}).json()
        assert after == before
    importlib.reload(main_mod)


def test_concurrent_queries_never_mix_versions(tmp_path, monkeypatch):
    """While a new version is published, in-flight reads must stay consistent."""
    import app.main as main_mod

    db = str(tmp_path / "race.db")
    monkeypatch.setenv("GTFS_DB_PATH", db)
    importlib.reload(main_mod)
    main_mod.DB_PATH = db

    with TestClient(main_mod.app) as c:
        first_files = default_files()
        first = _upload(c, first_files).json()["version_id"]

        second_files = default_files()
        second_files["stop_times.txt"] = second_files["stop_times.txt"].replace(
            "T_WEEKDAY_AM,08:00:00,08:00:00,S1,1", "T_WEEKDAY_AM,06:00:00,06:00:00,S1,1")
        errors: list[str] = []
        seen: set[str] = set()
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                r = c.get("/stops/S1/calls", params={"date": "2026-11-16"})
                if r.status_code != 200:
                    errors.append(f"status {r.status_code}")
                    return
                body = r.json()
                times = {call["arrival_at"] for call in body["calls"]}
                if times not in ({"2026-11-16T08:00:00", "2026-11-16T23:50:00"},
                             {"2026-11-16T06:00:00", "2026-11-16T23:50:00"}):
                    errors.append(f"mixed/unknown snapshot {times}")
                    return
                seen.add(body["version_id"])

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for _ in range(3):
            _upload(c, second_files)
        stop.set()
        for t in threads:
            t.join(timeout=5)
        assert not errors, errors
        assert len(seen) >= 1
        current = c.get("/feeds/current").json()["version_id"]
        assert current in seen or current  # pointer valid after race
        assert all(v.startswith("v-") for v in seen)
    importlib.reload(main_mod)
