from spanreed.apis.obsidian_blocks import SetNoteProperties
from spanreed.workflows import views
from spanreed.workflows.engine import WorkflowEngine
from spanreed.workflows.model import Step
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
