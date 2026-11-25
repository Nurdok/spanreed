"""The Health Connect triggers for workflows.

The plugin stores what the phone sends and says which days changed. The
triggers here turn a changed day into an event: the day's metrics, or the
day's raw records of one type. Both are read from the store when the
workflow runs, so an event holds what is stored at that moment.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Literal

from spanreed.apis.health_connect import (
    VO2_MAX_RECORD_TYPE,
    HealthConnectStore,
    daily_heart_rate_metrics,
    daily_step_total,
    daily_vo2_max,
    dump_record,
    max_heart_rate,
    type_name_of,
)
from spanreed.user import User
from spanreed.config_form import described
from spanreed.workflows.blocks import (
    Block,
    NoParams,
    NoQuestion,
    PushTrigger,
    TriggerContext,
)

# The record types of the store that a day's metrics are computed from.
METRIC_RECORD_TYPES = ("steps", "heart-rate", "resting-heart-rate", VO2_MAX_RECORD_TYPE)


@dataclass
class DayMetrics:
    """The metrics of one local date. A metric with no data is ``None``."""

    date: str = described("The day the data is of, as 2026-05-09")
    steps: int | None = described(
        "The steps of the day. Nothing if there is no data",
        default=None,
    )
    resting_heart_rate: int | None = described(
        "The resting heart rate, in beats per minute. Nothing if there is " "no data",
        default=None,
    )
    heart_rate_p5: int | None = described(
        "The heart rate that 5% of the day’s readings are below, in beats "
        "per minute",
        default=None,
    )
    heart_rate_avg: int | None = described(
        "The average heart rate of the day, in beats per minute",
        default=None,
    )
    heart_rate_p95: int | None = described(
        "The heart rate that 95% of the day’s readings are below, in beats "
        "per minute",
        default=None,
    )
    heart_rate_p98: int | None = described(
        "The heart rate that 98% of the day’s readings are below, in beats "
        "per minute",
        default=None,
    )
    heart_rate_p99: int | None = described(
        "The heart rate that 99% of the day’s readings are below, in beats "
        "per minute",
        default=None,
    )
    hr_moderate_minutes: int | None = described(
        "Minutes with a moderate heart rate. It needs your date of birth",
        default=None,
    )
    hr_vigorous_minutes: int | None = described(
        "Minutes with a vigorous heart rate. It needs your date of birth",
        default=None,
    )
    vo2_max: float | None = described(
        "The VO2 max of the day. Nothing if there is no data",
        default=None,
    )


async def day_metrics(user: User, date: datetime.date) -> DayMetrics | None:
    """Compute the metrics of a local date from the stored records.

    Return ``None`` if nothing is stored for the date. The heart rate zone
    minutes are left out unless the user's date of birth is set.
    """
    store = HealthConnectStore(user)
    steps = await store.get_steps(date)
    heart_rate = await store.get_heart_rate(date)
    resting = await store.get_resting_heart_rate(date)
    vo2_max = daily_vo2_max(await store.get_raw_records(VO2_MAX_RECORD_TYPE, date))
    if not (steps or heart_rate or resting or vo2_max is not None):
        return None

    # Read the date of birth now: ``user`` may have been loaded before the
    # user set it.
    user.birth_date = await User.get_user_birth_date(user.id)
    age = user.age_on(date)
    metrics = daily_heart_rate_metrics(
        heart_rate,
        resting,
        max_hr=max_heart_rate(age) if age is not None else None,
    )
    return DayMetrics(
        date=date.isoformat(),
        steps=daily_step_total(steps) if steps else None,
        vo2_max=vo2_max,
        # The keys are the daily note property names: heart-rate-avg.
        **{name.replace("-", "_"): value for name, value in metrics.items()},
    )


@dataclass
class DayRange:
    """Which days a manual run covers."""

    days: Literal["Today", "The last week", "Every stored day"] = field(
        metadata={"prompt": "Sync health data for…"}
    )


class DayChanged(PushTrigger[NoParams, DayMetrics]):
    """Fire for each local date whose steps, heart rate or VO2 max changed.

    The key of a day is its date, ``YYYY-MM-DD``.
    """

    id = "health-connect.day-changed"
    title = "A day’s data changed"
    Manual = DayRange

    async def event_for(
        self, ctx: TriggerContext, params: NoParams, key: str
    ) -> DayMetrics | None:
        """Return the metrics of the date, or ``None`` if none are stored."""
        try:
            date = datetime.date.fromisoformat(key)
        except ValueError:
            return None
        return await day_metrics(ctx.user, date)

    async def manual_events(
        self, ctx: TriggerContext, params: NoParams, answer: DayRange
    ) -> list[DayMetrics]:
        """Return the metrics of each day in the range that has any."""
        if answer.days == "Every stored day":
            stored = await HealthConnectStore(ctx.user).get_stored_days()
            dates = sorted(
                {
                    date
                    for record_type, date in stored
                    if record_type in METRIC_RECORD_TYPES
                }
            )
        else:
            count = 1 if answer.days == "Today" else 7
            today = datetime.datetime.now(tz=ctx.user.tzinfo).date()
            dates = [today - datetime.timedelta(days=i) for i in reversed(range(count))]
        events = [await day_metrics(ctx.user, date) for date in dates]
        return [event for event in events if event is not None]


@dataclass
class RawRecords:
    """The stored records of one record type and local date."""

    type: str = described(
        "The name of the record type as the phone sends it: heart_rate",
    )
    date: str = described("The day the records are of, as 2026-05-09")
    records: str = described(
        "One record as JSON on each line. Empty if the phone deleted every "
        "record of the day",
    )
    count: int = described("How many records there are")


def raw_records_key(record_type: str, date: datetime.date) -> str:
    """Return the key of a day's records of one type of the store."""
    return f"{record_type}|{date.isoformat()}"


class RawRecordsChanged(PushTrigger[NoParams, RawRecords]):
    """Fire for each record type and local date whose stored records changed.

    A day that deletions on the phone left with no records gives an event
    with no records. A day that has none for another reason (they expired,
    or the user deleted them here) gives no event.
    """

    id = "health-connect.raw-records-changed"
    title = "Raw records changed"
    Manual = NoQuestion

    async def event_for(
        self, ctx: TriggerContext, params: NoParams, key: str
    ) -> RawRecords | None:
        """Return the records of the key's type and date."""
        record_type, _, day = key.rpartition("|")
        try:
            date = datetime.date.fromisoformat(day)
        except ValueError:
            return None
        store = HealthConnectStore(ctx.user)
        records = await store.get_raw_records(record_type, date)
        if not records and not await store.is_export_emptied(record_type, date):
            return None
        if records:
            # The day has records again, so it is not an emptied day.
            await store.clear_export_emptied(record_type, date)
        return RawRecords(
            type=type_name_of(record_type),
            date=day,
            records="".join(f"{dump_record(record)}\n" for record in records),
            count=len(records),
        )

    async def manual_events(
        self, ctx: TriggerContext, params: NoParams, answer: NoQuestion
    ) -> list[RawRecords]:
        """Return the records of every stored day of every type."""
        stored = await HealthConnectStore(ctx.user).get_stored_days()
        events = [
            await self.event_for(ctx, params, raw_records_key(record_type, date))
            for record_type, date in stored
        ]
        return [event for event in events if event is not None]


def health_connect_blocks() -> list[Block]:
    """Return the blocks that the Health Connect plugin publishes."""
    return [DayChanged(), RawRecordsChanged()]
