import asyncio
import datetime
from unittest.mock import AsyncMock, MagicMock, patch

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
def test_sync_overwrites_when_value_changed(
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
    # An earlier, smaller count from a prior poll: steps kept growing, so it
    # must be overwritten with the new total.
    mock_obsidian.get_property.return_value = 9999

    user = mock_user_find_by_id(4)
    written = asyncio.run(plugin._sync(user))

    assert written == 1
    mock_obsidian.set_value_of_property.assert_awaited_once_with(
        "Daily/2024-01-01.md", "steps", 12345
    )


@patch("spanreed.plugins.googlefit.GoogleFitApi", autospec=True)
@patch_obsidian("spanreed.plugins.googlefit")
@patch_telegram_bot("spanreed.plugins.googlefit")
def test_sync_skips_when_value_unchanged(
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
    # The stored value already matches; no write needed.
    mock_obsidian.get_property.return_value = 12345

    user = mock_user_find_by_id(4)
    written = asyncio.run(plugin._sync(user))

    assert written == 0
    mock_obsidian.set_value_of_property.assert_not_called()


@patch("spanreed.plugins.googlefit.GoogleFitApi", autospec=True)
@patch_obsidian("spanreed.plugins.googlefit")
@patch_telegram_bot("spanreed.plugins.googlefit")
def test_sync_silent_when_notify_false(
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
    mock_obsidian.get_property.return_value = None

    user = mock_user_find_by_id(4)
    # The hourly background sync writes but must not send any Telegram message.
    written = asyncio.run(plugin._sync(user, notify=False))

    assert written == 1
    mock_obsidian.set_value_of_property.assert_awaited_once_with(
        "Daily/2024-01-01.md", "steps", 12345
    )
    mock_bot.notify.assert_not_called()


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


@patch("spanreed.plugins.googlefit.GoogleFitApi", autospec=True)
@patch_telegram_bot("spanreed.plugins.googlefit")
def test_ensure_authenticated_true_when_authed(
    mock_bot: AsyncMock, mock_fit: AsyncMock
) -> None:
    Plugin.reset_registry()
    plugin = GoogleFitPlugin()

    mock_fit.for_user = AsyncMock(return_value=mock_fit)
    mock_fit.is_authenticated = AsyncMock(return_value=True)

    user = mock_user_find_by_id(4)
    result = asyncio.run(plugin._ensure_authenticated(user, mock_bot))

    assert result is True
    mock_fit.start_authentication.assert_not_called()
    mock_bot.send_message.assert_not_called()


@patch("spanreed.plugins.googlefit.GoogleFitApi", autospec=True)
@patch_telegram_bot("spanreed.plugins.googlefit")
def test_ensure_authenticated_prompts_when_token_dead(
    mock_bot: AsyncMock, mock_fit: AsyncMock
) -> None:
    Plugin.reset_registry()
    plugin = GoogleFitPlugin()

    mock_fit.for_user = AsyncMock(return_value=mock_fit)
    mock_fit.is_authenticated = AsyncMock(return_value=False)
    mock_fit.is_app_configured = AsyncMock(return_value=True)

    flow = MagicMock()
    flow.get_done_event = AsyncMock()
    flow.get_auth_url = AsyncMock(return_value="https://auth.example/xyz")
    mock_fit.start_authentication = AsyncMock(return_value=flow)

    user = mock_user_find_by_id(4)
    result = asyncio.run(plugin._ensure_authenticated(user, mock_bot))

    assert result is False
    mock_bot.send_message.assert_awaited_once()
    assert "auth.example/xyz" in mock_bot.send_message.call_args.args[0]


@patch("spanreed.plugins.googlefit.GoogleFitApi", autospec=True)
@patch_telegram_bot("spanreed.plugins.googlefit")
def test_ensure_authenticated_no_double_prompt(
    mock_bot: AsyncMock, mock_fit: AsyncMock
) -> None:
    Plugin.reset_registry()
    plugin = GoogleFitPlugin()

    mock_fit.for_user = AsyncMock(return_value=mock_fit)
    mock_fit.is_authenticated = AsyncMock(return_value=False)
    mock_fit.is_app_configured = AsyncMock(return_value=True)
    # A flow is already outstanding: start_authentication raises.
    mock_fit.start_authentication = AsyncMock(side_effect=ValueError("already started"))

    user = mock_user_find_by_id(4)
    result = asyncio.run(plugin._ensure_authenticated(user, mock_bot))

    assert result is False
    mock_bot.send_message.assert_not_called()
