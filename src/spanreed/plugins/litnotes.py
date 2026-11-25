"""Move the users of Lit Notes to the workflow that does what it did.

Lit Notes is gone. The recipe "Add book note" does what the plugin did
itself: it asks which book, lets the user choose among what Google Books
finds, asks who recommended it and for free text, and writes the note.
``convert_to_workflows`` gives it to a user who had the plugin, with the
folder and the two note templates they had.

The search for books, which was what was left of the plugin, is a block of
the Google Books plugin now (see ``spanreed.apis.google_books`` and, for
the move of the users, ``spanreed.workflows.migrations``).
"""

import datetime
import json
import logging

from spanreed.apis.google_books import Book
from spanreed.apis.obsidian import note_path
from spanreed.interaction import UserInteractionApi
from spanreed.storage import redis_api
from spanreed.user import User
from spanreed.workflows import expressions
from spanreed.workflows.asking import AskChoiceOutput, AskListOutput, AskTextOutput
from spanreed.workflows.engine import WorkflowEngine
from spanreed.workflows.model import Workflow
from spanreed.workflows.recipes import all_recipes

_logger = logging.getLogger(__name__)

# What the plugin was registered as, and kept its settings under.
CANONICAL_NAME = "lit-notes"
# The recipe that does what the plugin used to do itself.
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


# -- from a plugin that writes the note to a workflow that does ----------------


def note_inputs(
    file_location: str, title_template: str, content_template: str
) -> dict[str, str]:
    """Return where and what the workflow writes, from the plugin's settings.

    The plugin wrote the note to the folder ``file_location``, under the
    title that ``title_template`` gave, as a ``.md`` file.
    """
    return {
        "path": OLD_NAMES + note_path(file_location, title_template),
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


async def _saved_settings(user: User) -> dict | None:
    """Return the settings the plugin had saved for a user, if it had any.

    The plugin is gone, so this reads the key that ``Plugin`` kept them
    under.
    """
    raw = await redis_api.get(f"config:plugin:name={CANONICAL_NAME}:user_id={user.id}")
    if raw is None:
        return None
    try:
        saved = json.loads(raw)
    except ValueError:
        return None
    return saved if isinstance(saved, dict) else None


async def convert_to_workflows(engine: WorkflowEngine, user: User) -> list[Workflow]:
    """Give a user of the old plugin the workflow that does what it did.

    Make it from the recipe, with the folder and the templates the user
    had. Return the workflow if it was made. Doing this again makes nothing.

    A template can name something that a workflow does not know, which the
    plugin left empty. The workflow is stored all the same, and the user is
    told what to change.
    """
    saved = await _saved_settings(user)
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

    workflow = all_recipes()[ADD_BOOK_NOTE_RECIPE].workflow_for(user.id)
    for step in workflow.steps:
        if step.id == NOTE_STEP:
            step.inputs.update(inputs)
    # Stored even if it cannot run: the workflow then says why on its page,
    # where the template can be changed. Not storing it would leave the user
    # with neither the plugin's command nor their templates.
    found = engine.problems(workflow)
    await engine.save(workflow)
    if found:
        problem: str | None = found[0]
        effect = "The workflow cannot run, and has no command, until"
    else:
        problem = template_problem(inputs)
        effect = "The workflow fails there, after it asked everything, until"
    if problem is not None:
        _logger.warning(
            f"The note templates of user {user.id} do not work in a workflow."
        )
        bot = await UserInteractionApi.for_user(user)
        await bot.notify(
            "A note template of “Add book note” uses something that is not "
            f"there. {problem} The plugin left that empty. {effect} the "
            "template is changed on its “Append to note” step.",
            parse_html=False,
        )
    return [workflow]
