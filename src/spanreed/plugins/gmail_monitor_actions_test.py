import asyncio
import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from spanreed.plugins.gmail_monitor_actions import (
    DownloadAttachmentAction,
    EmailMatch,
)
from spanreed.apis.gmail import EmailMessage, Attachment
from spanreed.test_utils import mock_user_find_by_id


def _email() -> EmailMessage:
    return EmailMessage(
        id="msg1",
        thread_id="t1",
        sender="outgoing@icount.co.il",
        subject="קבלה מס' 123",
        body="ליאור יהודאי",
        snippet="snippet",
        date=datetime.datetime(2024, 1, 1),
        labels=[],
        has_attachments=True,
    )


@patch("spanreed.plugins.gmail_monitor_actions.GmailApi", autospec=True)
@patch("spanreed.plugins.gmail_monitor_actions.TelegramBotApi", autospec=True)
def test_download_attachment_sends_pdf(
    mock_bot_cls: MagicMock, mock_gmail_cls: MagicMock
) -> None:
    bot = AsyncMock()
    mock_bot_cls.for_user = AsyncMock(return_value=bot)

    gmail = MagicMock()
    gmail.get_attachments = AsyncMock(
        return_value=[Attachment("receipt.pdf", "application/pdf", b"%PDF-data")]
    )
    mock_gmail_cls.for_user = AsyncMock(return_value=gmail)

    user = mock_user_find_by_id(1)
    match = EmailMatch(email=_email(), rule_name="Receipts")

    asyncio.run(
        DownloadAttachmentAction().execute(
            match, {"mime_types": ["application/pdf"]}, user
        )
    )

    gmail.get_attachments.assert_awaited_once_with(
        "msg1", mime_types=["application/pdf"]
    )
    bot.send_document.assert_awaited_once_with("receipt.pdf", b"%PDF-data")


@patch("spanreed.plugins.gmail_monitor_actions.GmailApi", autospec=True)
@patch("spanreed.plugins.gmail_monitor_actions.TelegramBotApi", autospec=True)
def test_download_attachment_notifies_when_none_found(
    mock_bot_cls: MagicMock, mock_gmail_cls: MagicMock
) -> None:
    bot = AsyncMock()
    mock_bot_cls.for_user = AsyncMock(return_value=bot)

    gmail = MagicMock()
    gmail.get_attachments = AsyncMock(return_value=[])
    mock_gmail_cls.for_user = AsyncMock(return_value=gmail)

    user = mock_user_find_by_id(1)
    match = EmailMatch(email=_email(), rule_name="Receipts")

    asyncio.run(
        DownloadAttachmentAction().execute(
            match, {"mime_types": ["application/pdf"]}, user
        )
    )

    bot.send_document.assert_not_called()
    bot.send_message.assert_awaited_once()
