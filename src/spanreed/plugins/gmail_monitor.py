"""Hold the user's Gmail login, and publish the Gmail blocks.

What is done with an email is up to the user's workflows: the "New email"
trigger with its filter, and steps that get attachments, find links, save
files and send messages.

This plugin used to hold rules: a filter and a list of actions each, edited
in chat. ``convert_to_workflows`` turns each rule that a user had into a
workflow that does what the rule did.
"""

import datetime
import json
import logging
from dataclasses import dataclass

from spanreed.plugin import Plugin
from spanreed.storage import redis_api
from spanreed.user import User
from spanreed.interaction import UserInteractionApi
from spanreed.apis.gmail import GmailApi
from spanreed.apis.gmail_blocks import gmail_blocks
from spanreed.plugins.gmail_rules import RULE_RECIPE, workflow_for_rule
from spanreed.workflows.blocks import Block, PollTrigger
from spanreed.workflows.engine import WorkflowEngine
from spanreed.workflows.model import Workflow, new_workflow_id
from spanreed.workflows.store import WorkflowStore

_logger = logging.getLogger(__name__)

# Where the plugin kept, for each user, when it last looked for mail and
# the ids of the emails it had checked against the rules.
LAST_CHECK_KEY = "last_check"
PROCESSED_KEY = "processed_emails"


@dataclass
class UserConfig:
    """Nothing: the Gmail login is kept by ``GmailApi``, not in a config."""


class GmailMonitorPlugin(Plugin[UserConfig]):
    @classmethod
    def name(cls) -> str:
        return "Gmail Monitor"

    @classmethod
    def has_user_config(cls) -> bool:
        # There is nothing to configure, but setting the plugin up logs in
        # to Gmail, and that is what ``ask_for_user_config`` is called for.
        return True

    @classmethod
    def get_config_class(cls) -> type[UserConfig]:
        return UserConfig

    @classmethod
    async def get_config(cls, user: User) -> UserConfig:
        """Return the user's config, which holds nothing.

        A config saved while this plugin held rules still has them until
        they are converted. They are workflows now, and are left out here.
        """
        return UserConfig()

    def blocks(self) -> list[Block]:
        """Return the Gmail blocks for workflows."""
        return gmail_blocks()

    @classmethod
    async def ask_for_user_config(cls, user: User) -> None:
        bot = await UserInteractionApi.for_user(user)

        # Check if app-level Gmail credentials are configured
        if not await GmailApi.is_app_configured():
            await bot.send_message(
                "Gmail app credentials not configured. Please contact your admin to set up Gmail integration."
            )
            return

        if await cls.get_saved_config_data(user) is None:
            await cls.set_config(user, UserConfig())

        # Authenticate user with Gmail
        await cls._setup_gmail_auth(user, bot)
        if not await cls._has_workflows(user):
            await bot.send_message(
                "Workflows say what happens to an email. Make one under "
                "Workflows in the web UI: “New workflow”, and start with the "
                "trigger “New email”."
            )

    @classmethod
    async def _setup_gmail_auth(cls, user: User, bot: UserInteractionApi) -> None:
        gmail = await GmailApi.for_user(user)

        if await gmail.is_authenticated():
            await bot.send_message("Gmail is already authenticated!")
            return

        try:
            auth_flow = await GmailApi.start_authentication(user)
            auth_done = await auth_flow.get_done_event()

            auth_url = await auth_flow.get_auth_url()
            await bot.send_message(
                f'Click <a href="{auth_url}">here</a> to authenticate with Gmail.'
            )

            await auth_done.wait()
            await bot.send_message("Gmail authentication successful!")

        except Exception as e:
            await bot.send_message(f"Gmail authentication failed: {str(e)}")
            raise

    @staticmethod
    async def _has_workflows(user: User) -> bool:
        """Return whether a workflow of the user uses the Gmail trigger."""
        triggers = {block.id for block in gmail_blocks()}
        return any(
            workflow.trigger.block in triggers
            for workflow in await WorkflowStore().list_for_user(user.id)
        )


# -- from rules to workflows ---------------------------------------------------


def _cursor(last_check: str | None) -> str | None:
    """Return when the plugin last looked for mail, as the trigger keeps it.

    The plugin kept the time of the server, without a timezone.
    """
    if not last_check:
        return None
    try:
        moment = datetime.datetime.fromisoformat(last_check)
    except ValueError:
        return None
    return moment.astimezone(datetime.timezone.utc).isoformat()


async def convert_to_workflows(engine: WorkflowEngine, user: User) -> list[Workflow]:
    """Give a user of the old plugin a workflow for each rule they had.

    A workflow is on if its rule was, and if the engine can run it. A rule
    that the plugin could not have run either, one with a pattern that is
    no regular expression for example, becomes a workflow that is off and
    says what is wrong with it.

    Each trigger is handed when the plugin last looked for mail and which
    emails it had checked, before its workflow is turned on, so that no
    email is acted on twice. Return the workflows that were made. Doing
    this again makes nothing new.
    """
    saved = await GmailMonitorPlugin.get_saved_config_data(user) or {}
    rules = saved.get("rules")
    rules = rules if isinstance(rules, list) else []
    since = _cursor(await GmailMonitorPlugin.get_user_data(user, LAST_CHECK_KEY))
    processed = await GmailMonitorPlugin.get_user_data(user, PROCESSED_KEY)
    seen = sorted(json.loads(processed)) if processed else []

    existing = {w.recipe for w in await engine.store.list_for_user(user.id)}
    made = []
    for place, rule in enumerate(rules, start=1):
        if not isinstance(rule, dict) or f"{RULE_RECIPE}-{place}" in existing:
            # Not a rule, or made by a conversion that stopped halfway.
            continue
        workflow = workflow_for_rule(
            new_workflow_id(),
            user.id,
            rule,
            saved.get("check_interval_minutes", 15),
            place,
        )
        turn_on = workflow.enabled and not engine.problems(workflow)
        # Off until its trigger knows what the plugin checked: a trigger
        # that polled before that would find every recent email new.
        workflow.enabled = False
        await engine.save(workflow)
        await PollTrigger.start_from(
            engine.store.trigger_state(workflow.id), cursor=since, keys=seen
        )
        if turn_on:
            workflow.enabled = True
            await engine.save(workflow)
        made.append(workflow)

    # Last: the rules and what the plugin kept about them are not needed.
    for key in (LAST_CHECK_KEY, PROCESSED_KEY):
        await redis_api.delete(GmailMonitorPlugin._get_user_data_key(user, key))
    await GmailMonitorPlugin.set_config(user, UserConfig())
    _logger.info(f"Converted {len(made)} Gmail Monitor rules of user {user.id}.")
    return made
