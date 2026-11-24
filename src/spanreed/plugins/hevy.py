"""Hold the user's Hevy API key, and publish the Hevy trigger.

What is done with the workouts is up to the user's workflows. The recipe
"Hevy workouts → set notes" does what this plugin used to do itself.
``convert_to_workflows`` gives it to a user who had the plugin before, with
the folder they had, and so that no workout is written twice.
"""

import json
from dataclasses import dataclass

from spanreed.plugin import Plugin
from spanreed.storage import redis_api
from spanreed.user import User
from spanreed.interaction import UserInteractionApi
from spanreed.apis.hevy_blocks import hevy_blocks
from spanreed.apis.obsidian import ObsidianPlugin
from spanreed.workflows.blocks import Block, PollTrigger
from spanreed.workflows.engine import WorkflowEngine
from spanreed.workflows.model import Step, Workflow
from spanreed.workflows.recipes import all_recipes
from spanreed.workflows.store import WorkflowStore

# The recipe that does what this plugin used to do itself.
SET_NOTES_RECIPE = "hevy-set-notes"
# Vault folder of the set notes in the recipe. A user who chose another one
# while the folder was a setting of this plugin keeps theirs.
DEFAULT_FOLDER = "Fitness/Sets"

# Where the plugin kept, for each user, how far its polls had got and the
# ids of the workouts it had written notes for.
SINCE_KEY = "since"
SYNCED_IDS_KEY = "synced_ids"


@dataclass
class UserConfig:
    api_key: str


class HevyPlugin(Plugin[UserConfig]):
    @classmethod
    def name(cls) -> str:
        return "Hevy"

    @classmethod
    def has_user_config(cls) -> bool:
        return True

    @classmethod
    def get_config_class(cls) -> type[UserConfig] | None:
        return UserConfig

    @classmethod
    async def get_config(cls, user: User) -> UserConfig:
        """Return the user's API key.

        A config saved while this plugin wrote the notes itself also holds
        the folder it wrote them to. That is part of the path in the
        user's workflow now, and is left out here.
        """
        saved = await cls.get_saved_config_data(user) or {}
        return UserConfig(api_key=str(saved.get("api_key") or ""))

    @classmethod
    def get_prerequisites(cls) -> list[type[Plugin]]:
        return [ObsidianPlugin]

    def blocks(self) -> list[Block]:
        """Return the Hevy trigger for workflows."""
        return hevy_blocks()

    @classmethod
    async def ask_for_user_config(cls, user: User) -> None:
        bot: UserInteractionApi = await UserInteractionApi.for_user(user)
        api_key = await bot.request_user_input(
            "Please enter your Hevy API key.\n"
            "You can generate one (Hevy Pro required) at "
            "https://hevy.com/settings?developer"
        )
        await cls.set_config(user, UserConfig(api_key=api_key.strip()))
        if not await cls._has_workflows(user):
            await bot.send_message(
                "Workflows put the workouts in your vault. Add “Hevy workouts "
                "→ set notes” from the recipes of “Manage workflows”."
            )

    @staticmethod
    async def _has_workflows(user: User) -> bool:
        """Return whether a workflow of the user uses the Hevy trigger."""
        triggers = {block.id for block in hevy_blocks()}
        return any(
            workflow.trigger.block in triggers
            for workflow in await WorkflowStore().list_for_user(user.id)
        )


# -- from a plugin that writes the notes to a workflow that does ---------------


def _keep_folder(workflow: Workflow, folder: str) -> None:
    """Make the paths of a new workflow start with the user's folder."""
    start = f"{folder}/" if folder else ""

    def change(steps: list[Step]) -> None:
        for step in steps:
            path = step.inputs.get("path")
            if isinstance(path, str):
                step.inputs["path"] = path.replace(f"{DEFAULT_FOLDER}/", start, 1)
            change(step.steps)

    change(workflow.steps)


async def convert_to_workflows(engine: WorkflowEngine, user: User) -> list[Workflow]:
    """Give a user of the old plugin the workflow that does what it did.

    Install the recipe with the folder the user had. Hand its trigger how
    far the plugin's polls had got and which workouts it wrote, so that no
    workout is written or announced twice. Return the workflow if it was
    installed. Doing this again installs nothing.
    """
    saved = await HevyPlugin.get_saved_config_data(user)
    if saved is None:
        # Registered, but never set up: there was nothing to keep doing.
        return []
    since = await HevyPlugin.get_user_data(user, SINCE_KEY)
    synced = await HevyPlugin.get_user_data(user, SYNCED_IDS_KEY)

    installed = []
    workflows = await engine.store.list_for_user(user.id)
    workflow = next((w for w in workflows if w.recipe == SET_NOTES_RECIPE), None)
    if workflow is None:

        def customize(new: Workflow) -> None:
            # Off until its trigger knows what the plugin wrote: a trigger
            # that polled before that would find every workout new.
            new.enabled = False
            _keep_folder(new, str(saved.get("vault_dir") or "").strip("/"))

        workflow = await engine.install(
            user, all_recipes()[SET_NOTES_RECIPE], customize
        )
        installed.append(workflow)

    # While the plugin's own notes of what it did are there, the workflow
    # was not started yet: this is the first time, or a time that stopped
    # halfway.
    if installed or since is not None or synced is not None:
        await PollTrigger.start_from(
            engine.store.trigger_state(workflow.id),
            cursor=since,
            keys=sorted(json.loads(synced)) if synced else [],
        )
        if not workflow.enabled:
            workflow.enabled = True
            await engine.save(workflow)
        for key in (SINCE_KEY, SYNCED_IDS_KEY):
            await redis_api.delete(HevyPlugin._get_user_data_key(user, key))

    # Last: the saved config loses the setting that the workflow now holds.
    await HevyPlugin.set_config(user, await HevyPlugin.get_config(user))
    return installed
