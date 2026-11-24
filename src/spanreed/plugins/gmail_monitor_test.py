import asyncio
import contextlib
import datetime
import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from spanreed.apis.gmail import EmailMessage
from spanreed.apis.obsidian import ObsidianPlugin
from spanreed.interaction import commands
from spanreed.plugin import Plugin
from spanreed.plugins.gmail_monitor import (
    LAST_CHECK_KEY,
    PROCESSED_KEY,
    GmailMonitorPlugin,
    UserConfig,
    convert_to_workflows,
)
from spanreed.plugins.workflows import WorkflowsPlugin
from spanreed.test_utils import (
    FakeRedis,
    async_return_true,
    mock_user_find_by_id,
    patch_user_interaction,
)
from spanreed.workflows import migrations
from spanreed.workflows import store as workflow_store
from spanreed.workflows.blocks import BlockRegistry
from spanreed.workflows.engine import WorkflowEngine
from spanreed.workflows.model import TriggerSpec, Workflow
from spanreed.workflows.store import WorkflowStore
from spanreed.workflows.testing import Polls

CONFIG_KEY = "config:plugin:name=gmail-monitor:user_id=4"
NOTIFY = {"type": "telegram_notification", "config": {}}


def _rule(name: str, **fields: Any) -> dict[str, Any]:
    rule: dict[str, Any] = {
        "name": name,
        "filter": {"sender_regex": name.lower()},
        "actions": [NOTIFY],
        "enabled": True,
    }
    rule.update(fields)
    return rule


def _message(message_id: str, sender: str) -> EmailMessage:
    return EmailMessage(
        id=message_id,
        thread_id="t",
        sender=sender,
        subject="Hello",
        body="Body",
        snippet="",
        date=datetime.datetime(2026, 5, 9, 9, 30),
        labels=[],
        has_attachments=False,
    )


@dataclass
class World:
    """What a test works with."""

    redis: FakeRedis
    engine: WorkflowEngine
    gmail: MagicMock
    bot: AsyncMock
    polls: Polls
    user: Any

    async def save_rules(self, *rules: dict[str, Any], minutes: int = 15) -> None:
        """Save a config as the plugin did while it held the rules."""
        config = {"rules": list(rules), "check_interval_minutes": minutes}
        await self.redis.set(CONFIG_KEY, json.dumps(config))

    async def workflows(self) -> list[Workflow]:
        """Return the user's workflows, in the order of the rules."""
        found = await self.engine.store.list_for_user(4)
        return sorted(found, key=lambda workflow: str(workflow.recipe))

    def said(self) -> list[str]:
        return [c.args[0] for c in self.bot.send_message.await_args_list]


@contextlib.contextmanager
def _world() -> Iterator[World]:
    fake = FakeRedis()
    workflow_store.reset_queues_for_tests()
    commands.reset_registry_for_tests()
    Plugin.reset_registry()
    registry = BlockRegistry.from_plugins(
        [WorkflowsPlugin(), ObsidianPlugin(), GmailMonitorPlugin()]
    )
    find_user = AsyncMock(side_effect=mock_user_find_by_id)
    polls = Polls()
    gmail = MagicMock()
    gmail.is_authenticated = AsyncMock(return_value=True)
    gmail.get_messages_after = AsyncMock(return_value=[])
    with (
        patch("spanreed.plugin.redis_api", new=fake),
        patch("spanreed.plugins.gmail_monitor.redis_api", new=fake),
        patch("spanreed.workflows.store.redis_api", new=fake),
        patch("spanreed.workflows.migrations.redis_api", new=fake),
        patch("spanreed.workflows.engine.User.find_by_id", new=find_user),
        patch("spanreed.workflows.migrations.User.find_by_id", new=find_user),
        patch.object(Plugin, "is_registered", async_return_true),
        patch("spanreed.apis.gmail_blocks.GmailApi") as gmail_cls,
        patch("spanreed.workflows.engine.UserInteractionApi", autospec=True) as bot,
        patch("spanreed.workflows.builtin.UserInteractionApi", new=bot),
        patch("spanreed.workflows.migrations.UserInteractionApi", new=bot),
        patch("spanreed.workflows.engine._sleep", new=AsyncMock()),
        patch("spanreed.workflows.blocks._sleep", new=polls.sleep),
    ):
        gmail_cls.for_user = AsyncMock(return_value=gmail)
        bot.for_user = AsyncMock(return_value=bot)
        for name in ("send_message", "notify"):
            setattr(bot, name, AsyncMock())
        yield World(
            redis=fake,
            engine=WorkflowEngine(registry),
            gmail=gmail,
            bot=bot,
            polls=polls,
            user=mock_user_find_by_id(4),
        )


def _run(scenario: Any) -> None:
    async def with_engine(world: World) -> None:
        try:
            await scenario(world)
        finally:
            await world.engine.stop()

    with _world() as world:
        asyncio.run(with_engine(world))


# -- the plugin ----------------------------------------------------------------


def test_the_plugin_publishes_the_gmail_blocks_and_has_no_commands() -> None:
    Plugin.reset_registry()
    commands.reset_registry_for_tests()
    plugin = GmailMonitorPlugin()

    assert [block.id for block in plugin.blocks()] == [
        "gmail.new-email",
        "gmail.get-attachments",
        "gmail.find-links",
    ]
    with patch("spanreed.plugin.redis_api", new=FakeRedis()):
        asyncio.run(plugin.run())
    user = mock_user_find_by_id(4)
    user.plugins = ["gmail-monitor"]
    assert commands.commands_for_user(user) == []


def test_a_config_that_still_holds_rules_reads_as_an_empty_one() -> None:
    async def scenario(world: World) -> None:
        await world.save_rules(_rule("Shop"))

        assert await GmailMonitorPlugin.get_config(world.user) == UserConfig()

    _run(scenario)


@patch_user_interaction("spanreed.plugins.gmail_monitor")
def test_setup_logs_in_to_gmail_and_points_to_the_trigger(mock_bot: AsyncMock) -> None:
    fake = FakeRedis()
    flow = MagicMock()
    done = asyncio.Event()
    done.set()
    flow.get_done_event = AsyncMock(return_value=done)
    flow.get_auth_url = AsyncMock(return_value="https://accounts.example/auth")
    gmail = MagicMock()
    gmail.is_authenticated = AsyncMock(return_value=False)

    with (
        patch("spanreed.plugin.redis_api", new=fake),
        patch("spanreed.workflows.store.redis_api", new=fake),
        patch("spanreed.plugins.gmail_monitor.GmailApi") as gmail_cls,
    ):
        gmail_cls.is_app_configured = AsyncMock(return_value=True)
        gmail_cls.for_user = AsyncMock(return_value=gmail)
        gmail_cls.start_authentication = AsyncMock(return_value=flow)
        user = mock_user_find_by_id(4)
        asyncio.run(GmailMonitorPlugin.ask_for_user_config(user))
        saved = asyncio.run(GmailMonitorPlugin.get_saved_config_data(user))

    said = [call.args[0] for call in mock_bot.send_message.await_args_list]
    assert "https://accounts.example/auth" in said[0]
    assert said[1] == "Gmail authentication successful!"
    assert "“New workflow”" in said[2] and "“New email”" in said[2]
    assert saved == {}


@patch_user_interaction("spanreed.plugins.gmail_monitor")
def test_setup_says_nothing_of_workflows_to_a_user_who_has_one(
    mock_bot: AsyncMock,
) -> None:
    fake = FakeRedis()
    gmail = MagicMock()
    gmail.is_authenticated = AsyncMock(return_value=True)
    workflow = Workflow(
        id="wf_1",
        user_id=4,
        name="Receipts",
        trigger=TriggerSpec(block="gmail.new-email", params={}),
        steps=[],
    )

    with (
        patch("spanreed.plugin.redis_api", new=fake),
        patch("spanreed.workflows.store.redis_api", new=fake),
        patch("spanreed.plugins.gmail_monitor.GmailApi") as gmail_cls,
    ):
        gmail_cls.is_app_configured = AsyncMock(return_value=True)
        gmail_cls.for_user = AsyncMock(return_value=gmail)
        asyncio.run(WorkflowStore().save(workflow))
        asyncio.run(GmailMonitorPlugin.ask_for_user_config(mock_user_find_by_id(4)))

    said = [call.args[0] for call in mock_bot.send_message.await_args_list]
    assert said == ["Gmail is already authenticated!"]


@patch_user_interaction("spanreed.plugins.gmail_monitor")
def test_setup_stops_if_the_gmail_app_is_not_configured(mock_bot: AsyncMock) -> None:
    with patch("spanreed.plugins.gmail_monitor.GmailApi") as gmail_cls:
        gmail_cls.is_app_configured = AsyncMock(return_value=False)
        asyncio.run(GmailMonitorPlugin.ask_for_user_config(mock_user_find_by_id(4)))

    (message,) = [call.args[0] for call in mock_bot.send_message.await_args_list]
    assert "not configured" in message
    gmail_cls.start_authentication.assert_not_called()


# -- from rules to workflows ---------------------------------------------------


def test_each_rule_becomes_a_workflow_that_is_on_if_the_rule_was() -> None:
    async def scenario(world: World) -> None:
        await world.save_rules(_rule("Shop"), _rule("Bank", enabled=False), minutes=30)

        made = await convert_to_workflows(world.engine, world.user)

        shop, bank = await world.workflows()
        assert [w.id for w in made] == [shop.id, bank.id]
        assert (shop.name, shop.enabled) == ("Shop", True)
        assert (bank.name, bank.enabled) == ("Bank", False)
        assert shop.trigger.params["every"] == 30
        assert shop.trigger.params["sender"] == "shop"
        assert [step.id for step in shop.steps] == ["notify"]
        # The config holds no rules any more.
        assert await GmailMonitorPlugin.get_saved_config_data(world.user) == {}

    _run(scenario)


def test_a_workflow_goes_on_where_the_plugin_was() -> None:
    async def scenario(world: World) -> None:
        await world.save_rules(_rule("Shop"))
        last_check = datetime.datetime(2026, 5, 9, 12, 0)
        await GmailMonitorPlugin.set_user_data(
            world.user, LAST_CHECK_KEY, last_check.isoformat()
        )
        await GmailMonitorPlugin.set_user_data(
            world.user, PROCESSED_KEY, json.dumps(["old"])
        )
        # Gmail returns an email that the plugin checked, and a new one.
        world.gmail.get_messages_after.return_value = [
            _message("new", "Shop <a@shop.example>"),
            _message("old", "Shop <a@shop.example>"),
        ]

        await convert_to_workflows(world.engine, world.user)
        await world.polls.over()

        # The first poll asks from a little before the plugin's last look.
        (asked,) = world.gmail.get_messages_after.await_args_list
        assert asked.args[0] == last_check.astimezone() - datetime.timedelta(hours=1)
        # Only the new email is acted on.
        assert len(world.said()) == 1
        (workflow,) = await world.workflows()
        runs = await world.engine.store.recent_runs(workflow.id, 10)
        assert [run["trigger"]["outputs"]["id"] for run in runs] == ["new"]
        # What the plugin kept is the trigger's now.
        assert (
            await GmailMonitorPlugin.get_user_data(world.user, LAST_CHECK_KEY) is None
        )
        assert await GmailMonitorPlugin.get_user_data(world.user, PROCESSED_KEY) is None

    _run(scenario)


def test_a_rule_that_could_not_run_becomes_a_workflow_that_is_off() -> None:
    async def scenario(world: World) -> None:
        broken = _rule("Broken", filter={"subject_regex": "receipt ("})
        await world.save_rules(broken, _rule("Shop"))

        await convert_to_workflows(world.engine, world.user)

        bad, shop = await world.workflows()
        assert (bad.name, bad.enabled) == ("Broken", False)
        assert world.engine.problems(bad) == [
            "New email: Subject: this is not a regular expression "
            "(missing ), unterminated subpattern at position 8)."
        ]
        # The other rules are not held up by it.
        assert shop.enabled

    _run(scenario)


def test_a_conversion_that_stopped_halfway_makes_no_workflow_twice() -> None:
    async def scenario(world: World) -> None:
        await world.save_rules(_rule("Shop"), _rule("Bank"))
        real = world.engine.save
        calls = 0

        async def failing_save(workflow: Workflow) -> None:
            nonlocal calls
            calls += 1
            if calls == 3:
                raise RuntimeError("Redis went away")
            await real(workflow)

        with patch.object(world.engine, "save", new=failing_save):
            try:
                await convert_to_workflows(world.engine, world.user)
            except RuntimeError:
                pass
        assert [w.name for w in await world.workflows()] == ["Shop"]

        made = await convert_to_workflows(world.engine, world.user)

        assert [w.name for w in made] == ["Bank"]
        assert [w.name for w in await world.workflows()] == ["Shop", "Bank"]

    _run(scenario)


def test_a_user_with_no_rules_gets_no_workflows() -> None:
    async def scenario(world: World) -> None:
        await world.save_rules()

        assert await convert_to_workflows(world.engine, world.user) == []
        assert await world.workflows() == []

    _run(scenario)


def test_a_user_of_the_plugin_is_converted_once_at_startup_and_told() -> None:
    async def scenario(world: World) -> None:
        await world.save_rules(
            _rule("Shop"), _rule("Bank", enabled=False), _rule("Tax")
        )
        await world.redis.sadd("config:plugin:name=gmail-monitor:users", 4)

        await migrations.convert_plugins(world.engine)
        await migrations.convert_plugins(world.engine)

        assert len(await world.workflows()) == 3
        world.bot.notify.assert_awaited_once()
        assert world.bot.notify.await_args.args[0] == (
            "Gmail Monitor now does its work through workflows: “Shop”, “Bank” "
            "and “Tax”. They do what the plugin did, with your settings. See "
            "them under Workflows in the web UI, or with “Manage workflows”. "
            "A rule that was off is a workflow that is off. One that could "
            "not be turned on says why on its page. Each email rule is a "
            "workflow of its own. The commands Manage Email Rules, Test Email "
            "Rule, Email Monitor Status and Run rules on inbox are gone: edit, "
            "test and run a workflow, and see its history, under Workflows in "
            "the web UI."
        )

    _run(scenario)
