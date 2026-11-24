"""What is left of Spanreed Monitor: the move of its users to workflows.

The plugin told a user three things: that Spanreed started, what error
Spanreed had kept, and, as it was meant, that the user's plugin for
Obsidian had gone quiet. Each is a recipe now, and the plugin is gone:

- "Spanreed started", on the trigger of that name.
- "Spanreed had an error", on the trigger of that name. The engine's
  plugin reads the list of errors now (see ``spanreed.workflows.system``),
  and the Obsidian connector reads what Obsidian's plugin says (see
  ``spanreed.apis.obsidian_watch``).
- "Obsidian stopped answering". The plugin never said this: the time since
  the last heartbeat was never counted. So the workflow starts turned off,
  for the user to turn on.

The plugin also tried to say "Spanreed is shutting down." when it was
stopped. Nothing does that now: a workflow does not run while the engine
stops.
"""

import logging

from spanreed.apis.obsidian import ObsidianPlugin
from spanreed.interaction import UserInteractionApi
from spanreed.user import User
from spanreed.workflows.engine import WorkflowEngine
from spanreed.workflows.model import Workflow
from spanreed.workflows.recipes import all_recipes

_logger = logging.getLogger(__name__)

CANONICAL_NAME = "spanreed-monitor"
# The recipes that tell what the plugin told.
RECIPES = ("spanreed-started", "spanreed-error")
# The recipe that tells what the plugin was meant to tell.
WATCHDOG_RECIPE = "obsidian-stopped-answering"


def _turn_off(workflow: Workflow) -> None:
    workflow.enabled = False


async def move(engine: WorkflowEngine, user: User) -> list[Workflow]:
    """Give a user of the plugin the workflows that do what it did.

    Return the workflows that were made. Doing this again makes nothing
    new.
    """
    existing = {w.recipe for w in await engine.store.list_for_user(user.id)}
    recipes = all_recipes()
    made = []
    for recipe_id in RECIPES:
        if recipe_id not in existing:
            made.append(await engine.install(user, recipes[recipe_id]))
    # The plugin watched Obsidian only for a user who had it.
    if WATCHDOG_RECIPE not in existing and await ObsidianPlugin.is_registered(user):
        made.append(await engine.install(user, recipes[WATCHDOG_RECIPE], _turn_off))
    if made:
        _logger.info(
            f"Moved user {user.id} from the monitor plugin to workflows"
            f" {[workflow.id for workflow in made]}."
        )
        await _tell(user, made)
    return made


async def _tell(user: User, made: list[Workflow]) -> None:
    on = [f"“{workflow.name}”" for workflow in made if workflow.enabled]
    off = [f"“{workflow.name}”" for workflow in made if not workflow.enabled]
    text = "The plugin “Spanreed Monitor” is now workflows"
    if on:
        text += (
            f": {' and '.join(on)}. They tell you what the plugin told you, "
            "in the same words. Change what they say, or turn one off, under "
            "Workflows in the web UI."
        )
    else:
        text += "."
    if off:
        text += (
            f" {off[0]} is new, and off: the plugin was meant to tell you "
            "when Obsidian went quiet, and never did. Turn it on if you want "
            "to hear of it."
        )
    text += " Nothing says “Spanreed is shutting down.” any more."
    bot = await UserInteractionApi.for_user(user)
    await bot.notify(text, parse_html=False)
