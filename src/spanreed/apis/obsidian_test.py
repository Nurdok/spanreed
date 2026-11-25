import asyncio
import base64
from unittest.mock import AsyncMock

import pytest

from spanreed.apis.obsidian import ObsidianApi, note_path, once_md
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


def test_summarize_request_elides_large_base64_content() -> None:
    request = {
        "request_id": "1",
        "method": "write-file",
        "params": {
            "filepath": "x.pdf",
            "format": "binary",
            "content": "A" * 5000,
            "overwrite": False,
        },
    }
    safe = ObsidianApi._summarize_request(request)

    # The huge payload is replaced; everything else is intact and the original
    # request is not mutated.
    assert safe["params"]["content"] == "<5000 base64 chars elided>"
    assert safe["params"]["filepath"] == "x.pdf"
    assert request["params"]["content"] == "A" * 5000


def test_summarize_request_leaves_short_content_alone() -> None:
    request = {"method": "read-file", "params": {"content": "short"}}
    assert ObsidianApi._summarize_request(request)["params"]["content"] == "short"


def test_delete_file_sends_delete_request() -> None:
    api = ObsidianApi(mock_user_find_by_id(1))
    api._send_request = AsyncMock()  # type: ignore[method-assign]

    asyncio.run(api.delete_file("Assets/scans/2026-01-01 Scan.pdf"))

    api._send_request.assert_awaited_once_with(
        "delete-file", {"filepath": "Assets/scans/2026-01-01 Scan.pdf"}
    )


@pytest.mark.parametrize(
    "folder, title, path",
    [
        ("Books", "Neverwhere", "Books/Neverwhere.md"),
        # The whole title is the name, whatever dots it has.
        ("Books", "Mr. Mercedes", "Books/Mr. Mercedes.md"),
        ("Books", "Dr. Jekyll and Mr. Hyde", "Books/Dr. Jekyll and Mr. Hyde.md"),
        ("Books", "Notes v1.2", "Books/Notes v1.2.md"),
        # A title that was written with its extension has it once.
        ("Health", "Therapy.md", "Health/Therapy.md"),
        ("Health", "Therapy.MD", "Health/Therapy.md"),
        ("Books", "{{ book.title }}.md", "Books/{{ book.title }}.md"),
        # The folder, with or without its slashes, or none.
        ("/a/b/", "T", "a/b/T.md"),
        ("", "T", "T.md"),
    ],
)
def test_a_note_is_named_by_its_whole_title(folder: str, title: str, path: str) -> None:
    assert note_path(folder, title) == path


def test_a_path_that_ends_in_md_twice_gets_one() -> None:
    assert once_md("Health/Therapy.md.md") == "Health/Therapy.md"
    assert once_md("Health/Therapy.MD.md") == "Health/Therapy.md"
    assert once_md("{% set x = 1 %}Books/{{ book.title }}.md.md") == (
        "{% set x = 1 %}Books/{{ book.title }}.md"
    )
    # A path that is right is left alone.
    for path in ("Health/Therapy.md", "Books/Mr. Mercedes.md", "a.md.txt", ""):
        assert once_md(path) is None
