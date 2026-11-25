"""The Audible blocks for workflows.

"Get library" gives the user's Audible books with how far each was listened
to. "Link a note to a book" finds the book that a note in the vault is
about: by the ASIN on the note, or by asking the user which of the books
with a similar title it is.
"""

from __future__ import annotations

import datetime
import difflib
import re
from dataclasses import dataclass, field
from typing import Any

from spanreed.apis.audible import AudibleApi, AudibleBook
from spanreed.apis.obsidian import ObsidianApi
from spanreed.config_form import HELP_KEY, described
from spanreed.workflows.asking import Priority, give_up_field, priority_field
from spanreed.workflows.blocks import Action, Block, NoInput, StepContext

# How long "Ask me later" keeps a note from being asked about.
LINK_SNOOZE = datetime.timedelta(hours=24)
# From here on, a book counts as finished even if Audible does not say so.
FINISH_THRESHOLD_PERCENT = 99
MATCH_SCORE_CUTOFF = 0.5
MAX_MATCH_CANDIDATES = 3
# Telegram cuts the text of a button at about this length.
MAX_CHOICE_LABEL_LENGTH = 60

NOT_ON_AUDIBLE = "Not on Audible"
ASK_ME_LATER = "Ask me later"


def _now() -> datetime.datetime:
    # Indirection so that tests can set the clock.
    return datetime.datetime.now(datetime.timezone.utc)


class Library:
    """The books of an Audible library, as one value.

    A step takes the whole library from "Get library" as this. A run's
    trace shows how many books it holds, and not each of them: a library
    can have a thousand, and every step that gets it would repeat them.
    """

    def __init__(self, books: list[AudibleBook]) -> None:
        """Create the library of these books."""
        self.books = list(books)

    def __repr__(self) -> str:
        """Return how many books there are."""
        return f"<{len(self.books)} Audible books>"


@dataclass
class GetLibraryOutput:
    """The user's Audible library."""

    library: Any = described(
        "Your Audible library. “Link a note to a book” takes it. Its books "
        "are library.books",
    )
    count: int = described("How many books the library has")


class GetLibrary(Action[NoInput, GetLibraryOutput]):
    """Get every book of the user's Audible library, with its progress."""

    id = "audible.get-library"
    title = "Get library"

    async def run(self, ctx: StepContext, inputs: NoInput) -> GetLibraryOutput:
        """Ask Audible for the library."""
        audible = await AudibleApi.for_user(ctx.user)
        books = await audible.get_library()
        return GetLibraryOutput(library=Library(books), count=len(books))


def _normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()


def _match_score(note_title: str, book: AudibleBook) -> float:
    """Return how alike a note's title and an Audible title are, from 0 to 1.

    An Audible title often has the series after a colon ("The Way of Kings:
    The Stormlight Archive, Book 1"), so the part before the colon is
    compared too, and the better of the two counts.
    """
    note = _normalize_title(note_title)
    return max(
        difflib.SequenceMatcher(None, note, _normalize_title(candidate)).ratio()
        for candidate in (book.title, book.title.split(":")[0])
    )


def find_match_candidates(
    note_title: str, books: list[AudibleBook]
) -> list[AudibleBook]:
    """Return the books whose titles are closest to a note's, best first."""
    scored = [(book, _match_score(note_title, book)) for book in books]
    matches = [(book, score) for book, score in scored if score >= MATCH_SCORE_CUTOFF]
    matches.sort(key=lambda pair: pair[1], reverse=True)
    return [book for book, _ in matches[:MAX_MATCH_CANDIDATES]]


@dataclass
class LinkNoteInput:
    """The note, what is known of it, and the library to find its book in."""

    # The path of the note in the vault.
    note: str
    # What "Get library" gave.
    library: Any
    title: str | None = field(
        default=None,
        metadata={HELP_KEY: "The title of the book. Empty: the name of the note"},
    )
    asin: str | None = field(
        default=None,
        metadata={HELP_KEY: "The ASIN the note has. Empty: the user is asked"},
    )
    property: str = field(
        default="asin",
        metadata={HELP_KEY: "The property that the chosen book’s ASIN is written to"},
    )
    priority: Priority = priority_field()
    give_up_after: int = give_up_field()


@dataclass
class LinkNoteOutput:
    """The book of the note, if there is one."""

    linked: bool = described(
        "Whether the note has a book. If not, “outcome” says why",
    )
    outcome: str = described(
        "known: the note had the ASIN. chosen: you chose the book just now. "
        "not in library: the note’s ASIN is not in the library. not on "
        "audible: you said so, now or before. later: you said to ask later, "
        "less than a day ago. no match: no title in the library is close, "
        "which also waits a day",
    )
    asin: str = described(
        "The ASIN of the book: the id that Audible gives it",
        default="",
    )
    title: str = described(
        "The title Audible has for the book",
        default="",
    )
    percent: int = described(
        "How much of it was listened to, in percent, as a whole number",
        default=0,
    )
    finished: bool = described(
        "Whether Audible has the book as finished",
        default=False,
    )


class LinkNote(Action[LinkNoteInput, LinkNoteOutput]):
    """Find the Audible book that a note is about, asking the user if need be.

    A note with an ASIN is matched by it. For a note without one, the user
    is asked which of the closest titles in the library it is, and the
    ASIN of the answer is written to the note, so that it is asked once.
    "Not on Audible" is remembered for good, and "Ask me later" for a day,
    in the memory of the workflow.
    """

    id = "audible.link-note"
    title = "Link a note to a book"
    converses = True

    async def run(self, ctx: StepContext, inputs: LinkNoteInput) -> LinkNoteOutput:
        """Return the book of the note, or why it has none."""
        library = inputs.library
        books: list[AudibleBook] = list(
            library.books if isinstance(library, Library) else library
        )
        if inputs.asin:
            known = next((book for book in books if book.asin == inputs.asin), None)
            if known is None:
                return LinkNoteOutput(linked=False, outcome="not in library")
            return self._linked(known, "known")

        if await ctx.memory.get(f"not-audible:{inputs.note}") is not None:
            return LinkNoteOutput(linked=False, outcome="not on audible")
        snoozed = await ctx.memory.get(f"link-snooze:{inputs.note}")
        if snoozed is not None and _now() < _when(snoozed.at) + LINK_SNOOZE:
            return LinkNoteOutput(linked=False, outcome="later")

        title = inputs.title or _name_of(inputs.note)
        candidates = find_match_candidates(title, books)
        if not candidates:
            await ctx.memory.set(f"link-snooze:{inputs.note}")
            return LinkNoteOutput(linked=False, outcome="no match")

        choice = await ctx.interaction.request_user_choice(
            f'Which Audible book is "{title}"?',
            [book.title[:MAX_CHOICE_LABEL_LENGTH] for book in candidates]
            + [NOT_ON_AUDIBLE, ASK_ME_LATER],
        )
        if 0 <= choice < len(candidates):
            book = candidates[choice]
            obsidian = await ObsidianApi.for_user(ctx.user)
            await obsidian.set_value_of_property(
                inputs.note, inputs.property, book.asin
            )
            return self._linked(book, "chosen")
        if choice == len(candidates):
            await ctx.memory.set(f"not-audible:{inputs.note}")
            return LinkNoteOutput(linked=False, outcome="not on audible")
        await ctx.memory.set(f"link-snooze:{inputs.note}")
        return LinkNoteOutput(linked=False, outcome="later")

    @staticmethod
    def _linked(book: AudibleBook, outcome: str) -> LinkNoteOutput:
        return LinkNoteOutput(
            linked=True,
            outcome=outcome,
            asin=book.asin,
            title=book.title,
            percent=round(book.percent_complete),
            finished=book.is_finished
            or book.percent_complete >= FINISH_THRESHOLD_PERCENT,
        )


def _name_of(note: str) -> str:
    """Return the name of a note: its file name without ``.md``."""
    return note.rsplit("/", 1)[-1].removesuffix(".md")


def _when(at: str) -> datetime.datetime:
    """Return a time that a memory kept, with its timezone."""
    moment = datetime.datetime.fromisoformat(at)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment


def audible_blocks() -> list[Block]:
    """Return the blocks that the Audible plugin publishes."""
    return [GetLibrary(), LinkNote()]
