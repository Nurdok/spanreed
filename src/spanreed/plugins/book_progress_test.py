import asyncio
import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from spanreed.apis.audible import AudibleBook
from spanreed.plugin import Plugin
from spanreed.plugins.book_progress import (
    BookProgressPlugin,
    find_match_candidates,
)
from spanreed.test_utils import (
    mock_user_find_by_id,
    patch_obsidian,
    patch_redis,
    patch_telegram_bot,
)


def _book(
    asin: str,
    title: str,
    percent_complete: float = 0.0,
    is_finished: bool = False,
) -> AudibleBook:
    return AudibleBook(
        asin=asin,
        title=title,
        subtitle=None,
        percent_complete=percent_complete,
        is_finished=is_finished,
    )


def _note(path: str, title: str | None, asin: str | None) -> SimpleNamespace:
    return SimpleNamespace(title=title, asin=asin, file={"path": path})


def test_name() -> None:
    Plugin.reset_registry()
    plugin = BookProgressPlugin()

    assert plugin.name() == "Audible Book Progress"
    assert plugin.canonical_name() == "audible-book-progress"


def test_find_match_candidates_handles_subtitle_suffix() -> None:
    books = [
        _book("B1", "The Way of Kings: The Stormlight Archive, Book 1"),
        _book("B2", "Project Hail Mary"),
    ]

    candidates = find_match_candidates("The Way of Kings", books)

    assert [book.asin for book in candidates] == ["B1"]


def test_find_match_candidates_orders_best_first() -> None:
    books = [
        _book("B1", "The Colour of Magic"),
        _book("B2", "A Colour of Magic Companion"),
    ]

    candidates = find_match_candidates("The Colour of Magic", books)

    assert [book.asin for book in candidates] == ["B1", "B2"]


def test_find_match_candidates_no_match() -> None:
    books = [_book("B1", "Project Hail Mary")]

    assert find_match_candidates("The Way of Kings", books) == []


@patch("spanreed.plugins.book_progress.AudibleApi", autospec=True)
@patch_obsidian("spanreed.plugins.book_progress")
@patch_telegram_bot("spanreed.plugins.book_progress")
@patch_redis
def test_sync_updates_progress_for_linked_note(
    mock_redis: Any,
    mock_bot: AsyncMock,
    mock_obsidian: AsyncMock,
    mock_audible: AsyncMock,
) -> None:
    Plugin.reset_registry()
    plugin = BookProgressPlugin()

    mock_obsidian.query_dataview.return_value = [
        _note("Books/The Way of Kings.md", "The Way of Kings", "B1")
    ]
    mock_audible.for_user = AsyncMock(return_value=mock_audible)
    mock_audible.get_library = AsyncMock(
        return_value=[_book("B1", "The Way of Kings", percent_complete=42.4)]
    )
    mock_obsidian.get_property.return_value = None

    user = mock_user_find_by_id(4)
    updated = asyncio.run(plugin._sync(user))

    assert updated == 1
    mock_obsidian.set_value_of_property.assert_awaited_once_with(
        "Books/The Way of Kings.md", "progress", 42
    )
    mock_bot.request_user_choice.assert_not_awaited()


@patch("spanreed.plugins.book_progress.AudibleApi", autospec=True)
@patch_obsidian("spanreed.plugins.book_progress")
@patch_telegram_bot("spanreed.plugins.book_progress")
@patch_redis
def test_sync_skips_unchanged_progress(
    mock_redis: Any,
    mock_bot: AsyncMock,
    mock_obsidian: AsyncMock,
    mock_audible: AsyncMock,
) -> None:
    Plugin.reset_registry()
    plugin = BookProgressPlugin()

    mock_obsidian.query_dataview.return_value = [
        _note("Books/The Way of Kings.md", "The Way of Kings", "B1")
    ]
    mock_audible.for_user = AsyncMock(return_value=mock_audible)
    mock_audible.get_library = AsyncMock(
        return_value=[_book("B1", "The Way of Kings", percent_complete=42.4)]
    )
    mock_obsidian.get_property.return_value = 42

    user = mock_user_find_by_id(4)
    updated = asyncio.run(plugin._sync(user))

    assert updated == 0
    mock_obsidian.set_value_of_property.assert_not_awaited()


@patch("spanreed.plugins.book_progress.AudibleApi", autospec=True)
@patch_obsidian("spanreed.plugins.book_progress")
@patch_telegram_bot("spanreed.plugins.book_progress")
@patch_redis
def test_unlinked_note_verification_persists_asin(
    mock_redis: Any,
    mock_bot: AsyncMock,
    mock_obsidian: AsyncMock,
    mock_audible: AsyncMock,
) -> None:
    Plugin.reset_registry()
    plugin = BookProgressPlugin()

    mock_redis.get.return_value = None  # No snooze/not-audible markers.
    mock_obsidian.query_dataview.return_value = [
        _note("Books/Project Hail Mary.md", "Project Hail Mary", None)
    ]
    mock_audible.for_user = AsyncMock(return_value=mock_audible)
    mock_audible.get_library = AsyncMock(
        return_value=[
            _book("B1", "The Way of Kings"),
            _book("B2", "Project Hail Mary", percent_complete=10.0),
        ]
    )
    mock_obsidian.get_property.return_value = None
    # The only candidate offered should be "Project Hail Mary"; pick it.
    mock_bot.request_user_choice.return_value = 0

    user = mock_user_find_by_id(4)
    updated = asyncio.run(plugin._sync(user))

    assert updated == 1
    mock_obsidian.set_value_of_property.assert_any_await(
        "Books/Project Hail Mary.md", "asin", "B2"
    )
    mock_obsidian.set_value_of_property.assert_any_await(
        "Books/Project Hail Mary.md", "progress", 10
    )


@patch("spanreed.plugins.book_progress.AudibleApi", autospec=True)
@patch_obsidian("spanreed.plugins.book_progress")
@patch_telegram_bot("spanreed.plugins.book_progress")
@patch_redis
def test_not_on_audible_choice_is_remembered(
    mock_redis: Any,
    mock_bot: AsyncMock,
    mock_obsidian: AsyncMock,
    mock_audible: AsyncMock,
) -> None:
    Plugin.reset_registry()
    plugin = BookProgressPlugin()

    mock_redis.get.return_value = None
    mock_obsidian.query_dataview.return_value = [
        _note("Books/Project Hail Mary.md", "Project Hail Mary", None)
    ]
    mock_audible.for_user = AsyncMock(return_value=mock_audible)
    mock_audible.get_library = AsyncMock(
        return_value=[_book("B2", "Project Hail Mary")]
    )
    # One candidate, so index 1 is "Not on Audible".
    mock_bot.request_user_choice.return_value = 1

    user = mock_user_find_by_id(4)
    updated = asyncio.run(plugin._sync(user))

    assert updated == 0
    mock_obsidian.set_value_of_property.assert_not_awaited()
    not_audible_keys = [
        call.args[0]
        for call in mock_redis.set.await_args_list
        if "not-audible:Books/Project Hail Mary.md" in call.args[0]
    ]
    assert len(not_audible_keys) == 1


@patch("spanreed.plugins.book_progress.AudibleApi", autospec=True)
@patch_obsidian("spanreed.plugins.book_progress")
@patch_telegram_bot("spanreed.plugins.book_progress")
@patch_redis
def test_finished_book_prompts_and_marks_read(
    mock_redis: Any,
    mock_bot: AsyncMock,
    mock_obsidian: AsyncMock,
    mock_audible: AsyncMock,
) -> None:
    Plugin.reset_registry()
    plugin = BookProgressPlugin()

    mock_redis.get.return_value = None  # Not prompted before.
    mock_obsidian.query_dataview.return_value = [
        _note("Books/The Way of Kings.md", "The Way of Kings", "B1")
    ]
    mock_audible.for_user = AsyncMock(return_value=mock_audible)
    mock_audible.get_library = AsyncMock(
        return_value=[_book("B1", "The Way of Kings", percent_complete=100.0)]
    )
    mock_obsidian.get_property.return_value = 98
    mock_bot.request_user_choice.return_value = 0  # Yes, mark as read.

    user = mock_user_find_by_id(4)
    asyncio.run(plugin._sync(user))

    mock_obsidian.set_value_of_property.assert_any_await(
        "Books/The Way of Kings.md", "progress", 100
    )
    mock_obsidian.set_value_of_property.assert_any_await(
        "Books/The Way of Kings.md", "status", "read"
    )
    mock_obsidian.set_value_of_property.assert_any_await(
        "Books/The Way of Kings.md",
        "finish-date",
        datetime.date.today().strftime("%Y-%m-%d"),
    )


@patch("spanreed.plugins.book_progress.AudibleApi", autospec=True)
@patch_obsidian("spanreed.plugins.book_progress")
@patch_telegram_bot("spanreed.plugins.book_progress")
@patch_redis
def test_finish_prompt_only_happens_once(
    mock_redis: Any,
    mock_bot: AsyncMock,
    mock_obsidian: AsyncMock,
    mock_audible: AsyncMock,
) -> None:
    Plugin.reset_registry()
    plugin = BookProgressPlugin()

    # A previous prompt was recorded for this book.
    mock_redis.get.return_value = "2026-07-01T00:00:00"
    mock_obsidian.query_dataview.return_value = [
        _note("Books/The Way of Kings.md", "The Way of Kings", "B1")
    ]
    mock_audible.for_user = AsyncMock(return_value=mock_audible)
    mock_audible.get_library = AsyncMock(
        return_value=[_book("B1", "The Way of Kings", percent_complete=100.0)]
    )
    mock_obsidian.get_property.return_value = 100

    user = mock_user_find_by_id(4)
    asyncio.run(plugin._sync(user))

    mock_bot.request_user_choice.assert_not_awaited()
    mock_obsidian.set_value_of_property.assert_not_awaited()
