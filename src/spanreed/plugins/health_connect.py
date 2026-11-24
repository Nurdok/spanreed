"""Receive Health Connect payloads and say which days changed.

See ``spanreed.apis.health_connect`` for how the data arrives. This plugin
stores every record of each payload by local date. Then it tells the
workflow triggers what changed: the days whose metrics changed, and the days
of each record type whose raw records changed.

What is written into the vault, and where, is up to the user's workflows.
Two recipes do what this plugin used to do itself: "Health data → daily
note" and "Health raw data → vault files". ``convert_to_workflows`` gives
them to a user who had the plugin before, with the settings they had.
"""

import asyncio
import datetime
import html
import json
import logging
from dataclasses import dataclass, field

from spanreed.plugin import Plugin
from spanreed.service import serve_users
from spanreed.storage import redis_api
from spanreed.user import User
from spanreed.interaction import UserInteractionApi, PluginCommand
from spanreed.plugins.user_profile import ask_for_birth_date, ask_for_timezone
from spanreed.apis.obsidian import ObsidianPlugin
from spanreed.apis.health_connect import (
    HealthConnectApi,
    HealthConnectPayload,
    HealthConnectStore,
    WEBHOOK_URL,
    parse_payload,
)
from spanreed.apis.health_connect_blocks import (
    METRIC_RECORD_TYPES,
    DayChanged,
    RawRecordsChanged,
    health_connect_blocks,
    raw_records_key,
)
from spanreed.workflows import migrations
from spanreed.workflows.blocks import Block
from spanreed.workflows.engine import WorkflowEngine
from spanreed.workflows.model import Workflow
from spanreed.workflows.recipes import all_recipes
from spanreed.workflows.store import WorkflowStore

# The recipes that do what this plugin used to do itself.
DAILY_NOTE_RECIPE = "health-connect-daily-note"
RAW_FILES_RECIPE = "health-connect-raw-files"
# Vault folder of the raw data files, unless the user chose another one
# while the folder was a setting of this plugin.
DEFAULT_RAW_DATA_FOLDER = "Health/raw"


@dataclass
class UserConfig:
    # Bearer token the bridge app sends; also registered in Redis so the
    # HTTP route can map it back to the user.
    # Empty until the plugin is set up for the user.
    # Hidden in the settings form: a new token must also be registered, which
    # only the setup flow does.
    token: str = field(default="", metadata={"config_form": "hidden"})


@dataclass
class IngestSummary:
    """Counts of what one payload added to the store."""

    heart_rate_samples: int = 0
    resting_heart_rate_samples: int = 0
    steps_records: int = 0
    skipped_records: int = 0
    # Local dates that got new or changed records: of any type, and of the
    # types that a day's metrics are computed from.
    touched_dates: set[datetime.date] = field(default_factory=set)
    metric_dates: set[datetime.date] = field(default_factory=set)
    # Changed local dates for each record type of the store, including the
    # types that no metric is computed from.
    stored_dates: dict[str, set[datetime.date]] = field(default_factory=dict)
    # Stored records removed because the phone reported them as deleted, and
    # deleted ids that matched no stored record.
    deleted_records: int = 0
    unmatched_deletions: int = 0


def normalize_vault_folder(text: str) -> str:
    """Return a vault folder path as typed by the user, in a clean form.

    Accept either kind of slash and drop the slashes and spaces around each
    part. Raise ``ValueError`` if no folder is left, or if a part is ``.``
    or ``..``.
    """
    parts = [part.strip() for part in text.replace("\\", "/").split("/")]
    parts = [part for part in parts if part]
    if not parts:
        raise ValueError("The folder is empty.")
    if any(part in (".", "..") for part in parts):
        raise ValueError("The folder must not contain '.' or '..' parts.")
    return "/".join(parts)


def setup_instructions(token: str) -> str:
    # HTML, not Markdown: the bot's send path defaults to parse_html=True, so
    # Markdown markers were shown literally and got copied into the app.
    # The token sits alone in <code> so Telegram copies exactly it on tap.
    return (
        "Install the <b>Health Connect Webhook</b> app on your phone "
        "(github.com/mcnaveen/health-connect-webhook), grant it the "
        "Health Connect permissions for the data you want synced "
        "(heart rate, steps, VO2 max), and add a webhook with:\n\n"
        f"URL: <code>{WEBHOOK_URL}</code>\n"
        "Custom header name: <code>Authorization</code>\n"
        "Custom header value: <code>Bearer</code>, a space, then this token:\n"
        f"<code>{html.escape(token)}</code>\n\n"
        "Then enable background sync (an interval of 30–60 minutes is fine) "
        "and press <b>Sync Now</b> once to verify."
    )


class HealthConnectPlugin(Plugin[UserConfig]):
    @classmethod
    def name(cls) -> str:
        return "Health Connect"

    @classmethod
    def has_user_config(cls) -> bool:
        return True

    @classmethod
    def get_config_class(cls) -> type[UserConfig]:
        return UserConfig

    @classmethod
    async def get_config(cls, user: User) -> UserConfig:
        """Return the user's config.

        A config saved while this plugin wrote to the vault itself also
        holds the folder and the properties it wrote. They are settings of
        the user's workflows now, and are left out here.
        """
        saved = await cls.get_saved_config_data(user) or {}
        return UserConfig(token=str(saved.get("token") or ""))

    @classmethod
    def get_prerequisites(cls) -> list[type[Plugin]]:
        return [ObsidianPlugin]

    def blocks(self) -> list[Block]:
        """Return the Health Connect triggers for workflows."""
        return health_connect_blocks()

    @classmethod
    async def ask_for_user_config(cls, user: User) -> None:
        logging.info("Asking for Health Connect user config.")
        bot: UserInteractionApi = await UserInteractionApi.for_user(user)

        # Records are bucketed by the user's local date, so the timezone
        # must be known before the first payload arrives.
        if user.timezone is None and not await ask_for_timezone(user, bot):
            await bot.send_message(
                "Health Connect setup stopped. Set your timezone and try again."
            )
            raise RuntimeError("User timezone not set.")

        # The heart rate zone minutes need the user's age. They are an
        # extra, so setup goes on without the date of birth.
        if user.birth_date is None and not await ask_for_birth_date(user, bot):
            await bot.send_message(
                "Heart rate zone minutes are skipped until you set your "
                "date of birth with “Set date of birth”."
            )

        # Reconfiguring keeps the token unless the user asks for a new one,
        # because a new token must be entered again in the phone app.
        config: UserConfig = await cls.get_config(user)
        old_token = config.token
        if old_token:
            choice = await bot.request_user_choice(
                "The phone app is set up with a token. A new token must be "
                "entered in the phone app again.",
                ["Keep the current token", "Generate a new token"],
            )
            if choice == 1:  # Generate a new token
                config.token = ""
        if not config.token:
            config.token = HealthConnectApi.generate_token()

        await HealthConnectApi.register_token(user, config.token)
        await cls.set_config(user, config)
        if config.token != old_token:
            if old_token:
                await HealthConnectApi.unregister_token(old_token)
            await bot.send_message(setup_instructions(config.token), parse_html=True)
        if not await cls._has_workflows(user):
            await bot.send_message(
                "The data is stored, and workflows put it in your vault. Add "
                "“Health data → daily note” and “Health raw data → vault "
                "files” from the recipes of “Manage workflows”."
            )

    @staticmethod
    async def _has_workflows(user: User) -> bool:
        """Return whether a workflow of the user uses a Health Connect trigger."""
        triggers = {block.id for block in health_connect_blocks()}
        return any(
            workflow.trigger.block in triggers
            for workflow in await WorkflowStore().list_for_user(user.id)
        )

    async def run(self) -> None:
        await UserInteractionApi.register_command(
            self,
            PluginCommand(text="Show Health Connect setup", callback=self._show_setup),
        )
        await UserInteractionApi.register_command(
            self,
            PluginCommand(
                text="Delete Health Connect data", callback=self._delete_data
            ),
        )
        await serve_users(self, self._drain)

    async def _show_setup(self, user: User) -> None:
        bot: UserInteractionApi = await UserInteractionApi.for_user(user)
        config: UserConfig = await self.get_config(user)
        await bot.send_message(setup_instructions(config.token), parse_html=True)

    async def _drain(self, user: User) -> None:
        """Take what the user's phone sent out of the inbox, for good.

        Store each payload, and tell the user's workflows what changed.
        """
        config: UserConfig = await self.get_config(user)
        # Re-register on every start so the token→user mapping survives a
        # Redis flush or a config restored from backup.
        await HealthConnectApi.register_token(user, config.token)

        while True:
            raw = await HealthConnectApi.dequeue_payload(user)
            if raw is None:
                continue  # poll timeout; loop so cancellation is noticed
            try:
                summary = await self._ingest(user, raw)
                await self._announce(user, summary)
            except asyncio.CancelledError:
                raise
            except Exception:
                # One bad payload must not take the drain loop down: it
                # would start again only after a pause, and the inbox
                # would pile up.
                self._logger.exception("Failed to ingest Health Connect payload.")

    async def _ingest(self, user: User, raw: str) -> IngestSummary:
        """Store the records of one raw payload and return a summary.

        Store every record, of every type. Then remove the stored records
        that the payload reports as deleted on the phone.
        """
        data = json.loads(raw)
        payload: HealthConnectPayload = parse_payload(data)
        store = HealthConnectStore(user)
        summary = IngestSummary(
            heart_rate_samples=len(payload.heart_rate),
            resting_heart_rate_samples=len(payload.resting_heart_rate),
            steps_records=len(payload.steps),
            skipped_records=payload.skipped_records,
        )
        summary.stored_dates = await store.add_payload(data)
        # After the new records, so that a record which is both sent and
        # reported as deleted does not stay.
        deletions = await store.apply_deletions(data)
        summary.deleted_records = deletions.removed_records
        summary.unmatched_deletions = deletions.unmatched_ids
        for record_type, dates in deletions.touched.items():
            summary.stored_dates.setdefault(record_type, set()).update(dates)
        await store.mark_export_emptied(deletions.emptied)
        for record_type, dates in summary.stored_dates.items():
            summary.touched_dates |= dates
            if record_type in METRIC_RECORD_TYPES:
                summary.metric_dates |= dates

        self._logger.info(
            f"Ingested Health Connect payload for user {user.id} "
            f"(app {payload.app_version}): "
            f"{summary.heart_rate_samples} heart-rate, "
            f"{summary.resting_heart_rate_samples} resting-heart-rate, "
            f"{summary.steps_records} steps records; "
            f"{summary.skipped_records} skipped; "
            f"{summary.deleted_records} deleted on the phone "
            f"({summary.unmatched_deletions} deleted ids not found); "
            f"dates={sorted(d.isoformat() for d in summary.touched_dates)}"
        )
        if payload.unknown_types:
            self._logger.info(
                f"Also stored record types with no metrics: {payload.unknown_types}"
            )
        return summary

    @staticmethod
    async def _announce(user: User, summary: IngestSummary) -> None:
        """Tell the user's workflows what a payload changed.

        The records are stored already. A workflow that cannot write them
        now, because Obsidian is closed, keeps the day in its queue.
        """
        await DayChanged.emit(
            user, [date.isoformat() for date in sorted(summary.metric_dates)]
        )
        await RawRecordsChanged.emit(
            user,
            [
                raw_records_key(record_type, date)
                for record_type, dates in sorted(summary.stored_dates.items())
                for date in sorted(dates)
            ],
        )

    async def _delete_data(self, user: User) -> None:
        """Delete stored records of recent days, on request from the user.

        Only the server-side store is cleared. Daily notes keep their values
        until new data for that day is synced and written over them, and raw
        data files are left as they are.
        """
        bot: UserInteractionApi = await UserInteractionApi.for_user(user)
        options = [("Today", 1), ("The last 7 days", 7), ("The last 30 days", 30)]
        choice = await bot.request_user_choice(
            "Delete stored Health Connect data for…",
            [label for label, _ in options] + ["Cancel"],
        )
        if not 0 <= choice < len(options):
            return
        label, days = options[choice]

        today = datetime.datetime.now(tz=user.tzinfo).date()
        dates = [today - datetime.timedelta(days=i) for i in reversed(range(days))]
        confirm = await bot.request_user_choice(
            f"Delete all stored Health Connect records from "
            f"{dates[0].isoformat()} to {dates[-1].isoformat()}? "
            "Daily notes and raw data files are not changed.",
            ["Delete", "Cancel"],
        )
        if confirm != 0:
            return

        deleted = await HealthConnectStore(user).delete_dates(dates)
        self._logger.info(
            f"Deleted {deleted} Health Connect day keys for user {user.id} "
            f"({dates[0].isoformat()}..{dates[-1].isoformat()})"
        )
        await bot.send_message(
            f"Deleted stored Health Connect data for {label.lower()}. "
            "To fill it in again, run a manual sync in the phone app that "
            "covers those days."
        )


# -- from a plugin that writes to the vault to workflows that do ---------------


def _legacy_pending_key(user: User) -> str:
    # Where the plugin kept the days whose raw data files were out of date.
    return f"health-connect:export-pending:{user.id}"


async def convert_to_workflows(engine: WorkflowEngine, user: User) -> list[Workflow]:
    """Give a user of the old plugin the workflows that do what it did.

    Install the two recipes with the settings the user had: the raw data
    folder, and without the daily note properties that were turned off.
    Queue what the plugin still had to write, and the last two days, so
    that nothing is missed in between. Return the workflows that were
    installed. Doing this again installs nothing.
    """
    saved = await HealthConnectPlugin.get_saved_config_data(user) or {}
    workflows = {w.recipe: w for w in await engine.store.list_for_user(user.id)}
    installed = []

    keep_properties = migrations.keep_daily_note_properties(
        saved.get("daily_note_fields")
    )

    def keep_folder(workflow: Workflow) -> None:
        try:
            folder = normalize_vault_folder(str(saved.get("raw_data_folder") or ""))
        except ValueError:
            folder = DEFAULT_RAW_DATA_FOLDER
        for step in workflow.steps:
            if "path" in step.inputs:
                step.inputs["path"] = step.inputs["path"].replace(
                    DEFAULT_RAW_DATA_FOLDER, folder, 1
                )

    for recipe_id, keep in (
        (DAILY_NOTE_RECIPE, keep_properties),
        (RAW_FILES_RECIPE, keep_folder),
    ):
        if recipe_id not in workflows:
            workflows[recipe_id] = await engine.install(
                user, all_recipes()[recipe_id], keep
            )
            installed.append(workflows[recipe_id])

    # Payloads that arrived while the workflows were not there yet changed
    # days that nothing wrote. They are recent, so queue the last two days.
    store = HealthConnectStore(user)
    today = datetime.datetime.now(tz=user.tzinfo).date()
    recent = {today, today - datetime.timedelta(days=1)}
    stored = await store.get_stored_days()
    days = engine.store.queue(workflows[DAILY_NOTE_RECIPE].id)
    for date in sorted(
        {d for t, d in stored if d in recent and t in METRIC_RECORD_TYPES}
    ):
        await days.put(date.isoformat())
    raw = engine.store.queue(workflows[RAW_FILES_RECIPE].id)
    for record_type, date in stored:
        if date in recent:
            await raw.put(raw_records_key(record_type, date))
    # What the plugin still had to write. Its marks have the form of the
    # trigger's keys.
    for member in await redis_api.smembers(_legacy_pending_key(user)):
        await raw.put(member.decode("utf-8") if isinstance(member, bytes) else member)
    await redis_api.delete(_legacy_pending_key(user))

    # Last: the saved config loses the settings that the workflows now hold.
    await HealthConnectPlugin.set_config(
        user, await HealthConnectPlugin.get_config(user)
    )
    return installed
