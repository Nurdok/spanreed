import asyncio
import datetime
import json
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from freezegun import freeze_time

from spanreed.plugin import Plugin
from spanreed.user import User
from spanreed.apis.gmail import EmailMessage, GmailApi
from spanreed.plugins.gmail_monitor import (
    GmailMonitorPlugin,
    EmailFilter,
    EmailAction,
    EmailRule,
    UserConfig,
)
from spanreed.plugins.gmail_monitor_actions import (
    EmailMatch,
    TelegramNotificationAction,
)


@pytest.fixture
async def user():
    user = User(id=123, name="Test User", plugins=["gmail-monitor"])
    return user


@pytest.fixture
async def plugin() -> Plugin:
    return GmailMonitorPlugin()


@pytest.fixture
def sample_email():
    return EmailMessage(
        id="msg123",
        thread_id="thread123",
        sender="invoice@example.com",
        subject="Invoice #12345",
        body="Please find your invoice attached.",
        snippet="Please find your invoice...",
        date=datetime.datetime(2023, 1, 15, 10, 30),
        labels=["INBOX"],
        has_attachments=True,
    )


@pytest.fixture
def sample_config():
    return UserConfig(
        rules=[
            EmailRule(
                name="Invoice Rule",
                filter=EmailFilter(
                    sender_regex=".*@example\\.com",
                    subject_regex="invoice",
                    has_attachments=True,
                ),
                actions=[
                    EmailAction(
                        type="telegram_notification",
                        config={
                            "include_body": False,
                            "include_snippet": True,
                        },
                    )
                ],
                enabled=True,
            )
        ],
        check_interval_minutes=15,
    )


class TestEmailFilter:
    def test_matches_sender_regex(self, sample_email):
        filter = EmailFilter(sender_regex=".*@example\\.com")
        assert filter.matches(sample_email) is True

        filter = EmailFilter(sender_regex=".*@different\\.com")
        assert filter.matches(sample_email) is False

    def test_matches_subject_regex(self, sample_email):
        filter = EmailFilter(subject_regex="invoice")
        assert filter.matches(sample_email) is True

        filter = EmailFilter(subject_regex="receipt")
        assert filter.matches(sample_email) is False

    def test_matches_body_regex(self, sample_email):
        filter = EmailFilter(body_regex="invoice.*attached")
        assert filter.matches(sample_email) is True

        filter = EmailFilter(body_regex="payment.*due")
        assert filter.matches(sample_email) is False

    def test_matches_has_attachments(self, sample_email):
        filter = EmailFilter(has_attachments=True)
        assert filter.matches(sample_email) is True

        filter = EmailFilter(has_attachments=False)
        assert filter.matches(sample_email) is False

    def test_matches_multiple_criteria(self, sample_email):
        filter = EmailFilter(
            sender_regex=".*@example\\.com",
            subject_regex="invoice",
            has_attachments=True,
        )
        assert filter.matches(sample_email) is True

        # One criteria fails
        filter = EmailFilter(
            sender_regex=".*@example\\.com",
            subject_regex="receipt",  # This will fail
            has_attachments=True,
        )
        assert filter.matches(sample_email) is False

    def test_matches_no_criteria(self, sample_email):
        # Empty filter should match everything
        filter = EmailFilter()
        assert filter.matches(sample_email) is True


class TestEmailAction:
    def test_post_init_dict_config(self):
        action = EmailAction(type="test", config={"key": "value"})
        assert action.config == {"key": "value"}

    def test_post_init_empty_config(self):
        action = EmailAction(type="test", config="invalid")
        assert action.config == {}


class TestEmailRule:
    def test_post_init_dict_filter(self):
        rule_data = {
            "name": "test",
            "filter": {"sender_regex": "test@test.com"},
            "actions": [{"type": "telegram_notification", "config": {}}],
            "enabled": True,
        }
        rule = EmailRule(**rule_data)
        assert isinstance(rule.filter, EmailFilter)
        assert rule.filter.sender_regex == "test@test.com"

    def test_post_init_dict_actions(self):
        rule_data = {
            "name": "test",
            "filter": EmailFilter(),
            "actions": [
                {"type": "telegram_notification", "config": {"key": "value"}}
            ],
            "enabled": True,
        }
        rule = EmailRule(**rule_data)
        assert len(rule.actions) == 1
        assert isinstance(rule.actions[0], EmailAction)
        assert rule.actions[0].type == "telegram_notification"


class TestUserConfig:
    def test_post_init_dict_rules(self):
        config_data = {
            "rules": [
                {
                    "name": "test",
                    "filter": {"sender_regex": "test@test.com"},
                    "actions": [
                        {"type": "telegram_notification", "config": {}}
                    ],
                    "enabled": True,
                }
            ],
            "check_interval_minutes": 15,
        }
        config = UserConfig(**config_data)
        assert len(config.rules) == 1
        assert isinstance(config.rules[0], EmailRule)
        assert config.rules[0].name == "test"


class TestTelegramNotificationAction:
    @pytest.mark.asyncio
    async def test_execute_basic_notification(self, user, sample_email):
        # Mock TelegramBotApi
        mock_bot = AsyncMock()
        with patch(
            "spanreed.plugins.gmail_monitor_actions.TelegramBotApi.for_user",
            return_value=mock_bot,
        ):
            action = TelegramNotificationAction()
            email_match = EmailMatch(email=sample_email, rule_name="Test Rule")
            config = {"include_body": False, "include_snippet": True}

            await action.execute(email_match, config, user)

            mock_bot.send_message.assert_called_once()
            call_args = mock_bot.send_message.call_args[0]
            message = call_args[0]

            assert "Test Rule" in message
            assert "invoice@example.com" in message
            assert "Invoice #12345" in message
            assert "Please find your invoice..." in message  # snippet
            assert "📎 Has attachments" in message

    @pytest.mark.asyncio
    async def test_execute_with_custom_message(self, user, sample_email):
        mock_bot = AsyncMock()
        with patch(
            "spanreed.plugins.gmail_monitor_actions.TelegramBotApi.for_user",
            return_value=mock_bot,
        ):
            action = TelegramNotificationAction()
            email_match = EmailMatch(email=sample_email, rule_name="Test Rule")
            config = {"custom_message": "New invoice received!"}

            await action.execute(email_match, config, user)

            mock_bot.send_message.assert_called_once()
            call_args = mock_bot.send_message.call_args[0]
            message = call_args[0]

            assert "New invoice received!" in message

    @pytest.mark.asyncio
    async def test_execute_with_body(self, user, sample_email):
        mock_bot = AsyncMock()
        with patch(
            "spanreed.plugins.gmail_monitor_actions.TelegramBotApi.for_user",
            return_value=mock_bot,
        ):
            action = TelegramNotificationAction()
            email_match = EmailMatch(email=sample_email, rule_name="Test Rule")
            config = {"include_body": True}

            await action.execute(email_match, config, user)

            mock_bot.send_message.assert_called_once()
            call_args = mock_bot.send_message.call_args[0]
            message = call_args[0]

            assert "Please find your invoice attached." in message


class TestGmailMonitorPlugin:
    @pytest.mark.asyncio
    async def test_plugin_name(self, plugin):
        assert plugin.name() == "Gmail Monitor"

    @pytest.mark.asyncio
    async def test_has_user_config(self, plugin):
        assert plugin.has_user_config() is True

    @pytest.mark.asyncio
    async def test_get_config_class(self, plugin):
        assert plugin.get_config_class() == UserConfig

    @pytest.mark.asyncio
    async def test_processed_email_ids(self, plugin, user):
        # Test empty initially
        processed = await plugin._get_processed_email_ids(user)
        assert processed == set()

        # Test marking emails processed
        await plugin._mark_emails_processed(user, ["msg1", "msg2"])
        processed = await plugin._get_processed_email_ids(user)
        assert processed == {"msg1", "msg2"}

        # Test adding more
        await plugin._mark_emails_processed(user, ["msg3"])
        processed = await plugin._get_processed_email_ids(user)
        assert processed == {"msg1", "msg2", "msg3"}

    @pytest.mark.asyncio
    async def test_run_monitor_for_user_no_rules(self, plugin, user):
        # Mock config with no rules
        config = UserConfig(rules=[], check_interval_minutes=15)

        with patch.object(plugin, "get_config", return_value=config):
            # Should return early without error
            await plugin._run_monitor_for_user(user)

    @pytest.mark.asyncio
    async def test_run_monitor_for_user_no_enabled_rules(self, plugin, user):
        # Mock config with disabled rule
        rule = EmailRule(
            name="Test", filter=EmailFilter(), actions=[], enabled=False
        )
        config = UserConfig(rules=[rule], check_interval_minutes=15)

        with patch.object(plugin, "get_config", return_value=config):
            await plugin._run_monitor_for_user(user)

    @pytest.mark.asyncio
    async def test_run_monitor_for_user_not_authenticated(
        self, plugin, user, sample_config
    ):
        mock_gmail = AsyncMock()
        mock_gmail.is_authenticated.return_value = False

        with (
            patch.object(plugin, "get_config", return_value=sample_config),
            patch(
                "spanreed.plugins.gmail_monitor.GmailApi.for_user",
                return_value=mock_gmail,
            ),
        ):

            await plugin._run_monitor_for_user(user)
            mock_gmail.is_authenticated.assert_called_once()

    @pytest.mark.asyncio
    @freeze_time("2023-01-15 12:00:00")
    async def test_run_monitor_for_user_with_matches(
        self, plugin, user, sample_config, sample_email
    ):
        mock_gmail = AsyncMock()
        mock_gmail.is_authenticated.return_value = True
        mock_gmail.get_messages_since.return_value = [sample_email]

        mock_bot = AsyncMock()
        mock_action = AsyncMock()

        with (
            patch.object(plugin, "get_config", return_value=sample_config),
            patch(
                "spanreed.plugins.gmail_monitor.GmailApi.for_user",
                return_value=mock_gmail,
            ),
            patch(
                "spanreed.plugins.gmail_monitor.TelegramBotApi.for_user",
                return_value=mock_bot,
            ),
            patch.object(
                plugin, "_get_processed_email_ids", return_value=set()
            ),
            patch.object(
                plugin, "_mark_emails_processed"
            ) as mock_mark_processed,
            patch.object(plugin, "get_user_data", return_value=None),
            patch.object(plugin, "set_user_data") as mock_set_data,
        ):

            # Mock the telegram action handler
            plugin._action_handlers["telegram_notification"] = mock_action

            await plugin._run_monitor_for_user(user)

            # Verify email was processed
            mock_mark_processed.assert_called_once_with(user, ["msg123"])

            # Verify action was executed
            mock_action.execute.assert_called_once()

            # Verify last check time was updated
            mock_set_data.assert_called()

    @pytest.mark.asyncio
    async def test_run_monitor_for_user_skip_processed_emails(
        self, plugin, user, sample_config, sample_email
    ):
        mock_gmail = AsyncMock()
        mock_gmail.is_authenticated.return_value = True
        mock_gmail.get_messages_since.return_value = [sample_email]

        mock_action = AsyncMock()

        with (
            patch.object(plugin, "get_config", return_value=sample_config),
            patch(
                "spanreed.plugins.gmail_monitor.GmailApi.for_user",
                return_value=mock_gmail,
            ),
            patch.object(
                plugin, "_get_processed_email_ids", return_value={"msg123"}
            ),
            patch.object(plugin, "get_user_data", return_value=None),
            patch.object(plugin, "set_user_data"),
        ):

            plugin._action_handlers["telegram_notification"] = mock_action

            await plugin._run_monitor_for_user(user)

            # Action should not be executed for already processed email
            mock_action.execute.assert_not_called()
