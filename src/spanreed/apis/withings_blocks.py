"""The Withings trigger for workflows.

The plugin holds the login. The trigger here asks Withings, at an interval,
for the measurements of the last days, and gives each day whose measurements
are new or changed as an event.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Literal

from spanreed.apis.withings import MeasurementType, WithingsApi, today_of
from spanreed.user import User
from spanreed.workflows.blocks import (
    Block,
    Poll,
    PollTrigger,
    Repeat,
    TriggerContext,
    digest,
)

# The field of ``DayMeasurements`` that holds each type of measurement.
_FIELDS = {
    MeasurementType.WEIGHT: "weight",
    MeasurementType.FAT_PERCENTAGE: "fat_percentage",
    MeasurementType.FAT_MASS: "fat_mass",
    MeasurementType.HEART_PULSE: "heart_pulse",
}

# A poll asks for today and for this many days before it. A measurement
# can reach Withings hours after it was taken, and a day whose run failed
# has to be found again.
LOOK_BACK_DAYS = 1


@dataclass
class DayMeasurements:
    """The measurements of one date. One that was not taken is ``None``.

    If a day has more than one reading of a measurement, this is the last.
    """

    # YYYY-MM-DD
    date: str
    # In kilograms.
    weight: float | None = None
    fat_percentage: float | None = None
    # In kilograms.
    fat_mass: float | None = None
    # In beats per minute.
    heart_pulse: float | None = None


async def measurements_of(
    user: User, start: datetime.date, end: datetime.date
) -> list[DayMeasurements]:
    """Return the measurements of each date from ``start`` to ``end``.

    Both dates are included. A date with no measurement is left out. The
    dates are in order, oldest first.
    """
    withings = await WithingsApi.for_user(user)
    by_date = await withings.get_measurements(start_date=start, end_date=end)
    return [
        DayMeasurements(
            date=date.isoformat(),
            **{
                _FIELDS[measurement]: value
                for measurement, value in by_date[date].items()
                if measurement in _FIELDS
            },
        )
        for date in sorted(by_date)
        if by_date[date]
    ]


@dataclass
class MeasurementRange:
    """Which days a manual run covers."""

    days: Literal["Today", "The last week"] = field(
        metadata={"prompt": "Sync Withings data for…"}
    )


class NewMeasurements(PollTrigger[Repeat, DayMeasurements]):
    """Fire for each date whose measurements are new or have changed.

    The key of a day is its date, ``YYYY-MM-DD``. A day fires again when a
    later poll finds other values for it: a measurement that was taken
    later in the day, or a new reading.
    """

    id = "withings.new-measurements"
    title = "New measurements for a day"
    Manual = MeasurementRange
    # A poll never looks further back than a few days.
    remembers = 30

    async def poll(
        self, ctx: TriggerContext, params: Repeat, cursor: str | None
    ) -> Poll[DayMeasurements]:
        """Return the measurements of today and of the days just before it.

        Today is the date where the user is, as ``WithingsApi`` counts the
        dates of the measurements.
        """
        today = today_of(ctx.user)
        start = today - datetime.timedelta(days=LOOK_BACK_DAYS)
        return Poll(events=await measurements_of(ctx.user, start, today))

    def key(self, event: DayMeasurements) -> str:
        """Return the date of the measurements."""
        return event.date

    def version(self, event: DayMeasurements) -> str:
        """Return a text that changes when a value of the day does."""
        return digest(event)

    async def manual_events(
        self, ctx: TriggerContext, params: Repeat, answer: MeasurementRange
    ) -> list[DayMeasurements]:
        """Return the measurements of each day in the range that has any."""
        count = 1 if answer.days == "Today" else 7
        today = today_of(ctx.user)
        start = today - datetime.timedelta(days=count - 1)
        return await measurements_of(ctx.user, start, today)


def withings_blocks() -> list[Block]:
    """Return the blocks that the Withings plugin publishes."""
    return [NewMeasurements()]
