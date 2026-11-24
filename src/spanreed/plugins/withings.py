"""Hold the user's Withings login, and publish the Withings trigger.

What is done with the measurements is up to the user's workflows. The
recipe "Withings → daily note" does what this plugin used to do itself.
``convert_to_workflows`` gives it to a user who had the plugin before, with
the settings they had.
"""

import asyncio
import dataclasses
import logging

from spanreed.plugin import Plugin
from spanreed.user import User
from spanreed.interaction import UserInteractionApi
from spanreed.apis.withings import (
    WithingsApi,
    UserConfig,
    AuthenticationFlow,
)
from spanreed.apis.withings_blocks import withings_blocks
from spanreed.workflows import migrations
from spanreed.workflows.blocks import Block
from spanreed.workflows.engine import WorkflowEngine
from spanreed.workflows.model import Workflow
from spanreed.workflows.recipes import all_recipes
from spanreed.workflows.store import WorkflowStore

# The recipe that does what this plugin used to do itself.
DAILY_NOTE_RECIPE = "withings-daily-note"


class WithingsPlugin(Plugin[UserConfig]):
    @classmethod
    def name(cls) -> str:
        return "Withings"

    @classmethod
    def has_user_config(cls) -> bool:
        return True

    @classmethod
    def get_config_class(cls) -> type[UserConfig] | None:
        return UserConfig

    @classmethod
    async def get_config(cls, user: User) -> UserConfig:
        """Return the user's login.

        A config saved while this plugin wrote to the daily note itself
        also holds which properties it wrote. They are rows of the user's
        workflow now, and are left out here.
        """
        saved = await cls.get_saved_config_data(user) or {}
        known = {field.name for field in dataclasses.fields(UserConfig)}
        return UserConfig(**{k: v for k, v in saved.items() if k in known})

    def blocks(self) -> list[Block]:
        """Return the Withings trigger for workflows."""
        return withings_blocks()

    @classmethod
    async def ask_for_user_config(cls, user: User) -> None:
        """Log in to Withings.

        A user who is already logged in can keep the login.
        """
        logging.info("Asking for Withings user config.")
        bot: UserInteractionApi = await UserInteractionApi.for_user(user)

        log_in = True
        if await cls.get_saved_config_data(user) is not None:
            choice = await bot.request_user_choice(
                "You are logged in to Withings.",
                ["Keep the current login", "Log in again"],
            )
            log_in = choice == 1
        if log_in:
            await cls._authenticate(user, bot)
        if not await cls._has_workflows(user):
            await bot.send_message(
                "Workflows put the measurements in your vault. Add "
                "“Withings → daily note” from the recipes of “Manage "
                "workflows”."
            )

    @classmethod
    async def _authenticate(cls, user: User, bot: UserInteractionApi) -> None:
        """Run the Withings login and save the config it gives."""
        auth: AuthenticationFlow
        async with WithingsApi.start_authentication(user) as auth:
            auth_done: asyncio.Event = await auth.get_done_event()

            await bot.send_message(
                f"Click [here]({auth.get_url()}) to authenticate with Withings.",
                parse_markdown=True,
            )

            await auth_done.wait()

            await cls.set_config(user, auth.get_user_config())

    @staticmethod
    async def _has_workflows(user: User) -> bool:
        """Return whether a workflow of the user uses the Withings trigger."""
        triggers = {block.id for block in withings_blocks()}
        return any(
            workflow.trigger.block in triggers
            for workflow in await WorkflowStore().list_for_user(user.id)
        )


# -- from a plugin that writes to the daily note to a workflow that does -------


async def convert_to_workflows(engine: WorkflowEngine, user: User) -> list[Workflow]:
    """Give a user of the old plugin the workflow that does what it did.

    Install the recipe without the daily note properties that the user had
    turned off. Return the workflow if it was installed. Doing this again
    installs nothing.
    """
    saved = await WithingsPlugin.get_saved_config_data(user)
    if saved is None:
        # Registered, but never logged in: there was nothing to keep doing.
        return []
    installed = []
    workflows = await engine.store.list_for_user(user.id)
    if not any(workflow.recipe == DAILY_NOTE_RECIPE for workflow in workflows):
        installed.append(
            await engine.install(
                user,
                all_recipes()[DAILY_NOTE_RECIPE],
                migrations.keep_daily_note_properties(saved.get("daily_note_fields")),
            )
        )

    # Last: the saved config loses the setting that the workflow now holds.
    await WithingsPlugin.set_config(user, await WithingsPlugin.get_config(user))
    return installed
