"""The core plugin that runs the workflow engine.

It publishes Spanreed's own blocks, starts the engine with the blocks of
every plugin, and owns "Manage workflows": the chat command that lists a
user's workflows and lets them be turned on and off, run, and added from a
recipe. Workflows are edited in the web UI, not in chat.
"""

from __future__ import annotations

import asyncio
import datetime
from typing import Any

from spanreed.interaction import PluginCommand, UserInteractionApi
from spanreed.plugin import Plugin
from spanreed.user import User
from spanreed.workflows import migrations
from spanreed.workflows.blocks import Block, BlockRegistry
from spanreed.workflows.asking import asking_blocks
from spanreed.workflows.builtin import builtin_blocks
from spanreed.workflows.files import file_blocks
from spanreed.workflows.engine import WorkflowEngine, outcome
from spanreed.workflows.model import Workflow
from spanreed.workflows.recipes import Recipe, all_recipes

RECENT_RUNS_SHOWN = 5


class WorkflowsPlugin(Plugin):
    """Runs every user's workflows."""

    def __init__(self) -> None:
        """Create the plugin. The engine is created when the plugin runs."""
        super().__init__()
        self._engine: WorkflowEngine | None = None

    @classmethod
    def name(cls) -> str:
        return "Workflows"

    def blocks(self) -> list[Block]:
        """Return the blocks that Spanreed itself publishes."""
        return [*builtin_blocks(), *asking_blocks(), *file_blocks()]

    @property
    def engine(self) -> WorkflowEngine:
        """Return the running engine."""
        if self._engine is None:
            raise RuntimeError("The workflow engine has not started yet.")
        return self._engine

    async def run(self) -> None:
        """Start the engine and keep it running."""
        await UserInteractionApi.register_command(
            self,
            PluginCommand(text="Manage workflows", callback=self._manage),
        )
        await self._register_existing_users()

        registry = BlockRegistry.from_plugins(await Plugin.get_all_plugins())
        self._engine = WorkflowEngine(registry, command_plugin=self.canonical_name())
        try:
            await self._engine.start(user.id for user in await self.get_users())
            # After the start, so that a workflow installed here is started
            # once.
            await migrations.retire_plugins(self._engine)
            await migrations.convert_plugins(self._engine)
            # The engine's work is in its own tasks.
            await asyncio.Event().wait()
        finally:
            await self._engine.stop()

    async def _register_existing_users(self) -> None:
        """Register every user to this core plugin.

        New users get it at /start. Users who signed up before it existed
        do not, and would have no workflows and no "Manage workflows". Every
        Spanreed user is a Telegram user, so the Telegram plugin's user list
        is the full list.
        """
        from spanreed.apis.telegram_bot import TelegramBotPlugin

        telegram = await Plugin.get_plugin_by_class(TelegramBotPlugin)
        for user in await telegram.get_users():
            if not await self.is_registered(user):
                self._logger.info(f"Registering existing user {user.id}")
                await self.register_user(user)

    # -- "Manage workflows" ------------------------------------------------

    async def _manage(self, user: User) -> None:
        bot = await UserInteractionApi.for_user(user)
        while True:
            workflows = await self.engine.store.list_for_user(user.id)
            choice = await bot.request_user_choice(
                "Choose a workflow." if workflows else "You have no workflows yet.",
                [
                    *(f"{'✅' if w.enabled else '⬜'} {w.name}" for w in workflows),
                    "Add from a recipe",
                    "Done",
                ],
            )
            if choice == len(workflows):
                await self._add_from_recipe(bot, user, workflows)
            elif 0 <= choice < len(workflows):
                await self._manage_one(bot, user, workflows[choice])
            else:  # Done
                return

    async def _manage_one(
        self, bot: UserInteractionApi, user: User, workflow: Workflow
    ) -> None:
        engine = self.engine
        while True:
            by_hand = engine.can_run_by_hand(workflow)
            actions = ["Turn off" if workflow.enabled else "Turn on"]
            if by_hand:
                actions.append("Run now")
                actions.append(
                    "Show in the command menu"
                    if workflow.command_hidden
                    else "Hide from the command menu"
                )
            actions += ["Show recent runs", "Delete", "Back"]
            choice = await bot.request_user_choice(
                await self._status(user, workflow), actions
            )
            action = actions[choice] if 0 <= choice < len(actions) else "Back"

            if action in ("Turn off", "Turn on"):
                workflow.enabled = not workflow.enabled
                await engine.save(workflow)
            elif action == "Run now":
                await engine.run_by_hand(workflow.id, user)
            elif action in ("Show in the command menu", "Hide from the command menu"):
                workflow.command_hidden = not workflow.command_hidden
                await engine.save(workflow)
            elif action == "Show recent runs":
                await self._show_recent_runs(bot, user, workflow)
            elif action == "Delete":
                confirm = await bot.request_user_choice(
                    f"Delete “{workflow.name}”? Its run history is deleted too.",
                    ["Delete", "Cancel"],
                )
                if confirm == 0:
                    await engine.delete(workflow)
                    await bot.send_message(
                        f"Deleted “{workflow.name}”.", parse_html=False
                    )
                    return
            else:  # Back
                return

    async def _status(self, user: User, workflow: Workflow) -> str:
        """Return what the user sees about a workflow before choosing."""
        engine = self.engine
        lines = [f"“{workflow.name}” is {'on' if workflow.enabled else 'off'}."]
        found = engine.problems(workflow)
        if found:
            lines.append(f"It cannot run. {found[0]}")
        runs = await engine.store.recent_runs(workflow.id, 1)
        if runs:
            lines.append(f"Last run, {_when(user, runs[0])}: {outcome(runs[0])}")
        else:
            lines.append("It has not run yet.")
        return "\n".join(lines)

    async def _show_recent_runs(
        self, bot: UserInteractionApi, user: User, workflow: Workflow
    ) -> None:
        runs = await self.engine.store.recent_runs(workflow.id, RECENT_RUNS_SHOWN)
        if not runs:
            await bot.send_message(
                f"“{workflow.name}” has not run yet.", parse_html=False
            )
            return
        lines = [f"The last runs of “{workflow.name}”:"]
        for run in runs:
            how = "by hand, " if run["manual"] else ""
            lines.append(f"{_when(user, run)}: {how}{outcome(run)}")
        await bot.send_message("\n".join(lines), parse_html=False)

    async def _add_from_recipe(
        self, bot: UserInteractionApi, user: User, workflows: list[Workflow]
    ) -> None:
        installed = {workflow.recipe for workflow in workflows}
        recipes = [r for r in all_recipes().values() if r.id not in installed]
        if not recipes:
            await bot.send_message("There are no more recipes to add.")
            return
        choice = await bot.request_user_choice(
            "Which recipe do you want to add?",
            [*(recipe.name for recipe in recipes), "Cancel"],
        )
        if not 0 <= choice < len(recipes):
            return
        recipe = recipes[choice]

        missing = await self.missing_plugins(user, recipe)
        if missing:
            await bot.send_message(
                f"“{recipe.name}” needs {' and '.join(missing)} first. "
                "Register with “Manage plugins”, then add the recipe.",
                parse_html=False,
            )
            return
        await self.engine.install(user, recipe)
        await bot.send_message(
            f"Added “{recipe.name}”, and it is on. {recipe.description}",
            parse_html=False,
        )

    @staticmethod
    async def missing_plugins(user: User, recipe: Recipe) -> list[str]:
        """Return the names of the plugins a recipe needs and the user lacks."""
        missing = []
        for plugin in await Plugin.get_all_plugins():
            if plugin.canonical_name() in recipe.requires:
                if not await plugin.is_registered(user):
                    missing.append(plugin.name())
        return missing


def _when(user: User, run: dict[str, Any]) -> str:
    """Return when a run started, in the user's timezone."""
    started = datetime.datetime.fromisoformat(run["started_at"])
    return started.astimezone(user.tzinfo).strftime("%Y-%m-%d %H:%M")
