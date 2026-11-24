"""Turn the users of a plugin that became a recipe into users of the recipe.

A plugin whose behavior is now a workflow is deleted from the code, but its
users are still registered to it in Redis. For each of them, install the
recipe that replaces the plugin and drop the registration. A user never
has both, and doing this again changes nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from spanreed.interaction import UserInteractionApi
from spanreed.storage import redis_api
from spanreed.user import User
from spanreed.workflows.engine import WorkflowEngine
from spanreed.workflows.recipes import all_recipes

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetiredPlugin:
    """A plugin that was deleted because a recipe does what it did."""

    canonical_name: str
    name: str
    recipe_id: str


RETIRED_PLUGINS = [
    RetiredPlugin(
        canonical_name="todoist-no-overdue",
        name="Todoist No Overdue",
        recipe_id="todoist-no-overdue",
    ),
]


async def retire_plugins(engine: WorkflowEngine) -> None:
    """Move the users of every retired plugin to its recipe."""
    for plugin in RETIRED_PLUGINS:
        # The keys that ``Plugin`` keeps registrations under.
        users_key = f"config:plugin:name={plugin.canonical_name}:users"
        for raw_user_id in await redis_api.smembers(users_key):
            user_id = int(raw_user_id)
            try:
                user = await User.find_by_id(user_id)
                await _move_user(engine, plugin, user)
            except Exception:
                # One user that cannot be moved must not keep the engine
                # from starting for everyone. The registration stays, so
                # the next start tries again.
                _logger.exception(
                    f"Could not move user {user_id} from plugin"
                    f" {plugin.canonical_name}."
                )
                continue
            # Only now: if this stops halfway, the next start finds the
            # workflow and does not install a second one.
            await redis_api.srem(f"user:{user_id}:plugins", plugin.canonical_name)
            await redis_api.srem(users_key, user_id)


async def _move_user(engine: WorkflowEngine, plugin: RetiredPlugin, user: User) -> None:
    workflows = await engine.store.list_for_user(user.id)
    if any(workflow.recipe == plugin.recipe_id for workflow in workflows):
        return
    workflow = await engine.install(user, all_recipes()[plugin.recipe_id])
    _logger.info(
        f"Moved user {user.id} from plugin {plugin.canonical_name}"
        f" to workflow {workflow.id}."
    )
    bot = await UserInteractionApi.for_user(user)
    await bot.notify(
        f"The plugin “{plugin.name}” is now a workflow, “{workflow.name}”. "
        "It does what the plugin did. Use “Manage workflows” to turn it "
        "off or to run it.",
        parse_html=False,
    )
