from dataclasses import dataclass, field
from typing import Any, Literal

import pytest

from spanreed.workflows import editing
from spanreed.workflows.builtin import ForEach, If
from spanreed.workflows.editing import FormError, workflow_from_form
from spanreed.workflows.model import Step, problems
from spanreed.workflows.testing import Record, registry_of, workflow_of


@dataclass
class _Inputs:
    """One input of each kind that a block can have."""

    note: str
    count: int = 1
    ratio: float = 0.5
    on: bool = False
    when: Literal["changed", "always"] = "changed"
    items: list[Any] = field(default_factory=list)
    properties: dict[str, Any] = field(default_factory=dict)
    anything: Any = None
    label: str | None = None


def _edit(stored: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return editing.fields(_Inputs, stored)


def _stored(submitted: dict[str, Any]) -> dict[str, Any]:
    found: list[str] = []
    inputs = editing._inputs(_Inputs, submitted, "Step 1", found)
    assert found == []
    return inputs


def _refused(submitted: dict[str, Any]) -> list[str]:
    found: list[str] = []
    editing._inputs(_Inputs, submitted, "Step 1", found)
    return found


# -- from a stored value to the form -------------------------------------------


def test_each_input_says_how_it_is_edited() -> None:
    edit = _edit({"note": "Daily/{{ trigger.date }}.md"})

    assert edit["note"] == {
        "control": "text",
        "required": True,
        "hint": "",
        "text": "Daily/{{ trigger.date }}.md",
    }
    assert (edit["count"]["control"], edit["count"]["hint"]) == (
        "text",
        "A whole number",
    )
    assert edit["on"]["hint"] == "yes or no"
    assert edit["when"] == {
        "control": "choice",
        "required": False,
        "hint": "",
        "text": "changed",
        "choices": ["changed", "always"],
    }
    assert edit["properties"]["control"] == "rows"
    # In the order of the block's fields.
    assert list(edit)[:3] == ["note", "count", "ratio"]


def test_an_input_that_is_not_stored_shows_its_default() -> None:
    edit = _edit({"note": "x"})

    assert edit["count"]["text"] == "1"
    assert edit["ratio"]["text"] == "0.5"
    assert edit["on"]["text"] == "no"
    assert edit["items"]["text"] == "[]"
    assert edit["properties"]["rows"] == []
    assert edit["anything"]["text"] == ""
    assert edit["label"]["text"] == ""


def test_a_typed_value_is_shown_as_text_and_a_template_as_it_is() -> None:
    edit = _edit(
        {
            "note": "x",
            "count": 4,
            "on": True,
            "items": ["a", "b"],
            "anything": {"a": 1},
        }
    )
    assert edit["count"]["text"] == "4"
    assert edit["on"]["text"] == "yes"
    assert edit["items"]["text"] == '["a", "b"]'
    assert edit["anything"]["text"] == '{"a": 1}'

    edit = _edit({"note": "x", "count": "{{ find.count }}", "items": "{{ t.sets }}"})
    assert edit["count"]["text"] == "{{ find.count }}"
    assert edit["items"]["text"] == "{{ t.sets }}"


def test_the_rows_of_a_mapping_keep_their_order_and_their_types() -> None:
    edit = _edit(
        {
            "note": "x",
            "properties": {
                "weight": "{{ trigger.weight }}",
                "type": "workout-set",
                "reps": 5,
                "label": "5",
                "tags": ["workout-set"],
                "done": True,
            },
        }
    )

    assert edit["properties"]["rows"] == [
        ["weight", "{{ trigger.weight }}"],
        ["type", "workout-set"],
        ["reps", "5"],
        # A text that reads as a number is in quotes, so that it stays one.
        ["label", '"5"'],
        ["tags", '["workout-set"]'],
        ["done", "true"],
    ]


# -- from the form to a stored value -------------------------------------------


def test_a_text_without_a_template_becomes_what_the_input_asks_for() -> None:
    assert _stored(
        {
            "note": " Daily/x.md ",
            "count": " 4 ",
            "ratio": "0.25",
            "on": "Yes",
            "when": "always",
            "items": '["a", 2]',
        }
    ) == {
        # A text is kept as it is typed.
        "note": " Daily/x.md ",
        "count": 4,
        "ratio": 0.25,
        "on": True,
        "when": "always",
        "items": ["a", 2],
    }


def test_a_text_with_a_template_is_kept_whatever_the_input_asks_for() -> None:
    submitted = {
        "note": "Daily/{{ trigger.date }}.md",
        "count": "{{ find.count }}",
        "on": "{{ run.manual and write.count > 0 }}",
        "items": "{{ trigger.sets }}",
    }

    assert _stored(submitted) == submitted


def test_what_the_form_shows_is_what_it_stores_again() -> None:
    stored = {
        "note": "Daily/{{ trigger.date }}.md",
        "count": 4,
        "ratio": 0.25,
        "on": True,
        "when": "always",
        "items": ["a", 2],
        "properties": {"reps": 5, "label": "5", "tags": ["x"], "t": "{{ a.b }}"},
        "anything": {"a": [1, None]},
        "label": "front",
    }
    edit = _edit(stored)
    submitted = {
        name: field.get("rows", field.get("text")) for name, field in edit.items()
    }

    assert _stored(submitted) == stored
    # The rows are stored in the order they were sent.
    assert list(_stored(submitted)["properties"]) == ["reps", "label", "tags", "t"]


def test_any_type_keeps_a_number_a_list_or_yes_and_no_as_such() -> None:
    assert _stored(
        {
            "note": "x",
            "properties": [
                ["reps", "5"],
                ["weight", "82.5"],
                ["text", "workout-set"],
                ["quoted", '"5"'],
                ["tags", '["a"]'],
                ["done", "true"],
                ["none", "null"],
                ["empty", ""],
            ],
            "anything": "12",
        }
    ) == {
        "note": "x",
        "properties": {
            "reps": 5,
            "weight": 82.5,
            "text": "workout-set",
            "quoted": "5",
            "tags": ["a"],
            "done": True,
            "none": None,
            "empty": "",
        },
        "anything": 12,
    }


def test_an_empty_text_leaves_an_input_to_its_default() -> None:
    assert _stored({"note": "x", "count": "", "items": " ", "label": ""}) == {
        "note": "x",
        # An input that can be empty is stored as empty.
        "label": None,
    }


def test_an_input_the_form_does_not_send_is_not_stored() -> None:
    assert _stored({"note": "x"}) == {"note": "x"}


def test_a_text_that_does_not_fit_is_refused_with_the_name_of_the_input() -> None:
    assert _refused({"note": "x", "count": "four"}) == [
        "Step 1: Count: “four” is not a whole number."
    ]
    assert _refused({"note": "x", "ratio": "half"}) == [
        "Step 1: Ratio: “half” is not a number."
    ]
    assert _refused({"note": "x", "on": "maybe"}) == [
        "Step 1: On: “maybe” is not yes or no."
    ]
    assert _refused({"note": "x", "when": "never"}) == [
        "Step 1: When: “never” is not one of the choices."
    ]
    assert _refused({"note": "x", "items": "a, b"}) == [
        "Step 1: Items: this must be a value from another step, or JSON."
    ]
    assert _refused({"note": "x", "items": '{"a": 1}'}) == [
        "Step 1: Items: this must be a list."
    ]


def test_rows_need_names_that_differ() -> None:
    assert _refused(
        {"note": "x", "properties": [["", "1"], ["a", "1"], ["a", "2"]]}
    ) == [
        "Step 1: Properties: a row has no name.",
        "Step 1: Properties: “a” is there twice.",
    ]
    assert _refused({"note": "x", "properties": {"a": 1}}) == [
        "Step 1: Properties: rows are expected."
    ]
    assert _refused({"note": 5}) == ["Step 1: Note: a text is expected."]


# -- a whole workflow ----------------------------------------------------------


def _form(**changes: Any) -> dict[str, Any]:
    form: dict[str, Any] = {
        "name": "Renamed",
        "command_name": "",
        "trigger": {"block": "fake.events", "params": {"events": '["a"]'}},
        "steps": [
            {
                "id": "each",
                "block": "core.for-each",
                "inputs": {"items": "{{ trigger.value }}"},
                "steps": [
                    {
                        "id": "inner",
                        "block": "fake.record",
                        "inputs": {"text": "{{ each.item }}"},
                    }
                ],
            },
            {"id": "after", "block": "fake.record", "inputs": {"text": "done"}},
        ],
    }
    form.update(changes)
    return form


def test_a_form_becomes_the_workflow_it_shows() -> None:
    registry = registry_of(Record(), ForEach())
    stored = workflow_of(
        Step(id="old", block="fake.record", inputs={"text": "x"}),
        enabled=False,
        command_hidden=True,
        recipe="some-recipe",
    )

    edited = workflow_from_form(registry, stored, _form())

    assert edited.name == "Renamed"
    assert edited.command_name is None
    assert edited.trigger.params == {"events": ["a"]}
    assert edited.steps == [
        Step(
            id="each",
            block="core.for-each",
            inputs={"items": "{{ trigger.value }}"},
            steps=[
                Step(
                    id="inner", block="fake.record", inputs={"text": "{{ each.item }}"}
                )
            ],
        ),
        Step(id="after", block="fake.record", inputs={"text": "done"}),
    ]
    assert problems(edited, registry) == []
    # What the form does not hold stays as it was.
    assert (edited.id, edited.user_id) == (stored.id, stored.user_id)
    assert (edited.enabled, edited.command_hidden, edited.recipe) == (
        False,
        True,
        "some-recipe",
    )
    # The stored workflow is not changed by the edit.
    assert stored.name == "Test workflow"


def test_the_command_can_be_given_a_text_of_its_own() -> None:
    registry = registry_of(Record(), ForEach())

    edited = workflow_from_form(registry, workflow_of(), _form(command_name=" Go "))

    assert edited.command_text == "Go"


def test_every_value_that_cannot_be_stored_is_named_with_its_step() -> None:
    registry = registry_of(Record(), ForEach())
    form = _form(name="  ")
    form["trigger"]["params"]["events"] = "not a list"
    form["steps"][0]["steps"][0]["inputs"]["text"] = 5

    with pytest.raises(FormError) as refused:
        workflow_from_form(registry, workflow_of(), form)

    assert refused.value.problems == [
        "The workflow needs a name.",
        "Fake events: Events: this must be a value from another step, or JSON.",
        "Step 1.1, Fake record: Text: a text is expected.",
    ]


def test_a_block_that_is_not_there_is_left_for_the_check_of_the_workflow() -> None:
    registry = registry_of(Record())
    form = _form(steps=[{"id": "one", "block": "fake.nothing", "inputs": {"a": "b"}}])

    edited = workflow_from_form(registry, workflow_of(), form)

    assert problems(edited, registry) == ["Step 1: there is no block “fake.nothing”."]


@pytest.mark.parametrize(
    "form",
    [None, [], {"name": "x"}, _form(steps="none"), _form(steps=[{"id": "x"}])],
)
def test_a_form_that_is_not_a_workflow_is_refused(form: Any) -> None:
    with pytest.raises(FormError):
        workflow_from_form(registry_of(Record()), workflow_of(), form)


def test_a_form_with_a_branch_becomes_a_step_with_both_sides() -> None:
    registry = registry_of(Record(), If())
    form = _form(
        steps=[
            {
                "id": "check",
                "block": "core.if",
                "inputs": {"condition": "{{ trigger.value == 'a' }}"},
                "steps": [
                    {"id": "yes", "block": "fake.record", "inputs": {"text": "y"}}
                ],
                "otherwise": [
                    {"id": "no", "block": "fake.record", "inputs": {"text": 5}}
                ],
            }
        ]
    )

    with pytest.raises(FormError) as refused:
        workflow_from_form(registry, workflow_of(), form)
    # The second side goes on counting where the first stopped.
    assert refused.value.problems == [
        "Step 1.2, Fake record: Text: a text is expected."
    ]

    form["steps"][0]["otherwise"][0]["inputs"]["text"] = "n"
    edited = workflow_from_form(registry, workflow_of(), form)

    (check,) = edited.steps
    assert [step.id for step in check.steps] == ["yes"]
    assert [step.id for step in check.otherwise] == ["no"]
    assert problems(edited, registry) == []
