"""What is left of Audible Book Progress: its memory, for the workflow.

The plugin wrote how far each book the user is reading was listened to on
Audible to the book's note, and offered once to mark a finished book as
read. The recipe "Audible progress → book notes" does that now, and the
plugin is gone.

The plugin remembered three things for a user: the notes whose book is not
on Audible, the notes to ask about later, and the books it had offered to
mark as read. ``carry_over`` hands them to the workflow, which keeps them
under the same names. Without them the user would be asked all of it again.
"""

import logging

from spanreed.storage import redis_api
from spanreed.user import User
from spanreed.workflows.engine import WorkflowEngine
from spanreed.workflows.model import Workflow

_logger = logging.getLogger(__name__)

# The recipe that does what the plugin did.
RECIPE = "audible-progress"
CANONICAL_NAME = "audible-book-progress"
# The starts of the names the plugin kept things under, each followed by
# the path of a note or the ASIN of a book.
KEPT = ("not-audible:", "link-snooze:", "finish-prompted:")


def _data_prefix(user: User) -> str:
    # Where ``Plugin.set_user_data`` kept what the plugin remembered.
    return f"plugin-data:plugin:name={CANONICAL_NAME}:user_id={user.id}:"


def _text(value: object) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


async def carry_over(engine: WorkflowEngine, user: User, workflow: Workflow) -> None:
    """Hand what the plugin remembered for a user to the user's workflow.

    Doing this again keeps the same things again, and changes nothing.
    """
    prefix = _data_prefix(user)
    memory = engine.store.memory(workflow.id)
    count = 0
    async for key in redis_api.scan_iter(match=f"{prefix}*"):
        name = _text(key).removeprefix(prefix)
        if not name.startswith(KEPT):
            continue
        value = await redis_api.get(_text(key))
        if value is None:
            continue
        await memory.set(name, _text(value))
        count += 1
    _logger.info(
        f"Carried {count} things the plugin remembered for user {user.id}"
        f" over to workflow {workflow.id}."
    )
