import datetime
import types
from dataclasses import dataclass, field
from typing import Any

import pytest

from spanreed.workflows import expressions
from spanreed.workflows.values import (
    NotPlain,
    from_data,
    from_whole_data,
    in_words,
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


# -- a value that does not fit says what it is ---------------------------------


@dataclass
class _Check:
    condition: bool
    count: int = 0
    text: str = ""
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Found:
    """What a step gave."""

    path: str = "Daily/2026-11-25.md"
    count: int = 3
    value: Any = None
    items: tuple[str, ...] = ("a", "b")
    exists: bool = True


def _why(written: dict[str, Any]) -> str:
    """Return why what was written does not fit, as a run finds it."""
    rendered = expressions.render(written, {"day": _Found()})
    with pytest.raises(ValueError) as error:
        from_data(_Check, rendered, written)
    return str(error.value)


@pytest.mark.parametrize(
    ("condition", "came_to", "example"),
    [
        ("{{ day.path }}", "the text “Daily/2026-11-25.md”", "{{ day.path != '' }}"),
        ("{{ day.count }}", "the number 3", "{{ day.count > 0 }}"),
        ("{{day.count * 1.5}}", "the number 4.5", "{{ day.count * 1.5 > 0 }}"),
        ("{{ day.value }}", "nothing", "{{ day.value is not none }}"),
        ("{{ day.items }}", "a list of 2", "{{ day.items | length > 0 }}"),
        ("{{ day }}", "a group of values", "{{ day | length > 0 }}"),
        ("{{ '' }}", "an empty text", "{{ '' != '' }}"),
    ],
)
def test_a_condition_that_is_neither_yes_nor_no_says_what_it_came_to(
    condition: str, came_to: str, example: str
) -> None:
    assert _why({"condition": condition}) == (
        f"Condition: “{condition}” came to {came_to}, which is neither yes "
        "nor no. Ask something of it that is true or false, such as "
        f"“{example}”."
    )


def test_a_condition_that_is_yes_or_no_is_taken() -> None:
    for condition, holds in (
        ("{{ day.exists }}", True),
        ("{{ day.count > 5 }}", False),
        ("{{ day.value is none and day.path != '' }}", True),
        (True, True),
    ):
        rendered = expressions.render({"condition": condition}, {"day": _Found()})
        assert from_data(_Check, rendered, {"condition": condition}).condition is holds


def test_what_is_asked_outside_the_braces_is_put_inside() -> None:
    # Only what is inside is worked out, so this is the text "3 > 0".
    assert _why({"condition": "{{ day.count }} > 0"}) == (
        "Condition: “{{ day.count }} > 0” came to the text “3 > 0”, which is "
        "neither yes nor no. Only what is inside the braces is worked out. "
        "Put all of it inside: “{{ day.count > 0 }}”."
    )


def test_two_templates_in_a_condition_are_a_text() -> None:
    assert _why({"condition": "{{ day.exists }} and {{ day.count }}"}) == (
        "Condition: “{{ day.exists }} and {{ day.count }}” came to the text "
        "“True and 3”, which is neither yes nor no. A text is never yes or "
        "no. Put all of what is asked inside one pair of braces, such as "
        "“{{ step.count > 0 and step.found }}”."
    )


def test_a_value_that_was_written_as_it_is_says_what_it_is() -> None:
    # No template: there is nothing that "came to" anything.
    assert _why({"condition": 3}) == "Condition: the number 3 is neither yes nor no."
    assert _why({"condition": "maybe"}) == (
        "Condition: the text “maybe” is neither yes nor no."
    )
    with pytest.raises(ValueError) as error:
        from_data(_Check, {"condition": "maybe"})
    assert str(error.value) == "Condition: the text “maybe” is neither yes nor no."


def test_a_value_of_another_kind_says_what_was_written_and_what_it_came_to() -> None:
    assert _why({"condition": True, "count": "{{ day.path }}"}) == (
        "Count: “{{ day.path }}” came to the text “Daily/2026-11-25.md”. "
        "Expected a number."
    )
    assert _why({"condition": True, "text": "{{ day.count }}"}) == (
        "Text: “{{ day.count }}” came to the number 3. Expected text."
    )
    # What is not from a template is told as before.
    assert _why({"condition": True, "count": "three"}) == "Count: Expected a number."
    assert _why({"count": 3}) == "Condition: A value is required."


def test_a_long_text_is_quoted_by_its_start() -> None:
    long = "{{ day.path * 40 }}"

    said = _why({"condition": long})

    assert "came to the text “Daily/2026-11-25.mdDaily/" in said
    assert "…”, which is neither yes nor no." in said
    assert len(said) < 400


def test_a_value_is_named_as_a_user_would_name_it() -> None:
    assert in_words(True) == "yes"
    assert in_words(False) == "no"
    assert in_words(0) == "the number 0"
    assert in_words([]) == "an empty list"
    assert in_words({"a": 1}) == "a group of values"
    assert in_words(types.SimpleNamespace(a=1)) == "a group of values"
    assert in_words(datetime.date(2026, 5, 9)) == "“2026-05-09”"
