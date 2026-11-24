"""Move users from what a plugin did itself to the recipes that do it now.

There are two cases, and both run once for each user, at startup.

A plugin whose whole behavior is now a recipe is retired: it is deleted
from the code, but its users are still registered to it in Redis. For each
of them, install the recipe and drop the registration.

A plugin that stays, as the connection to its service, is converted: what
it used to do with the data is now a set of recipes. For each user the
plugin had at that moment, install them with the settings the user had, and
remember that it was done. A user who joins the plugin later never had the
old behavior, and adds the recipes they want.

A user never has the old behavior and the new one, and doing either again
changes nothing.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from spanreed.interaction import UserInteractionApi
from spanreed.storage import redis_api
from spanreed.user import User
from spanreed.workflows.engine import WorkflowEngine
from spanreed.workflows.model import Workflow
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


@dataclass(frozen=True)
class Conversion:
    """A plugin that stays, whose behavior became recipes."""

    canonical_name: str
    name: str
    # Installs the recipes for a user and returns the new workflows. It
    # must be safe to call again after it stopped halfway.
    convert: Callable[[WorkflowEngine, User], Awaitable[list[Workflow]]]
    # Added to what the user is told, for what changed besides.
    note: str = ""


def _conversions() -> list[Conversion]:
    # Imported here: the plugins import this module.
    from spanreed.plugins import health_connect, withings

    return [
        Conversion(
            canonical_name="health-connect",
            name="Health Connect",
            convert=health_connect.convert_to_workflows,
            note=(
                "The commands Sync steps, Sync heart rate and Sync VO2 max are "
                "now one, “Sync health data”."
            ),
        ),
        Conversion(
            canonical_name="withings",
            name="Withings",
            convert=withings.convert_to_workflows,
        ),
    ]


def keep_daily_note_properties(fields: Any) -> Callable[[Workflow], None]:
    """Return what makes a workflow write the properties a plugin wrote.

    ``fields`` is the plugin's saved choice of daily note properties:
    whether each one was written, by its name with ``_`` for ``-``. The
    result takes the rows of the properties that were off out of a new
    workflow. A workflow that would write nothing starts off instead,
    with every row.
    """
    chosen = fields if isinstance(fields, dict) else {}
    turned_off = {name.replace("_", "-") for name, on in chosen.items() if not on}

    def customize(workflow: Workflow) -> None:
        for step in workflow.steps:
            properties = step.inputs.get("properties")
            if not isinstance(properties, dict):
                continue
            if turned_off >= set(properties):
                workflow.enabled = False
            else:
                for name in turned_off:
                    properties.pop(name, None)

    return customize


async def convert_plugins(engine: WorkflowEngine) -> None:
    """Give the users of every converted plugin its recipes, once."""
    for conversion in _conversions():
        done_key = f"workflows:converted:{conversion.canonical_name}"
        to_convert = await _users_to_convert(conversion)
        for user_id in list(to_convert):
            try:
                user = await User.find_by_id(user_id)
                workflows = await conversion.convert(engine, user)
                await _tell_about_conversion(conversion, user, workflows)
            except Exception:
                # As for a retired plugin: one user must not stop the rest,
                # and the next start tries again.
                _logger.exception(
                    f"Could not convert plugin {conversion.canonical_name}"
                    f" for user {user_id}."
                )
                continue
            await redis_api.sadd(done_key, str(user_id))
            to_convert.remove(user_id)
            await redis_api.set(_to_convert_key(conversion), json.dumps(to_convert))


def _to_convert_key(conversion: Conversion) -> str:
    return f"workflows:to-convert:{conversion.canonical_name}"


async def _users_to_convert(conversion: Conversion) -> list[int]:
    """Return the users of the old plugin who do not have its recipes yet.

    The first time, they are the users the plugin has now. The list is
    kept, so that a user who registers to the plugin after that is not
    converted: such a user never had the old behavior.
    """
    stored = await redis_api.get(_to_convert_key(conversion))
    if stored is not None:
        return [int(user_id) for user_id in json.loads(stored)]
    users_key = f"config:plugin:name={conversion.canonical_name}:users"
    done_key = f"workflows:converted:{conversion.canonical_name}"
    to_convert = []
    for user_id in sorted(int(raw) for raw in await redis_api.smembers(users_key)):
        # A start before the list was kept may have converted the user.
        if not await redis_api.sismember(done_key, str(user_id)):
            to_convert.append(user_id)
    await redis_api.set(_to_convert_key(conversion), json.dumps(to_convert))
    return to_convert


async def _tell_about_conversion(
    conversion: Conversion, user: User, workflows: list[Workflow]
) -> None:
    if not workflows:
        return
    _logger.info(
        f"Converted plugin {conversion.canonical_name} for user {user.id}:"
        f" {[workflow.id for workflow in workflows]}."
    )
    names = " and ".join(f"“{workflow.name}”" for workflow in workflows)
    off = [workflow.name for workflow in workflows if not workflow.enabled]
    text = (
        f"{conversion.name} now does its work through workflows: {names}. "
        "They do what the plugin did, with your settings. See them under "
        "Workflows in the web UI, or with “Manage workflows”."
    )
    if off:
        text += f" “{off[0]}” is off, because it had nothing to write."
    if conversion.note:
        text += f" {conversion.note}"
    bot = await UserInteractionApi.for_user(user)
    await bot.notify(text, parse_html=False)
