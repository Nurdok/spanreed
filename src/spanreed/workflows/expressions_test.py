from dataclasses import dataclass

import pytest

from spanreed.workflows import expressions
from spanreed.workflows.expressions import ExpressionError


@dataclass
class Found:
    tasks: list[str]
    count: int


CONTEXT = {"find": Found(tasks=["a", "b"], count=2), "trigger": {"date": "2026-05-09"}}


def test_text_without_a_template_is_unchanged() -> None:
    assert expressions.render("spanreed/no-overdue", CONTEXT) == "spanreed/no-overdue"
    assert expressions.render("costs { 5 }", CONTEXT) == "costs { 5 }"


def test_template_in_a_text_gives_text() -> None:
    assert (
        expressions.render(
            "Updated {{ find.count }} tasks on {{ trigger.date }}.", CONTEXT
        )
        == "Updated 2 tasks on 2026-05-09."
    )


def test_a_whole_expression_keeps_its_type() -> None:
    assert expressions.render("{{ find.tasks }}", CONTEXT) == ["a", "b"]
    assert expressions.render("{{ find.count }}", CONTEXT) == 2
    assert expressions.render(" {{ find.count > 0 }} ", CONTEXT) is True
    assert expressions.render("{{ find.count > 5 }}", CONTEXT) is False


def test_templates_inside_lists_and_objects_are_filled_in() -> None:
    value = {"path": "Daily/{{ trigger.date }}.md", "rows": ["{{ find.count }}", 3]}
    assert expressions.render(value, CONTEXT) == {
        "path": "Daily/2026-05-09.md",
        "rows": [2, 3],
    }


@pytest.mark.parametrize(
    "text",
    [
        "{{ nothing }}",
        "{{ find.nothing }}",
        "Count: {{ nothing.count }}",
        "{{ find.count + }}",
        "{{ find.count / 0 }}",
    ],
)
def test_a_template_that_cannot_be_filled_in_is_an_error(text: str) -> None:
    with pytest.raises(ExpressionError):
        expressions.render(text, CONTEXT)


def test_templates_cannot_reach_into_python() -> None:
    with pytest.raises(ExpressionError):
        expressions.render("{{ find.__class__.__mro__ }}", CONTEXT)


def test_referenced_names_are_the_first_parts() -> None:
    value = {
        "a": "{{ find.count }} of {{ trigger.date }}",
        "b": ["{{ run.manual }}", 1],
    }
    assert expressions.referenced_names(value) == {"find", "trigger", "run"}
    assert expressions.referenced_names("no template") == set()


def test_referenced_names_of_a_broken_template_is_an_error() -> None:
    with pytest.raises(ExpressionError):
        expressions.referenced_names("{{ find.count")
