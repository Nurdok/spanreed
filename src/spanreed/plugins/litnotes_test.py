"""Lit Notes from end to end: the command, the conversation, the note.

The plugin, the engine, the asking blocks and the Obsidian block are the
real ones. Redis is in memory, and Google Books, Obsidian and the user are
mocks. The conversations are the ones the plugin was run on before it
became a workflow (see ``litnotes_test_data``).
"""

import asyncio
import contextlib
import datetime
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, patch

from spanreed.apis.google_books import Book
from spanreed.apis.obsidian import ObsidianPlugin
from spanreed.interaction import UserInteractionPriority, commands
from spanreed.interaction import engine as turns
from spanreed.plugin import Plugin
from spanreed.plugins.litnotes import (
    ADD_BOOK_NOTE_RECIPE,
    OLD_NAMES,
    LitNotesPlugin,
    convert_to_workflows,
    note_inputs,
    template_problem,
)
from spanreed.plugins.litnotes_test_data import (
    BOOKS_FOUND,
    CONTENT_TEMPLATE,
    OLD_PLUGIN,
    TITLE_TEMPLATE,
    sample_books,
)
from spanreed.plugins.workflows import WorkflowsPlugin
from spanreed.test_utils import FakeRedis, async_return_true, mock_user_find_by_id
from spanreed.workflows import migrations
from spanreed.workflows import store as workflow_store
from spanreed.workflows.blocks import BlockRegistry
from spanreed.workflows.engine import WorkflowEngine
from spanreed.workflows.model import Workflow
from spanreed.workflows.recipes import all_recipes

CONFIG_KEY = "config:plugin:name=lit-notes:user_id=4"
MULTIPLE = "Found multiple books. Which one did you mean?"

# The plugin left out the space before "and" in a line of authors.
FIXED_LABELS = {
    "Good Omens: The Nice and Accurate Prophecies by Terry Pratchettand Neil "
    "Gaiman (1990)": "Good Omens: The Nice and Accurate Prophecies by Terry "
    "Pratchett and Neil Gaiman (1990)",
    "Design Patterns by Erich Gamma, Richard Helm, Ralph Johnsonand John "
    "Vlissides (1994)": "Design Patterns by Erich Gamma, Richard Helm, Ralph "
    "Johnson and John Vlissides (1994)",
}


def test_the_plugin_publishes_the_search_and_has_no_settings() -> None:
    Plugin.reset_registry()
    plugin = LitNotesPlugin()

    assert plugin.name() == "Lit Notes"
    assert plugin.canonical_name() == "lit-notes"
    assert plugin.has_user_config() is False
    assert [block.id for block in plugin.blocks()] == ["lit-notes.search-books"]


@dataclass
class World:
    """What a test works with."""

    redis: FakeRedis
    engine: WorkflowEngine
    obsidian: AsyncMock
    bot: AsyncMock
    user: Any
    # What the search finds.
    books: list[Book] = field(default_factory=list)
    # What the user answers, first answer first.
    answers: list[Any] = field(default_factory=list)
    # What was asked, said and written, as ``litnotes_test_data`` holds it.
    lines: list[list[Any]] = field(default_factory=list)
    # What the user was told outside of a conversation.
    notified: list[str] = field(default_factory=list)

    async def save_old_settings(
        self,
        file_location: str = "Books/",
        title: str = TITLE_TEMPLATE,
        content: str = CONTENT_TEMPLATE,
    ) -> None:
        """Save the settings the plugin had while it wrote the note itself."""
        config = {
            "vault": "Vault",
            "file_location": file_location,
            "note_title_template": title,
            "note_content_template": content,
        }
        await self.redis.set(CONFIG_KEY, json.dumps(config))

    async def convert(self) -> Workflow:
        """Give the user the workflow, as the conversion at startup does."""
        if await self.redis.get(CONFIG_KEY) is None:
            await self.save_old_settings()
        await convert_to_workflows(self.engine, self.user)
        (workflow,) = await self.engine.store.list_for_user(4)
        return workflow

    async def run_command(self, workflow: Workflow) -> dict[str, Any]:
        """Run the workflow as its command does, and return the trace."""
        async with asyncio.timeout(3):
            async with turns.user_interaction(4, priority=UserInteractionPriority.HIGH):
                await self.engine.run_by_hand(workflow.id, self.user)
        (run,) = await self.engine.store.recent_runs(workflow.id, 1)
        return run


@contextlib.contextmanager
def _world() -> Iterator[World]:
    fake = FakeRedis()
    workflow_store.reset_queues_for_tests()
    commands.reset_registry_for_tests()
    turns.reset_state_for_tests()
    Plugin.reset_registry()
    registry = BlockRegistry.from_plugins(
        [WorkflowsPlugin(), ObsidianPlugin(), LitNotesPlugin()]
    )
    find_user = AsyncMock(side_effect=mock_user_find_by_id)
    search = AsyncMock()
    with (
        patch("spanreed.plugin.redis_api", new=fake),
        patch("spanreed.workflows.store.redis_api", new=fake),
        patch("spanreed.workflows.migrations.redis_api", new=fake),
        patch("spanreed.workflows.engine.User.find_by_id", new=find_user),
        patch("spanreed.workflows.migrations.User.find_by_id", new=find_user),
        patch.object(Plugin, "is_registered", async_return_true),
        patch("spanreed.apis.google_books_blocks.GoogleBooks.get_books", new=search),
        patch("spanreed.apis.obsidian_blocks.ObsidianApi", autospec=True) as obsidian,
        patch("spanreed.workflows.engine.UserInteractionApi", autospec=True) as bot,
        patch("spanreed.workflows.blocks.UserInteractionApi", new=bot),
        patch("spanreed.workflows.migrations.UserInteractionApi", new=bot),
        patch("spanreed.plugins.litnotes.UserInteractionApi", new=bot),
    ):
        world = World(
            redis=fake,
            engine=WorkflowEngine(registry),
            obsidian=obsidian,
            bot=bot,
            user=mock_user_find_by_id(4),
        )

        async def books(query: str) -> list[Book]:
            return list(world.books)

        async def choice(prompt: str, choices: list[Any], **how: Any) -> int:
            answer = world.answers.pop(0)
            world.lines.append(["choice", prompt, list(choices), answer])
            return int(answer)

        async def text(prompt: str) -> str:
            answer = world.answers.pop(0)
            world.lines.append(["input", prompt, answer])
            return str(answer)

        async def say(message: str, **how: Any) -> None:
            world.lines.append(["say", message])

        async def append(path: str, content: str, heading: Any = None) -> None:
            world.lines.append(["append", path, content])

        async def notify(message: str, **how: Any) -> None:
            world.notified.append(message)

        search.side_effect = books
        obsidian.for_user = AsyncMock(return_value=obsidian)
        obsidian.append_to_note = AsyncMock(side_effect=append)
        bot.for_user = AsyncMock(return_value=bot)
        bot.request_user_choice = AsyncMock(side_effect=choice)
        bot.request_user_input = AsyncMock(side_effect=text)
        bot.send_message = AsyncMock(side_effect=say)
        bot.notify = AsyncMock(side_effect=notify)
        yield world


def _run(scenario: Any) -> None:
    with _world() as world:

        async def run() -> None:
            try:
                await scenario(world)
            finally:
                await world.engine.stop()

        asyncio.run(run())


def _as_the_workflow_does(lines: list[list[Any]]) -> list[list[Any]]:
    """Return what the plugin did, with what the workflow does on purpose.

    Two things differ. A line of authors has its space before "and". And
    "Show more" is offered only while there are more books to show: the
    plugin offered it under the last ones too, and then showed none.
    """
    expected = []
    for line in lines:
        if line[0] == "choice" and line[1] == MULTIPLE:
            *books, more, cancel = line[2]
            assert (more, cancel) == ("Show more", "Cancel")
            books = [FIXED_LABELS.get(book, book) for book in books]
            last_page = len(books) < 5
            choices = [*books, *([] if last_page else [more]), cancel]
            line = [line[0], line[1], choices, line[3]]
        expected.append(line)
    return expected


def _answers(lines: list[list[Any]]) -> list[Any]:
    return [line[-1] for line in lines if line[0] in ("choice", "input")]


def test_the_workflow_asks_says_and_writes_what_the_plugin_did() -> None:
    async def scenario(world: World) -> None:
        workflow = await world.convert()
        assert world.engine.problems(workflow) == []
        # The command is the one the plugin had.
        command = commands.get_command(f"workflow.{workflow.id}")
        assert command is not None
        assert command.text == "Add book note"

        for name, old in OLD_PLUGIN.items():
            world.books = sample_books(BOOKS_FOUND[name])
            world.answers = _answers(old)
            world.lines = []

            await world.run_command(workflow)

            # Nothing more is said: no line that sums the run up.
            assert world.lines == _as_the_workflow_does(old), name
            assert world.answers == [], name

    _run(scenario)


def test_the_runs_that_end_early_do_not_fail() -> None:
    async def scenario(world: World) -> None:
        workflow = await world.convert()
        ends = {}
        for name in ("one, not the one", "many, cancelled", "none found"):
            world.books = sample_books(BOOKS_FOUND[name])
            world.answers = _answers(OLD_PLUGIN[name])
            run = await world.run_command(workflow)
            ends[name] = (run["status"], run["steps"][-1]["reason"])

        assert ends == {
            "one, not the one": ("stopped", "No book was chosen."),
            "many, cancelled": ("stopped", "No book was chosen."),
            "none found": ("stopped", "No books were found."),
        }
        world.obsidian.append_to_note.assert_not_awaited()

    _run(scenario)


def test_the_history_keeps_the_conversation() -> None:
    async def scenario(world: World) -> None:
        workflow = await world.convert()
        name = "one, confirmed, nothing added"
        world.books = sample_books(1)
        world.answers = _answers(OLD_PLUGIN[name])

        run = await world.run_command(workflow)

        assert run["status"] == "finished"
        said = {step["id"]: step.get("conversation") for step in run["steps"]}
        assert said["query"] == [
            {"asked": "Which book do you want to add a note for?", "answer": "dune"}
        ]
        assert said["book"] == [
            {
                "asked": "Found one book: Dune by Frank Herbert (1965).\n"
                "Is this the one you meant?",
                "answer": "Yes",
            }
        ]
        assert said["free_text"] == [
            {"asked": "Any free text to add to the note?", "answer": "No"}
        ]
        # A step that only sends keeps what it sent as its input.
        assert said["done"] is None
        book = next(step for step in run["steps"] if step["id"] == "book")
        assert book["outputs"]["item"]["title"] == "Dune"

    _run(scenario)


def test_a_title_with_a_dot_keeps_all_of_it() -> None:
    """The plugin cut the name of the note at the last dot of the title."""

    async def scenario(world: World) -> None:
        workflow = await world.convert()
        world.books = [
            Book("Mr. Fox: A Tale", ["R. Dahl"], "Knopf", None, "", ""),
        ]
        world.answers = ["fox", 0, 1, 1]

        await world.run_command(workflow)

        (written,) = [line for line in world.lines if line[0] == "append"]
        assert written[1] == "Books/Mr. Fox.md"

    _run(scenario)


def test_the_recipe_as_it_ships_writes_a_note() -> None:
    async def scenario(world: World) -> None:
        workflow = await world.engine.install(
            world.user, all_recipes()[ADD_BOOK_NOTE_RECIPE]
        )
        assert world.engine.problems(workflow) == []
        world.books = sample_books(1)
        world.answers = ["dune", 0, 0, "Dana", 1, 0, "A gift."]

        run = await world.run_command(workflow)

        assert run["status"] == "finished"
        (written,) = [line for line in world.lines if line[0] == "append"]
        assert written[1:] == [
            "Books/Dune.md",
            "# Dune\n\nauthor:: [[Frank Herbert]]\nyear:: 1965\n"
            "recommended_by:: [[Dana]]\n\nA gift.",
        ]

    _run(scenario)


# -- from the plugin to the workflow -------------------------------------------


def test_the_conversion_keeps_the_folder_and_the_templates() -> None:
    async def scenario(world: World) -> None:
        await world.save_old_settings(file_location="Library/Books")

        workflow = await world.convert()

        assert workflow.recipe == ADD_BOOK_NOTE_RECIPE
        assert workflow.enabled is True
        (note,) = [step for step in workflow.steps if step.id == "note"]
        assert note.inputs == {
            "path": OLD_NAMES + "Library/Books/" + TITLE_TEMPLATE + ".md",
            "text": OLD_NAMES + CONTENT_TEMPLATE,
        }
        # The templates work as they are.
        assert world.notified == []

        # Doing it again installs nothing.
        assert await convert_to_workflows(world.engine, world.user) == []
        assert len(await world.engine.store.list_for_user(4)) == 1

    _run(scenario)


def test_a_user_who_never_set_the_plugin_up_gets_no_workflow() -> None:
    async def scenario(world: World) -> None:
        assert await convert_to_workflows(world.engine, world.user) == []
        assert await world.engine.store.list_for_user(4) == []

    _run(scenario)


def test_the_user_is_told_once_what_became_of_the_plugin() -> None:
    async def scenario(world: World) -> None:
        await world.save_old_settings()
        await world.redis.sadd("config:plugin:name=lit-notes:users", "4")

        await migrations.convert_plugins(world.engine)
        await migrations.convert_plugins(world.engine)

        (told,) = world.notified
        assert "Lit Notes now does its work through workflows: “Add book note”." in told
        assert "“Append to note”" in told
        assert len(await world.engine.store.list_for_user(4)) == 1

    _run(scenario)


def test_the_user_is_told_about_a_template_that_fails_in_a_workflow() -> None:
    """The plugin left out what a template named that is not there."""

    async def scenario(world: World) -> None:
        await world.save_old_settings(content="{{ book.subtitle }} {{ free_text }}")

        await world.convert()

        (told,) = world.notified
        assert "The template of the text:" in told
        assert "subtitle" in told
        assert "“Append to note”" in told

    _run(scenario)


def test_where_and_what_the_workflow_writes() -> None:
    assert note_inputs("Books/", "{{ book.title }}", "text") == {
        "path": OLD_NAMES + "Books/{{ book.title }}.md",
        "text": OLD_NAMES + "text",
    }
    # No folder: the note goes to the top of the vault.
    assert note_inputs("", "T", "")["path"] == OLD_NAMES + "T.md"
    assert note_inputs("/a/b/", "T", "")["path"] == OLD_NAMES + "a/b/T.md"


def test_a_template_is_tried_on_a_book() -> None:
    fine = note_inputs("Books", TITLE_TEMPLATE, CONTENT_TEMPLATE)
    assert template_problem(fine) is None

    bad_title = note_inputs("Books", "{{ book.isbn }}", "")
    problem = template_problem(bad_title)
    assert problem is not None
    assert problem.startswith("The template of the title:")
    assert "isbn" in problem

    bad_text = note_inputs("Books", "{{ book.title }}", "{{ rating }}")
    problem = template_problem(bad_text)
    assert problem is not None
    assert problem.startswith("The template of the text:")
    assert "rating" in problem


def test_the_sample_books_have_dates() -> None:
    # A guard for the test data: the labels depend on the years.
    assert [book.publication_date for book in sample_books(2)] == [
        datetime.date(1965, 8, 1),
        datetime.date(1990, 5, 10),
    ]
