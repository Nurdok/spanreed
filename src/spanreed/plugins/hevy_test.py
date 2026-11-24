import asyncio
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from spanreed.plugin import Plugin
from spanreed.plugins.hevy import HevyPlugin, UserConfig
from spanreed.test_utils import (
    FakeRedis,
    mock_user_find_by_id,
    patch_user_interaction,
)
from spanreed.workflows.model import TriggerSpec, Workflow
from spanreed.workflows.store import WorkflowStore

CONFIG_KEY = "config:plugin:name=hevy:user_id=4"


@pytest.fixture(autouse=True)
def redis() -> Iterator[FakeRedis]:
    """Give the plugin config and the workflows one empty Redis."""
    fake = FakeRedis()
    with (
        patch("spanreed.plugin.redis_api", new=fake),
        patch("spanreed.workflows.store.redis_api", new=fake),
    ):
        yield fake


def _plugin_and_user() -> tuple[HevyPlugin, Any]:
    Plugin.reset_registry()
    return HevyPlugin(), mock_user_find_by_id(4)


def _said(mock_bot: AsyncMock) -> list[str]:
    return [call.args[0] for call in mock_bot.send_message.await_args_list]


def test_the_plugin_publishes_the_hevy_trigger() -> None:
    plugin, _ = _plugin_and_user()

    assert [block.id for block in plugin.blocks()] == ["hevy.new-workout"]


def test_a_config_saved_with_the_old_setting_gives_the_api_key(
    redis: FakeRedis,
) -> None:
    plugin, user = _plugin_and_user()
    # As the plugin saved it while it wrote the notes itself.
    asyncio.run(
        redis.set(CONFIG_KEY, json.dumps({"api_key": "key", "vault_dir": "Gym"}))
    )

    assert asyncio.run(HevyPlugin.get_config(user)) == UserConfig(api_key="key")


@patch_user_interaction("spanreed.plugins.hevy")
def test_setup_asks_for_the_api_key_and_points_to_the_recipe(
    mock_bot: AsyncMock,
) -> None:
    plugin, user = _plugin_and_user()
    mock_bot.request_user_input.return_value = "  secret-key \n"

    asyncio.run(HevyPlugin.ask_for_user_config(user))

    assert asyncio.run(HevyPlugin.get_config(user)) == UserConfig(api_key="secret-key")
    # The folder is not asked for: it is part of the workflow's path.
    mock_bot.request_user_input.assert_awaited_once()
    (hint,) = _said(mock_bot)
    assert "“Hevy workouts → set notes”" in hint and "“Manage workflows”" in hint


@patch_user_interaction("spanreed.plugins.hevy")
def test_setup_says_nothing_of_recipes_to_a_user_who_has_the_workflow(
    mock_bot: AsyncMock,
) -> None:
    plugin, user = _plugin_and_user()
    mock_bot.request_user_input.return_value = "key"
    workflow = Workflow(
        id="wf_1",
        user_id=4,
        name="Hevy workouts → set notes",
        trigger=TriggerSpec(block="hevy.new-workout", params={}),
        steps=[],
    )
    asyncio.run(WorkflowStore().save(workflow))

    asyncio.run(HevyPlugin.ask_for_user_config(user))

    assert _said(mock_bot) == []
