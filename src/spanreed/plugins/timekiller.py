"""Publish the Timekiller blocks, for the workflows that kill time.

The plugin used to push, every hour or two during the day, one of four
things at random: a journaling prompt, a question about the mood of the
day, a check on the books being read, or a scan that waits for a name. Five
recipes do that now: "Kill time" picks, and runs one of "Journaling Prompt",
"Mood", "Books" and "Scan Processing". ``convert_to_workflows`` gives them
to a user who had the plugin before, with the journal heading they had and
with what the plugin remembered.
"""

import json
import logging

from spanreed.apis.obsidian import ObsidianPlugin
from spanreed.plugin import Plugin
from spanreed.plugins.timekiller_blocks import skipped_name, timekiller_blocks
from spanreed.user import User
from spanreed.workflows.blocks import Block
from spanreed.workflows.engine import WorkflowEngine
from spanreed.workflows.model import Workflow
from spanreed.workflows.recipes import all_recipes
from spanreed.workflows.store import MOVED_MARKER

_logger = logging.getLogger(__name__)

JOURNAL_RECIPE = "timekiller-journal"
MOOD_RECIPE = "timekiller-mood"
BOOKS_RECIPE = "timekiller-books"
SCANS_RECIPE = "timekiller-scans"
KILL_TIME_RECIPE = "timekiller-kill-time"
# The parts first: "Kill time" runs them by their names.
RECIPES = (JOURNAL_RECIPE, MOOD_RECIPE, BOOKS_RECIPE, SCANS_RECIPE, KILL_TIME_RECIPE)

# Where the plugin kept, for each user, when it last asked about the books
# being read, and the scans the user said not to ask about again. "Kill
# time" keeps the first under the same name.
LAST_ASKED_BOOKS_KEY = "currently-reading-books-last-asked"
SKIPPED_SCANS_KEY = "skipped-scans"


class TimekillerPlugin(Plugin):
    @classmethod
    def name(cls) -> str:
        return "Timekiller"

    @classmethod
    def has_user_config(cls) -> bool:
        return False

    @classmethod
    def get_prerequisites(cls) -> list[type[Plugin]]:
        return [ObsidianPlugin]

    def blocks(self) -> list[Block]:
        """Return the Timekiller blocks for workflows."""
        return timekiller_blocks()


# -- from a plugin that pushes to workflows that do ------------------------------


async def convert_to_workflows(engine: WorkflowEngine, user: User) -> list[Workflow]:
    """Give a user of the old plugin the workflows that do what it did.

    Install the five recipes, with the journal heading the user had. Hand
    "Kill time" when the books were last asked about, and "Scan Processing"
    the scans that were skipped for good. "Kill time" is off until that is
    done: it would ask about the books at once. Return the workflows that
    were installed. Doing this again installs nothing, and finishes what an
    earlier time did not get to.
    """
    saved = await TimekillerPlugin.get_saved_config_data(user) or {}
    heading = saved.get("journal_heading") or None
    have = {w.recipe: w for w in await engine.store.list_for_user(user.id)}
    installed = []
    for recipe_id in RECIPES:
        if recipe_id in have:
            continue

        def customize(new: Workflow, recipe_id: str = recipe_id) -> None:
            if recipe_id == KILL_TIME_RECIPE:
                new.enabled = False
            if recipe_id == JOURNAL_RECIPE:
                for step in new.steps:
                    if step.block == "timekiller.journal":
                        step.inputs["heading"] = heading

        have[recipe_id] = await engine.install(
            user, all_recipes()[recipe_id], customize
        )
        installed.append(have[recipe_id])

    kill_time = have[KILL_TIME_RECIPE]
    memory = engine.store.memory(kill_time.id)
    if await memory.get(MOVED_MARKER) is None:
        await _carry_over(engine, user, kill_time, have[SCANS_RECIPE])
        await memory.set(MOVED_MARKER)
        kill_time.enabled = True
        await engine.save(kill_time)
    return installed


async def _carry_over(
    engine: WorkflowEngine, user: User, kill_time: Workflow, scans: Workflow
) -> None:
    """Hand what the plugin remembered for a user to the user's workflows."""
    last_asked = await TimekillerPlugin.get_user_data(user, LAST_ASKED_BOOKS_KEY)
    if last_asked:
        # Kept with the time it was true at, so that the three days until
        # the books come up again do not start over.
        await engine.store.memory(kill_time.id).set(
            LAST_ASKED_BOOKS_KEY, True, at=last_asked
        )
    raw = await TimekillerPlugin.get_user_data(user, SKIPPED_SCANS_KEY)
    try:
        skipped = json.loads(raw) if raw else []
    except ValueError:
        skipped = []
    for path in skipped if isinstance(skipped, list) else []:
        await engine.store.memory(scans.id).set(skipped_name(str(path)))
    _logger.info(
        f"Carried what Timekiller remembered for user {user.id} over to "
        f"workflows {kill_time.id} and {scans.id}."
    )
