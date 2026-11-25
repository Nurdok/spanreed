"""The Timekiller blocks for workflows: three conversations, one card each.

They are the conversations of the Timekiller plugin, moved into blocks as
they were: each goes back on itself (another prompt, another name for a
file), which steps on a page do not do. What can be changed is an input: the
prompts, the heading, the folder.

Each asks through ``ctx.interaction`` and reaches the vault through
``ObsidianApi``.
"""

from __future__ import annotations

import datetime
import pathlib
import random
import re
from dataclasses import dataclass, field
from typing import Any

from spanreed.apis.obsidian import ObsidianApi
from spanreed.config_form import HELP_KEY, described
from spanreed.workflows.asking import Priority, give_up_field, priority_field
from spanreed.workflows.blocks import Action, Block, BlockKind, StepContext

# Indirection so that tests can say which prompt is picked.
_choice = random.choice

PROMPTS = [
    "What are you doing right now?",
    "What are you grateful for today?",
    "What are you looking forward to today?",
    "What are you struggling with today?",
    "What are you proud of today?",
    "What are you excited about today?",
    "What are you worried about today?",
    "Did you make progress on a project today? If so, what?",
    "What is one thing you learned today?",
    "What did you do today to take care of yourself?",
    "What did you do today to take care of someone else?",
    "What did you do today to take care of your home?",
    "What friend did you talk to today?",
    "What family member did you talk to today?",
    "What did you do today for your yearly theme?",
    "Take a picture of you or something you did today.",
    "What did you do for physical health today?",
    "What did you do for mental health today?",
    "What did you do today for fun?",
    "What are you currently watching?",
    "What are you currently listening to?",
    "What is your favorite song right now?",
    "Do you have plans to meet up with friends anytime soon?",
    "What project are you currently working on?",
]

_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# A scan that was not named yet: the scanner calls it "2026-11-20 Scan 1".
_UNNAMED_SCAN_RE = re.compile(r"\d{4}-\d{2}-\d{2} Scan")


def _today(ctx: StepContext) -> datetime.date:
    """Return today's date where the user is."""
    return datetime.datetime.now(ctx.user.tzinfo).date()


# -- journaling ----------------------------------------------------------------


@dataclass
class JournalInput:
    """Where the answers go, and what is asked."""

    # The path of the note that the answers are added to.
    note: str
    prompts: list[str] = field(
        default_factory=lambda: list(PROMPTS),
        metadata={HELP_KEY: "One of these is picked at random each time"},
    )
    heading: str | None = field(
        default=None,
        metadata={
            HELP_KEY: "The heading the answers go under. Empty: the end of the note"
        },
    )
    priority: Priority = priority_field()
    give_up_after: int = give_up_field()

    def __post_init__(self) -> None:
        """Raise ``ValueError`` if there is nothing to ask."""
        if not self.prompts:
            raise ValueError("Journaling needs a prompt or more.")


@dataclass
class JournalOutput:
    """How it went."""

    answered: int = described("How many prompts were answered")


class Journal(Action[JournalInput, JournalOutput]):
    """Offer a journaling prompt, and add the answer to a note.

    The user can take the prompt, ask for another one, or leave. After an
    answer, the user is asked whether to go on with another prompt.
    """

    id = "timekiller.journal"
    title = "Journaling prompt"
    kind = BlockKind.ASK
    converses = True

    async def run(self, ctx: StepContext, inputs: JournalInput) -> JournalOutput:
        """Hold the conversation, and return how many prompts were answered."""
        bot = ctx.interaction
        obsidian = await ObsidianApi.for_user(ctx.user)
        answered = 0
        while True:
            prompt = _choice(inputs.prompts)
            choices = ["Answer", "Change", "Cancel"]
            choice = await bot.request_user_choice(
                f"Prompt: {prompt}", choices, columns=3
            )
            if choices[choice] == "Cancel":
                break
            if choices[choice] == "Change":
                continue

            prompt_answer = await bot.request_user_input(prompt)
            await obsidian.append_to_note(
                inputs.note,
                f"\n\n### {prompt}\n{prompt_answer}\n",
                heading=inputs.heading or None,
            )
            answered += 1
            await bot.send_message("Noted!")
            if await bot.request_user_choice("Another?", ["Yes", "No"], columns=2) == 1:
                break
        return JournalOutput(answered=answered)


# -- the books being read --------------------------------------------------------


@dataclass
class ReadingInput:
    """The books to ask about."""

    books: list[Any] = field(
        metadata={
            HELP_KEY: "The rows of a query for the notes of the books: each "
            "needs the note and a title"
        }
    )
    priority: Priority = priority_field()
    give_up_after: int = give_up_field()


@dataclass
class ReadingOutput:
    """How it went."""

    asked: int = described("How many of the books you were asked about")
    finished: int = described("How many of them were marked as read")


def _value(row: Any, name: str) -> Any:
    return row.get(name) if isinstance(row, dict) else getattr(row, name, None)


def _note_of(row: Any) -> str:
    """Return the path of the note of a row of a query."""
    return str(_value(_value(row, "file"), "path"))


class ReadingCheckIn(Action[ReadingInput, ReadingOutput]):
    """Ask how it is going with each book that the user is reading.

    For each book the user can add notes to the book's note, mark it as
    read with the date it was finished, give it up, go on to the next
    book, or leave.
    """

    id = "timekiller.reading-check-in"
    title = "Reading check-in"
    kind = BlockKind.ASK
    converses = True

    async def run(self, ctx: StepContext, inputs: ReadingInput) -> ReadingOutput:
        """Hold the conversation, and return how many books it was about."""
        bot = ctx.interaction
        obsidian = await ObsidianApi.for_user(ctx.user)
        asked = finished = 0
        for book in inputs.books:
            note = _note_of(book)
            mark_as_finished = False
            choice = await bot.request_user_choice(
                f'How\'s it going with reading "{_value(book, "title")}"?',
                [
                    "No notes (next)",
                    "I have notes",
                    "Finished!",
                    "Giving up",
                    "Cancel",
                ],
                columns=2,
            )
            asked += 1
            if choice == 0:
                continue
            if choice == 4:
                break
            if choice == 2:
                mark_as_finished = True
            if choice == 3:
                mark_as_finished = (
                    await bot.request_user_choice(
                        "Do you want to mark it as finished?", ["Yes", "No"]
                    )
                    == 0
                )
            if mark_as_finished:
                await obsidian.set_value_of_property(note, "status", "read")
                finished += 1
                finish_date = await self._finish_date(ctx)
                if finish_date is not None:
                    await obsidian.set_value_of_property(
                        note, "finish-date", finish_date.strftime("%Y-%m-%d")
                    )
            if choice == 1:
                await obsidian.append_to_note(
                    note,
                    "\n\n### Thoughts\n"
                    + await bot.request_user_input("Go ahead then:"),
                )
        return ReadingOutput(asked=asked, finished=finished)

    @staticmethod
    async def _finish_date(ctx: StepContext) -> datetime.date | None:
        """Ask when a book was finished. Return ``None`` if the user skips it."""
        bot = ctx.interaction
        choice = await bot.request_user_choice(
            "When did you finish it?",
            ["Today", "Yesterday", "Other (specify)", "Other (skip)"],
        )
        if choice == 0:
            return _today(ctx)
        if choice == 1:
            return _today(ctx) - datetime.timedelta(days=1)
        if choice != 2:
            return None
        while True:
            written = await bot.request_user_input(
                "When did you finish it?\nUse the format YYYY-MM-DD."
            )
            try:
                return datetime.datetime.strptime(written.strip(), "%Y-%m-%d").date()
            except ValueError:
                # The plugin failed here. Ask again instead.
                continue


# -- scans -----------------------------------------------------------------------


@dataclass
class ProcessScanInput:
    """Where the scans are."""

    folder: str = "Assets/scans"
    priority: Priority = priority_field()
    give_up_after: int = give_up_field()


@dataclass
class ProcessScanOutput:
    """What became of the scan."""

    outcome: str = described(
        "What became of the scan: renamed, deleted, skipped (for good), "
        "cancelled, or nothing to do if no scan waits for a name",
    )
    path: str = described(
        "The path of the scan that the conversation was about",
        default="",
    )
    new_path: str = described(
        "The path of the scan after it was renamed. Empty if it was not",
        default="",
    )


def skipped_name(path: str) -> str:
    """Return the name under which a workflow remembers that a scan is skipped."""
    return f"skipped:{path}"


class ProcessScan(Action[ProcessScanInput, ProcessScanOutput]):
    """Send the user a scan that has no name yet, and ask what to do with it.

    A scan has no name yet if it is a PDF called like "2026-11-20 Scan 1".
    The user can rename it, with the date kept or changed, delete it, or
    skip it for good. A scan that was skipped is remembered by the
    workflow: its page can make it forget, and then the scan comes up
    again.
    """

    id = "timekiller.process-scan"
    title = "Process a scan"
    kind = BlockKind.ASK
    converses = True

    async def run(
        self, ctx: StepContext, inputs: ProcessScanInput
    ) -> ProcessScanOutput:
        """Hold the conversation about the first scan that waits."""
        bot = ctx.interaction
        obsidian = await ObsidianApi.for_user(ctx.user)
        folder = inputs.folder.rstrip("/")
        scans = [
            pathlib.PurePosixPath(path) for path in await obsidian.list_dir(folder)
        ]
        unprocessed = [
            scan
            for scan in scans
            if _UNNAMED_SCAN_RE.search(scan.name) is not None and scan.suffix == ".pdf"
        ]
        pdf_file = None
        for scan in unprocessed:
            if await ctx.memory.get(skipped_name(str(scan))) is None:
                pdf_file = scan
                break
        if pdf_file is None:
            return ProcessScanOutput(outcome="nothing to do")
        path = str(pdf_file)
        await bot.send_document(pdf_file.name, await obsidian.read_binary_file(path))

        action = await bot.request_user_choice(
            f"What do you want to do with “{pdf_file.name}”?",
            ["Rename", "Delete", "Skip (don't ask again)", "Cancel"],
            columns=2,
        )
        if action == 3:  # Cancel
            return ProcessScanOutput(outcome="cancelled", path=path)
        if action == 2:  # Skip (don't ask again)
            await ctx.memory.set(skipped_name(path))
            await bot.send_message(
                f"Okay, I won't ask about “{pdf_file.name}” again. To be asked "
                "again, make the workflow forget it on its page."
            )
            return ProcessScanOutput(outcome="skipped", path=path)
        if action == 1:  # Delete
            if (
                await bot.request_user_choice(
                    f"Permanently delete “{pdf_file.name}”? This cannot be undone.",
                    ["Yes, delete", "No"],
                )
                == 0
            ):
                await obsidian.delete_file(path)
                await bot.send_message(f"Permanently deleted “{pdf_file.name}”.")
                return ProcessScanOutput(outcome="deleted", path=path)
            return ProcessScanOutput(outcome="cancelled", path=path)

        # action == 0: Rename
        existing_date = pdf_file.stem[:10]
        new_date = ""
        # Some errors will require doing the whole interaction again
        while True:
            date_choice = await bot.request_user_choice(
                f"Is the existing date ({existing_date}) okay?",
                ["Yes (keep)", "No (change)", "Cancel"],
                columns=2,
            )
            if date_choice == 2:
                return ProcessScanOutput(outcome="cancelled", path=path)
            if date_choice == 1:
                while _ISO_DATE_RE.match(new_date) is None:
                    new_date = await bot.request_user_input(
                        "Enter a date (in ISO-8601 format, YYYY-MM-DD) for the scan:"
                    )
            new_name = await bot.request_user_input("Enter a new name for the scan:")
            new_path = pathlib.PurePosixPath(
                f"{folder}/{new_date or existing_date} {new_name}.pdf"
            )
            if (
                await bot.request_user_choice(
                    f"Rename {pdf_file.name} to {new_path.name}?", ["Yes", "No"]
                )
                == 0
            ):
                try:
                    await obsidian.move_file(path, str(new_path))
                except FileExistsError:
                    await bot.send_message(f'File "{new_path.name}" already exists.')
                    if (
                        await bot.request_user_choice(
                            "Do you want to enter a new name?", ["Yes", "No"]
                        )
                    ) == 1:
                        return ProcessScanOutput(outcome="cancelled", path=path)
                else:
                    await bot.send_message("Moved!")
                    return ProcessScanOutput(
                        outcome="renamed", path=path, new_path=str(new_path)
                    )


def timekiller_blocks() -> list[Block]:
    """Return the blocks that the Timekiller plugin publishes."""
    return [Journal(), ReadingCheckIn(), ProcessScan()]
