import asyncio
import base64
import datetime
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from spanreed.apis.obsidian import (
    DailyNote,
    ObsidianApi,
    ObsidianPluginTooOld,
    note_path,
    once_md,
)
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


# -- the daily note ---------------------------------------------------------------

DAY = datetime.date(2026, 3, 1)


def _answering(result: Any) -> ObsidianApi:
    """Return an API whose plugin answers every request with ``result``."""
    api = ObsidianApi(mock_user_find_by_id(1))
    api._send_request = AsyncMock(return_value=result)  # type: ignore[method-assign]
    return api


def test_the_daily_note_of_a_day_is_created_where_obsidian_puts_it() -> None:
    api = _answering({"path": "Journal/01.03.2026.md", "created": True})

    note = asyncio.run(api.create_daily_note(DAY))

    api._send_request.assert_awaited_once_with(  # type: ignore[attr-defined]
        "generate-daily-note", {"date": "2026-03-01"}
    )
    assert note == DailyNote(path="Journal/01.03.2026.md", exists=True, created=True)


def test_a_daily_note_that_was_there_is_not_created() -> None:
    api = _answering({"path": "Daily/2026-03-01.md", "created": False})

    note = asyncio.run(api.create_daily_note(DAY))

    assert note == DailyNote(path="Daily/2026-03-01.md", exists=True, created=False)


@pytest.mark.parametrize("answer", [None, {}, "ok", {"path": None}])
def test_a_plugin_from_before_it_said_where_the_note_is_gives_no_path(
    answer: Any,
) -> None:
    # It made today's note, as it always did, and answered with nothing.
    note = asyncio.run(_answering(answer).create_daily_note(DAY))

    assert note == DailyNote(path="", exists=True, created=False)


@pytest.mark.parametrize("exists", [True, False])
def test_the_daily_note_of_a_day_is_found_without_making_it(exists: bool) -> None:
    api = _answering({"path": "Daily/2026-03-01.md", "exists": exists})

    note = asyncio.run(api.find_daily_note(DAY))

    api._send_request.assert_awaited_once_with(  # type: ignore[attr-defined]
        "get-daily-note", {"date": "2026-03-01"}
    )
    assert note == DailyNote(path="Daily/2026-03-01.md", exists=exists)


def test_an_answer_without_a_path_is_no_daily_note() -> None:
    with pytest.raises(RuntimeError, match="did not say where a daily note is"):
        asyncio.run(_answering(None).find_daily_note(DAY))


def _plugin_answers(response: dict[str, Any]) -> Any:
    """Stand in for Redis, with a plugin that answers a request with ``response``."""
    redis = AsyncMock()
    redis.blpop = AsyncMock(return_value=("queue", json.dumps(response)))
    return redis


def test_a_request_that_the_plugin_does_not_know_says_to_update_the_plugin() -> None:
    api = ObsidianApi(mock_user_find_by_id(1))
    redis = _plugin_answers(
        {"success": False, "result": "unknown method get-daily-note"}
    )

    with patch("spanreed.apis.obsidian.redis_api", new=redis), patch(
        "spanreed.apis.obsidian.UserInteractionApi", autospec=True
    ) as bot:
        bot.for_user = AsyncMock(return_value=bot)
        with pytest.raises(ObsidianPluginTooOld) as error:
            asyncio.run(api.find_daily_note(DAY))

    assert str(error.value) == (
        "The Spanreed plugin for Obsidian is too old: it does not know "
        "“get-daily-note”. Update the plugin."
    )
    # The run says it. It is no failure of the vault to tell about besides.
    bot.notify.assert_not_called()


def test_another_failure_is_still_told_and_raised() -> None:
    api = ObsidianApi(mock_user_find_by_id(1))
    redis = _plugin_answers({"success": False, "result": "not a date: x"})

    with patch("spanreed.apis.obsidian.redis_api", new=redis), patch(
        "spanreed.apis.obsidian.UserInteractionApi", autospec=True
    ) as bot:
        bot.for_user = AsyncMock(return_value=bot)
        bot.notify = AsyncMock()
        with pytest.raises(RuntimeError, match="not a date: x"):
            asyncio.run(api.find_daily_note(DAY))

    bot.notify.assert_awaited_once()


@pytest.mark.parametrize("path", ["", "  ", None])
def test_a_note_without_a_name_is_not_asked_for(path: Any) -> None:
    api = ObsidianApi(mock_user_find_by_id(1))
    redis = _plugin_answers({"success": True, "result": None})

    with patch("spanreed.apis.obsidian.redis_api", new=redis):
        for request in (
            api.get_property(path, "steps"),
            api.set_value_of_property(path, "steps", 1),
            api.append_to_note(path, "text"),
            api.read_file(path),
        ):
            with pytest.raises(ValueError, match="This step names no note") as error:
                asyncio.run(request)
            assert "Update the plugin." in str(error.value)

    # Obsidian is not asked: it would find no such note, and a step that
    # skips a note that is missing would write nothing without a word.
    redis.lpush.assert_not_awaited()
