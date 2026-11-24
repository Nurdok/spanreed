import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

from spanreed.apis.audible import AudibleBook
from spanreed.apis.audible_blocks import (
    GetLibrary,
    Library,
    LinkNote,
    LinkNoteInput,
    audible_blocks,
    find_match_candidates,
)
from spanreed.test_utils import mock_user_find_by_id, patch_user_interaction
from spanreed.workflows.blocks import (
    BlockRegistry,
    LocalMemory,
    NoInput,
    RunInfo,
    StepContext,
)
from spanreed.workflows.testing import fake_plugin
from spanreed.workflows.values import to_data


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


def _ctx() -> StepContext:
    return StepContext(
        user=mock_user_find_by_id(4),
        workflow_id="wf_1",
        run=RunInfo(id="run_1", manual=False),
    )


def test_the_audible_blocks_are_well_formed() -> None:
    registry = BlockRegistry()
    for block in audible_blocks():
        registry.register(block, fake_plugin())
    assert [block.id for block in registry.all()] == [
        "audible.get-library",
        "audible.link-note",
    ]
    assert LinkNote.converses is True


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


def test_get_library_gives_the_books_as_one_value() -> None:
    books = [_book("B1", "Dune", 12.5), _book("B2", "Emma")]
    audible = AsyncMock()
    audible.get_library.return_value = books

    with patch(
        "spanreed.apis.audible_blocks.AudibleApi.for_user",
        new=AsyncMock(return_value=audible),
    ):
        output = asyncio.run(GetLibrary().run(_ctx(), NoInput()))

    assert isinstance(output.library, Library)
    assert output.library.books == books
    assert output.count == 2
    # A trace says how many there are, and does not list them.
    assert to_data(output) == {"library": "<2 Audible books>", "count": 2}


def _link(ctx: StepContext, books: list[AudibleBook], **inputs: Any) -> Any:
    given = {"note": "Books/Kings.md", "library": Library(books), **inputs}
    return asyncio.run(LinkNote().run(ctx, LinkNoteInput(**given)))


@patch_user_interaction("spanreed.workflows.blocks")
def test_a_note_with_an_asin_is_matched_without_a_question(bot: AsyncMock) -> None:
    books = [_book("B1", "The Way of Kings", 42.4), _book("B2", "Emma", 99.0)]

    known = _link(_ctx(), books, asin="B1")
    assert (known.linked, known.outcome, known.asin, known.percent) == (
        True,
        "known",
        "B1",
        42,
    )
    assert known.finished is False
    # Nearly all of it counts as finished, whatever Audible says.
    assert _link(_ctx(), books, asin="B2").finished is True
    # The library can also be given as a plain list of books.
    assert _link(_ctx(), [], library=books, asin="B1").linked is True

    missing = _link(_ctx(), books, asin="BX")
    assert (missing.linked, missing.outcome) == (False, "not in library")
    bot.request_user_choice.assert_not_awaited()


@patch("spanreed.apis.audible_blocks.ObsidianApi", autospec=True)
@patch_user_interaction("spanreed.workflows.blocks")
def test_the_user_says_which_book_a_note_is(
    bot: AsyncMock, obsidian: AsyncMock
) -> None:
    obsidian.for_user = AsyncMock(return_value=obsidian)
    obsidian.set_value_of_property = AsyncMock()
    books = [
        _book("B1", "The Way of Kings: The Stormlight Archive, Book 1", 10.0),
        _book("B2", "Project Hail Mary"),
    ]
    bot.request_user_choice.return_value = 0

    chosen = _link(_ctx(), books, title="The Way of Kings", property="audible")

    assert (chosen.linked, chosen.outcome, chosen.asin) == (True, "chosen", "B1")
    bot.request_user_choice.assert_awaited_once_with(
        'Which Audible book is "The Way of Kings"?',
        [
            "The Way of Kings: The Stormlight Archive, Book 1",
            "Not on Audible",
            "Ask me later",
        ],
        columns=1,
    )
    obsidian.set_value_of_property.assert_awaited_once_with(
        "Books/Kings.md", "audible", "B1"
    )


@patch_user_interaction("spanreed.workflows.blocks")
def test_what_the_user_says_about_a_note_is_remembered(bot: AsyncMock) -> None:
    books = [_book("B1", "The Way of Kings")]
    title = "The Way of Kings"
    ctx = _ctx()
    assert isinstance(ctx.memory, LocalMemory)

    bot.request_user_choice.return_value = 1  # Not on Audible
    assert _link(ctx, books, title=title).outcome == "not on audible"
    assert _link(ctx, books, title=title).outcome == "not on audible"
    assert bot.request_user_choice.await_count == 1
    assert list(ctx.memory.kept) == ["not-audible:Books/Kings.md"]

    later = _ctx()
    bot.request_user_choice.return_value = 2  # Ask me later
    assert _link(later, books, title=title).outcome == "later"
    assert _link(later, books, title=title).outcome == "later"
    assert bot.request_user_choice.await_count == 2

    # Nothing close in the library: wait a day, without a question.
    nothing = _ctx()
    assert _link(nothing, [_book("B2", "Emma")], title=title).outcome == "no match"
    assert _link(nothing, [_book("B2", "Emma")], title=title).outcome == "later"
    assert bot.request_user_choice.await_count == 2


@patch_user_interaction("spanreed.workflows.blocks")
def test_a_note_without_a_title_goes_by_its_name(bot: AsyncMock) -> None:
    bot.request_user_choice.return_value = 1

    _link(_ctx(), [_book("B3", "Dune")], note="Library/Books/Dune.md")

    assert bot.request_user_choice.await_args.args[0] == 'Which Audible book is "Dune"?'
