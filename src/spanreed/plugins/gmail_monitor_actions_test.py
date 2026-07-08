import asyncio
import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from spanreed.plugins.gmail_monitor_actions import (
    DownloadAttachmentAction,
    SaveAttachmentToVaultAction,
    SaveLinkToVaultAction,
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


@patch("spanreed.plugins.gmail_monitor_actions.ObsidianApi", autospec=True)
@patch("spanreed.plugins.gmail_monitor_actions.GmailApi", autospec=True)
@patch("spanreed.plugins.gmail_monitor_actions.TelegramBotApi", autospec=True)
def test_save_attachment_writes_to_vault(
    mock_bot_cls: MagicMock,
    mock_gmail_cls: MagicMock,
    mock_obsidian_cls: MagicMock,
) -> None:
    bot = AsyncMock()
    mock_bot_cls.for_user = AsyncMock(return_value=bot)

    gmail = MagicMock()
    gmail.get_attachments = AsyncMock(
        return_value=[Attachment("receipt.pdf", "application/pdf", b"%PDF-data")]
    )
    mock_gmail_cls.for_user = AsyncMock(return_value=gmail)

    obsidian = MagicMock()
    obsidian.write_binary_file = AsyncMock()
    mock_obsidian_cls.for_user = AsyncMock(return_value=obsidian)

    user = mock_user_find_by_id(1)
    match = EmailMatch(email=_email(), rule_name="Receipts")
    config = {
        "vault_dir": "Receipts/2026",
        "mime_types": ["application/pdf"],
        "filename_template": "{original}",
        "overwrite": False,
    }

    asyncio.run(SaveAttachmentToVaultAction().execute(match, config, user))

    obsidian.write_binary_file.assert_awaited_once_with(
        "Receipts/2026/receipt.pdf", b"%PDF-data", overwrite=False
    )


@patch("spanreed.plugins.gmail_monitor_actions.ObsidianApi", autospec=True)
@patch("spanreed.plugins.gmail_monitor_actions.GmailApi", autospec=True)
@patch("spanreed.plugins.gmail_monitor_actions.TelegramBotApi", autospec=True)
def test_save_attachment_skips_on_conflict(
    mock_bot_cls: MagicMock,
    mock_gmail_cls: MagicMock,
    mock_obsidian_cls: MagicMock,
) -> None:
    bot = AsyncMock()
    mock_bot_cls.for_user = AsyncMock(return_value=bot)

    gmail = MagicMock()
    gmail.get_attachments = AsyncMock(
        return_value=[Attachment("receipt.pdf", "application/pdf", b"%PDF-data")]
    )
    mock_gmail_cls.for_user = AsyncMock(return_value=gmail)

    obsidian = MagicMock()
    # The file already exists and overwrite is off.
    obsidian.write_binary_file = AsyncMock(side_effect=FileExistsError)
    mock_obsidian_cls.for_user = AsyncMock(return_value=obsidian)

    user = mock_user_find_by_id(1)
    match = EmailMatch(email=_email(), rule_name="Receipts")

    # Must not raise; a skip message is sent instead.
    asyncio.run(
        SaveAttachmentToVaultAction().execute(match, {"vault_dir": "Receipts"}, user)
    )

    bot.send_message.assert_awaited_once()
    assert "already exists" in bot.send_message.call_args.args[0]


@patch("spanreed.plugins.gmail_monitor_actions.ObsidianApi", autospec=True)
@patch("spanreed.plugins.gmail_monitor_actions.GmailApi", autospec=True)
@patch("spanreed.plugins.gmail_monitor_actions.TelegramBotApi", autospec=True)
def test_save_attachment_none_found(
    mock_bot_cls: MagicMock,
    mock_gmail_cls: MagicMock,
    mock_obsidian_cls: MagicMock,
) -> None:
    bot = AsyncMock()
    mock_bot_cls.for_user = AsyncMock(return_value=bot)

    gmail = MagicMock()
    gmail.get_attachments = AsyncMock(return_value=[])
    mock_gmail_cls.for_user = AsyncMock(return_value=gmail)

    obsidian = MagicMock()
    obsidian.write_binary_file = AsyncMock()
    mock_obsidian_cls.for_user = AsyncMock(return_value=obsidian)

    user = mock_user_find_by_id(1)
    match = EmailMatch(email=_email(), rule_name="Receipts")

    asyncio.run(SaveAttachmentToVaultAction().execute(match, {"vault_dir": "R"}, user))

    obsidian.write_binary_file.assert_not_called()
    bot.send_message.assert_awaited_once()


def test_build_filename_renders_and_preserves_extension() -> None:
    name = SaveAttachmentToVaultAction._build_filename(
        "{date}-{original}", "receipt.pdf", _email(), 1
    )
    assert name == "2024-01-01-receipt.pdf"


def test_build_filename_appends_extension_when_template_omits_it() -> None:
    name = SaveAttachmentToVaultAction._build_filename(
        "{index}", "receipt.pdf", _email(), 3
    )
    assert name == "3.pdf"


def test_build_filename_falls_back_on_bad_template() -> None:
    name = SaveAttachmentToVaultAction._build_filename(
        "{nonexistent}", "receipt.pdf", _email(), 1
    )
    assert name == "receipt.pdf"


@patch("spanreed.plugins.gmail_monitor_actions.ObsidianApi", autospec=True)
@patch("spanreed.plugins.gmail_monitor_actions.TelegramBotApi", autospec=True)
def test_save_link_writes_downloaded_file_to_vault(
    mock_bot_cls: MagicMock, mock_obsidian_cls: MagicMock
) -> None:
    bot = AsyncMock()
    mock_bot_cls.for_user = AsyncMock(return_value=bot)
    obsidian = MagicMock()
    obsidian.write_binary_file = AsyncMock()
    mock_obsidian_cls.for_user = AsyncMock(return_value=obsidian)

    action = SaveLinkToVaultAction()
    action._extract_links_from_email = AsyncMock(  # type: ignore[method-assign]
        return_value=["https://track.icount.co.il/x"]
    )
    action._download_bytes = AsyncMock(  # type: ignore[method-assign]
        return_value=(b"%PDF-data", "application/pdf", "invoice.pdf")
    )

    user = mock_user_find_by_id(1)
    match = EmailMatch(email=_email(), rule_name="Keren")
    config = {
        "vault_dir": "Assets/Invoices/Inbox",
        "filename_template": "{original}",
        "overwrite": False,
    }

    asyncio.run(action.execute(match, config, user))

    obsidian.write_binary_file.assert_awaited_once_with(
        "Assets/Invoices/Inbox/invoice.pdf", b"%PDF-data", overwrite=False
    )


@patch("spanreed.plugins.gmail_monitor_actions.ObsidianApi", autospec=True)
@patch("spanreed.plugins.gmail_monitor_actions.TelegramBotApi", autospec=True)
def test_save_link_infers_pdf_extension_when_missing(
    mock_bot_cls: MagicMock, mock_obsidian_cls: MagicMock
) -> None:
    bot = AsyncMock()
    mock_bot_cls.for_user = AsyncMock(return_value=bot)
    obsidian = MagicMock()
    obsidian.write_binary_file = AsyncMock()
    mock_obsidian_cls.for_user = AsyncMock(return_value=obsidian)

    action = SaveLinkToVaultAction()
    action._extract_links_from_email = AsyncMock(  # type: ignore[method-assign]
        return_value=["https://track.icount.co.il/redirect"]
    )
    # Redirect endpoint gives no filename and no extension.
    action._download_bytes = AsyncMock(  # type: ignore[method-assign]
        return_value=(b"%PDF", "application/pdf; charset=binary", "download")
    )

    user = mock_user_find_by_id(1)
    match = EmailMatch(email=_email(), rule_name="Keren")

    asyncio.run(action.execute(match, {"vault_dir": "Inbox"}, user))

    obsidian.write_binary_file.assert_awaited_once_with(
        "Inbox/download.pdf", b"%PDF", overwrite=False
    )


@patch("spanreed.plugins.gmail_monitor_actions.ObsidianApi", autospec=True)
@patch("spanreed.plugins.gmail_monitor_actions.TelegramBotApi", autospec=True)
def test_save_link_no_links_found(
    mock_bot_cls: MagicMock, mock_obsidian_cls: MagicMock
) -> None:
    bot = AsyncMock()
    mock_bot_cls.for_user = AsyncMock(return_value=bot)
    obsidian = MagicMock()
    obsidian.write_binary_file = AsyncMock()
    mock_obsidian_cls.for_user = AsyncMock(return_value=obsidian)

    action = SaveLinkToVaultAction()
    action._extract_links_from_email = AsyncMock(  # type: ignore[method-assign]
        return_value=[]
    )

    user = mock_user_find_by_id(1)
    match = EmailMatch(email=_email(), rule_name="Keren")

    asyncio.run(action.execute(match, {"vault_dir": "Inbox"}, user))

    obsidian.write_binary_file.assert_not_called()
    bot.send_message.assert_awaited_once()


def test_filename_from_content_disposition() -> None:
    headers = {"content-disposition": 'attachment; filename="invoice-123.pdf"'}
    assert (
        SaveLinkToVaultAction._filename_from_headers(headers, "https://x/y")
        == "invoice-123.pdf"
    )


def test_filename_from_url_basename() -> None:
    assert (
        SaveLinkToVaultAction._filename_from_headers(
            {}, "https://x/files/doc.pdf?token=1"
        )
        == "doc.pdf"
    )
