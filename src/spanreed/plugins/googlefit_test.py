import asyncio
import datetime
from unittest.mock import AsyncMock, patch

from spanreed.plugins.googlefit import GoogleFitPlugin
from spanreed.plugin import Plugin
from spanreed.test_utils import (
    mock_user_find_by_id,
    patch_telegram_bot,
    patch_obsidian,
)


def test_name() -> None:
    Plugin.reset_registry()
    plugin = GoogleFitPlugin()

    assert plugin.name() == "Google Fit"
    assert plugin.canonical_name() == "google-fit"


@patch("spanreed.plugins.googlefit.GoogleFitApi", autospec=True)
@patch_obsidian("spanreed.plugins.googlefit")
@patch_telegram_bot("spanreed.plugins.googlefit")
def test_sync_writes_steps_to_empty_note(
    mock_bot: AsyncMock,
    mock_obsidian: AsyncMock,
    mock_fit: AsyncMock,
) -> None:
    Plugin.reset_registry()
    plugin = GoogleFitPlugin()

    mock_bot.notify = AsyncMock()

    date = datetime.date(2024, 1, 1)
    mock_fit.for_user = AsyncMock(return_value=mock_fit)
    mock_fit.get_daily_steps = AsyncMock(return_value={date: 12345})

    mock_obsidian.get_daily_note.return_value = "Daily/2024-01-01.md"
    # No existing value in the note yet.
    mock_obsidian.get_property.return_value = None

    user = mock_user_find_by_id(4)
    written = asyncio.run(plugin._sync(user))

    assert written == 1
    mock_obsidian.safe_generate_today_note.assert_awaited_once()
    mock_obsidian.set_value_of_property.assert_awaited_once_with(
        "Daily/2024-01-01.md", "steps", 12345
    )


@patch("spanreed.plugins.googlefit.GoogleFitApi", autospec=True)
@patch_obsidian("spanreed.plugins.googlefit")
@patch_telegram_bot("spanreed.plugins.googlefit")
def test_sync_skips_when_property_present(
    mock_bot: AsyncMock,
    mock_obsidian: AsyncMock,
    mock_fit: AsyncMock,
) -> None:
    Plugin.reset_registry()
    plugin = GoogleFitPlugin()

    mock_bot.notify = AsyncMock()

    date = datetime.date(2024, 1, 1)
    mock_fit.for_user = AsyncMock(return_value=mock_fit)
    mock_fit.get_daily_steps = AsyncMock(return_value={date: 12345})

    mock_obsidian.get_daily_note.return_value = "Daily/2024-01-01.md"
    # The note already has a steps value; it must be left untouched.
    mock_obsidian.get_property.return_value = 9999

    user = mock_user_find_by_id(4)
    written = asyncio.run(plugin._sync(user))

    assert written == 0
    mock_obsidian.set_value_of_property.assert_not_called()


@patch("spanreed.plugins.googlefit.GoogleFitApi", autospec=True)
@patch_obsidian("spanreed.plugins.googlefit")
@patch_telegram_bot("spanreed.plugins.googlefit")
def test_sync_no_data_is_noop(
    mock_bot: AsyncMock,
    mock_obsidian: AsyncMock,
    mock_fit: AsyncMock,
) -> None:
    Plugin.reset_registry()
    plugin = GoogleFitPlugin()

    mock_bot.notify = AsyncMock()

    mock_fit.for_user = AsyncMock(return_value=mock_fit)
    mock_fit.get_daily_steps = AsyncMock(return_value={})

    user = mock_user_find_by_id(4)
    written = asyncio.run(plugin._sync(user))

    assert written == 0
    # Nothing to write, so we shouldn't even touch the vault.
    mock_obsidian.safe_generate_today_note.assert_not_called()
    mock_obsidian.set_value_of_property.assert_not_called()


@patch("spanreed.plugins.googlefit.GoogleFitApi", autospec=True)
@patch_obsidian("spanreed.plugins.googlefit")
@patch_telegram_bot("spanreed.plugins.googlefit")
def test_sync_skips_missing_daily_note(
    mock_bot: AsyncMock,
    mock_obsidian: AsyncMock,
    mock_fit: AsyncMock,
) -> None:
    Plugin.reset_registry()
    plugin = GoogleFitPlugin()

    mock_bot.notify = AsyncMock()

    date = datetime.date(2024, 1, 1)
    mock_fit.for_user = AsyncMock(return_value=mock_fit)
    mock_fit.get_daily_steps = AsyncMock(return_value={date: 12345})

    mock_obsidian.get_daily_note.return_value = "Daily/2024-01-01.md"
    # A past day whose note doesn't exist: reading the property raises.
    mock_obsidian.get_property.side_effect = FileNotFoundError

    user = mock_user_find_by_id(4)
    written = asyncio.run(plugin._sync(user))

    assert written == 0
    mock_obsidian.set_value_of_property.assert_not_called()
    mock_bot.notify.assert_awaited_once_with("No daily note for 2024-01-01; skipping.")
