import datetime

from spanreed.apis.googlefit import GoogleFitApi


def _bucket(date: datetime.date, *step_values: int) -> dict:
    """Build one aggregate bucket for ``date`` with the given step points.

    ``startTimeMillis`` is derived from local midnight so that the parser's
    local-timezone conversion round-trips back to ``date`` regardless of the
    timezone the test runs in.
    """
    start_ms = int(
        datetime.datetime.combine(date, datetime.time.min).timestamp() * 1000
    )
    return {
        "startTimeMillis": str(start_ms),
        "dataset": [{"point": [{"value": [{"intVal": v}]} for v in step_values]}],
    }


def test_extract_steps_sums_points_per_day() -> None:
    d1 = datetime.date(2024, 1, 1)
    d2 = datetime.date(2024, 1, 2)
    data = {
        "bucket": [
            _bucket(d1, 1000, 234),
            _bucket(d2, 5000),
        ]
    }

    assert GoogleFitApi.extract_steps_from_aggregate(data) == {
        d1: 1234,
        d2: 5000,
    }


def test_extract_steps_skips_empty_buckets() -> None:
    d1 = datetime.date(2024, 1, 1)
    d2 = datetime.date(2024, 1, 2)
    data = {
        "bucket": [
            _bucket(d1, 4200),
            # A day with no recorded steps yields an empty dataset; it must be
            # omitted rather than written as 0.
            {"startTimeMillis": _bucket(d2)["startTimeMillis"], "dataset": []},
        ]
    }

    assert GoogleFitApi.extract_steps_from_aggregate(data) == {d1: 4200}


def test_extract_steps_empty_response() -> None:
    assert GoogleFitApi.extract_steps_from_aggregate({}) == {}
