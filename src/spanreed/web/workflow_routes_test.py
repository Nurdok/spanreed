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
            assert trigger["fields"] == [
                {"name": "every", "label": "Every", "value": 4},
                {"name": "unit", "label": "Unit", "value": "hours"},
            ]
            assert trigger["outputs"] == ["date", "time"]

            find, check, move, tell = shown["steps"]
            assert [step["number"] for step in shown["steps"]] == [1, 2, 3, 4]
            assert (find["id"], find["title"], find["app"]) == (
                "find",
                "Find tasks",
                "Todoist",
            )
            assert find["outputs"] == ["tasks", "count"]
            # Inputs are shown as they are stored, templates and all.
            assert check["fields"] == [
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

            assert shown["steps"][0]["fields"] == [
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


def test_workflows_are_not_there_until_the_engine_runs() -> None:
    async def run() -> None:
        async with _workflows_app(running=False) as (client, _, _):
            await _login(client)

            listed = await client.get("/app/api/workflows")
            one = await client.get("/app/api/workflows/wf_1")

            assert (listed.status_code, one.status_code) == (503, 503)
            assert (await _json(listed))["error"] == "Workflows are not running yet."

    asyncio.run(run())
