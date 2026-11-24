"""Publish the book search, for workflows that write a note for a book.

The recipe "Add book note" does what this plugin used to do itself: it asks
which book, lets the user choose among what Google Books finds, asks who
recommended it and for free text, and writes the note. ``convert_to_workflows``
gives it to a user who had the plugin before, with the folder and the two
note templates they had.
"""

import datetime
import logging

from spanreed.apis.google_books import Book
from spanreed.apis.google_books_blocks import google_books_blocks
from spanreed.interaction import UserInteractionApi
from spanreed.plugin import Plugin
from spanreed.user import User
from spanreed.workflows import expressions
from spanreed.workflows.asking import AskChoiceOutput, AskListOutput, AskTextOutput
from spanreed.workflows.blocks import Block
from spanreed.workflows.engine import WorkflowEngine
from spanreed.workflows.model import Workflow
from spanreed.workflows.recipes import all_recipes

_logger = logging.getLogger(__name__)

# The recipe that does what this plugin used to do itself.
ADD_BOOK_NOTE_RECIPE = "lit-notes-add-book-note"
# The step of the recipe that writes the note.
NOTE_STEP = "note"

# The plugin's templates knew the book, the people who recommended it and
# the free text by these names. In the workflow, each is a value of the
# step that asked for it. Starting a template with this keeps the names,
# so that a template goes on as the user wrote it.
OLD_NAMES = (
    "{% set book = book.item %}"
    "{% set recommended_by = recommended_by.answers %}"
    "{% set free_text = free_text.answer %}"
)


class LitNotesPlugin(Plugin):
    @classmethod
    def name(cls) -> str:
        return "Lit Notes"

    @classmethod
    def has_user_config(cls) -> bool:
        return False

    def blocks(self) -> list[Block]:
        """Return the book search for workflows."""
        return google_books_blocks()


# -- from a plugin that writes the note to a workflow that does ----------------


def note_inputs(
    file_location: str, title_template: str, content_template: str
) -> dict[str, str]:
    """Return where and what the workflow writes, from the plugin's settings.

    The plugin wrote the note to the folder ``file_location``, under the
    title that ``title_template`` gave, with ``.md`` after it.
    """
    folder = file_location.strip("/")
    return {
        "path": f"{OLD_NAMES}{folder + '/' if folder else ''}{title_template}.md",
        "text": f"{OLD_NAMES}{content_template}",
    }


def template_problem(inputs: dict[str, str]) -> str | None:
    """Return why a template of the note would fail in a workflow, if one would.

    The plugin left out what a template named that a book does not have. A
    workflow fails there instead. Try the templates on a made-up book.
    """
    book = Book(
        title="A Title: And a Subtitle",
        authors=["An Author", "Another Author"],
        publisher="A Publisher",
        publication_date=datetime.date(2000, 1, 1),
        description="A description.",
        thumbnail_url="https://example.com/cover.jpg",
    )
    context = {
        "book": AskChoiceOutput(chosen=True, answer=book.label, item=book, index=1),
        "recommended_by": AskListOutput(answers=["Someone"], count=1),
        "free_text": AskTextOutput(answer="Some text.", answered=True),
    }
    for name, template in (("path", "title"), ("text", "text")):
        try:
            expressions.render(inputs[name], context)
        except expressions.ExpressionError as e:
            return f"The template of the {template}: {e.__cause__ or e}."
    return None


async def convert_to_workflows(engine: WorkflowEngine, user: User) -> list[Workflow]:
    """Give a user of the old plugin the workflow that does what it did.

    Install the recipe with the folder and the templates the user had.
    Return the workflow if it was installed. Doing this again installs
    nothing.
    """
    saved = await LitNotesPlugin.get_saved_config_data(user)
    if saved is None:
        # Registered, but never set up: the command had nothing to write with.
        return []
    workflows = await engine.store.list_for_user(user.id)
    if any(workflow.recipe == ADD_BOOK_NOTE_RECIPE for workflow in workflows):
        return []

    inputs = note_inputs(
        str(saved.get("file_location") or ""),
        str(saved.get("note_title_template") or ""),
        str(saved.get("note_content_template") or ""),
    )

    def customize(new: Workflow) -> None:
        for step in new.steps:
            if step.id == NOTE_STEP:
                step.inputs.update(inputs)

    workflow = await engine.install(
        user, all_recipes()[ADD_BOOK_NOTE_RECIPE], customize
    )
    problem = template_problem(inputs)
    if problem is not None:
        _logger.warning(
            f"The note templates of user {user.id} would fail in a workflow."
        )
        bot = await UserInteractionApi.for_user(user)
        await bot.notify(
            "A note template of “Add book note” uses something that is not "
            f"there. {problem} The plugin left that empty. The workflow "
            "fails there instead, after it asked everything. Change the "
            "template on its “Append to note” step.",
            parse_html=False,
        )
    return [workflow]
