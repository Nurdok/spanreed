import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from spanreed.plugins.gmail_monitor import GmailMonitorPlugin
from spanreed.plugin import Plugin
from spanreed.test_utils import mock_user_find_by_id, patch_telegram_bot


@patch("spanreed.plugins.gmail_monitor.GmailApi", autospec=True)
@patch_telegram_bot("spanreed.plugins.gmail_monitor")
def test_ensure_authenticated_true_when_authed(
    mock_bot: AsyncMock, mock_gmail: AsyncMock
) -> None:
    Plugin.reset_registry()
    plugin = GmailMonitorPlugin()

    mock_gmail.for_user = AsyncMock(return_value=mock_gmail)
    mock_gmail.is_authenticated = AsyncMock(return_value=True)

    user = mock_user_find_by_id(1)
    result = asyncio.run(plugin._ensure_authenticated(user, mock_bot))

    assert result is True
    mock_gmail.start_authentication.assert_not_called()
    mock_bot.send_message.assert_not_called()


@patch("spanreed.plugins.gmail_monitor.GmailApi", autospec=True)
@patch_telegram_bot("spanreed.plugins.gmail_monitor")
def test_ensure_authenticated_prompts_when_token_dead(
    mock_bot: AsyncMock, mock_gmail: AsyncMock
) -> None:
    Plugin.reset_registry()
    plugin = GmailMonitorPlugin()

    mock_gmail.for_user = AsyncMock(return_value=mock_gmail)
    mock_gmail.is_authenticated = AsyncMock(return_value=False)
    mock_gmail.is_app_configured = AsyncMock(return_value=True)

    flow = MagicMock()
    flow.get_done_event = AsyncMock()
    flow.get_auth_url = AsyncMock(return_value="https://auth.example/gmail")
    mock_gmail.start_authentication = AsyncMock(return_value=flow)

    user = mock_user_find_by_id(1)
    result = asyncio.run(plugin._ensure_authenticated(user, mock_bot))

    assert result is False
    mock_bot.send_message.assert_awaited_once()
    assert "auth.example/gmail" in mock_bot.send_message.call_args.args[0]


@patch("spanreed.plugins.gmail_monitor.GmailApi", autospec=True)
@patch_telegram_bot("spanreed.plugins.gmail_monitor")
def test_ensure_authenticated_no_double_prompt(
    mock_bot: AsyncMock, mock_gmail: AsyncMock
) -> None:
    Plugin.reset_registry()
    plugin = GmailMonitorPlugin()

    mock_gmail.for_user = AsyncMock(return_value=mock_gmail)
    mock_gmail.is_authenticated = AsyncMock(return_value=False)
    mock_gmail.is_app_configured = AsyncMock(return_value=True)
    mock_gmail.start_authentication = AsyncMock(
        side_effect=ValueError("already started")
    )

    user = mock_user_find_by_id(1)
    result = asyncio.run(plugin._ensure_authenticated(user, mock_bot))

    assert result is False
    mock_bot.send_message.assert_not_called()
