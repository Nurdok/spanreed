from typing import Any

from spanreed.workflows.model import Step, Workflow, problems
from spanreed.workflows.testing import Record, registry_of, workflow_of


def _problems(*steps: Step, **fields: Any) -> list[str]:
    return problems(workflow_of(*steps, **fields), registry_of(Record()))


def test_a_workflow_survives_being_stored() -> None:
    workflow = workflow_of(
        Step(id="one", block="fake.record", inputs={"text": "{{ trigger.value }}"}),
        events=["a"],
        command_name="Do it",
        command_hidden=True,
        recipe="some-recipe",
    )
    assert Workflow.from_data(workflow.to_data()) == workflow


def test_a_field_this_version_does_not_know_is_ignored() -> None:
    data = workflow_of().to_data()
    data["added_later"] = 1
    assert Workflow.from_data(data) == workflow_of()


def test_the_command_is_named_after_the_workflow_unless_it_has_a_name() -> None:
    assert workflow_of().command_text == "Test workflow"
    assert workflow_of(command_name="Do it").command_text == "Do it"


def test_a_good_workflow_has_no_problems() -> None:
    assert not _problems(
        Step(id="one", block="fake.record", inputs={"text": "{{ trigger.value }}"}),
        Step(id="two", block="fake.record", inputs={"text": "{{ one.length }}"}),
        Step(id="three", block="fake.record", inputs={"text": "{{ run.manual }}"}),
    )


def test_an_unknown_trigger_is_a_problem() -> None:
    assert _problems(trigger="fake.nothing") == ["There is no trigger “fake.nothing”."]


def test_parameters_that_do_not_fit_the_trigger_are_a_problem() -> None:
    workflow = workflow_of()
    workflow.trigger.params = {"events": "not a list"}
    (problem,) = problems(workflow, registry_of())
    assert problem.startswith("Fake events: Events")


def test_an_unknown_block_is_a_problem() -> None:
    assert _problems(Step(id="one", block="fake.nothing")) == [
        "Step 1: there is no block “fake.nothing”."
    ]


def test_a_trigger_cannot_be_a_step() -> None:
    assert _problems(Step(id="one", block="fake.events")) == [
        "Step 1: there is no block “fake.events”."
    ]


def test_step_ids_must_be_usable_in_templates() -> None:
    step = Step(id="Step One", block="fake.record", inputs={"text": "a"})
    (problem,) = _problems(step)
    assert "the id “Step One”" in problem


def test_step_ids_must_be_free() -> None:
    steps = [
        Step(id="trigger", block="fake.record", inputs={"text": "a"}),
        Step(id="one", block="fake.record", inputs={"text": "a"}),
        Step(id="one", block="fake.record", inputs={"text": "a"}),
    ]
    assert _problems(*steps) == [
        "Step 1: the id “trigger” is already in use.",
        "Step 3: the id “one” is already in use.",
    ]


def test_an_input_the_block_does_not_have_is_a_problem() -> None:
    step = Step(id="one", block="fake.record", inputs={"text": "a", "colour": "red"})
    assert _problems(step) == ["Step 1, Fake record: there is no input “colour”."]


def test_a_missing_input_is_a_problem() -> None:
    assert _problems(Step(id="one", block="fake.record")) == [
        "Step 1, Fake record: Text: A value is required."
    ]


def test_a_value_of_the_wrong_type_is_a_problem() -> None:
    step = Step(id="one", block="fake.record", inputs={"text": 5})
    assert _problems(step) == ["Step 1, Fake record: Text: Expected text."]


def test_a_step_can_only_refer_to_what_comes_before_it() -> None:
    steps = [
        Step(id="one", block="fake.record", inputs={"text": "{{ two.text }}"}),
        Step(id="two", block="fake.record", inputs={"text": "{{ nothing }}"}),
    ]
    assert _problems(*steps) == [
        "Step 1, Fake record: “two” is not the trigger, the run or an earlier step.",
        "Step 2, Fake record: “nothing” is not the trigger, the run or an "
        "earlier step.",
    ]


def test_a_broken_template_is_a_problem() -> None:
    step = Step(id="one", block="fake.record", inputs={"text": "{{ trigger.value"})
    (problem,) = _problems(step)
    assert problem.startswith("Step 1, Fake record: ")
