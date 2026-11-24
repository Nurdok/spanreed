import datetime
import types
from dataclasses import dataclass, field
from typing import Any

from spanreed.workflows.values import from_whole_data, to_data, whole_data


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
