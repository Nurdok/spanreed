import datetime
import types
from dataclasses import dataclass, field
from typing import Any

import pytest

from spanreed.workflows.values import (
    NotPlain,
    from_whole_data,
    make_again,
    plain_data,
    to_data,
    whole_data,
)


@dataclass
class _Set:
    reps: int
    note: str = ""


@dataclass
class _Event:
    date: datetime.date
    records: str
    sets: list[_Set] = field(default_factory=list)
    extra: Any = None


def test_whole_data_keeps_what_a_trace_cuts_off() -> None:
    event = _Event(
        date=datetime.date(2026, 5, 9),
        records="x" * 5000,
        sets=[_Set(reps=n) for n in range(80)],
    )

    kept, traced = whole_data(event), to_data(event)

    assert kept["date"] == "2026-05-09"
    assert len(kept["records"]) == 5000 and len(traced["records"]) < 5000
    assert len(kept["sets"]) == 80 and len(traced["sets"]) == 51
    assert kept["sets"][79] == {"reps": 79, "note": ""}


def test_whole_data_is_json_whatever_it_is_given() -> None:
    assert whole_data(_Event(datetime.date(2026, 5, 9), "", extra={1: (2, 3)})) == {
        "date": "2026-05-09",
        "records": "",
        "sets": [],
        "extra": {"1": [2, 3]},
    }
    assert whole_data(object).startswith("<class")


def test_an_event_that_was_kept_reads_as_the_event_it_was() -> None:
    event = _Event(datetime.date(2026, 5, 9), "r", sets=[_Set(8, "top"), _Set(6)])

    again = from_whole_data(whole_data(event))

    assert again.date == "2026-05-09"
    assert again.records == "r"
    assert [(s.reps, s.note) for s in again.sets] == [(8, "top"), (6, "")]
    assert again.extra is None
    # A field named like a method of a mapping is still the field.
    assert from_whole_data({"items": [1], "keys": "k"}).items == [1]
    # What a test ran for goes into its trace like any event.
    assert to_data(again)["sets"][0] == {"reps": 8, "note": "top"}
    assert whole_data(again) == whole_data(event)
    assert isinstance(again, types.SimpleNamespace)


@dataclass
class _Given:
    text: str
    count: int = 0
    rows: list[Any] = field(default_factory=list)
    found: Any = None


def test_an_object_of_plain_values_is_made_again_as_it_was() -> None:
    given = _Given(
        text="x" * 5000,
        count=3,
        rows=[{"file": "a.md", "tags": ["x", None]}, 1.5, True],
        found={"mood": 4},
    )

    data = plain_data(given, _Given)
    again = make_again(_Given, data)

    assert again == given
    assert type(again) is _Given
    # The inside stays what it was: a mapping is not turned into an object.
    assert again.rows[0]["file"] == "a.md"


@pytest.mark.parametrize(
    "value",
    [
        b"bytes",
        datetime.date(2026, 5, 9),
        _Set(reps=1),
        ("a", "tuple"),
        {1: "a key that is a number"},
        {"deep": [{"down": b"bytes"}]},
        types.SimpleNamespace(a=1),
    ],
)
def test_a_value_that_would_come_back_as_something_else_is_not_plain(
    value: Any,
) -> None:
    with pytest.raises(NotPlain):
        plain_data(_Given(text="", found=value))


def test_an_object_of_another_class_than_expected_is_not_plain() -> None:
    assert plain_data(_Set(reps=2)) == {"reps": 2, "note": ""}
    with pytest.raises(NotPlain):
        plain_data(_Set(reps=2), _Given)
    with pytest.raises(NotPlain):
        plain_data("a text")


def test_an_object_without_a_class_comes_back_with_its_fields() -> None:
    data = plain_data(types.SimpleNamespace(date="2026-05-09", rows=[{"a": 1}]))

    again = make_again(types.SimpleNamespace, data)

    assert (again.date, again.rows) == ("2026-05-09", [{"a": 1}])


def test_data_that_no_longer_fits_the_class_is_not_made_again() -> None:
    for data in ({"text": "a", "gone": 1}, {"count": 2}, ["a list"], None):
        with pytest.raises(NotPlain):
            make_again(_Given, data)
