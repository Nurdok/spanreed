"""Tests of taking a workflow out as a file, and bringing one in."""

import copy
import json
from typing import Any
from unittest.mock import patch

import pytest

from spanreed.apis.claude import ClaudePlugin
from spanreed.apis.audible import AudiblePlugin
from spanreed.apis.google_books import GoogleBooksPlugin
from spanreed.apis.obsidian import ObsidianPlugin
from spanreed.plugin import Plugin
from spanreed.plugins.health_connect import HealthConnectPlugin
from spanreed.plugins.hevy import HevyPlugin
from spanreed.plugins.timekiller import TimekillerPlugin
from spanreed.plugins.todoist import TodoistPlugin
from spanreed.plugins.withings import WithingsPlugin
from spanreed.plugins.workflows import WorkflowsPlugin
from spanreed.workflows import migrations, sharing
from spanreed.workflows.blocks import BlockRegistry
from spanreed.workflows.builtin import builtin_blocks
from spanreed.workflows.model import Step
from spanreed.workflows.recipes import Recipe, all_recipes
from spanreed.workflows.sharing import NotAWorkflow, exported, imported
from spanreed.workflows.testing import Record, registry_of, workflow_of


def _real_registry() -> BlockRegistry:
    """Return the blocks of the real plugins that publish any."""
    Plugin.reset_registry()
    return BlockRegistry.from_plugins(
        [
            WorkflowsPlugin(),
            TodoistPlugin(),
            ObsidianPlugin(),
            HealthConnectPlugin(),
            WithingsPlugin(),
            HevyPlugin(),
            GoogleBooksPlugin(),
            AudiblePlugin(),
            TimekillerPlugin(),
            ClaudePlugin(),
        ]
    )


def _file() -> dict[str, Any]:
    """Return the data of a small file that can be imported."""
    return {
        "name": "Tell me",
        "trigger": {"block": "fake.events", "params": {"events": ["monday"]}},
        "steps": [
            {"id": "one", "block": "fake.record", "inputs": {"text": "hello"}},
        ],
    }


# -- out ----------------------------------------------------------------------


@pytest.mark.parametrize("recipe", all_recipes().values(), ids=lambda r: r.id)
def test_a_recipe_that_goes_out_and_comes_back_is_the_same_workflow(
    recipe: Recipe,
) -> None:
    registry = _real_registry()
    workflow = recipe.workflow_for(4)

    data = exported(workflow, registry)
    again = imported(json.loads(json.dumps(data)), 7, registry, taken=set())

    assert again.trigger == workflow.trigger
    assert again.steps == workflow.steps
    assert (again.name, again.command_name, again.command_hidden) == (
        workflow.name,
        workflow.command_name,
        workflow.command_hidden,
    )
    # It is another user's own workflow now, and off until they look at it.
    assert (again.user_id, again.enabled, again.recipe) == (7, False, None)
    assert again.id != workflow.id
    # What went out names the plugins it needs, as its recipe does.
    assert data["requires"] == sorted(recipe.requires)
    # And it is a recipe itself: the file could ship.
    assert Recipe(**data).workflow_for(4).steps == workflow.steps


def test_a_file_holds_the_workflow_and_nothing_about_its_user() -> None:
    registry = registry_of(Record())
    workflow = workflow_of(
        Step(id="one", block="fake.record", inputs={"text": "{{ trigger.value }}"}),
        name="Morning: tell me!",
        events=["monday"],
        command_name="Tell",
        command_hidden=True,
    )

    data = exported(workflow, registry)

    assert data == {
        "id": "morning-tell-me",
        "name": "Morning: tell me!",
        "description": "",
        # The blocks of the plugin that every user has need no naming.
        "requires": ["fake"],
        "command_name": "Tell",
        "command_hidden": True,
        "trigger": {"block": "fake.events", "params": {"events": ["monday"]}},
        # A step that holds no steps has no lists of them.
        "steps": [
            {
                "id": "one",
                "block": "fake.record",
                "inputs": {"text": "{{ trigger.value }}"},
            }
        ],
    }
    assert "user_id" not in json.dumps(data) and workflow.id not in json.dumps(data)


def test_a_file_has_no_command_fields_for_a_workflow_that_set_none() -> None:
    data = exported(workflow_of(name="Plain"), registry_of())

    assert "command_name" not in data and "command_hidden" not in data


def test_a_name_without_letters_still_gives_a_file_a_name() -> None:
    assert sharing.slug("→ ✓") == "workflow"
    assert sharing.slug("Health data → daily note") == "health-data-daily-note"
    assert len(sharing.slug("word " * 40)) <= 60


# -- in -----------------------------------------------------------------------


def test_a_file_comes_in_as_a_workflow_that_is_off() -> None:
    workflow = imported(_file(), 4, registry_of(Record()), taken={"Other"})

    assert (workflow.name, workflow.user_id, workflow.enabled) == ("Tell me", 4, False)
    assert workflow.id.startswith("wf_")
    assert workflow.steps == [
        Step(id="one", block="fake.record", inputs={"text": "hello"})
    ]
    assert workflow.recipe is None


def test_a_workflow_that_comes_in_gets_a_name_of_its_own() -> None:
    registry = registry_of(Record())

    assert imported(_file(), 4, registry, taken={"Tell me"}).name == "Tell me (2)"
    assert (
        imported(_file(), 4, registry, taken={"Tell me", "Tell me (2)"}).name
        == "Tell me (3)"
    )


def test_the_steps_inside_a_step_come_in_with_it() -> None:
    registry = registry_of(Record())
    for block in builtin_blocks():
        registry.register(block, registry.plugin_of("fake.record"))
    data = _file()
    data["steps"] = [
        {
            "id": "check",
            "block": "core.if",
            "inputs": {"condition": True},
            "steps": [{"id": "yes", "block": "fake.record", "inputs": {"text": "y"}}],
            "otherwise": [
                {"id": "no", "block": "fake.record", "inputs": {"text": "n"}}
            ],
        }
    ]

    (step,) = imported(data, 4, registry, taken=set()).steps

    assert [inside.id for inside in step.steps] == ["yes"]
    assert [inside.id for inside in step.otherwise] == ["no"]


def _broken(change: Any) -> dict[str, Any]:
    data = copy.deepcopy(_file())
    change(data)
    return data


@pytest.mark.parametrize(
    "data",
    [
        None,
        ["a list"],
        "a text",
        {},
        _broken(lambda d: d.pop("name")),
        _broken(lambda d: d.update(name="  ")),
        _broken(lambda d: d.update(name=5)),
        _broken(lambda d: d.pop("trigger")),
        _broken(lambda d: d.update(trigger="fake.events")),
        _broken(lambda d: d["trigger"].update(params=["a list"])),
        _broken(lambda d: d.pop("steps")),
        _broken(lambda d: d.update(steps={"one": {}})),
        _broken(lambda d: d.update(steps=["a text"])),
        _broken(lambda d: d["steps"][0].pop("block")),
        _broken(lambda d: d["steps"][0].update(inputs="a text")),
        _broken(lambda d: d["steps"][0].update(steps="a text")),
        _broken(lambda d: d.update(command_name=5)),
    ],
)
def test_what_is_not_the_file_of_a_workflow_does_not_come_in(data: Any) -> None:
    with pytest.raises(NotAWorkflow) as error:
        imported(data, 4, registry_of(Record()), taken=set())

    (problem,) = error.value.problems
    assert problem.startswith("This is not the file of a workflow.")


def test_a_workflow_that_this_spanreed_cannot_run_does_not_come_in() -> None:
    data = _file()
    data["steps"].append(
        {"id": "two", "block": "elsewhere.block", "inputs": {"text": "{{ nobody.x }}"}}
    )
    data["steps"].append({"id": "one", "block": "fake.record", "inputs": {}})

    with pytest.raises(NotAWorkflow) as error:
        imported(data, 4, registry_of(Record()), taken=set())

    problems = error.value.problems
    assert "Step 2: there is no block “elsewhere.block”." in problems
    assert any("the id “one” is already in use" in problem for problem in problems)
    assert any(problem.startswith("Step 3, Fake record:") for problem in problems)


def test_a_file_from_before_a_plugin_was_renamed_comes_in_with_the_ids_of_now() -> None:
    registry = _real_registry()
    workflow = all_recipes()["lit-notes-add-book-note"].workflow_for(4)
    # As "Add book note" went out while Lit Notes published the search.
    file = json.loads(
        json.dumps(exported(workflow, registry))
        .replace('"google-books.search-books"', '"lit-notes.search-books"')
        .replace('"google-books"', '"lit-notes"')
    )
    assert file["requires"] == ["lit-notes", "obsidian"]
    assert "lit-notes.search-books" in json.dumps(file["steps"])

    again = imported(file, 7, registry, taken=set())

    assert again.steps == workflow.steps
    # What goes out again names the plugin that publishes the search now.
    assert exported(again, registry)["requires"] == ["google-books", "obsidian"]


def test_a_trigger_from_before_a_plugin_was_renamed_comes_in_too() -> None:
    renamed = migrations.RenamedPlugin(
        canonical_name="old",
        name="Old",
        new_canonical_name="fake",
        new_name="Fake",
        blocks={"old.events": "fake.events", "old.record": "fake.record"},
    )
    data = _file()
    data["trigger"]["block"] = "old.events"
    data["steps"] = [
        {
            "id": "each",
            "block": "core.for-each",
            "inputs": {"items": "{{ [1] }}"},
            "steps": [{"id": "one", "block": "old.record", "inputs": {"text": "x"}}],
        }
    ]
    registry = registry_of(Record(), *builtin_blocks())

    with patch.object(migrations, "RENAMED_PLUGINS", [renamed]):
        workflow = imported(data, 4, registry, taken=set())

    assert workflow.trigger.block == "fake.events"
    assert workflow.steps[0].steps[0].block == "fake.record"


def test_a_file_with_too_many_steps_does_not_come_in() -> None:
    data = _file()
    data["steps"] = [
        {"id": f"s{n}", "block": "fake.record", "inputs": {"text": "x"}}
        for n in range(sharing.MAX_STEPS + 1)
    ]

    with pytest.raises(NotAWorkflow) as error:
        imported(data, 4, registry_of(Record()), taken=set())

    assert "more than 200 steps" in error.value.problems[0]


def test_a_long_name_is_cut_and_what_is_not_known_is_left_out() -> None:
    data = _file()
    data.update(name="n" * 300, id="some-id", user_id=1, enabled=True, recipe="x")

    workflow = imported(data, 4, registry_of(Record()), taken=set())

    assert len(workflow.name) == sharing.MAX_NAME
    assert (workflow.user_id, workflow.enabled, workflow.recipe) == (4, False, None)
