"""Tests of conditions as rows, and as the template they are stored as."""

import json
from typing import Any

import pytest

from spanreed.workflows import expressions
from spanreed.workflows.conditions import (
    QUESTIONS,
    ConditionError,
    empty_rows,
    from_template,
    questions,
    to_template,
)
from spanreed.workflows.recipes import all_recipes


def _rows(*rows: list[str], join: str = "all") -> dict[str, Any]:
    return {"join": join, "rows": [list(row) for row in rows]}


def _holds(shown: dict[str, Any], **context: Any) -> Any:
    """Return what the condition of some rows comes to in a run."""
    return expressions.render(to_template(shown), context)


# -- from rows to a template ---------------------------------------------------


@pytest.mark.parametrize(
    ("row", "template"),
    [
        (["{{ day.exists }}", "true", ""], "{{ day.exists }}"),
        (["{{ day.exists }}", "false", ""], "{{ not day.exists }}"),
        (["{{ find.count }}", "is", "0"], "{{ find.count == 0 }}"),
        (["{{ find.count }}", "is-not", "0"], "{{ find.count != 0 }}"),
        (["{{ find.count }}", "bigger", "2"], "{{ find.count > 2 }}"),
        (["{{ find.count }}", "at-least", "2.5"], "{{ find.count >= 2.5 }}"),
        (["{{ find.count }}", "smaller", "-1"], "{{ find.count < -1 }}"),
        (
            ["{{ find.count }}", "at-most", "{{ other.count }}"],
            "{{ find.count <= other.count }}",
        ),
        (
            ["{{ note.tags }}", "contains", "book"],
            '{{ note.tags is containing("book") }}',
        ),
        (
            ["{{ note.tags }}", "lacks", "book"],
            '{{ note.tags is not containing("book") }}',
        ),
        (["{{ note.value }}", "empty", ""], "{{ note.value is empty }}"),
        (["{{ note.value }}", "not-empty", ""], "{{ note.value is not empty }}"),
    ],
)
def test_each_question_is_a_template(row: list[str], template: str) -> None:
    assert to_template(_rows(row)) == template
    # And the template is read as the row again.
    assert from_template(template) == _rows(row)


def test_every_question_can_be_asked_and_read_again() -> None:
    for question in QUESTIONS:
        row = ["{{ a.b }}", question.id, "x" if question.second else ""]
        assert from_template(to_template(_rows(row))) == _rows(row), question.id


def test_rows_go_together_with_and_or_with_or() -> None:
    rows = (["{{ run.manual }}", "true", ""], ["{{ write.count }}", "bigger", "0"])

    assert to_template(_rows(*rows)) == "{{ run.manual and write.count > 0 }}"
    assert to_template(_rows(*rows, join="any")) == (
        "{{ run.manual or write.count > 0 }}"
    )


@pytest.mark.parametrize(
    ("written", "expression"),
    [
        # A text is a text, whatever is in it.
        ("Yes (add)", '"Yes (add)"'),
        ('say "hi"', '"say \\"hi\\""'),
        ("it's", '"it\'s"'),
        ("  spaced  ", '"spaced"'),
        # What reads as a number or as yes and no is one.
        ("5", "5"),
        ("-2.5", "-2.5"),
        ("yes", "true"),
        ("No", "false"),
        # In quotes, it is the text.
        ('"5"', '"5"'),
        ('"yes"', '"yes"'),
        ('""', '""'),
        # A value of an earlier step, and one that is worked out.
        ("{{ other.count }}", "other.count"),
        ("{{ a.count + 1 }}", "(a.count + 1)"),
        ("{{ a.items | length }}", "(a.items | length)"),
        # Text with values in it is put together.
        ("Daily/{{ trigger.date }}.md", '("Daily/" ~ trigger.date ~ ".md")'),
    ],
)
def test_a_value_is_written_as_in_any_text_box(written: str, expression: str) -> None:
    assert to_template(_rows(["{{ x.value }}", "is", written])) == (
        "{{ x.value == " + expression + " }}"
    )


def test_what_the_rows_ask_is_what_a_run_works_out() -> None:
    day = {"path": "Daily/2026-05-09.md", "exists": True, "tags": ["a", "b"]}

    assert _holds(_rows(["{{ day.exists }}", "true", ""]), day=day) is True
    assert _holds(_rows(["{{ day.exists }}", "false", ""]), day=day) is False
    assert _holds(_rows(["{{ day.tags }}", "contains", "b"]), day=day) is True
    assert _holds(_rows(["{{ day.path }}", "contains", "2026"]), day=day) is True
    assert _holds(_rows(["{{ day.tags }}", "lacks", "b"]), day=day) is False
    assert _holds(_rows(["{{ day.tags | length }}", "bigger", "1"]), day=day) is True
    assert _holds(
        _rows(["{{ day.path }}", "is", "Daily/{{ d }}.md"]), day=day, d="2026-05-09"
    )
    assert _holds(_rows(["{{ day.path }}", "empty", ""]), day=day) is False
    both = _rows(["{{ day.exists }}", "true", ""], ["{{ day.tags }}", "empty", ""])
    assert _holds(both, day=day) is False
    assert _holds({**both, "join": "any"}, day=day) is True


@pytest.mark.parametrize(
    ("value", "is_empty"),
    [(None, True), ("", True), ([], True), ({}, True), ((), True)]
    + [(0, False), (False, False), ("x", False), ([0], False), (" ", False)],
)
def test_empty_is_no_text_no_items_or_nothing(value: Any, is_empty: bool) -> None:
    # 0 and "no" are values: a count of 0 is not an empty count.
    assert _holds(_rows(["{{ x }}", "empty", ""]), x=value) is is_empty
    assert _holds(_rows(["{{ x }}", "not-empty", ""]), x=value) is not is_empty


@pytest.mark.parametrize(
    ("shown", "problem"),
    [
        ("{{ a }}", "rows are expected."),
        ({"join": "all"}, "rows are expected."),
        ({"join": "all", "rows": []}, "a condition has at least one row."),
        (
            {"join": "xor", "rows": [["{{ a }}", "true", ""]]},
            "the rows are joined by “all” or by “any”.",
        ),
        (
            _rows(["{{ a }}", "true"]),
            "a row is a value, a question and a second value.",
        ),
        (
            _rows(["{{ a }}", "resembles", "b"]),
            "“resembles” is nothing that can be asked.",
        ),
        (_rows(["", "true", ""]), "the value is missing."),
        (_rows(["  ", "is", "3"]), "the value is missing."),
        (_rows(["{{ a }}", "is", ""]), "the value after “is” is missing."),
        (
            _rows(["{% if a %}x{% endif %}", "true", ""]),
            "the value cannot hold “{% … %}”.",
        ),
    ],
)
def test_rows_that_are_no_condition_say_why(shown: Any, problem: str) -> None:
    with pytest.raises(ConditionError) as error:
        to_template(shown)
    assert str(error.value) == problem


def test_a_row_that_lacks_a_value_is_named_by_its_place() -> None:
    shown = _rows(["{{ a }}", "true", ""], ["{{ b }}", "bigger", ""])

    with pytest.raises(ConditionError) as error:
        to_template(shown)

    assert str(error.value) == "row 2: the value after “is bigger than” is missing."


def test_a_value_that_cannot_be_worked_out_is_refused() -> None:
    with pytest.raises(
        ConditionError, match="this is no condition that can be worked out"
    ):
        to_template(_rows(["{{ a + }}", "true", ""]))


# -- from a template to rows ---------------------------------------------------


def test_a_condition_that_nobody_wrote_yet_is_one_empty_row() -> None:
    assert from_template("") == from_template(None) == empty_rows()
    assert empty_rows() == _rows(["", "true", ""])


@pytest.mark.parametrize(
    ("template", "shown"),
    [
        # As the recipes write them.
        (
            "{{ sure.answer == 'Yes (add)' }}",
            _rows(["{{ sure.answer }}", "is", "Yes (add)"]),
        ),
        ("{{ not tally.has_date }}", _rows(["{{ tally.has_date }}", "false", ""])),
        (
            "{{ run.manual or not books.found or books.age_hours > 72 }}",
            _rows(
                ["{{ run.manual }}", "true", ""],
                ["{{ books.found }}", "false", ""],
                ["{{ books.age_hours }}", "bigger", "72"],
                join="any",
            ),
        ),
        # Without spaces, and with more than are needed.
        ("{{find.count>0}}", _rows(["{{ find.count }}", "bigger", "0"])),
        ("{{   a.b   ==   'x'   }}", _rows(["{{ a.b }}", "is", "x"])),
        # A text that would read as something else keeps its quotes.
        ("{{ x.v == '5' }}", _rows(["{{ x.v }}", "is", '"5"'])),
        ("{{ x.v == 'no' }}", _rows(["{{ x.v }}", "is", '"no"'])),
        ("{{ x.v != '' }}", _rows(["{{ x.v }}", "is-not", '""'])),
        ("{{ x.v == true }}", _rows(["{{ x.v }}", "is", "yes"])),
        # What is worked out is a value like any other.
        (
            "{{ a.items | length > 2 }}",
            _rows(["{{ a.items | length }}", "bigger", "2"]),
        ),
        (
            "{{ a.x + 1 >= b.y * 2 }}",
            _rows(["{{ a.x + 1 }}", "at-least", "{{ b.y * 2 }}"]),
        ),
        ("{{ not (a.x > b.y) }}", _rows(["{{ a.x > b.y }}", "false", ""])),
        # Words in a text are not what joins the rows.
        ("{{ a.v == 'this and that' }}", _rows(["{{ a.v }}", "is", "this and that"])),
        ("{{ a.v == 'x > y' }}", _rows(["{{ a.v }}", "is", "x > y"])),
        (
            "{{ a.words is containing('in (x)') }}",
            _rows(["{{ a.words }}", "contains", "in (x)"]),
        ),
    ],
)
def test_a_template_is_read_as_rows(template: str, shown: dict[str, Any]) -> None:
    assert from_template(template) == shown
    # What the rows give back means the same.
    assert expressions._env.parse(to_template(shown)) == expressions._env.parse(
        template
    )


@pytest.mark.parametrize(
    "template",
    [
        # Anything that comes to yes or no is the value of "is true".
        "{{ x.v is none }}",
        "{{ a.x < b.y < c.z }}",
        "{{ f(a.x, b.y) }}",
        "{{ a.v in ['x', 'y'] and b.w }}",
        "{{ a.x and (b.y or c.z) }}",
        "{{ (a.x and b.y) or (c.z and d.w) }}",
    ],
)
def test_what_is_no_question_of_the_list_is_the_value_of_is_true(template: str) -> None:
    shown = from_template(template)

    assert shown is not None
    assert expressions._env.parse(to_template(shown)) == expressions._env.parse(
        template
    )


@pytest.mark.parametrize(
    "stored",
    [
        # "And" and "or" at once: which goes with which is not a list.
        "{{ a.x and b.y or c.z }}",
        # Not one template, and nothing else.
        "{{ a.x }} > 0",
        "{{ a.x }}{{ b.y }}",
        "{% if a.x %}yes{% endif %}",
        "yes",
        "{{ a.x > }}",
        "{{ (a.x }}",
        # Not a text at all: yes or no as it is.
        True,
        False,
        3,
    ],
)
def test_what_is_no_list_of_rows_stays_as_it_is_written(stored: Any) -> None:
    assert from_template(stored) is None


def test_every_condition_of_the_recipes_is_a_list_of_rows() -> None:
    def conditions_of(steps: list[Any]) -> list[str]:
        found = []
        for step in steps:
            if step.block in ("core.if", "core.continue-if"):
                found.append(step.inputs["condition"])
            found += conditions_of(step.steps) + conditions_of(step.otherwise)
        return found

    written = [
        condition
        for recipe in all_recipes().values()
        for condition in conditions_of(recipe.workflow_for(4).steps)
    ]

    assert len(written) > 10
    for condition in written:
        shown = from_template(condition)
        assert shown is not None, condition
        # Opening a workflow and saving it asks what it asked before.
        again = to_template(shown)
        assert expressions._env.parse(again) == expressions._env.parse(condition)


def test_the_page_is_told_what_can_be_asked() -> None:
    told = questions()

    assert [question["label"] for question in told] == [
        "is true",
        "is false",
        "is",
        "is not",
        "is bigger than",
        "is at least",
        "is smaller than",
        "is at most",
        "contains",
        "does not contain",
        "is empty",
        "is not empty",
    ]
    assert told[0] == {
        "id": "true",
        "label": "is true",
        "second": False,
        "kinds": ["yes or no"],
    }
    assert told[4]["second"] is True and told[4]["kinds"] == ["number"]
    json.dumps(told)
