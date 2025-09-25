import asyncio
import datetime
import json
import re
from dataclasses import dataclass, asdict
from typing import Optional, Any, Dict, List
from contextlib import suppress

from spanreed.plugin import Plugin
from spanreed.user import User
from spanreed.apis.telegram_bot import (
    PluginCommand,
    TelegramBotApi,
    UserInteractionPriority,
    UserInteractionPreempted,
)
from spanreed.apis.gmail import GmailApi, EmailMessage, GmailAuthenticationFlow
from spanreed.plugins.gmail_monitor_actions import (
    EmailActionHandler,
    TelegramNotificationAction,
    EmailMatch,
)
from spanreed.plugins.spanreed_monitor import suppress_and_log_exception


@dataclass
class EmailFilter:
    sender_regex: Optional[str] = None
    subject_regex: Optional[str] = None
    body_regex: Optional[str] = None
    has_attachments: Optional[bool] = None

    def matches(self, email: EmailMessage) -> bool:
        if self.sender_regex and not re.search(self.sender_regex, email.sender, re.IGNORECASE):
            return False

        if self.subject_regex and not re.search(self.subject_regex, email.subject, re.IGNORECASE):
            return False

        if self.body_regex and not re.search(self.body_regex, email.body, re.IGNORECASE):
            return False

        if self.has_attachments is not None and email.has_attachments != self.has_attachments:
            return False

        return True


@dataclass
class EmailAction:
    type: str
    config: Dict[str, Any]

    def __post_init__(self) -> None:
        if isinstance(self.config, dict):
            pass  # Already a dict
        else:
            self.config = {}


@dataclass
class EmailRule:
    name: str
    filter: EmailFilter
    actions: List[EmailAction]
    enabled: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.filter, dict):
            self.filter = EmailFilter(**self.filter)

        for index, action in enumerate(self.actions):
            if isinstance(action, dict):
                self.actions[index] = EmailAction(**action)


@dataclass
class UserConfig:
    rules: List[EmailRule]
    check_interval_minutes: int = 15

    def __post_init__(self) -> None:
        for index, rule in enumerate(self.rules):
            if isinstance(rule, dict):
                self.rules[index] = EmailRule(**rule)


class GmailMonitorPlugin(Plugin[UserConfig]):

    def __init__(self) -> None:
        super().__init__()
        self._action_handlers: Dict[str, EmailActionHandler] = {
            'telegram_notification': TelegramNotificationAction(),
        }

    @classmethod
    def name(cls) -> str:
        return "Gmail Monitor"

    @classmethod
    def has_user_config(cls) -> bool:
        return True

    @classmethod
    def get_config_class(cls) -> type[UserConfig]:
        return UserConfig

    async def run(self) -> None:
        await TelegramBotApi.register_command(
            self,
            PluginCommand(
                text="Manage Email Rules",
                callback=self.manage_email_rules,
            ),
        )

        await TelegramBotApi.register_command(
            self,
            PluginCommand(
                text="Test Email Rule",
                callback=self.test_email_rule,
            ),
        )

        await TelegramBotApi.register_command(
            self,
            PluginCommand(
                text="Email Monitor Status",
                callback=self.show_monitor_status,
            ),
        )

        await super().run()

    @classmethod
    async def ask_for_user_config(cls, user: User) -> None:
        bot = await TelegramBotApi.for_user(user)

        # Check if app-level Gmail credentials are configured
        if not await GmailApi.is_app_configured():
            await bot.send_message(
                "Gmail app credentials not configured. Please contact your admin to set up Gmail integration."
            )
            return

        # Start with empty rules - user can add them later
        config = UserConfig(
            rules=[],
            check_interval_minutes=15
        )

        await cls.set_config(user, config)

        # Authenticate user with Gmail
        await cls._setup_gmail_auth(user, bot)

    @classmethod
    async def _setup_gmail_auth(cls, user: User, bot: TelegramBotApi) -> None:
        gmail = await GmailApi.for_user(user)

        if await gmail.is_authenticated():
            await bot.send_message("Gmail is already authenticated!")
            return

        try:
            auth_flow = await GmailApi.start_authentication(user)
            auth_done = await auth_flow.get_done_event()

            auth_url = await auth_flow.get_auth_url()
            await bot.send_message(
                f"Click [here]({auth_url}) to authenticate with Gmail.",
                parse_markdown=True,
            )

            await auth_done.wait()
            await bot.send_message("Gmail authentication successful!")

        except Exception as e:
            await bot.send_message(f"Gmail authentication failed: {str(e)}")
            raise

    async def manage_email_rules(self, user: User) -> None:
        bot = await TelegramBotApi.for_user(user)
        config = await self.get_config(user)

        while True:
            if not config.rules:
                await bot.send_message("You have no email rules configured.")
            else:
                rule_status = []
                for rule in config.rules:
                    status = "✅" if rule.enabled else "❌"
                    rule_status.append(f"{status} {rule.name}")

                await bot.send_message(
                    "Your email rules:\n" + "\n".join(rule_status)
                )

            choice = await bot.request_user_choice(
                "What would you like to do?",
                [
                    "Add new rule",
                    "Edit existing rule",
                    "Delete rule",
                    "Enable/Disable rule",
                    "Done",
                ],
            )

            if choice == 0:  # Add new rule
                await self._add_email_rule(user, bot, config)
            elif choice == 1:  # Edit existing rule
                await self._edit_email_rule(user, bot, config)
            elif choice == 2:  # Delete rule
                await self._delete_email_rule(user, bot, config)
            elif choice == 3:  # Enable/Disable rule
                await self._toggle_email_rule(user, bot, config)
            elif choice == 4:  # Done
                break

    async def _add_email_rule(self, user: User, bot: TelegramBotApi, config: UserConfig) -> None:
        rule_name = await bot.request_user_input("Enter a name for this email rule:")

        # Check for duplicate names
        if any(rule.name.lower() == rule_name.lower() for rule in config.rules):
            await bot.send_message(f"Rule '{rule_name}' already exists!")
            return

        # Create filter
        email_filter = await self._create_email_filter(bot)
        if email_filter is None:
            return

        # Create actions
        actions = await self._create_email_actions(bot)
        if not actions:
            return

        # Create and add rule
        new_rule = EmailRule(
            name=rule_name,
            filter=email_filter,
            actions=actions,
            enabled=True
        )

        config.rules.append(new_rule)
        await self.set_config(user, config)

        await bot.send_message(f"Email rule '{rule_name}' added successfully!")

    async def _create_email_filter(self, bot: TelegramBotApi) -> Optional[EmailFilter]:
        await bot.send_message("Let's create the email filter criteria.")

        criteria = []
        sender_regex = None
        subject_regex = None
        body_regex = None
        has_attachments = None

        # Sender filter
        if await bot.request_user_choice(
            "Do you want to filter by sender?", ["Yes", "No"]
        ) == 0:
            sender_regex = await bot.request_user_input(
                "Enter sender regex pattern (e.g., '.*@example\\.com'):"
            )
            criteria.append(f"Sender: {sender_regex}")

        # Subject filter
        if await bot.request_user_choice(
            "Do you want to filter by subject?", ["Yes", "No"]
        ) == 0:
            subject_regex = await bot.request_user_input(
                "Enter subject regex pattern (e.g., 'invoice|receipt'):"
            )
            criteria.append(f"Subject: {subject_regex}")

        # Body filter
        if await bot.request_user_choice(
            "Do you want to filter by email body content?", ["Yes", "No"]
        ) == 0:
            body_regex = await bot.request_user_input(
                "Enter body regex pattern:"
            )
            criteria.append(f"Body: {body_regex}")

        # Attachment filter
        attachment_choice = await bot.request_user_choice(
            "Filter by attachments?",
            ["Must have attachments", "Must not have attachments", "Don't care"]
        )
        if attachment_choice == 0:
            has_attachments = True
            criteria.append("Has attachments: Yes")
        elif attachment_choice == 1:
            has_attachments = False
            criteria.append("Has attachments: No")

        if not criteria:
            await bot.send_message("You must specify at least one filter criteria!")
            return None

        # Confirm filter
        await bot.send_message(
            "Filter criteria:\n" + "\n".join(f"• {c}" for c in criteria)
        )

        if await bot.request_user_choice("Is this correct?", ["Yes", "No"]) == 1:
            return None

        return EmailFilter(
            sender_regex=sender_regex,
            subject_regex=subject_regex,
            body_regex=body_regex,
            has_attachments=has_attachments
        )

    async def _create_email_actions(self, bot: TelegramBotApi) -> List[EmailAction]:
        actions = []

        await bot.send_message("Now let's set up actions to take when emails match this filter.")

        # Currently only support telegram notifications, but structured for extension
        action_choice = await bot.request_user_choice(
            "What action should be taken?",
            ["Send Telegram notification", "Done"]
        )

        if action_choice == 0:  # Telegram notification
            # Configure notification options
            include_body = await bot.request_user_choice(
                "Include email body in notification?", ["Yes", "No"]
            ) == 0

            include_snippet = await bot.request_user_choice(
                "Include email snippet in notification?", ["Yes", "No"]
            ) == 0

            custom_message = ""
            if await bot.request_user_choice(
                "Add custom message prefix?", ["Yes", "No"]
            ) == 0:
                custom_message = await bot.request_user_input(
                    "Enter custom message:"
                )

            action_config = {
                'include_body': include_body,
                'include_snippet': include_snippet,
                'custom_message': custom_message
            }

            actions.append(EmailAction(type='telegram_notification', config=action_config))

        return actions

    async def _edit_email_rule(self, user: User, bot: TelegramBotApi, config: UserConfig) -> None:
        if not config.rules:
            await bot.send_message("No rules to edit!")
            return

        rule_names = [rule.name for rule in config.rules] + ["Cancel"]
        choice = await bot.request_user_choice("Which rule to edit?", rule_names)

        if choice == len(config.rules):  # Cancel
            return

        rule = config.rules[choice]
        await bot.send_message(f"Editing rule: {rule.name}")
        # For now, just recreate the rule
        # TODO: More granular editing
        await bot.send_message("Rule editing will recreate the entire rule.")

        if await bot.request_user_choice("Continue?", ["Yes", "No"]) == 1:
            return

        # Remove old rule
        config.rules.remove(rule)

        # Create new rule with same name
        email_filter = await self._create_email_filter(bot)
        if email_filter is None:
            config.rules.append(rule)  # Restore if cancelled
            return

        actions = await self._create_email_actions(bot)
        if not actions:
            config.rules.append(rule)  # Restore if cancelled
            return

        new_rule = EmailRule(
            name=rule.name,
            filter=email_filter,
            actions=actions,
            enabled=rule.enabled
        )

        config.rules.append(new_rule)
        await self.set_config(user, config)

        await bot.send_message(f"Rule '{rule.name}' updated successfully!")

    async def _delete_email_rule(self, user: User, bot: TelegramBotApi, config: UserConfig) -> None:
        if not config.rules:
            await bot.send_message("No rules to delete!")
            return

        rule_names = [rule.name for rule in config.rules] + ["Cancel"]
        choice = await bot.request_user_choice("Which rule to delete?", rule_names)

        if choice == len(config.rules):  # Cancel
            return

        rule = config.rules[choice]

        if await bot.request_user_choice(
            f"Delete rule '{rule.name}'?", ["Yes", "No"]
        ) == 0:
            config.rules.remove(rule)
            await self.set_config(user, config)
            await bot.send_message(f"Rule '{rule.name}' deleted!")

    async def _toggle_email_rule(self, user: User, bot: TelegramBotApi, config: UserConfig) -> None:
        if not config.rules:
            await bot.send_message("No rules to toggle!")
            return

        rule_names = [
            f"{'Disable' if rule.enabled else 'Enable'} {rule.name}"
            for rule in config.rules
        ] + ["Cancel"]

        choice = await bot.request_user_choice("Which rule to toggle?", rule_names)

        if choice == len(config.rules):  # Cancel
            return

        rule = config.rules[choice]
        rule.enabled = not rule.enabled
        await self.set_config(user, config)

        status = "enabled" if rule.enabled else "disabled"
        await bot.send_message(f"Rule '{rule.name}' {status}!")

    async def test_email_rule(self, user: User) -> None:
        bot = await TelegramBotApi.for_user(user)
        config = await self.get_config(user)

        if not config.rules:
            await bot.send_message("No rules configured to test!")
            return

        # Select rule to test
        rule_names = [rule.name for rule in config.rules] + ["Cancel"]
        choice = await bot.request_user_choice("Which rule to test?", rule_names)

        if choice == len(config.rules):  # Cancel
            return

        rule = config.rules[choice]

        try:
            gmail = await GmailApi.for_user(user)
            if not await gmail.is_authenticated():
                await bot.send_message("Gmail not authenticated! Please reconfigure the plugin.")
                return

            await bot.send_message("Fetching recent emails to test against...")

            # Get recent 50 emails
            recent_emails = await gmail.get_recent_messages(max_results=500)

            await bot.send_message(f"Got {len(recent_emails)} emails to test against.")

            # Debug: Show rule criteria
            criteria = []
            if rule.filter.sender_regex:
                criteria.append(f"Sender: {rule.filter.sender_regex}")
            if rule.filter.subject_regex:
                criteria.append(f"Subject: {rule.filter.subject_regex}")
            if rule.filter.body_regex:
                criteria.append(f"Body: {rule.filter.body_regex}")
            if rule.filter.has_attachments is not None:
                criteria.append(f"Has attachments: {rule.filter.has_attachments}")

            await bot.send_message(f"Rule criteria:\n" + "\n".join(f"• {c}" for c in criteria))

            matches = []
            # Debug: Show first few emails being tested
            debug_emails = recent_emails[:3]  # Show first 3 emails for debugging
            debug_info = []

            for email in recent_emails:
                # Debug info for first 3 emails
                if email in debug_emails:
                    debug_info.append(
                        f"Email {len(debug_info) + 1}:\n"
                        f"  From: {email.sender}\n"
                        f"  Subject: {email.subject}\n"
                        f"  Body preview: {email.body[:100]}...\n"
                        f"  Has attachments: {email.has_attachments}\n"
                        f"  Matches: {rule.filter.matches(email)}"
                    )

                if rule.filter.matches(email):
                    matches.append(email)

            # Show debug info
            if debug_info:
                await bot.send_message("Debug - First 3 emails:\n\n" + "\n\n".join(debug_info))

            if not matches:
                await bot.send_message(f"No recent emails match rule '{rule.name}'.")
            else:
                await bot.send_message(
                    f"Found {len(matches)} matching email(s) for rule '{rule.name}':\n\n" +
                    "\n".join([
                        f"• From: {email.sender}\n  Subject: {email.subject}\n  Date: {email.date.strftime('%Y-%m-%d %H:%M')}"
                        for email in matches[:5]  # Show first 5
                    ]) +
                    (f"\n\n... and {len(matches) - 5} more" if len(matches) > 5 else "")
                )

        except Exception as e:
            await bot.send_message(f"Error testing rule: {str(e)}")

    async def show_monitor_status(self, user: User) -> None:
        bot = await TelegramBotApi.for_user(user)
        config = await self.get_config(user)

        gmail = await GmailApi.for_user(user)
        auth_status = "✅ Authenticated" if await gmail.is_authenticated() else "❌ Not authenticated"

        enabled_rules = [rule for rule in config.rules if rule.enabled]
        disabled_rules = [rule for rule in config.rules if not rule.enabled]

        last_check_key = self._get_user_data_key(user, "last_check")
        last_check = await self.get_user_data(user, "last_check")
        last_check_str = last_check if last_check else "Never"

        status_parts = [
            f"**Gmail Status:** {auth_status}",
            f"**Check Interval:** {config.check_interval_minutes} minutes",
            f"**Last Check:** {last_check_str}",
            f"**Enabled Rules:** {len(enabled_rules)}",
            f"**Disabled Rules:** {len(disabled_rules)}",
        ]

        if enabled_rules:
            status_parts.append("\n**Active Rules:**")
            status_parts.extend([f"• {rule.name}" for rule in enabled_rules])

        await bot.send_message("\n".join(status_parts))

    async def _get_processed_emails_key(self, user: User) -> str:
        return self._get_user_data_key(user, "processed_emails")

    async def _get_processed_email_ids(self, user: User) -> set[str]:
        data = await self.get_user_data(user, "processed_emails")
        if data:
            return set(json.loads(data))
        return set()

    async def _mark_emails_processed(self, user: User, email_ids: List[str]) -> None:
        processed = await self._get_processed_email_ids(user)
        processed.update(email_ids)

        # Keep only recent email IDs to prevent unlimited growth
        # Gmail message IDs are unique, so we can safely limit this
        if len(processed) > 1000:
            processed = set(list(processed)[-500:])  # Keep latest 500

        await self.set_user_data(user, "processed_emails", json.dumps(list(processed)))

    async def run_for_user(self, user: User) -> None:
        self._logger.info(f"Starting Gmail monitor loop for user {user}")

        try:
            while True:
                config = await self.get_config(user)

                # Run the monitor check
                await self._run_monitor_for_user(user)

                # Wait for next check interval
                check_interval = datetime.timedelta(minutes=config.check_interval_minutes)
                self._logger.info(f"Sleeping for {config.check_interval_minutes} minutes")
                await asyncio.sleep(check_interval.total_seconds())

        except Exception as e:
            self._logger.exception(f"Error in Gmail monitor loop for user {user}: {e}")
            raise
        finally:
            self._logger.info("Exiting Gmail monitor run_for_user")

    async def _run_monitor_for_user(self, user: User) -> None:
        self._logger.info(f"Running Gmail monitor for user {user}")
        bot = await TelegramBotApi.for_user(user)

        try:
            config = await self.get_config(user)

            if not config.rules:
                self._logger.info("No rules configured, skipping")
                return

            enabled_rules = [rule for rule in config.rules if rule.enabled]
            if not enabled_rules:
                self._logger.info("No enabled rules, skipping")
                return

            gmail = await GmailApi.for_user(user)
            if not await gmail.is_authenticated():
                self._logger.warning("Gmail not authenticated")
                return

            # Get time since last check
            last_check_data = await self.get_user_data(user, "last_check")
            if last_check_data:
                last_check = datetime.datetime.fromisoformat(last_check_data)
            else:
                # First run - check last hour only
                last_check = datetime.datetime.now() - datetime.timedelta(hours=1)

            # Get recent emails since last check
            recent_emails = await gmail.get_messages_since(last_check)
            self._logger.info(f"Found {len(recent_emails)} recent emails")

            # Get already processed email IDs
            processed_email_ids = await self._get_processed_email_ids(user)

            # Filter out already processed emails
            new_emails = [email for email in recent_emails if email.id not in processed_email_ids]
            self._logger.info(f"Found {len(new_emails)} new emails to process")

            matches = []
            new_processed_ids = []

            # Process each email against each rule
            for email in new_emails:
                new_processed_ids.append(email.id)

                for rule in enabled_rules:
                    if rule.filter.matches(email):
                        match = EmailMatch(email=email, rule_name=rule.name)
                        matches.append((match, rule.actions))

            # Mark emails as processed
            if new_processed_ids:
                await self._mark_emails_processed(user, new_processed_ids)

            # Execute actions for matches
            for match, actions in matches:
                self._logger.info(f"Executing {len(actions)} actions for match: {match.rule_name}")

                for action in actions:
                    if action.type in self._action_handlers:
                        try:
                            handler = self._action_handlers[action.type]
                            await handler.execute(match, action.config, user)
                        except Exception as e:
                            self._logger.error(f"Error executing action {action.type}: {e}")

            # Update last check time
            await self.set_user_data(user, "last_check", datetime.datetime.now().isoformat())

            self._logger.info(f"Gmail monitor completed. Processed {len(new_emails)} emails, found {len(matches)} matches")

        except Exception as e:
            self._logger.exception(f"Error in Gmail monitor for user {user}: {e}")
            raise
