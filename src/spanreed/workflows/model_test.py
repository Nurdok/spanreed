from typing import Any

from spanreed.workflows.builtin import ForEach, If
from spanreed.workflows.model import Step, Workflow, problems
from spanreed.workflows.testing import Record, registry_of, workflow_of


def _problems(*steps: Step, **fields: Any) -> list[str]:
    return problems(
        workflow_of(*steps, **fields), registry_of(Record(), ForEach(), If())
    )


def _record(step_id: str, text: str) -> Step:
    return Step(id=step_id, block="fake.record", inputs={"text": text})


def _for_each(step_id: str, *steps: Step) -> Step:
    return Step(
        id=step_id,
        block="core.for-each",
        inputs={"items": "{{ trigger.value }}"},
        steps=list(steps),
    )


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


# -- steps inside a loop -------------------------------------------------------


def test_a_workflow_with_steps_inside_a_loop_survives_being_stored() -> None:
    workflow = workflow_of(
        _for_each("each", _record("inner", "{{ each.item }}")),
        _record("after", "{{ each.count }}"),
    )

    stored = Workflow.from_data(workflow.to_data())

    assert stored == workflow
    assert isinstance(stored.steps[0].steps[0], Step)


def test_a_step_inside_a_loop_can_refer_to_the_loop_and_to_what_is_before() -> None:
    assert not _problems(
        _record("before", "{{ trigger.value }}"),
        _for_each(
            "each",
            _record("one", "{{ each.item }} {{ before.text }} {{ run.manual }}"),
            _record("two", "{{ one.length }}"),
        ),
        _record("after", "{{ each.count }}"),
    )


def test_a_step_after_a_loop_cannot_refer_to_a_step_inside_it() -> None:
    assert _problems(
        _for_each("each", _record("inner", "{{ each.item }}")),
        _record("after", "{{ inner.text }}"),
    ) == [
        "Step 2, Fake record: “inner” is not the trigger, the run or an earlier step."
    ]


def test_a_problem_inside_a_loop_says_where() -> None:
    assert _problems(
        _record("one", "a"),
        _for_each("each", _record("ok", "b"), _record("bad", "{{ later.text }}")),
        _record("later", "c"),
    ) == [
        "Step 2.2, Fake record: “later” is not the trigger, the run or an "
        "earlier step."
    ]


def test_an_id_is_used_once_in_the_whole_workflow() -> None:
    assert _problems(
        _for_each("each", _record("same", "a")),
        _record("same", "b"),
    ) == ["Step 2: the id “same” is already in use."]
    assert _problems(
        _record("same", "a"),
        _for_each("each", _record("same", "b")),
    ) == ["Step 2.1: the id “same” is already in use."]


def test_only_a_loop_can_hold_steps() -> None:
    step = _record("one", "a")
    step.steps = [_record("inner", "b")]

    assert _problems(step) == [
        "Step 1, Fake record: only a block that repeats steps can hold steps."
    ]


def test_a_loop_inside_a_loop_is_checked_too() -> None:
    assert not _problems(
        _for_each(
            "outer",
            Step(
                id="inner",
                block="core.for-each",
                inputs={"items": "{{ outer.item }}"},
                steps=[_record("leaf", "{{ outer.index }}.{{ inner.index }}")],
            ),
        )
    )


# -- steps inside a branch -----------------------------------------------------


def _if(step_id: str, then: list[Step], otherwise: list[Step]) -> Step:
    return Step(
        id=step_id,
        block="core.if",
        inputs={"condition": "{{ trigger.value == 'a' }}"},
        steps=then,
        otherwise=otherwise,
    )


def test_a_workflow_with_a_branch_survives_being_stored() -> None:
    workflow = workflow_of(
        _if("check", [_record("yes", "a")], [_record("no", "b")]),
    )

    stored = Workflow.from_data(workflow.to_data())

    assert stored == workflow
    assert isinstance(stored.steps[0].otherwise[0], Step)
    # A step stored before there were branches has no second list.
    data = workflow.to_data()
    del data["steps"][0]["otherwise"]
    assert Workflow.from_data(data).steps[0].otherwise == []


def test_each_side_of_a_branch_can_refer_to_what_is_before_and_to_the_branch() -> None:
    assert not _problems(
        _record("before", "x"),
        _if(
            "check",
            [
                _record("yes", "{{ before.text }} {{ check.yes }}"),
                _record("more", "{{ yes.text }}"),
            ],
            [_record("no", "{{ before.text }}")],
        ),
        _record("after", "{{ check.yes }}"),
    )


def test_a_side_of_a_branch_cannot_refer_to_the_other_side_or_be_referred_to_after() -> (
    None
):
    assert _problems(
        _if("check", [_record("yes", "a")], [_record("no", "{{ yes.text }}")]),
        _record("after", "{{ no.text }}"),
    ) == [
        "Step 1.2, Fake record: “yes” is not the trigger, the run or an earlier step.",
        "Step 2, Fake record: “no” is not the trigger, the run or an earlier step.",
    ]


def test_the_second_side_of_a_branch_goes_on_counting() -> None:
    assert _problems(
        _if(
            "check",
            [_record("one", "a"), _record("two", "b")],
            [_record("three", "c"), _record("four", "{{ nothing.text }}")],
        )
    ) == [
        "Step 1.4, Fake record: “nothing” is not the trigger, the run or an "
        "earlier step."
    ]


def test_a_step_cannot_be_named_by_a_word_that_templates_use() -> None:
    assert _problems(_record("if", "a"), _record("is_ok", "b")) == [
        "Step 1: the id “if” is a word that templates read as something else."
    ]


def test_only_a_branch_can_hold_otherwise_steps() -> None:
    plain = _record("one", "a")
    plain.otherwise = [_record("inner", "b")]
    loop = _for_each("each", _record("inside", "c"))
    loop.otherwise = [_record("other", "d")]

    assert _problems(plain, loop) == [
        "Step 1, Fake record: only a block that chooses between steps can hold "
        "“otherwise” steps.",
        "Step 2, For each: only a block that chooses between steps can hold "
        "“otherwise” steps.",
    ]
