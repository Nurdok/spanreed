import contextlib
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock

from spanreed.plugins.todoist_nooverdue import TodoistNoOverduePlugin
from spanreed.plugin import Plugin
from spanreed.test_utils import mock_user_find_by_id, EndPluginRun
from spanreed.apis.todoist import Task
from spanreed.apis.telegram_bot import TelegramBotPlugin


def test_name() -> None:
    Plugin.reset_registry()
    plugin = TodoistNoOverduePlugin()

    assert plugin.name() == "Todoist No Overdue"
    assert plugin.canonical_name() == "todoist-no-overdue"


def test_ask_for_user_config() -> None:
    Plugin.reset_registry()
    plugin = TodoistNoOverduePlugin()

    user: MagicMock = mock_user_find_by_id(3)
    # There's no user config for this plugin yet,
    # so this should return silently.
    asyncio.run(plugin.ask_for_user_config(user))


def test_run_for_user() -> None:
    Plugin.reset_registry()
    plugin = TodoistNoOverduePlugin()

    user: MagicMock = mock_user_find_by_id(3)

    with patch(
        "spanreed.plugins.todoist_nooverdue.Todoist", autospec=True
    ) as mock_todoist, patch(
        "asyncio.sleep", autospec=True
    ) as mock_sleep, patch(
        "spanreed.plugins.todoist_nooverdue.TelegramBotPlugin",
        autospec=True,
    ) as mock_telegram_bot_plugin:
        task = MagicMock(name="task", spec=Task)
        mock_todoist.for_user.return_value.get_overdue_tasks_with_label.return_value = [
            task
        ]

        mock_telegram_bot_plugin.is_registered.return_value = False

        mock_sleep.side_effect = [EndPluginRun]
        with contextlib.suppress(EndPluginRun):
            asyncio.run(plugin.run_for_user(user))

        mock_todoist.for_user.return_value.get_overdue_tasks_with_label.assert_called_once_with(
            "spanreed/no-overdue"
        )
        mock_todoist.for_user.return_value.set_due_date_to_today.assert_called_once_with(
            task
        )
