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
from spanreed.workflows.store import MOVED_MARKER

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetiredPlugin:
    """A plugin that was deleted because a recipe does what it did."""

    canonical_name: str
    name: str
    recipe_id: str
    # Given the config the plugin had saved for a user, or ``None`` if it
    # had none, returns what changes the new workflow to match it.
    keep_settings: (
        Callable[[dict[str, Any] | None], Callable[[Workflow], None]] | None
    ) = None
    # Hands what the plugin remembered for a user to the new workflow. The
    # workflow is off until this is done, so that it does not run without
    # what the plugin knew. It must be safe to call again.
    carry_over: Callable[[WorkflowEngine, User, Workflow], Awaitable[None]] | None = (
        None
    )
    # Added to what the user is told, for what changed besides.
    note: str = ""


# The name under which a workflow remembers that ``carry_over`` is done.
CARRIED_OVER = MOVED_MARKER


def _retired_plugins() -> list[RetiredPlugin]:
    # Imported here: what is left of a plugin imports the engine, as this does.
    from spanreed.plugins import book_progress, habit_check_in

    return [
        RetiredPlugin(
            canonical_name="todoist-no-overdue",
            name="Todoist No Overdue",
            recipe_id="todoist-no-overdue",
        ),
        RetiredPlugin(
            canonical_name="habit-tracker",
            name="Habit Tracker",
            recipe_id=habit_check_in.RECIPE,
            keep_settings=habit_check_in.keep_settings,
            note=(
                "The command is still “Track Habits”. Add Habit and Remove "
                "Habit are gone: the habits are the options of the "
                "workflow’s question, under Workflows in the web UI."
            ),
        ),
        RetiredPlugin(
            canonical_name=book_progress.CANONICAL_NAME,
            name="Audible Book Progress",
            recipe_id=book_progress.RECIPE,
            carry_over=book_progress.carry_over,
            note=(
                "The command is still “Sync book progress”. It knows which "
                "books you said are not on Audible, and which you were "
                "asked about."
            ),
        ),
    ]


async def retire_plugins(engine: WorkflowEngine) -> None:
    """Move the users of every retired plugin to its recipe."""
    for plugin in _retired_plugins():
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
    workflow = next((w for w in workflows if w.recipe == plugin.recipe_id), None)
    is_new = workflow is None
    if workflow is None:
        workflow = await _install(engine, plugin, user)
    if plugin.carry_over is not None:
        # Also for a workflow that an earlier start installed and then
        # stopped at: it is still off, and still knows nothing.
        memory = engine.store.memory(workflow.id)
        if await memory.get(CARRIED_OVER) is None:
            await plugin.carry_over(engine, user, workflow)
            await memory.set(CARRIED_OVER)
            workflow.enabled = True
            await engine.save(workflow)
    if not is_new:
        return
    _logger.info(
        f"Moved user {user.id} from plugin {plugin.canonical_name}"
        f" to workflow {workflow.id}."
    )
    text = f"The plugin “{plugin.name}” is now a workflow, “{workflow.name}”. "
    if workflow.enabled:
        text += (
            "It does what the plugin did. Use “Manage workflows” to turn it "
            "off or to run it."
        )
    else:
        text += (
            "It is off, because the plugin had no settings to carry over. "
            "Fill it in under Workflows in the web UI, and turn it on."
        )
    if plugin.note:
        text += f" {plugin.note}"
    bot = await UserInteractionApi.for_user(user)
    await bot.notify(text, parse_html=False)


async def _install(
    engine: WorkflowEngine, plugin: RetiredPlugin, user: User
) -> Workflow:
    """Give a user the recipe of a retired plugin, with the settings they had."""
    keep = None
    if plugin.keep_settings is not None:
        keep = plugin.keep_settings(await _saved_config(plugin, user))

    def customize(workflow: Workflow) -> None:
        if keep is not None:
            keep(workflow)
        if plugin.carry_over is not None:
            workflow.enabled = False

    return await engine.install(user, all_recipes()[plugin.recipe_id], customize)


async def _saved_config(plugin: RetiredPlugin, user: User) -> dict[str, Any] | None:
    """Return the config a retired plugin had saved for a user, if any.

    The plugin is gone, so this reads the key that ``Plugin`` kept it under.
    """
    raw = await redis_api.get(
        f"config:plugin:name={plugin.canonical_name}:user_id={user.id}"
    )
    if raw is None:
        return None
    try:
        saved = json.loads(raw)
    except ValueError:
        return None
    return saved if isinstance(saved, dict) else None


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
    # What the user is told if a workflow was installed turned off. Empty:
    # that its plugin wrote nothing, which is why the others are off.
    off: str = ""


def _conversions() -> list[Conversion]:
    # Imported here: the plugins import this module.
    from spanreed.plugins import (
        gmail_monitor,
        health_connect,
        hevy,
        litnotes,
        withings,
    )

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
        Conversion(
            canonical_name="hevy",
            name="Hevy",
            convert=hevy.convert_to_workflows,
            note=(
                "The commands Sync Hevy workouts and Re-create Hevy data are "
                "now one, “Sync Hevy workouts”, which asks what to write."
            ),
        ),
        Conversion(
            canonical_name="gmail-monitor",
            name="Gmail Monitor",
            convert=gmail_monitor.convert_to_workflows,
            note=(
                "Each email rule is a workflow of its own. The commands Manage "
                "Email Rules, Test Email Rule, Email Monitor Status and Run "
                "rules on inbox are gone: edit, test and run a workflow, and "
                "see its history, under Workflows in the web UI."
            ),
            off=(
                "A rule that was off is a workflow that is off. One that "
                "could not be turned on says why on its page."
            ),
        ),
        Conversion(
            canonical_name="lit-notes",
            name="Lit Notes",
            convert=litnotes.convert_to_workflows,
            note=(
                "The folder of the notes and the two note templates are on "
                "the step “Append to note” of the workflow. The command is "
                "still “Add book note”."
            ),
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
    quoted = [f"“{workflow.name}”" for workflow in workflows]
    names = (
        " and ".join(quoted)
        if len(quoted) < 3
        else ", ".join(quoted[:-1]) + " and " + quoted[-1]
    )
    off = [workflow.name for workflow in workflows if not workflow.enabled]
    text = (
        f"{conversion.name} now does its work through workflows: {names}. "
        "They do what the plugin did, with your settings. See them under "
        "Workflows in the web UI, or with “Manage workflows”."
    )
    if off and conversion.off:
        text += f" {conversion.off}"
    elif off:
        text += f" “{off[0]}” is off, because it had nothing to write."
    if conversion.note:
        text += f" {conversion.note}"
    bot = await UserInteractionApi.for_user(user)
    await bot.notify(text, parse_html=False)
