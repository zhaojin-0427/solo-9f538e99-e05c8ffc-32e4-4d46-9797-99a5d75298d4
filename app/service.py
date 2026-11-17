"""Daily stop arrival/departure queries with GTFS service-day semantics."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .gtfs import format_time
from .store import Store, VersionReader

SECONDS_PER_DAY = 24 * 3600


@dataclass(frozen=True)
class ServiceResolution:
    service_date: date
    active: set[str]
    added_by_exception: set[str]
    removed_by_exception: set[str]


def resolve_services(reader: VersionReader, services: dict, service_date: date) -> ServiceResolution:
    """services running on ``service_date`` after calendar + exceptions.

    Baseline: calendar.txt weekday flag AND start_date <= date <= end_date.
    calendar_dates.txt then applies exception_type 2 (removed) or 1 (added,
    even if the weekday flag is off or the date is outside the range).
    """
    idx = service_date.weekday()  # Monday == 0
    active = {
        sid for sid, e in services.items()
        if e.weekdays[idx] and e.start_date <= service_date <= e.end_date
    }
    exc = reader.exceptions([service_date.isoformat()])
    added: set[str] = set()
    removed: set[str] = set()
    for (sid, _d), et in exc.items():
        if et == 1:
            active.add(sid)
            added.add(sid)
        elif et == 2:
            active.discard(sid)
            removed.add(sid)
    return ServiceResolution(service_date, active, added, removed)


def query_stop_day(store: Store, stop_id: str, local_date: date,
                   version_id: str | None = None) -> dict:
    """Return arrivals/departures at ``stop_id`` attributed to ``local_date``.

    Attribution rule: a call belongs to the local date of its *arrival*.
    A call emitted by the D-1 operating day with arrival >= 24:00:00 lands on
    D.  Both D and D-1 operating days are evaluated with their own weekday and
    calendar_dates rules (and their own version snapshot).

    The whole query runs inside one read transaction pinned to one version.
    """
    with store.open_current(version_id) as reader:
        stop = reader.stop(stop_id)
        if stop is None:
            raise KeyError(stop_id)

        services = reader.services()
        # A call arriving on local date D belongs either to operating day D
        # (arrival < 24:00) or D-1 (arrival >= 24:00); per the task only those
        # two operating days are merged.
        operating_days = [local_date - timedelta(days=1), local_date]
        active_by_day = {d: resolve_services(reader, services, d) for d in operating_days}

        # Any call attributed to local_date has arrival <= 47:59:59, so one
        # indexed scan of this stop covers both operating days.
        rows = reader.calls_at_stop(stop_id, 2 * SECONDS_PER_DAY - 1)

        calls: list[dict] = []
        for r in rows:
            for service_date in operating_days:
                if r["service_id"] not in active_by_day[service_date].active:
                    continue
                arrival_dt = datetime.combine(service_date, time.min) + timedelta(seconds=r["arrival"])
                if arrival_dt.date() != local_date:
                    continue
                departure_dt = datetime.combine(service_date, time.min) + timedelta(seconds=r["departure"])
                calls.append({
                    "trip_id": r["trip_id"],
                    "route_id": r["route_id"],
                    "service_id": r["service_id"],
                    "trip_headsign": r["trip_headsign"],
                    "stop_id": stop_id,
                    "stop_name": stop.stop_name,
                    "stop_sequence": r["stop_sequence"],
                    "stop_headsign": r["stop_headsign"],
                    "arrival_time": format_time(r["arrival"]),
                    "departure_time": format_time(r["departure"]),
                    "arrival_at": arrival_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                    "departure_at": departure_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                    "service_date": service_date.isoformat(),
                    "local_date": local_date.isoformat(),
                    "is_after_midnight": r["arrival"] >= SECONDS_PER_DAY,
                    "timing_interpolated": bool(r["interpolated"]),
                    "version_id": reader.version_id,
                })

        calls.sort(key=lambda c: (c["arrival_at"], c["trip_id"], c["stop_sequence"]))
        return {
            "stop_id": stop_id,
            "stop_name": stop.stop_name,
            "local_date": local_date.isoformat(),
            "version_id": reader.version_id,
            "operating_days_evaluated": [
                {
                    "service_date": d.isoformat(),
                    "active_service_ids": sorted(active_by_day[d].active),
                    "added_by_calendar_dates": sorted(active_by_day[d].added_by_exception),
                    "removed_by_calendar_dates": sorted(active_by_day[d].removed_by_exception),
                }
                for d in operating_days
            ],
            "count": len(calls),
            "calls": calls,
        }
