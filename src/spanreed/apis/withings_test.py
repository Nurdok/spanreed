"""Tests of the dates that Withings measurements are counted by."""

import asyncio
import datetime
import zoneinfo
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from spanreed.apis.withings import (
    MeasurementType,
    UserConfig,
    WithingsApi,
    today_of,
)
from spanreed.test_utils import mock_user_find_by_id

UTC = datetime.timezone.utc
# Three hours ahead of UTC in May, two in November.
JERUSALEM = zoneinfo.ZoneInfo("Asia/Jerusalem")
# Behind UTC: its day starts after the day of UTC does.
NEW_YORK = zoneinfo.ZoneInfo("America/New_York")


def _user(timezone: datetime.tzinfo) -> Any:
    user = mock_user_find_by_id(4)
    user.tzinfo = timezone
    return user


def _api(timezone: datetime.tzinfo) -> WithingsApi:
    config = UserConfig(
        userid="u",
        access_token="a",
        refresh_token="r",
        expires_in="3600",
        scope="s",
        token_type="Bearer",
    )
    return WithingsApi(_user(timezone), config)


def _weight(when: datetime.datetime, grams: int) -> dict[str, Any]:
    return {
        "grpid": int(when.timestamp()),
        "date": int(when.timestamp()),
        "measures": [{"value": grams, "type": 1, "unit": -3}],
    }


def _response(*groups: dict[str, Any]) -> MagicMock:
    response = MagicMock()
    response.json.return_value = {"status": 0, "body": {"measuregrps": list(groups)}}
    return response


def _dates(api: WithingsApi, *groups: dict[str, Any]) -> list[str]:
    found = api.extract_measurements_from_json({"body": {"measuregrps": list(groups)}})
    return [date.isoformat() for date in sorted(found)]


# 22:30 UTC on May 9: half past one at night on May 10 in Jerusalem, and
# half past six in the evening of May 9 in New York.
NIGHT = datetime.datetime(2026, 5, 9, 22, 30, tzinfo=UTC)


def test_a_measurement_belongs_to_the_date_it_was_taken_on_where_the_user_is() -> None:
    weighed = _weight(NIGHT, 80500)

    assert _dates(_api(JERUSALEM), weighed) == ["2026-05-10"]
    assert _dates(_api(UTC), weighed) == ["2026-05-09"]
    assert _dates(_api(NEW_YORK), weighed) == ["2026-05-09"]


def test_a_morning_measurement_is_of_the_same_date_everywhere_near() -> None:
    morning = _weight(datetime.datetime(2026, 5, 10, 6, 0, tzinfo=UTC), 80500)

    assert _dates(_api(JERUSALEM), morning) == ["2026-05-10"]
    assert _dates(_api(UTC), morning) == ["2026-05-10"]


def test_the_last_reading_of_the_users_day_wins() -> None:
    api = _api(JERUSALEM)
    # Both on May 10 in Jerusalem, on two dates in UTC.
    night = _weight(NIGHT, 80500)
    noon = _weight(datetime.datetime(2026, 5, 10, 9, 0, tzinfo=UTC), 80100)

    found = api.extract_measurements_from_json({"body": {"measuregrps": [noon, night]}})

    assert list(found) == [datetime.date(2026, 5, 10)]
    assert found[datetime.date(2026, 5, 10)] == {
        MeasurementType.WEIGHT: pytest.approx(80.1)
    }


def _asked_for(api: WithingsApi, **dates: Any) -> tuple[datetime.datetime, ...]:
    """Return the two moments that a request for these dates asks between."""
    with patch(
        "spanreed.apis.withings.requests.post", return_value=_response()
    ) as post:
        asyncio.run(api.get_measurements(**dates))
    sent = post.call_args.kwargs["data"]
    return tuple(
        datetime.datetime.fromtimestamp(sent[name], tz=UTC)
        for name in ("startdate", "enddate")
    )


def test_a_day_is_asked_for_from_midnight_to_midnight_where_the_user_is() -> None:
    day = datetime.date(2026, 5, 10)

    # Midnight in Jerusalem is 21:00 UTC the day before, in May.
    assert _asked_for(_api(JERUSALEM), start_date=day, end_date=day) == (
        datetime.datetime(2026, 5, 9, 21, 0, tzinfo=UTC),
        datetime.datetime(2026, 5, 10, 21, 0, tzinfo=UTC),
    )
    assert _asked_for(_api(UTC), start_date=day, end_date=day) == (
        datetime.datetime(2026, 5, 10, 0, 0, tzinfo=UTC),
        datetime.datetime(2026, 5, 11, 0, 0, tzinfo=UTC),
    )


def test_the_days_of_a_range_keep_their_own_midnights_across_a_clock_change() -> None:
    # The clocks of Jerusalem went back on October 25, 2026.
    asked = _asked_for(
        _api(JERUSALEM),
        start_date=datetime.date(2026, 10, 24),
        end_date=datetime.date(2026, 10, 26),
    )

    assert asked == (
        datetime.datetime(2026, 10, 23, 21, 0, tzinfo=UTC),
        datetime.datetime(2026, 10, 26, 22, 0, tzinfo=UTC),
    )


def test_today_is_the_date_where_the_user_is() -> None:
    def now(timezone: datetime.tzinfo) -> datetime.datetime:
        return NIGHT.astimezone(timezone)

    with patch("spanreed.apis.withings._now", new=now):
        assert today_of(_user(JERUSALEM)) == datetime.date(2026, 5, 10)
        assert today_of(_user(UTC)) == datetime.date(2026, 5, 9)
        # With no dates, today is asked for: the user's.
        assert _asked_for(_api(JERUSALEM)) == (
            datetime.datetime(2026, 5, 9, 21, 0, tzinfo=UTC),
            datetime.datetime(2026, 5, 10, 21, 0, tzinfo=UTC),
        )


def test_what_is_asked_for_is_what_is_counted_as_the_day() -> None:
    """A measurement at either end of the user's day is found, and is of that day."""
    api = _api(JERUSALEM)
    day = datetime.date(2026, 5, 10)
    start, end = _asked_for(api, start_date=day, end_date=day)
    first = _weight(start, 80000)
    last = _weight(end - datetime.timedelta(seconds=1), 80000)

    assert _dates(api, first, last) == ["2026-05-10"]
    # And the second after is of the next day.
    assert _dates(api, _weight(end, 80000)) == ["2026-05-11"]
