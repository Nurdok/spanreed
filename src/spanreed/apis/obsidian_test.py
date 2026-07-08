import asyncio
import base64
from unittest.mock import AsyncMock

from spanreed.apis.obsidian import ObsidianApi
from spanreed.test_utils import mock_user_find_by_id


def test_write_binary_file_sends_base64_write_request() -> None:
    api = ObsidianApi(mock_user_find_by_id(1))
    api._send_request = AsyncMock()  # type: ignore[method-assign]

    data = b"%PDF-1.7 binary payload"
    asyncio.run(api.write_binary_file("Receipts/2026/x.pdf", data, overwrite=True))

    api._send_request.assert_awaited_once_with(
        "write-file",
        {
            "filepath": "Receipts/2026/x.pdf",
            "format": "binary",
            "content": base64.b64encode(data).decode("ascii"),
            "overwrite": True,
        },
    )


def test_write_binary_file_defaults_to_no_overwrite() -> None:
    api = ObsidianApi(mock_user_find_by_id(1))
    api._send_request = AsyncMock()  # type: ignore[method-assign]

    asyncio.run(api.write_binary_file("note.pdf", b"data"))

    sent = api._send_request.await_args.args
    assert sent[0] == "write-file"
    assert sent[1]["overwrite"] is False
