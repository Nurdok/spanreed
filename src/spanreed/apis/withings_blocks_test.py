import asyncio
import datetime
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from spanreed.apis.withings import MeasurementType
from spanreed.apis.withings_blocks import (
    DayMeasurements,
    MeasurementRange,
    NewMeasurements,
    withings_blocks,
)
from spanreed.test_utils import mock_user_find_by_id
from spanreed.workflows.blocks import BlockRegistry, Repeat, TriggerContext
from spanreed.workflows.store import WorkflowQueue
from spanreed.workflows.testing import fake_plugin

TODAY = datetime.date(2026, 5, 9)
YESTERDAY = datetime.date(2026, 5, 8)


class _State:
    """A trigger's state, kept in a dict."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, name: str) -> str | None:
        return self.values.get(name)

    async def set(self, name: str, value: str) -> None:
        self.values[name] = value


def _context() -> TriggerContext:
    return TriggerContext(
        user=mock_user_find_by_id(4),
        workflow_id="wf_1",
        state=_State(),
        queue=WorkflowQueue("wf_1"),
    )


@pytest.fixture(autouse=True)
def withings() -> Iterator[AsyncMock]:
    """Stand in for the Withings API. A test sets what it returns."""
    api = AsyncMock()
    api.get_measurements.return_value = {}
    with (
        patch(
            "spanreed.apis.withings_blocks.WithingsApi.for_user",
            new=AsyncMock(return_value=api),
        ),
        patch("spanreed.apis.withings_blocks.today_of", new=lambda user: TODAY),
    ):
        yield api


def _poll() -> list[DayMeasurements]:
    poll = asyncio.run(NewMeasurements().poll(_context(), Repeat(), None))
    assert poll.cursor is None
    return poll.events


def test_the_withings_blocks_are_well_formed() -> None:
    registry = BlockRegistry()
    for block in withings_blocks():
        registry.register(block, fake_plugin())
    assert [block.id for block in registry.all()] == ["withings.new-measurements"]


def test_a_poll_gives_the_measurements_of_each_day_oldest_first(
    withings: AsyncMock,
) -> None:
    withings.get_measurements.return_value = {
        TODAY: {
            MeasurementType.WEIGHT: 80.5,
            MeasurementType.FAT_PERCENTAGE: 20.1,
            MeasurementType.FAT_MASS: 16.2,
            MeasurementType.HEART_PULSE: 60,
        },
        YESTERDAY: {MeasurementType.WEIGHT: 80.9},
    }

    assert _poll() == [
        # A measurement that was not taken is left empty.
        DayMeasurements(date="2026-05-08", weight=80.9),
        DayMeasurements(
            date="2026-05-09",
            weight=80.5,
            fat_percentage=20.1,
            fat_mass=16.2,
            heart_pulse=60,
        ),
    ]


def test_a_poll_asks_for_today_and_the_day_before(withings: AsyncMock) -> None:
    assert _poll() == []

    # Yesterday too: a measurement can arrive after midnight, and a day
    # whose run failed has to be found again.
    withings.get_measurements.assert_awaited_once_with(
        start_date=YESTERDAY, end_date=TODAY
    )


def test_a_day_runs_again_when_its_measurements_change() -> None:
    trigger = NewMeasurements()
    morning = DayMeasurements(date="2026-05-09", weight=80.5)
    evening = DayMeasurements(date="2026-05-09", weight=80.5, heart_pulse=60)

    assert trigger.key(morning) == trigger.key(evening) == "2026-05-09"
    assert trigger.version(morning) == trigger.version(
        DayMeasurements(date="2026-05-09", weight=80.5)
    )
    assert trigger.version(morning) != trigger.version(evening)


def _by_hand(days: Any) -> list[DayMeasurements]:
    return asyncio.run(
        NewMeasurements().manual_events(_context(), Repeat(), MeasurementRange(days))
    )


def test_by_hand_a_range_gives_its_days_that_have_measurements(
    withings: AsyncMock,
) -> None:
    withings.get_measurements.return_value = {
        TODAY: {MeasurementType.WEIGHT: 80.5},
        datetime.date(2026, 5, 5): {MeasurementType.WEIGHT: 81.0},
    }

    assert _by_hand("The last week") == [
        DayMeasurements(date="2026-05-05", weight=81.0),
        DayMeasurements(date="2026-05-09", weight=80.5),
    ]
    # The seven days up to today.
    withings.get_measurements.assert_awaited_once_with(
        start_date=datetime.date(2026, 5, 3), end_date=TODAY
    )


def test_by_hand_today_asks_for_today_only(withings: AsyncMock) -> None:
    assert _by_hand("Today") == []

    withings.get_measurements.assert_awaited_once_with(start_date=TODAY, end_date=TODAY)
