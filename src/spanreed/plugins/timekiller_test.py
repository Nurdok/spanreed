import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from spanreed.plugins.timekiller import TimekillerPlugin, UserConfig
from spanreed.plugin import Plugin
from spanreed.test_utils import mock_user_find_by_id, patch_telegram_bot


def _plugin_with_store() -> tuple[TimekillerPlugin, dict]:
    """A plugin whose per-user data is backed by an in-memory dict."""
    Plugin.reset_registry()
    plugin = TimekillerPlugin()
    store: dict = {}

    async def _get(user: Any, key: str) -> Any:
        return store.get(key)

    async def _set(user: Any, key: str, data: str) -> None:
        store[key] = data

    plugin.get_user_data = _get  # type: ignore[method-assign]
    plugin.set_user_data = _set  # type: ignore[method-assign]
    return plugin, store


def _stub_config(plugin: TimekillerPlugin, config: UserConfig) -> dict:
    """Back the plugin's config get/set with an in-memory holder."""
    holder: dict = {"config": config}

    async def _get_config(user: Any) -> UserConfig:
        return holder["config"]

    async def _set_config(user: Any, config: UserConfig) -> None:
        holder["config"] = config

    plugin.get_config = _get_config  # type: ignore[method-assign]
    plugin.set_config = _set_config  # type: ignore[method-assign]
    return holder


def test_skipped_scans_add_get_clear() -> None:
    plugin, _ = _plugin_with_store()
    user = mock_user_find_by_id(1)

    assert asyncio.run(plugin._get_skipped_scans(user)) == set()

    asyncio.run(plugin._add_skipped_scan(user, "Assets/scans/2026-01-01 Scan.pdf"))
    asyncio.run(plugin._add_skipped_scan(user, "Assets/scans/2026-01-02 Scan.pdf"))
    assert asyncio.run(plugin._get_skipped_scans(user)) == {
        "Assets/scans/2026-01-01 Scan.pdf",
        "Assets/scans/2026-01-02 Scan.pdf",
    }

    asyncio.run(plugin._clear_skipped_scans(user))
    assert asyncio.run(plugin._get_skipped_scans(user)) == set()


@patch_telegram_bot("spanreed.plugins.timekiller")
def test_clear_skipped_scans_command_reports_count(mock_bot: AsyncMock) -> None:
    plugin, store = _plugin_with_store()
    store[TimekillerPlugin.SKIPPED_SCANS_KEY] = json.dumps(["a", "b"])
    user = mock_user_find_by_id(1)

    asyncio.run(plugin._clear_skipped_scans_command(user))

    mock_bot.send_message.assert_awaited_once()
    assert "2" in mock_bot.send_message.call_args.args[0]
    assert asyncio.run(plugin._get_skipped_scans(user)) == set()


@patch_telegram_bot("spanreed.plugins.timekiller")
def test_clear_skipped_scans_command_when_empty(mock_bot: AsyncMock) -> None:
    plugin, _ = _plugin_with_store()
    user = mock_user_find_by_id(1)

    asyncio.run(plugin._clear_skipped_scans_command(user))

    mock_bot.send_message.assert_awaited_once_with("No skipped scans to clear.")


@patch_telegram_bot("spanreed.plugins.timekiller")
def test_prompt_ignores_skipped_scan(mock_bot: AsyncMock) -> None:
    plugin, store = _plugin_with_store()
    store[TimekillerPlugin.SKIPPED_SCANS_KEY] = json.dumps(
        ["Assets/scans/2026-01-01 Scan.pdf"]
    )
    obsidian = MagicMock()
    obsidian.list_dir = AsyncMock(return_value=["Assets/scans/2026-01-01 Scan.pdf"])
    obsidian.read_binary_file = AsyncMock()

    user = mock_user_find_by_id(1)
    asyncio.run(plugin.prompt_for_scan_processing(user, mock_bot, obsidian))

    # The only scan is skipped, so nothing is fetched or shown.
    obsidian.read_binary_file.assert_not_called()
    mock_bot.send_document.assert_not_called()


@patch_telegram_bot("spanreed.plugins.timekiller")
def test_prompt_delete_moves_to_trash(mock_bot: AsyncMock) -> None:
    plugin, _ = _plugin_with_store()
    obsidian = MagicMock()
    obsidian.list_dir = AsyncMock(return_value=["Assets/scans/2026-01-01 Scan.pdf"])
    obsidian.read_binary_file = AsyncMock(return_value=b"%PDF")
    obsidian.delete_file = AsyncMock()

    mock_bot.send_document = AsyncMock()
    # Action = Delete (1), then confirm = Yes (0).
    mock_bot.request_user_choice = AsyncMock(side_effect=[1, 0])

    user = mock_user_find_by_id(1)
    asyncio.run(plugin.prompt_for_scan_processing(user, mock_bot, obsidian))

    obsidian.delete_file.assert_awaited_once_with("Assets/scans/2026-01-01 Scan.pdf")


@patch_telegram_bot("spanreed.plugins.timekiller")
def test_journal_prompt_appends_under_configured_heading(mock_bot: AsyncMock) -> None:
    plugin, _ = _plugin_with_store()
    _stub_config(plugin, UserConfig(journal_heading="Journal"))
    obsidian = MagicMock()
    obsidian.append_to_note = AsyncMock()

    # Answer (0) the first prompt, then decline "Another?" (1) to stop.
    mock_bot.request_user_choice = AsyncMock(side_effect=[0, 1])
    mock_bot.request_user_input = AsyncMock(return_value="my answer")

    user = mock_user_find_by_id(1)
    asyncio.run(plugin._journal_prompt(user, mock_bot, obsidian))

    obsidian.append_to_note.assert_awaited_once()
    _, kwargs = obsidian.append_to_note.call_args
    assert kwargs["heading"] == "Journal"
    assert "my answer" in obsidian.append_to_note.call_args.args[1]


@patch_telegram_bot("spanreed.plugins.timekiller")
def test_journal_prompt_no_heading_when_unset(mock_bot: AsyncMock) -> None:
    plugin, _ = _plugin_with_store()
    _stub_config(plugin, UserConfig())
    obsidian = MagicMock()
    obsidian.append_to_note = AsyncMock()

    mock_bot.request_user_choice = AsyncMock(side_effect=[0, 1])
    mock_bot.request_user_input = AsyncMock(return_value="my answer")

    user = mock_user_find_by_id(1)
    asyncio.run(plugin._journal_prompt(user, mock_bot, obsidian))

    _, kwargs = obsidian.append_to_note.call_args
    assert kwargs["heading"] is None


@patch_telegram_bot("spanreed.plugins.timekiller")
def test_set_journal_heading_command(mock_bot: AsyncMock) -> None:
    plugin, _ = _plugin_with_store()
    holder = _stub_config(plugin, UserConfig())
    mock_bot.request_user_input = AsyncMock(return_value="  Daily Journal  ")

    user = mock_user_find_by_id(1)
    asyncio.run(plugin._set_journal_heading_command(user))

    assert holder["config"].journal_heading == "Daily Journal"


@patch_telegram_bot("spanreed.plugins.timekiller")
def test_set_journal_heading_command_none_clears(mock_bot: AsyncMock) -> None:
    plugin, _ = _plugin_with_store()
    holder = _stub_config(plugin, UserConfig(journal_heading="Journal"))
    mock_bot.request_user_input = AsyncMock(return_value="none")

    user = mock_user_find_by_id(1)
    asyncio.run(plugin._set_journal_heading_command(user))

    assert holder["config"].journal_heading is None


@patch_telegram_bot("spanreed.plugins.timekiller")
def test_prompt_skip_adds_to_skiplist(mock_bot: AsyncMock) -> None:
    plugin, _ = _plugin_with_store()
    obsidian = MagicMock()
    obsidian.list_dir = AsyncMock(return_value=["Assets/scans/2026-01-01 Scan.pdf"])
    obsidian.read_binary_file = AsyncMock(return_value=b"%PDF")

    mock_bot.send_document = AsyncMock()
    # Action = Skip (2).
    mock_bot.request_user_choice = AsyncMock(side_effect=[2])

    user = mock_user_find_by_id(1)
    asyncio.run(plugin.prompt_for_scan_processing(user, mock_bot, obsidian))

    assert "Assets/scans/2026-01-01 Scan.pdf" in asyncio.run(
        plugin._get_skipped_scans(user)
    )
