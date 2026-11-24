import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch

from spanreed.interaction import commands
from spanreed.plugin import Plugin
from spanreed.plugins.todoist import TodoistPlugin
from spanreed.plugins.workflows import WorkflowsPlugin
from spanreed.test_utils import async_return_true
from spanreed.web import routes
from spanreed.web import routes_test
from spanreed.web.routes_test import (
    H,
    OTHER_USER_ID,
    USER_ID,
    _app,
    _login,
    _user,
    _wait_for,
)
from spanreed.workflows.blocks import BlockRegistry
from spanreed.workflows.engine import WorkflowEngine
from spanreed.workflows.model import Workflow
from spanreed.workflows.recipes import all_recipes

NO_OVERDUE = "Daily chores never overdue"


def setup_function() -> None:
    routes_test.setup_function()
    Plugin.reset_registry()


@contextlib.asynccontextmanager
async def _workflows_app(
    running: bool = True,
) -> AsyncIterator[tuple[Any, WorkflowEngine, Any]]:
    """Serve the web UI with a workflow engine that keeps nothing in Redis.

    Todoist is set up and has no overdue tasks. Schedules are not due, so
    only what a test starts by hand runs.
    """
    async with _app() as (client, fake, telegram):
        plugin = WorkflowsPlugin()
        engine = WorkflowEngine(BlockRegistry.from_plugins([plugin, TodoistPlugin()]))
        if running:
            plugin._engine = engine
        with patch("spanreed.workflows.store.redis_api", new=fake), patch(
            "spanreed.workflows.store.StoredTriggerState.get",
            new=AsyncMock(return_value="2999-01-01T00:00:00+00:00"),
        ), patch.object(Plugin, "is_registered", async_return_true), patch(
            "spanreed.apis.todoist_blocks.Todoist", autospec=True
        ) as todoist:
            todoist.for_user.return_value.get_overdue_tasks_with_label = AsyncMock(
                return_value=[]
            )
            try:
                yield client, engine, telegram
            finally:
                await engine.stop()


async def _install(engine: WorkflowEngine, user_id: int = USER_ID) -> Workflow:
    return await engine.install(_user(user_id), all_recipes()["todoist-no-overdue"])


async def _json(response: Any) -> Any:
    return await response.get_json()


def _shown(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return what the cards show of some inputs, without how they are edited."""
    return [{k: v for k, v in field.items() if k != "edit"} for field in fields]


def _form_of(shown: dict[str, Any]) -> dict[str, Any]:
    """Return what the editor sends for a workflow that nothing was changed in."""

    def inputs(card: dict[str, Any]) -> dict[str, Any]:
        return {
            field["name"]: field["edit"].get("rows", field["edit"].get("text"))
            for field in card["fields"]
        }

    def steps(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "id": card["id"],
                "block": card["block"],
                "inputs": inputs(card),
                "steps": steps(card["steps"]),
            }
            for card in cards
        ]

    return {
        "name": shown["name"],
        "command_name": shown["command_name"] or "",
        "trigger": {
            "block": shown["trigger"]["block"],
            "params": inputs(shown["trigger"]),
        },
        "steps": steps(shown["steps"]),
    }


def test_the_list_has_only_the_workflows_of_the_user() -> None:
    async def run() -> None:
        async with _workflows_app() as (client, engine, _):
            mine = await _install(engine)
            await _install(engine, OTHER_USER_ID)
            assert (await client.get("/app/api/workflows")).status_code == 401
            await _login(client)

            listed = (await _json(await client.get("/app/api/workflows")))["workflows"]

            (workflow,) = listed
            assert workflow["id"] == mine.id
            assert workflow["name"] == NO_OVERDUE
            assert workflow["enabled"] is True
            assert workflow["problem"] is None
            assert workflow["last_run"] is None
            # The trigger, then the four steps.
            assert workflow["apps"] == [
                {"app": "Spanreed", "kind": "trigger"},
                {"app": "Todoist", "kind": "action"},
                {"app": "Spanreed", "kind": "logic"},
                {"app": "Todoist", "kind": "action"},
                {"app": "Spanreed", "kind": "ask"},
            ]

    asyncio.run(run())


def test_a_workflow_is_shown_as_its_trigger_and_its_steps() -> None:
    async def run() -> None:
        async with _workflows_app() as (client, engine, _):
            workflow = await _install(engine)
            await _login(client)

            shown = await _json(await client.get(f"/app/api/workflows/{workflow.id}"))

            assert shown["problems"] == []
            assert shown["by_hand"] is True
            # Todoist No Overdue had no command, so the recipe hides it.
            assert shown["command"] == {"text": NO_OVERDUE, "hidden": True}
            trigger = shown["trigger"]
            assert (trigger["title"], trigger["app"], trigger["kind"]) == (
                "On a schedule",
                "Spanreed",
                "trigger",
            )
            assert _shown(trigger["fields"]) == [
                {"name": "every", "label": "Every", "value": 4},
                {"name": "unit", "label": "Unit", "value": "hours"},
            ]
            assert trigger["outputs"] == ["date", "time"]
            # Each input also says how the editor edits it.
            assert [field["edit"] for field in trigger["fields"]] == [
                {
                    "control": "text",
                    "required": False,
                    "hint": "A whole number",
                    "text": "4",
                },
                {
                    "control": "choice",
                    "required": False,
                    "hint": "",
                    "text": "hours",
                    "choices": ["minutes", "hours", "days"],
                },
            ]
            assert shown["run_outputs"] == ["id", "manual"]

            find, check, move, tell = shown["steps"]
            assert [step["number"] for step in shown["steps"]] == ["1", "2", "3", "4"]
            assert (find["id"], find["title"], find["app"]) == (
                "find",
                "Find tasks",
                "Todoist",
            )
            assert find["outputs"] == ["tasks", "count"]
            # Inputs are shown as they are stored, templates and all.
            assert _shown(check["fields"]) == [
                {
                    "name": "condition",
                    "label": "Condition",
                    "value": "{{ find.count > 0 }}",
                }
            ]
            assert (check["kind"], tell["kind"]) == ("logic", "ask")
            assert move["fields"][0]["value"] == "{{ find.tasks }}"

    asyncio.run(run())


def test_an_input_that_is_not_stored_shows_its_default() -> None:
    async def run() -> None:
        async with _workflows_app() as (client, engine, _):
            workflow = await _install(engine)
            del workflow.steps[0].inputs["due"]
            await engine.save(workflow)
            await _login(client)

            shown = await _json(await client.get(f"/app/api/workflows/{workflow.id}"))

            assert _shown(shown["steps"][0]["fields"]) == [
                {"name": "label", "label": "Label", "value": "spanreed/no-overdue"},
                {"name": "due", "label": "Due", "value": "any"},
            ]

    asyncio.run(run())


def test_a_workflow_with_a_problem_says_so_and_cannot_be_started() -> None:
    async def run() -> None:
        async with _workflows_app() as (client, engine, _):
            workflow = await _install(engine)
            workflow.steps[0].block = "todoist.gone"
            await engine.save(workflow)
            await _login(client)

            listed = (await _json(await client.get("/app/api/workflows")))["workflows"]
            shown = await _json(await client.get(f"/app/api/workflows/{workflow.id}"))
            started = await client.post(
                f"/app/api/workflows/{workflow.id}/run", headers=H
            )

            assert listed[0]["problem"] == "Step 1: there is no block “todoist.gone”."
            assert shown["problems"][0] == listed[0]["problem"]
            assert shown["command"] is None
            card = shown["steps"][0]
            assert (card["title"], card["app"], card["missing"]) == (
                "todoist.gone",
                "",
                True,
            )
            assert started.status_code == 400

    asyncio.run(run())


def test_a_user_cannot_reach_the_workflow_of_another_user() -> None:
    async def run() -> None:
        async with _workflows_app() as (client, engine, _):
            theirs = await _install(engine, OTHER_USER_ID)
            await _login(client)
            base = f"/app/api/workflows/{theirs.id}"

            responses = [
                await client.get(base),
                await client.get(f"{base}/runs"),
                await client.post(
                    f"{base}/enabled", json={"enabled": False}, headers=H
                ),
                await client.post(f"{base}/command", json={"hidden": False}, headers=H),
                await client.post(f"{base}/run", headers=H),
                await client.get("/app/api/workflows/wf_nothing"),
            ]

            assert [response.status_code for response in responses] == [404] * 6
            stored = await engine.store.get(theirs.id)
            assert stored is not None and stored.enabled and stored.command_hidden

    asyncio.run(run())


def test_the_switch_turns_a_workflow_off_and_on() -> None:
    async def run() -> None:
        async with _workflows_app() as (client, engine, _):
            workflow = await _install(engine)
            await _login(client)
            url = f"/app/api/workflows/{workflow.id}/enabled"

            # A write needs the header that a cross-site page cannot send.
            assert (await client.post(url, json={"enabled": False})).status_code == 403
            assert (await client.post(url, json={}, headers=H)).status_code == 400

            off = await _json(
                await client.post(url, json={"enabled": False}, headers=H)
            )
            assert off["enabled"] is False
            stored = await engine.store.get(workflow.id)
            assert stored is not None and not stored.enabled
            assert workflow.id not in engine._sources

            on = await _json(await client.post(url, json={"enabled": True}, headers=H))
            assert on["enabled"] is True
            assert workflow.id in engine._sources

    asyncio.run(run())


def test_the_command_can_be_shown_and_hidden() -> None:
    async def run() -> None:
        async with _workflows_app() as (client, engine, _):
            workflow = await _install(engine)
            await _login(client)
            url = f"/app/api/workflows/{workflow.id}/command"
            command_id = f"workflow.{workflow.id}"
            assert commands.get_command(command_id) is None

            shown = await _json(
                await client.post(url, json={"hidden": False}, headers=H)
            )

            assert shown["command"] == {"text": NO_OVERDUE, "hidden": False}
            command = commands.get_command(command_id)
            assert command is not None and command.user_id == USER_ID

            await client.post(url, json={"hidden": True}, headers=H)
            assert commands.get_command(command_id) is None
            assert (await client.post(url, json={}, headers=H)).status_code == 400

    asyncio.run(run())


def test_run_now_runs_the_workflow_as_a_conversation_on_the_page() -> None:
    async def run() -> None:
        async with _workflows_app() as (client, engine, telegram):
            workflow = await _install(engine)
            await _login(client)
            state = await _json(await client.get("/app/api/state"))

            started = await client.post(
                f"/app/api/workflows/{workflow.id}/run", headers=H
            )
            assert started.status_code == 200
            flow_id = (await _json(started))["flow_id"]
            await _wait_for(lambda: routes.active_web_flow_id(USER_ID) is None)

            events = await _json(
                await client.get(f"/app/api/events?after={state['seq']}")
            )
            said = [
                (e["type"], e.get("title") or e.get("html") or e.get("flow_id"))
                for e in events["events"]
            ]
            closing = f"{NO_OVERDUE}: done. The run ended at step 2, Only continue if."
            assert said == [
                ("flow-started", NO_OVERDUE),
                ("message", closing),
                ("flow-finished", flow_id),
            ]
            # Started from the page, so nothing went to Telegram.
            assert telegram.sent == []

            # The run is in the history, and the list shows it as the last.
            runs = (
                await _json(await client.get(f"/app/api/workflows/{workflow.id}/runs"))
            )["runs"]
            (run_,) = runs
            assert run_["manual"] is True
            assert run_["outcome"] == "stopped at step 2, Only continue if."
            assert [step["title"] for step in run_["steps"]] == [
                "Find tasks",
                "Only continue if",
            ]
            assert run_["steps"][0]["outputs"] == {"tasks": [], "count": 0}
            listed = (await _json(await client.get("/app/api/workflows")))["workflows"]
            assert listed[0]["last_run"]["id"] == run_["id"]

    asyncio.run(run())


# -- editing -------------------------------------------------------------------


def test_saving_what_is_shown_changes_nothing() -> None:
    async def run() -> None:
        async with _workflows_app() as (client, engine, _):
            workflow = await _install(engine)
            before = workflow.to_data()
            await _login(client)
            url = f"/app/api/workflows/{workflow.id}"
            shown = await _json(await client.get(url))

            response = await client.put(url, json=_form_of(shown), headers=H)

            assert response.status_code == 200
            stored = await engine.store.get(workflow.id)
            assert stored is not None
            assert stored.to_data() == before

    asyncio.run(run())


def test_the_editor_changes_the_values_of_a_workflow() -> None:
    async def run() -> None:
        async with _workflows_app() as (client, engine, _):
            workflow = await _install(engine)
            await _login(client)
            url = f"/app/api/workflows/{workflow.id}"
            form = _form_of(await _json(await client.get(url)))
            form["name"] = "Chores stay due"
            form["trigger"]["params"]["every"] = "6"
            form["steps"][0]["inputs"]["label"] = "chores"
            form["steps"][3]["inputs"]["message"] = "Moved {{ find.count }} tasks."

            response = await client.put(url, json=form, headers=H)

            assert response.status_code == 200
            shown = await _json(response)
            assert shown["name"] == "Chores stay due"
            assert shown["trigger"]["fields"][0]["value"] == 6
            stored = await engine.store.get(workflow.id)
            assert stored is not None
            assert stored.trigger.params == {"every": 6, "unit": "hours"}
            assert stored.steps[0].inputs == {"label": "chores", "due": "overdue"}
            assert stored.steps[3].inputs == {
                "message": "Moved {{ find.count }} tasks."
            }
            # It still came from the recipe, and it is still on.
            assert (stored.recipe, stored.enabled) == ("todoist-no-overdue", True)

    asyncio.run(run())


def test_a_value_that_does_not_fit_is_refused_and_nothing_is_stored() -> None:
    async def run() -> None:
        async with _workflows_app() as (client, engine, _):
            workflow = await _install(engine)
            before = workflow.to_data()
            await _login(client)
            url = f"/app/api/workflows/{workflow.id}"
            form = _form_of(await _json(await client.get(url)))
            form["trigger"]["params"]["every"] = "often"
            form["steps"][0]["inputs"]["label"] = "chores"

            response = await client.put(url, json=form, headers=H)

            assert response.status_code == 400
            assert (await _json(response))["problems"] == [
                "On a schedule: Every: “often” is not a whole number."
            ]
            stored = await engine.store.get(workflow.id)
            assert stored is not None and stored.to_data() == before

    asyncio.run(run())


def test_a_workflow_that_could_not_run_is_refused_with_what_is_wrong() -> None:
    async def run() -> None:
        async with _workflows_app() as (client, engine, _):
            workflow = await _install(engine)
            await _login(client)
            url = f"/app/api/workflows/{workflow.id}"
            form = _form_of(await _json(await client.get(url)))
            form["trigger"]["params"]["every"] = "0"
            form["steps"][1]["inputs"]["condition"] = "{{ later.count > 0 }}"

            response = await client.put(url, json=form, headers=H)

            assert response.status_code == 400
            assert (await _json(response))["problems"] == [
                "On a schedule: A trigger repeats every 1 or more.",
                "Step 2, Only continue if: “later” is not the trigger, the run "
                "or an earlier step.",
            ]

    asyncio.run(run())


def test_a_user_cannot_edit_the_workflow_of_another_user() -> None:
    async def run() -> None:
        async with _workflows_app() as (client, engine, _):
            theirs = await _install(engine, OTHER_USER_ID)
            url = f"/app/api/workflows/{theirs.id}"
            assert (await client.put(url, json={}, headers=H)).status_code == 401
            await _login(client)

            response = await client.put(url, json={"name": "Mine now"}, headers=H)

            assert response.status_code == 404
            stored = await engine.store.get(theirs.id)
            assert stored is not None and stored.name == NO_OVERDUE

    asyncio.run(run())


def test_an_edit_takes_effect_at_once() -> None:
    async def run() -> None:
        async with _workflows_app() as (client, engine, _):
            workflow = await _install(engine)
            workflow.command_hidden = False
            await engine.save(workflow)
            await _login(client)
            url = f"/app/api/workflows/{workflow.id}"
            form = _form_of(await _json(await client.get(url)))
            form["command_name"] = "Move my chores"

            await client.put(url, json=form, headers=H)

            user = _user(USER_ID)
            user.plugins = ["workflows"]
            texts = [command.text for command in commands.commands_for_user(user)]
            assert texts == ["Move my chores"]

    asyncio.run(run())


def test_the_editor_adds_removes_and_moves_steps() -> None:
    async def run() -> None:
        async with _workflows_app() as (client, engine, _):
            workflow = await _install(engine)
            await _login(client)
            url = f"/app/api/workflows/{workflow.id}"
            form = _form_of(await _json(await client.get(url)))
            catalog = await _json(await client.get("/app/api/workflows/catalog"))
            (message,) = [
                b for b in catalog["blocks"] if b["block"] == "core.send-message"
            ]
            find, check, move, tell = form["steps"]
            # A new first step, made from the card that the catalog gives.
            hello = {
                "id": "hello",
                "block": message["block"],
                "inputs": {
                    f["name"]: f["edit"]["text"] for f in message["card"]["fields"]
                },
                "steps": [],
            }
            hello["inputs"]["message"] = "Looking for chores."
            # The message about what moved goes; the move stays last.
            form["steps"] = [hello, find, check, move]

            response = await client.put(url, json=form, headers=H)

            assert response.status_code == 200
            shown = await _json(response)
            assert [(s["number"], s["id"]) for s in shown["steps"]] == [
                ("1", "hello"),
                ("2", "find"),
                ("3", "any"),
                ("4", "move"),
            ]
            stored = await engine.store.get(workflow.id)
            assert stored is not None
            assert stored.steps[0].inputs == {"message": "Looking for chores."}

    asyncio.run(run())


def test_a_step_moved_before_what_it_needs_is_refused() -> None:
    async def run() -> None:
        async with _workflows_app() as (client, engine, _):
            workflow = await _install(engine)
            await _login(client)
            url = f"/app/api/workflows/{workflow.id}"
            form = _form_of(await _json(await client.get(url)))
            find, check, move, tell = form["steps"]
            form["steps"] = [check, find, move, tell]

            response = await client.put(url, json=form, headers=H)

            assert response.status_code == 400
            assert (await _json(response))["problems"] == [
                "Step 1, Only continue if: “find” is not the trigger, the run "
                "or an earlier step."
            ]

    asyncio.run(run())


def test_the_trigger_of_a_workflow_cannot_be_changed() -> None:
    async def run() -> None:
        async with _workflows_app() as (client, engine, _):
            workflow = await _install(engine)
            await _login(client)
            url = f"/app/api/workflows/{workflow.id}"
            form = _form_of(await _json(await client.get(url)))
            form["trigger"] = {"block": "core.other-trigger", "params": {}}

            response = await client.put(url, json=form, headers=H)

            assert response.status_code == 400
            assert (await _json(response))["problems"][0].startswith(
                "The trigger of a workflow cannot be changed."
            )

    asyncio.run(run())


# -- trying a workflow out -----------------------------------------------------


def test_a_workflow_that_has_not_run_can_be_tested_with_an_event_made_now() -> None:
    async def run() -> None:
        async with _workflows_app() as (client, engine, _):
            workflow = await _install(engine)
            await _login(client)
            url = f"/app/api/workflows/{workflow.id}"
            shown = await _json(await client.get(url))
            assert shown["test"] == {"last": None, "now": True, "asks": False}
            form = _form_of(shown)
            # Not saved: the test runs what the editor holds.
            form["steps"][1]["inputs"]["condition"] = "{{ find.count == 0 }}"
            form["steps"] = form["steps"][:2]

            response = await client.post(
                f"{url}/test", json={"workflow": form, "event": "now"}, headers=H
            )

            assert response.status_code == 200
            ran = (await _json(response))["run"]
            assert (ran["status"], ran["test"], ran["outcome"]) == (
                "finished",
                True,
                "finished.",
            )
            assert [step["id"] for step in ran["steps"]] == ["find", "any"]
            stored = await engine.store.get(workflow.id)
            assert stored is not None and len(stored.steps) == 4
            # The test is in the history.
            runs = (await _json(await client.get(f"{url}/runs")))["runs"]
            assert [r.get("test") for r in runs] == [True]

    asyncio.run(run())


def test_a_workflow_is_tested_with_the_event_of_its_last_run() -> None:
    async def run() -> None:
        async with _workflows_app() as (client, engine, _):
            workflow = await _install(engine)
            await _login(client)
            url = f"/app/api/workflows/{workflow.id}"
            await engine.run_by_hand(workflow.id, _user(USER_ID))
            shown = await _json(await client.get(url))
            last = shown["test"]["last"]
            assert last["summary"].startswith("date ")

            response = await client.post(
                f"{url}/test",
                json={"workflow": _form_of(shown), "event": "last"},
                headers=H,
            )

            assert response.status_code == 200
            ran = (await _json(response))["run"]
            assert ran["outcome"] == "stopped at step 2, Only continue if."
            # The test did not become the last event.
            again = await _json(await client.get(url))
            assert again["test"]["last"] == last

    asyncio.run(run())


def test_a_test_needs_an_event_and_a_workflow_that_can_run() -> None:
    async def run() -> None:
        async with _workflows_app() as (client, engine, _):
            workflow = await _install(engine)
            theirs = await _install(engine, OTHER_USER_ID)
            await _login(client)
            url = f"/app/api/workflows/{workflow.id}"
            form = _form_of(await _json(await client.get(url)))

            async def test(body: Any, target: str = url) -> Any:
                return await client.post(f"{target}/test", json=body, headers=H)

            # It has not run, so there is no last event.
            response = await test({"workflow": form, "event": "last"})
            assert response.status_code == 409
            assert (await test({"workflow": form})).status_code == 400
            form["steps"][1]["inputs"]["condition"] = "{{ later.count }}"
            response = await test({"workflow": form, "event": "now"})
            assert response.status_code == 400
            assert (await _json(response))["problems"] == [
                "Step 2, Only continue if: “later” is not the trigger, the run "
                "or an earlier step."
            ]
            response = await test(
                {"workflow": form, "event": "now"},
                f"/app/api/workflows/{theirs.id}",
            )
            assert response.status_code == 404
            assert await engine.store.recent_runs(workflow.id, 5) == []

    asyncio.run(run())


def test_a_workflow_says_what_another_of_the_user_writes_too() -> None:
    async def run() -> None:
        async with _workflows_app() as (client, engine, _):
            workflow = await _install(engine)
            await _login(client)

            shown = await _json(await client.get(f"/app/api/workflows/{workflow.id}"))

            # Nothing in this recipe writes what another workflow could.
            assert shown["warnings"] == []

    asyncio.run(run())


# -- what a workflow can be made of --------------------------------------------


def test_the_catalog_lists_the_blocks_with_a_card_for_a_new_step() -> None:
    async def run() -> None:
        async with _workflows_app() as (client, engine, _):
            assert (await client.get("/app/api/workflows/catalog")).status_code == 401
            await _login(client)

            catalog = await _json(await client.get("/app/api/workflows/catalog"))

            blocks = {block["block"]: block for block in catalog["blocks"]}
            assert list(blocks) == [
                "core.schedule",
                "core.at-time",
                "core.random-schedule",
                "core.continue-if",
                "core.if",
                "core.for-each",
                "core.send-message",
                "core.notify",
                "core.command",
                "core.ask-choice",
                "core.ask-each",
                "core.ask-text",
                "core.ask-list",
                "core.stop",
                "core.called",
                "core.call-workflow",
                "core.run-one",
                "core.remember",
                "core.recall",
                "core.forget",
                "core.download-file",
                "core.send-file",
                "todoist.find-tasks",
                "todoist.set-due-date",
                "todoist.read-tally",
                "todoist.write-tally",
            ]
            find = blocks["todoist.find-tasks"]
            assert (find["title"], find["app"], find["kind"]) == (
                "Find tasks",
                "Todoist",
                "action",
            )
            assert find["connected"] is True
            assert find["description"]
            # The card of a new step: every input at its default.
            assert [
                (f["name"], f["edit"]["control"], f["edit"]["required"])
                for f in find["card"]["fields"]
            ] == [("label", "text", True), ("due", "choice", False)]
            assert find["card"]["fields"][1]["edit"]["text"] == "any"
            assert find["card"]["outputs"] == ["tasks", "count"]
            # A block that holds steps says which lists it has.
            assert blocks["core.for-each"]["card"]["lanes"] == [
                {"key": "steps", "title": "For each item"}
            ]
            assert blocks["core.if"]["card"]["lanes"] == [
                {"key": "steps", "title": "If yes"},
                {"key": "otherwise", "title": "Otherwise"},
            ]
            assert find["card"]["lanes"] == []
            schedule = blocks["core.schedule"]
            assert (schedule["kind"], schedule["by_hand"]) == ("trigger", True)

    asyncio.run(run())


def test_the_catalog_says_which_plugins_are_not_set_up() -> None:
    async def run() -> None:
        async with _workflows_app() as (client, engine, _):
            await _login(client)

            async def registered(cls: Any, user: Any) -> bool:
                return bool(cls.canonical_name() != "todoist")

            with patch.object(Plugin, "is_registered", classmethod(registered)):
                catalog = await _json(await client.get("/app/api/workflows/catalog"))

            connected = {b["block"]: b["connected"] for b in catalog["blocks"]}
            assert connected["todoist.find-tasks"] is False
            assert connected["core.send-message"] is True
            recipes = {recipe["id"]: recipe for recipe in catalog["recipes"]}
            assert recipes["todoist-no-overdue"]["missing"] == ["Todoist"]

    asyncio.run(run())


def test_the_catalog_lists_the_recipes_and_which_the_user_has() -> None:
    async def run() -> None:
        async with _workflows_app() as (client, engine, _):
            await _install(engine)
            await _login(client)

            catalog = await _json(await client.get("/app/api/workflows/catalog"))

            recipes = {recipe["id"]: recipe for recipe in catalog["recipes"]}
            chores = recipes["todoist-no-overdue"]
            assert (chores["name"], chores["installed"], chores["missing"]) == (
                NO_OVERDUE,
                True,
                [],
            )
            assert chores["description"]
            # A recipe whose plugins are not loaded here cannot be added.
            hevy = recipes["hevy-set-notes"]
            assert hevy["installed"] is False
            assert hevy["missing"] == ["hevy", "obsidian"]

    asyncio.run(run())


# -- new workflows, and deleting one -------------------------------------------


def test_a_workflow_can_be_added_from_a_recipe() -> None:
    async def run() -> None:
        async with _workflows_app() as (client, engine, _):
            await _login(client)

            response = await client.post(
                "/app/api/workflows", json={"recipe": "todoist-no-overdue"}, headers=H
            )

            assert response.status_code == 201
            shown = await _json(response)
            assert (shown["name"], shown["enabled"]) == (NO_OVERDUE, True)
            (stored,) = await engine.store.list_for_user(USER_ID)
            assert (stored.id, stored.recipe) == (shown["id"], "todoist-no-overdue")

    asyncio.run(run())


def test_a_recipe_whose_plugins_are_missing_is_not_added() -> None:
    async def run() -> None:
        async with _workflows_app() as (client, engine, _):
            await _login(client)

            async def nothing_registered(cls: Any, user: Any) -> bool:
                return False

            with patch.object(Plugin, "is_registered", classmethod(nothing_registered)):
                response = await client.post(
                    "/app/api/workflows",
                    json={"recipe": "todoist-no-overdue"},
                    headers=H,
                )

            assert response.status_code == 409
            assert "needs Todoist first" in (await _json(response))["error"]
            assert await engine.store.list_for_user(USER_ID) == []

    asyncio.run(run())


def test_a_workflow_can_start_with_only_a_trigger_and_is_off() -> None:
    async def run() -> None:
        async with _workflows_app() as (client, engine, _):
            await _login(client)

            response = await client.post(
                "/app/api/workflows", json={"trigger": "core.schedule"}, headers=H
            )

            assert response.status_code == 201
            shown = await _json(response)
            assert (shown["name"], shown["enabled"], shown["steps"]) == (
                "New workflow",
                False,
                [],
            )
            assert shown["trigger"]["block"] == "core.schedule"
            assert shown["problems"] == []
            (stored,) = await engine.store.list_for_user(USER_ID)
            assert (stored.recipe, stored.user_id) == (None, USER_ID)

    asyncio.run(run())


def test_a_workflow_that_asks_is_a_command_and_is_not_tested() -> None:
    """A workflow made on the page from the trigger that is only a command."""

    async def run() -> None:
        async with _workflows_app() as (client, engine, _):
            await _login(client)
            response = await client.post(
                "/app/api/workflows", json={"trigger": "core.command"}, headers=H
            )
            assert response.status_code == 201
            shown = await _json(response)
            url = f"/app/api/workflows/{shown['id']}"
            form = _form_of(shown)
            form["name"] = "Ask me"
            form["steps"] = [
                {
                    "id": "ask",
                    "block": "core.ask-text",
                    "inputs": {
                        "question": "Which book?",
                        "ask_first": "",
                        "priority": "low",
                        "give_up_after": "30",
                    },
                    "steps": [],
                }
            ]

            saved = await client.put(url, json=form, headers=H)

            assert saved.status_code == 200
            shown = await _json(saved)
            assert shown["test"] == {"last": None, "now": True, "asks": True}
            (card,) = shown["steps"]
            assert card["kind"] == "ask"
            stored = await engine.store.get(shown["id"])
            assert stored is not None
            assert stored.steps[0].inputs == {
                "question": "Which book?",
                "ask_first": "",
                "priority": "low",
                "give_up_after": 30,
            }

            # The page says why, and so does the server.
            tested = await client.post(
                f"{url}/test", json={"workflow": form, "event": "now"}, headers=H
            )
            assert tested.status_code == 400
            assert "asks you something" in (await _json(tested))["error"]

            # Turned on, it is a command. It is not one before.
            assert commands.get_command(f"workflow.{shown['id']}") is None
            on = await client.post(f"{url}/enabled", json={"enabled": True}, headers=H)
            assert on.status_code == 200
            command = commands.get_command(f"workflow.{shown['id']}")
            assert command is not None and command.text == "Ask me"

    asyncio.run(run())


def test_the_page_shows_what_a_workflow_remembers_and_can_make_it_forget() -> None:
    async def run() -> None:
        async with _workflows_app() as (client, engine, _):
            workflow = await _install(engine)
            theirs = await _install(engine, OTHER_USER_ID)
            await _login(client)
            url = f"/app/api/workflows/{workflow.id}"
            shown = await _json(await client.get(url))
            assert shown["remembers"] == {"count": 0, "names": []}

            memory = engine.store.memory(workflow.id)
            for name in ("asked:B2", "asked:B1", "skipped", "moved-from-plugin"):
                await memory.set(name)
            await engine.store.memory(theirs.id).set("asked:B1")

            # In order, and without the engine's own note.
            shown = await _json(await client.get(url))
            assert shown["remembers"] == {
                "count": 3,
                "names": ["asked:B1", "asked:B2", "skipped"],
            }

            one = await client.post(
                f"{url}/forget", json={"name": "skipped"}, headers=H
            )
            assert one.status_code == 200
            assert (await _json(one))["remembers"]["names"] == ["asked:B1", "asked:B2"]
            gone = await client.post(
                f"{url}/forget", json={"name": "skipped"}, headers=H
            )
            assert gone.status_code == 404

            everything = await client.post(f"{url}/forget", json={}, headers=H)
            assert (await _json(everything))["remembers"] == {"count": 0, "names": []}
            # The engine's note stays, and so does another user's memory.
            assert await memory.names() == ["moved-from-plugin"]
            assert await engine.store.memory(theirs.id).names() == ["asked:B1"]
            other = await client.post(
                f"/app/api/workflows/{theirs.id}/forget", json={}, headers=H
            )
            assert other.status_code == 404

    asyncio.run(run())


def test_a_new_workflow_needs_a_recipe_or_a_trigger_that_is_there() -> None:
    async def run() -> None:
        async with _workflows_app() as (client, engine, _):
            assert (
                await client.post("/app/api/workflows", json={}, headers=H)
            ).status_code == 401
            await _login(client)

            for body, status in (
                ({}, 400),
                ({"recipe": "no-such-recipe"}, 404),
                ({"trigger": "core.no-such-trigger"}, 404),
                # A step is not a trigger.
                ({"trigger": "core.send-message"}, 404),
            ):
                response = await client.post("/app/api/workflows", json=body, headers=H)
                assert response.status_code == status, body
            assert await engine.store.list_for_user(USER_ID) == []

    asyncio.run(run())


def test_a_workflow_can_be_deleted_by_its_user_only() -> None:
    async def run() -> None:
        async with _workflows_app() as (client, engine, _):
            mine = await _install(engine)
            theirs = await _install(engine, OTHER_USER_ID)
            assert (
                await client.delete(f"/app/api/workflows/{mine.id}", headers=H)
            ).status_code == 401
            await _login(client)

            response = await client.delete(f"/app/api/workflows/{theirs.id}", headers=H)
            assert response.status_code == 404
            assert await engine.store.get(theirs.id) is not None

            response = await client.delete(f"/app/api/workflows/{mine.id}", headers=H)

            assert response.status_code == 200
            assert await engine.store.get(mine.id) is None
            assert await engine.store.list_for_user(USER_ID) == []
            # Its command goes with it.
            user = _user(USER_ID)
            user.plugins = ["workflows"]
            assert commands.commands_for_user(user) == []

    asyncio.run(run())


def test_workflows_are_not_there_until_the_engine_runs() -> None:
    async def run() -> None:
        async with _workflows_app(running=False) as (client, _, _):
            await _login(client)

            listed = await client.get("/app/api/workflows")
            one = await client.get("/app/api/workflows/wf_1")

            assert (listed.status_code, one.status_code) == (503, 503)
            assert (await _json(listed))["error"] == "Workflows are not running yet."

    asyncio.run(run())
