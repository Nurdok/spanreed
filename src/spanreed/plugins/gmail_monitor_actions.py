import abc
from typing import Any, Dict
from dataclasses import dataclass

from spanreed.user import User
from spanreed.apis.gmail import EmailMessage
from spanreed.apis.telegram_bot import TelegramBotApi


@dataclass
class EmailMatch:
    email: EmailMessage
    rule_name: str


class EmailActionHandler(abc.ABC):
    @abc.abstractmethod
    async def execute(self, email_match: EmailMatch, config: Dict[str, Any], user: User) -> None:
        pass


class TelegramNotificationAction(EmailActionHandler):
    async def execute(self, email_match: EmailMatch, config: Dict[str, Any], user: User) -> None:
        bot = await TelegramBotApi.for_user(user)

        email = email_match.email
        rule_name = email_match.rule_name

        # Get configuration with defaults
        include_body = config.get('include_body', False)
        include_snippet = config.get('include_snippet', True)
        custom_message = config.get('custom_message', '')

        # Build notification message
        message_parts = []

        if custom_message:
            message_parts.append(custom_message)
        else:
            message_parts.append(f"📧 Email matched rule: **{rule_name}**")

        message_parts.append(f"**From:** {email.sender}")
        message_parts.append(f"**Subject:** {email.subject}")
        message_parts.append(f"**Date:** {email.date.strftime('%Y-%m-%d %H:%M:%S')}")

        if email.has_attachments:
            message_parts.append("📎 Has attachments")

        if include_snippet and email.snippet:
            message_parts.append(f"**Preview:** {email.snippet}")

        if include_body and email.body:
            # Limit body length to avoid telegram message limits
            body_preview = email.body[:500]
            if len(email.body) > 500:
                body_preview += "..."
            message_parts.append(f"**Body:** {body_preview}")

        message = "\n\n".join(message_parts)

        await bot.send_message(message)