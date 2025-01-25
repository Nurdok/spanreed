import asyncio

import pytest

from spanreed.plugin import Plugin
from spanreed.plugins.habit_tracker import (
    HabitTrackerPlugin,
    UserConfig,
    Habit,
)
from spanreed.test_utils import (
    patch_telegram_bot,
    mock_user_find_by_id,
    patch_obsidian,
    AsyncContextManager,
)
from unittest.mock import MagicMock, patch, AsyncMock, call


@patch.object(asyncio, "timeout")
@patch.object(HabitTrackerPlugin, "get_config")
@patch_obsidian("spanreed.plugins.habit_tracker")
@patch_telegram_bot("spanreed.plugins.habit_tracker")
def test_run_for_user(
    mock_bot: AsyncMock,
    mock_obsidian: AsyncMock,
    mock_get_config: AsyncMock,
    mock_timeout: AsyncContextManager,
) -> None:
    Plugin.reset_registry()
    plugin = HabitTrackerPlugin()

    mock_user = mock_user_find_by_id(4)
    mock_get_config.return_value = UserConfig(
        daily_note_path="myDaily",
        habit_tracker_property_name="habits",
        habits=[
            Habit("habit1", "my habit 1"),
            Habit("habit2", "my habit 2"),
        ],
    )
    mock_obsidian.get_daily_note.return_value = "daily/2024-01-01.md"

    def fake_user_choice(prompt: str, choices: list[str], columns: int) -> int:
        if "Did you do any of these habits today?" in prompt:
            assert "Habit1" in choices
            assert "Cancel" in choices
            if "Habit2" in choices:
                return 1
            raise asyncio.CancelledError

        assert False, f"Unexpected prompt: {prompt}"

    mock_bot.request_user_choice.side_effect = fake_user_choice

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(plugin.run_for_user(user=mock_user))

    mock_obsidian.add_value_to_list_property.assert_called_once_with(
        filepath="daily/2024-01-01.md",
        property_name="habits",
        value="habit2",
    )
