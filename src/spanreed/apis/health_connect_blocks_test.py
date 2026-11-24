import asyncio
import datetime
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from spanreed.apis.health_connect import (
    HealthConnectStore,
    HeartRateSample,
    StepsRecord,
)
from spanreed.apis.health_connect_blocks import (
    DayChanged,
    DayMetrics,
    DayRange,
    RawRecords,
    RawRecordsChanged,
    health_connect_blocks,
    raw_records_key,
)
from spanreed.test_utils import FakeRedis, mock_user_find_by_id
from spanreed.workflows.blocks import (
    BlockRegistry,
    NoParams,
    NoQuestion,
    TriggerContext,
)
from spanreed.workflows.store import StoredTriggerState, WorkflowQueue
from spanreed.workflows.testing import fake_plugin

UTC = datetime.timezone.utc
DATE = datetime.date(2026, 5, 9)
START = datetime.datetime(2026, 5, 9, 8, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def redis() -> Iterator[FakeRedis]:
    """Give the Health Connect store an empty Redis."""
    fake = FakeRedis()
    with patch("spanreed.apis.health_connect.redis_api", new=fake):
        yield fake


@pytest.fixture(autouse=True)
def stored_birth_date() -> Iterator[AsyncMock]:
    """Stand in for the user's stored date of birth, which is not set by default.

    A test sets ``return_value`` to give the user a date of birth.
    """
    with patch(
        "spanreed.apis.health_connect_blocks.User.get_user_birth_date",
        new=AsyncMock(return_value=None),
    ) as mock:
        yield mock


def _context() -> TriggerContext:
    # The mock user's timezone is UTC, so the store buckets by UTC date.
    return TriggerContext(
        user=mock_user_find_by_id(4),
        workflow_id="wf_1",
        state=StoredTriggerState("wf_1"),
        queue=WorkflowQueue("wf_1"),
    )


def _store() -> HealthConnectStore:
    return HealthConnectStore(mock_user_find_by_id(4))


def _seed_steps(*counts: int, date: datetime.date = DATE) -> None:
    """Store one step interval per count on a date, each an hour apart."""
    start = datetime.datetime.combine(date, datetime.time(8), tzinfo=UTC)
    records = [
        StepsRecord(
            start + datetime.timedelta(hours=i),
            start + datetime.timedelta(hours=i, minutes=10),
            count,
        )
        for i, count in enumerate(counts)
    ]
    asyncio.run(_store().add_steps(records))


def _seed_heart_rate(bpm: list[int], resting: list[int]) -> None:
    """Store one sample a minute on DATE, and one resting record an hour."""
    store = _store()
    asyncio.run(
        store.add_heart_rate(
            [
                HeartRateSample(START + datetime.timedelta(minutes=i), b)
                for i, b in enumerate(bpm)
            ]
        )
    )
    if resting:
        asyncio.run(
            store.add_resting_heart_rate(
                [
                    HeartRateSample(START + datetime.timedelta(hours=i), r)
                    for i, r in enumerate(resting)
                ]
            )
        )


def _seed(type_name: str, *records: Any) -> None:
    asyncio.run(_store().add_records(type_name, list(records)))


def _day(key: str = "2026-05-09") -> DayMetrics | None:
    return asyncio.run(DayChanged().event_for(_context(), NoParams(), key))


def _raw(record_type: str, date: datetime.date = DATE) -> RawRecords | None:
    key = raw_records_key(record_type, date)
    return asyncio.run(RawRecordsChanged().event_for(_context(), NoParams(), key))


def test_the_health_connect_blocks_are_well_formed() -> None:
    registry = BlockRegistry()
    for block in health_connect_blocks():
        registry.register(block, fake_plugin())
    assert [block.id for block in registry.all()] == [
        "health-connect.day-changed",
        "health-connect.raw-records-changed",
    ]


# -- A day's data changed ----------------------------------------------------


def test_a_day_gives_the_total_of_its_steps() -> None:
    _seed_steps(100, 250)

    assert _day() == DayMetrics(date="2026-05-09", steps=350)


def test_a_day_gives_its_heart_rate_metrics() -> None:
    _seed_heart_rate(list(range(60, 161)), resting=[58, 55])

    # Resting is the day's last record; 60..160 has p5=65, avg=110, p95=155,
    # p98=158, p99=159.
    assert _day() == DayMetrics(
        date="2026-05-09",
        resting_heart_rate=55,
        heart_rate_p5=65,
        heart_rate_avg=110,
        heart_rate_p95=155,
        heart_rate_p98=158,
        heart_rate_p99=159,
    )


def test_zone_minutes_are_there_when_the_date_of_birth_is_set(
    stored_birth_date: AsyncMock,
) -> None:
    # 40 years old on DATE: max 180, moderate from 115.2, vigorous from 138.6.
    stored_birth_date.return_value = datetime.date(1986, 5, 9)
    # One sample a minute: 3 minutes below, 2 moderate, 1 vigorous.
    _seed_heart_rate([70, 80, 115, 116, 138, 139], resting=[])

    day = _day()

    assert day is not None
    assert (day.hr_moderate_minutes, day.hr_vigorous_minutes) == (2, 1)


def test_zone_minutes_are_left_out_without_a_date_of_birth() -> None:
    _seed_heart_rate([70, 150], resting=[])

    day = _day()

    assert day is not None
    assert (day.hr_moderate_minutes, day.hr_vigorous_minutes) == (None, None)
    assert day.heart_rate_avg == 110


def test_vo2_max_is_the_last_measurement_of_the_day() -> None:
    _seed(
        "vo2_max",
        {"ml_per_kg_per_min": 44.84, "time": "2026-05-09T07:00:00Z"},
        {"ml_per_kg_per_min": 45.26, "time": "2026-05-09T18:00:00Z"},
    )

    assert _day() == DayMetrics(date="2026-05-09", vo2_max=45.3)


def test_a_day_with_no_metrics_stored_gives_no_event() -> None:
    # Sleep is stored, but no metric is computed from it.
    _seed("sleep", {"session_end_time": "2026-05-09T06:00:00Z"})

    assert _day() is None
    assert _day("2026-05-08") is None
    assert _day("not a date") is None


def _manual_dates(days: str, today: datetime.date) -> list[str]:
    now = datetime.datetime.combine(today, datetime.time(12), tzinfo=UTC)
    with patch("spanreed.apis.health_connect_blocks.datetime") as mock_datetime:
        mock_datetime.datetime.now.return_value = now
        mock_datetime.date = datetime.date
        mock_datetime.timedelta = datetime.timedelta
        events = asyncio.run(
            DayChanged().manual_events(_context(), NoParams(), DayRange(days))  # type: ignore[arg-type]
        )
    return [event.date for event in events]


def test_by_hand_a_range_gives_its_days_that_have_data() -> None:
    for day in (1, 5, 9):
        _seed_steps(100, date=datetime.date(2026, 5, day))

    assert _manual_dates("Today", DATE) == ["2026-05-09"]
    assert _manual_dates("Today", datetime.date(2026, 5, 8)) == []
    # The seven days up to today: the 3rd to the 9th.
    assert _manual_dates("The last week", DATE) == ["2026-05-05", "2026-05-09"]


def test_by_hand_every_stored_day_gives_each_day_that_has_metrics() -> None:
    for day in (1, 5, 9):
        _seed_steps(100, date=datetime.date(2026, 5, day))
    _seed("sleep", {"session_end_time": "2026-05-07T06:00:00Z"})

    assert _manual_dates("Every stored day", DATE) == [
        "2026-05-01",
        "2026-05-05",
        "2026-05-09",
    ]


# -- Raw records changed -----------------------------------------------------


def test_raw_records_are_one_json_record_on_each_line_oldest_first() -> None:
    _seed(
        "heart_rate",
        {"bpm": 80, "time": "2026-05-09T09:00:00Z"},
        {"bpm": 72, "time": "2026-05-09T08:15:00Z", "source": "watch"},
    )

    assert _raw("heart-rate") == RawRecords(
        # The name of the type as the phone sends it, for the path.
        type="heart_rate",
        date="2026-05-09",
        records=(
            '{"bpm":72,"time":"2026-05-09T08:15:00Z","source":"watch"}\n'
            '{"bpm":80,"time":"2026-05-09T09:00:00Z"}\n'
        ),
        count=2,
    )


def test_a_type_that_no_metric_uses_is_given_too() -> None:
    record = {"session_end_time": "2026-05-09T06:00:00Z", "stages": [1, 2]}
    _seed("sleep", record)

    event = _raw("sleep")

    assert event is not None
    assert event.type == "sleep"
    assert [json.loads(line) for line in event.records.splitlines()] == [record]


def test_a_day_that_the_phone_emptied_gives_an_event_with_no_records() -> None:
    asyncio.run(_store().mark_export_emptied([("weight", DATE)]))

    assert _raw("weight") == RawRecords(
        type="weight", date="2026-05-09", records="", count=0
    )


def test_a_day_with_no_records_for_another_reason_gives_no_event() -> None:
    # Its records expired, or the user deleted them here: the file stays.
    assert _raw("weight") is None
    assert (
        asyncio.run(RawRecordsChanged().event_for(_context(), NoParams(), "nonsense"))
        is None
    )


def test_a_day_that_has_records_again_is_no_longer_an_emptied_day() -> None:
    asyncio.run(_store().mark_export_emptied([("weight", DATE)]))
    _seed("weight", {"kilograms": 70.5, "time": "2026-05-09T07:00:00Z"})

    event = _raw("weight")

    assert event is not None and event.count == 1
    assert not asyncio.run(_store().is_export_emptied("weight", DATE))


def test_by_hand_the_raw_records_of_every_stored_day_are_given() -> None:
    _seed("heart_rate", {"bpm": 72, "time": "2026-05-09T08:15:00Z"})
    _seed("heart_rate", {"bpm": 75, "time": "2026-05-10T08:15:00Z"})
    _seed("sleep", {"session_end_time": "2026-05-09T06:00:00Z"})

    events = asyncio.run(
        RawRecordsChanged().manual_events(_context(), NoParams(), NoQuestion())
    )

    assert [(event.type, event.date, event.count) for event in events] == [
        ("heart_rate", "2026-05-09", 1),
        ("heart_rate", "2026-05-10", 1),
        ("sleep", "2026-05-09", 1),
    ]
