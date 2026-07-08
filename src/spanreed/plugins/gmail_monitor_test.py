import asyncio
import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from spanreed.plugins.gmail_monitor import (
    GmailMonitorPlugin,
    UserConfig,
    EmailRule,
    EmailFilter,
    EmailAction,
)
from spanreed.plugins.gmail_monitor_actions import EmailMatch
from spanreed.apis.gmail import EmailMessage
from spanreed.plugin import Plugin
from spanreed.test_utils import mock_user_find_by_id, patch_telegram_bot


def _email(subject: str = "Receipt 123", sender: str = "a@b.com") -> EmailMessage:
    return EmailMessage(
        id="m1",
        thread_id="t1",
        sender=sender,
        subject=subject,
        body="body text",
        snippet="",
        date=datetime.datetime(2024, 1, 1),
        labels=[],
        has_attachments=False,
    )


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


def test_execute_matches_dispatches_and_isolates_failures() -> None:
    Plugin.reset_registry()
    plugin = GmailMonitorPlugin()

    good = AsyncMock()
    boom = AsyncMock()
    boom.execute = AsyncMock(side_effect=RuntimeError("handler failed"))
    plugin._action_handlers = {"good": good, "boom": boom}

    match = EmailMatch(email=_email(), rule_name="R")
    actions = [
        EmailAction(type="good", config={"k": "v"}),
        EmailAction(type="boom", config={}),
        EmailAction(type="unknown", config={}),  # no handler -> skipped
    ]
    user = mock_user_find_by_id(1)

    # A failing handler must not stop the others.
    asyncio.run(plugin._execute_matches(user, [(match, actions)]))

    good.execute.assert_awaited_once_with(match, {"k": "v"}, user)
    boom.execute.assert_awaited_once()


@patch("spanreed.plugins.gmail_monitor.GmailApi", autospec=True)
@patch_telegram_bot("spanreed.plugins.gmail_monitor")
def test_run_rules_on_existing_matches_and_executes(
    mock_bot: AsyncMock, mock_gmail: AsyncMock
) -> None:
    Plugin.reset_registry()
    plugin = GmailMonitorPlugin()

    rule = EmailRule(
        name="Receipts",
        filter=EmailFilter(subject_regex="Receipt"),
        actions=[EmailAction(type="telegram_notification", config={})],
        enabled=True,
    )
    config = UserConfig(rules=[rule], check_interval_minutes=15)

    mock_gmail.for_user = AsyncMock(return_value=mock_gmail)
    mock_gmail.is_authenticated = AsyncMock(return_value=True)
    email = _email(subject="Receipt 123")
    mock_gmail.get_recent_messages = AsyncMock(return_value=[email])

    # Scope = Inbox (0), then confirm = Run actions (0).
    mock_bot.request_user_choice = AsyncMock(side_effect=[0, 0])

    with patch.object(
        GmailMonitorPlugin, "get_config", new=AsyncMock(return_value=config)
    ), patch.object(plugin, "_execute_matches", new=AsyncMock()) as mock_exec:
        user = mock_user_find_by_id(1)
        asyncio.run(plugin.run_rules_on_existing(user))

        mock_gmail.get_recent_messages.assert_awaited_once_with(
            query="in:inbox", max_results=200
        )
        mock_exec.assert_awaited_once()
        # One match (the receipt email against the enabled rule).
        passed_matches = mock_exec.await_args.args[1]
        assert len(passed_matches) == 1
        assert passed_matches[0][0].rule_name == "Receipts"


@patch("spanreed.plugins.gmail_monitor.GmailApi", autospec=True)
@patch_telegram_bot("spanreed.plugins.gmail_monitor")
def test_run_rules_on_existing_no_match_skips_execution(
    mock_bot: AsyncMock, mock_gmail: AsyncMock
) -> None:
    Plugin.reset_registry()
    plugin = GmailMonitorPlugin()

    rule = EmailRule(
        name="Receipts",
        filter=EmailFilter(subject_regex="Receipt"),
        actions=[EmailAction(type="telegram_notification", config={})],
        enabled=True,
    )
    config = UserConfig(rules=[rule], check_interval_minutes=15)

    mock_gmail.for_user = AsyncMock(return_value=mock_gmail)
    mock_gmail.is_authenticated = AsyncMock(return_value=True)
    mock_gmail.get_recent_messages = AsyncMock(
        return_value=[_email(subject="Unrelated newsletter")]
    )

    # Scope = Inbox + Starred (1); no confirm should be requested.
    mock_bot.request_user_choice = AsyncMock(side_effect=[1])

    with patch.object(
        GmailMonitorPlugin, "get_config", new=AsyncMock(return_value=config)
    ), patch.object(plugin, "_execute_matches", new=AsyncMock()) as mock_exec:
        user = mock_user_find_by_id(1)
        asyncio.run(plugin.run_rules_on_existing(user))

        mock_gmail.get_recent_messages.assert_awaited_once_with(
            query="in:inbox OR is:starred", max_results=200
        )
        mock_exec.assert_not_called()
