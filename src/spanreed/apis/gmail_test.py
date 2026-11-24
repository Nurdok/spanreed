import asyncio
import datetime
from unittest.mock import AsyncMock, patch

from spanreed.apis.gmail import GmailApi
from spanreed.test_utils import mock_user_find_by_id


def test_collect_attachment_parts_finds_nested_attachment() -> None:
    # multipart/mixed wrapping a multipart/alternative body plus a PDF part.
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": "x"}},
                    {"mimeType": "text/html", "body": {"data": "y"}},
                ],
            },
            {
                "mimeType": "application/pdf",
                "filename": "receipt.pdf",
                "body": {"attachmentId": "a1"},
            },
        ],
    }

    parts = GmailApi._collect_attachment_parts(payload)

    assert [p["filename"] for p in parts] == ["receipt.pdf"]


def test_collect_attachment_parts_none_when_no_filename() -> None:
    payload = {"mimeType": "text/plain", "body": {"data": "x"}}
    assert GmailApi._collect_attachment_parts(payload) == []


def test_messages_after_a_moment_are_asked_for_in_seconds() -> None:
    gmail = GmailApi(mock_user_find_by_id(4))
    moment = datetime.datetime(2026, 5, 9, 10, 0, tzinfo=datetime.timezone.utc)

    with patch.object(
        GmailApi, "get_recent_messages", new=AsyncMock(return_value=[])
    ) as recent:
        asyncio.run(gmail.get_messages_after(moment))

    # Not a date: Gmail takes a date as midnight in Pacific time.
    recent.assert_awaited_once_with(query="after:1778320800")
