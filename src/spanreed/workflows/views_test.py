from typing import Any

from spanreed.apis.obsidian_blocks import SetNoteProperties, WriteNote
from spanreed.workflows import views
from spanreed.workflows.builtin import ForEach, If
from spanreed.workflows.engine import WorkflowEngine
from spanreed.workflows.model import Step, Workflow
from spanreed.workflows.testing import registry_of, workflow_of


def test_a_mapping_is_shown_in_the_order_it_is_stored() -> None:
    engine = WorkflowEngine(registry_of(SetNoteProperties()))
    properties = {"steps": "{{ trigger.value }}", "heart-rate-avg": 71, "a": None}
    workflow = workflow_of(
        Step(
            id="write",
            block="obsidian.set-note-properties",
            inputs={"note": "Daily/2026-05-09.md", "properties": properties},
        )
    )

    (card,) = views.detail(engine, workflow, None)["steps"]

    fields = {field["name"]: field for field in card["fields"]}
    # A JSON object does not keep its order on the way to the page, so the
    # rows also come as a list.
    assert fields["properties"]["pairs"] == [
        ["steps", "{{ trigger.value }}"],
        ["heart-rate-avg", 71],
        ["a", None],
    ]
    assert fields["properties"]["value"] == properties
    assert "pairs" not in fields["note"]
    # The inputs that are not stored show their defaults.
    assert fields["write_when"]["value"] == "changed"
    assert card["outputs"] == ["note", "written", "count", "summary", "note_missing"]


# -- two workflows that write the same thing -----------------------------------


def _writer(workflow_id: str, name: str, *properties: str, **fields: Any) -> Workflow:
    """Return a workflow that sets these properties of the daily note."""
    step = Step(
        id="write",
        block="obsidian.set-note-properties",
        inputs={
            "note": "Daily/{{ trigger.date }}.md",
            "properties": {name: "x" for name in properties},
        },
    )
    return workflow_of(step, id=workflow_id, name=name, **fields)


def _warnings(workflow: Workflow, *others: Workflow) -> list[str]:
    engine = WorkflowEngine(registry_of(SetNoteProperties(), WriteNote(), ForEach()))
    shown: list[str] = views.detail(engine, workflow, None, others=others)["warnings"]
    return shown


def test_a_workflow_says_which_other_writes_the_same_property() -> None:
    health = _writer("wf_1", "Health", "steps", "weight")
    scale = _writer("wf_2", "Scale", "weight", "fat-mass")
    sleep = _writer("wf_3", "Sleep", "sleep-hours")

    assert _warnings(health, health, scale, sleep) == [
        "“Scale” also writes the property “weight” of Daily/{{ trigger.date }}.md."
    ]
    assert _warnings(sleep, health, scale, sleep) == []


def test_a_workflow_that_is_off_writes_nothing() -> None:
    health = _writer("wf_1", "Health", "weight")
    scale = _writer("wf_2", "Scale", "weight", enabled=False)

    assert _warnings(health, scale) == []


def test_the_same_property_of_another_note_is_another_thing() -> None:
    health = _writer("wf_1", "Health", "weight")
    other = _writer("wf_2", "Scale", "weight")
    other.steps[0].inputs["note"] = "Weekly/{{ trigger.date }}.md"

    assert _warnings(health, other) == []


def test_what_a_step_inside_a_loop_writes_counts_too() -> None:
    def notes(workflow_id: str, name: str) -> Workflow:
        inner = Step(
            id="note",
            block="obsidian.write-note",
            inputs={"path": "Sets/{{ each.item.name }}.md"},
        )
        loop = Step(
            id="each",
            block="core.for-each",
            inputs={"items": "{{ trigger.value }}"},
            steps=[inner],
        )
        return workflow_of(loop, id=workflow_id, name=name)

    assert _warnings(notes("wf_1", "First"), notes("wf_2", "Second")) == [
        "“Second” also writes the file Sets/{{ each.item.name }}.md."
    ]


# -- what a workflow can be tried out with -------------------------------------


def test_the_last_event_is_named_by_its_time_and_how_it_starts() -> None:
    engine = WorkflowEngine(registry_of())
    kept = {
        "at": "2026-05-09T07:42:00+00:00",
        "event": {"date": "2026-05-09", "weight": 80.5, "new": True, "sets": [1, 2]},
    }

    shown = views.detail(
        engine, workflow_of(), None, test_events={"last": kept, "now": False}
    )

    assert shown["test"] == {
        # The plain values, not the event itself: an event can be large.
        "last": {"at": kept["at"], "summary": "date 2026-05-09 · weight 80.5"},
        "now": False,
    }
    assert views.detail(engine, workflow_of(), None)["test"] == {
        "last": None,
        "now": False,
    }


def test_a_long_event_is_cut_short_in_its_summary() -> None:
    engine = WorkflowEngine(registry_of())
    kept = {"at": "2026-05-09T07:42:00+00:00", "event": {"records": "x" * 500}}

    shown = views.detail(
        engine, workflow_of(), None, test_events={"last": kept, "now": True}
    )

    assert len(shown["test"]["last"]["summary"]) == 120
    assert shown["test"]["now"] is True


# -- the cards of a branch -----------------------------------------------------


def test_a_branch_shows_both_of_its_sides_numbered_one_after_the_other() -> None:
    engine = WorkflowEngine(registry_of(If(), ForEach(), WriteNote()))

    def note(step_id: str) -> Step:
        return Step(id=step_id, block="obsidian.write-note", inputs={"path": step_id})

    check = Step(
        id="check",
        block="core.if",
        inputs={"condition": "{{ trigger.value }}"},
        steps=[note("a"), note("b")],
        otherwise=[note("c")],
    )
    workflow = workflow_of(check, note("d"))

    shown = views.detail(engine, workflow, None)
    card, after = shown["steps"]

    assert card["lanes"] == [
        {"key": "steps", "title": "If yes"},
        {"key": "otherwise", "title": "Otherwise"},
    ]
    assert [(c["id"], c["number"]) for c in card["steps"]] == [
        ("a", "1.1"),
        ("b", "1.2"),
    ]
    assert [(c["id"], c["number"]) for c in card["otherwise"]] == [("c", "1.3")]
    assert (after["number"], after["lanes"], after["otherwise"]) == ("2", [], [])
    # The list of workflows counts the steps of both sides.
    assert len(views.summary(engine, workflow, None)["apps"]) == 6
    # What a step on either side writes counts for the note about writes.
    other = workflow_of(note("c"), id="wf_2", name="Other")
    assert views.detail(engine, workflow, None, others=[other])["warnings"] == [
        "“Other” also writes the file c."
    ]
