"""Move Health Connect payloads from the webhook inbox into the vault.

See ``spanreed.apis.health_connect`` for how the data arrives. This plugin
stores every record of each payload by local date. For the dates that
changed, it writes the step total and the heart rate metrics into the daily
note, and it rewrites the raw data files, which hold every stored record.
"""

import asyncio
import datetime
import html
import json
import logging
from dataclasses import dataclass, field

from spanreed.plugin import Plugin
from spanreed.user import User
from spanreed.interaction import UserInteractionApi, PluginCommand
from spanreed.plugins.user_profile import ask_for_timezone
from spanreed.apis.obsidian import ObsidianApi, ObsidianPlugin
from spanreed.apis.health_connect import (
    HealthConnectApi,
    HealthConnectPayload,
    HealthConnectStore,
    WEBHOOK_URL,
    daily_heart_rate_metrics,
    daily_step_total,
    dump_record,
    parse_payload,
    type_name_of,
)


# Frontmatter properties written into the daily note.
STEPS_PROPERTY = "steps"
# Keys of daily_heart_rate_metrics, written in this order.
HEART_RATE_PROPERTIES = (
    "resting-heart-rate",
    "heart-rate-p5",
    "heart-rate-avg",
    "heart-rate-p95",
    "heart-rate-p98",
    "heart-rate-p99",
)
# Folder holding the daily notes (matches the other daily-note plugins).
DAILY_NOTE_FOLDER = "Daily"
# Default vault folder of the raw data files. It holds one JSON Lines file for
# each record type and local date: ``Health/raw/heart_rate/2026-05-09.jsonl``.
DEFAULT_RAW_DATA_FOLDER = "Health/raw"


@dataclass
class UserConfig:
    # Bearer token the bridge app sends; also registered in Redis so the
    # HTTP route can map it back to the user.
    token: str
    # Vault folder that the raw data files are written under.
    raw_data_folder: str = DEFAULT_RAW_DATA_FOLDER


@dataclass
class IngestSummary:
    """Counts of what one payload added to the store."""

    heart_rate_samples: int = 0
    resting_heart_rate_samples: int = 0
    steps_records: int = 0
    skipped_records: int = 0
    # Local dates that got new or changed records, for all types together
    # and per daily-note section (those are the dates to rewrite).
    touched_dates: set[datetime.date] = field(default_factory=set)
    steps_dates: set[datetime.date] = field(default_factory=set)
    heart_rate_dates: set[datetime.date] = field(default_factory=set)
    # Changed local dates for each record type of the store, including the
    # types that no metric is computed from.
    stored_dates: dict[str, set[datetime.date]] = field(default_factory=dict)


def raw_file_path(folder: str, record_type: str, date: datetime.date) -> str:
    """Return the vault path of the raw data file of one type and date."""
    return f"{folder}/{type_name_of(record_type)}/{date.isoformat()}.jsonl"


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
        "(heart rate, steps), and add a webhook with:\n\n"
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
    def get_prerequisites(cls) -> list[type[Plugin]]:
        return [ObsidianPlugin]

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

        token = HealthConnectApi.generate_token()
        await HealthConnectApi.register_token(user, token)
        await cls.set_config(user, UserConfig(token=token))

        await bot.send_message(setup_instructions(token), parse_html=True)

    async def run(self) -> None:
        await UserInteractionApi.register_command(
            self,
            PluginCommand(text="Show Health Connect setup", callback=self._show_setup),
        )
        await UserInteractionApi.register_command(
            self,
            PluginCommand(text="Sync steps", callback=self._sync_steps_now),
        )
        await UserInteractionApi.register_command(
            self,
            PluginCommand(text="Sync heart rate", callback=self._sync_heart_rate_now),
        )
        await UserInteractionApi.register_command(
            self,
            PluginCommand(
                text="Export Health Connect raw data", callback=self._export_raw_now
            ),
        )
        await UserInteractionApi.register_command(
            self,
            PluginCommand(
                text="Set Health Connect raw data folder",
                callback=self._set_raw_data_folder,
            ),
        )
        await UserInteractionApi.register_command(
            self,
            PluginCommand(
                text="Delete Health Connect data", callback=self._delete_data
            ),
        )
        await super().run()

    async def _show_setup(self, user: User) -> None:
        bot: UserInteractionApi = await UserInteractionApi.for_user(user)
        config: UserConfig = await self.get_config(user)
        await bot.send_message(setup_instructions(config.token), parse_html=True)

    async def run_for_user(self, user: User) -> None:
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
                # Silent: the background path must never message the user.
                # If Obsidian is offline the records are already stored, and
                # the next payload or a manual sync writes them.
                await self._write_steps(user, sorted(summary.steps_dates), notify=False)
                await self._write_heart_rate(
                    user, sorted(summary.heart_rate_dates), notify=False
                )
                await self._write_raw_files(user)
            except asyncio.CancelledError:
                raise
            except Exception:
                # One bad payload must not take the drain loop down (the
                # supervisor would back off and the inbox would pile up).
                self._logger.exception("Failed to ingest Health Connect payload.")

    async def _ingest(self, user: User, raw: str) -> IngestSummary:
        """Store the records of one raw payload and return a summary.

        Store every record, of every type. Mark the raw data files of the
        changed days as out of date.
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
        await store.mark_export_pending(
            (record_type, date)
            for record_type, dates in summary.stored_dates.items()
            for date in dates
        )
        for record_type, dates in summary.stored_dates.items():
            summary.touched_dates |= dates
            if record_type == "steps":
                summary.steps_dates |= dates
            elif record_type in ("heart-rate", "resting-heart-rate"):
                summary.heart_rate_dates |= dates

        self._logger.info(
            f"Ingested Health Connect payload for user {user.id} "
            f"(app {payload.app_version}): "
            f"{summary.heart_rate_samples} heart-rate, "
            f"{summary.resting_heart_rate_samples} resting-heart-rate, "
            f"{summary.steps_records} steps records; "
            f"{summary.skipped_records} skipped; "
            f"dates={sorted(d.isoformat() for d in summary.touched_dates)}"
        )
        if payload.unknown_types:
            self._logger.info(
                f"Also stored record types with no metrics: {payload.unknown_types}"
            )
        return summary

    async def _write_raw_files(self, user: User) -> int:
        """Bring the raw data file of every out-of-date day up to date.

        Rewrite each file from the stored records, one JSON record on each
        line. Keep a day marked as out of date if its file cannot be
        written, so that the next run writes it. Return the number of files
        written.
        """
        store = HealthConnectStore(user)
        pending = await store.get_export_pending()
        if not pending:
            return 0
        config: UserConfig = await self.get_config(user)
        obsidian = await ObsidianApi.for_user(user)

        written = 0
        for record_type, date in pending:
            # Clear the mark before reading, so that records which arrive
            # during the write mark the day again.
            await store.clear_export_pending(record_type, date)
            try:
                records = await store.get_raw_records(record_type, date)
                if not records:
                    continue  # the stored records expired or were deleted
                content = "".join(f"{dump_record(record)}\n" for record in records)
                await obsidian.write_binary_file(
                    raw_file_path(config.raw_data_folder, record_type, date),
                    content.encode("utf-8"),
                    overwrite=True,
                )
            except BaseException:
                await store.mark_export_pending([(record_type, date)])
                raise
            written += 1
            self._logger.info(
                f"Wrote {len(records)} {record_type} records for {date.isoformat()}"
            )
        return written

    async def _export_raw_now(self, user: User) -> None:
        """Rewrite the raw data file of every stored day, on request."""
        bot: UserInteractionApi = await UserInteractionApi.for_user(user)
        config: UserConfig = await self.get_config(user)
        store = HealthConnectStore(user)
        await store.mark_export_pending(await store.get_stored_days())
        await bot.notify("Exporting raw Health Connect data…")
        written = await self._write_raw_files(user)
        if written == 0:
            await bot.notify("No stored Health Connect data to export.")
        else:
            await bot.notify(
                f"Wrote {written} raw data {'file' if written == 1 else 'files'} "
                f"to {config.raw_data_folder}."
            )

    async def _set_raw_data_folder(self, user: User) -> None:
        """Change the vault folder of the raw data files, on request.

        Leave the files of the old folder where they are. New and changed
        days are written under the new folder from then on.
        """
        bot: UserInteractionApi = await UserInteractionApi.for_user(user)
        config: UserConfig = await self.get_config(user)
        choice = await bot.request_user_choice(
            f"Raw Health Connect data is written to {config.raw_data_folder}.",
            ["Change folder", "Cancel"],
        )
        if choice != 0:
            return
        answer = await bot.request_user_input(
            "Send the vault folder for the raw data files, for example "
            f"{DEFAULT_RAW_DATA_FOLDER}."
        )
        try:
            folder = normalize_vault_folder(answer)
        except ValueError as e:
            await bot.send_message(f"Folder not changed. {e}")
            return
        if folder == config.raw_data_folder:
            await bot.send_message(f"The folder is already {folder}.")
            return

        old_folder = config.raw_data_folder
        config.raw_data_folder = folder
        await self.set_config(user, config)
        self._logger.info(
            f"Raw data folder of user {user.id} changed from {old_folder!r} "
            f"to {folder!r}"
        )
        await bot.send_message(
            f"Raw data files are now written to {folder}. Files already in "
            f"{old_folder} were not moved. Run “Export Health Connect raw "
            "data” to write every stored day to the new folder."
        )

    async def _sync_steps_now(self, user: User) -> None:
        """Write step totals for recent days, on request from the user."""
        bot: UserInteractionApi = await UserInteractionApi.for_user(user)
        choice = await bot.request_user_choice(
            "Sync steps for…",
            ["Today", "The last week", "Cancel"],
        )
        if choice == 0:  # Today
            days = 1
        elif choice == 1:  # The last week
            days = 7
        else:  # Cancel
            return

        today = datetime.datetime.now(tz=user.tzinfo).date()
        dates = [today - datetime.timedelta(days=i) for i in reversed(range(days))]
        await bot.notify("Syncing steps…")
        if await self._write_steps(user, dates, notify=True) == 0:
            await bot.notify("No new step counts found.")

    async def _sync_heart_rate_now(self, user: User) -> None:
        """Write heart rate metrics into daily notes, on request from the user.

        Offer today, the last week, or all time. All time covers every date
        that still has stored records, and reports one total instead of a
        message for each day.
        """
        bot: UserInteractionApi = await UserInteractionApi.for_user(user)
        choice = await bot.request_user_choice(
            "Sync heart rate for…",
            ["Today", "The last week", "All time", "Cancel"],
        )
        if choice == 2:  # All time
            dates = await HealthConnectStore(user).get_heart_rate_dates()
            await bot.notify("Syncing heart rate…")
            written = await self._write_heart_rate(user, dates, notify=False)
            if written == 0:
                await bot.notify("No new heart rate values found.")
            else:
                await bot.notify(
                    f"Logged heart rate in {written} daily "
                    f"{'note' if written == 1 else 'notes'}, out of "
                    f"{len(dates)} stored {'day' if len(dates) == 1 else 'days'}."
                )
            return
        if choice == 0:  # Today
            days = 1
        elif choice == 1:  # The last week
            days = 7
        else:  # Cancel
            return

        today = datetime.datetime.now(tz=user.tzinfo).date()
        dates = [today - datetime.timedelta(days=i) for i in reversed(range(days))]
        await bot.notify("Syncing heart rate…")
        if await self._write_heart_rate(user, dates, notify=True) == 0:
            await bot.notify("No new heart rate values found.")

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

    async def _write_steps(
        self, user: User, dates: list[datetime.date], *, notify: bool
    ) -> int:
        """Write the step total of each date into its daily note.

        Compute each total from the stored records. Skip a date that has no
        records. Overwrite the property when the total changed, because
        today's total grows through the day. Leave it alone when it is the
        same. Send Telegram messages only when ``notify`` is true. Return
        the number of notes written.
        """
        if not dates:
            return 0
        bot: UserInteractionApi = await UserInteractionApi.for_user(user)
        store = HealthConnectStore(user)
        obsidian = await ObsidianApi.for_user(user)

        # Only today's daily note is generated on demand; past dates are
        # expected to already have their notes.
        await obsidian.safe_generate_today_note()

        written = 0
        for date in dates:
            records = await store.get_steps(date)
            if not records:
                continue
            total = daily_step_total(records)
            daily_note: str = await obsidian.get_daily_note(DAILY_NOTE_FOLDER, date)
            try:
                existing = await obsidian.get_property(daily_note, STEPS_PROPERTY)
                if existing == total:
                    continue
                await obsidian.set_value_of_property(daily_note, STEPS_PROPERTY, total)
            except FileNotFoundError:
                if notify:
                    await bot.notify(f"No daily note for {date.isoformat()}; skipping.")
                continue

            written += 1
            self._logger.info(f"Wrote {STEPS_PROPERTY}={total} for {date.isoformat()}")
            if notify:
                await bot.notify(f"Logged steps for {date.isoformat()}: {total}.")
        return written

    async def _write_heart_rate(
        self, user: User, dates: list[datetime.date], *, notify: bool
    ) -> int:
        """Write heart rate metrics of each date into its daily note.

        Compute ``resting-heart-rate``, ``heart-rate-p5``, ``heart-rate-avg``,
        ``heart-rate-p95``, ``heart-rate-p98`` and ``heart-rate-p99`` from the
        stored records. Skip a date with no records and leave out a metric the
        day has no data for. Set only the properties whose value changed. Send Telegram messages only when ``notify`` is true. Return
        the number of notes written.
        """
        if not dates:
            return 0
        bot: UserInteractionApi = await UserInteractionApi.for_user(user)
        store = HealthConnectStore(user)
        obsidian = await ObsidianApi.for_user(user)
        await obsidian.safe_generate_today_note()

        written = 0
        for date in dates:
            metrics = daily_heart_rate_metrics(
                await store.get_heart_rate(date),
                await store.get_resting_heart_rate(date),
            )
            if not metrics:
                continue
            daily_note: str = await obsidian.get_daily_note(DAILY_NOTE_FOLDER, date)
            changed: dict[str, int] = {}
            try:
                for prop in HEART_RATE_PROPERTIES:
                    if prop not in metrics:
                        continue
                    if await obsidian.get_property(daily_note, prop) == metrics[prop]:
                        continue
                    await obsidian.set_value_of_property(
                        daily_note, prop, metrics[prop]
                    )
                    changed[prop] = metrics[prop]
            except FileNotFoundError:
                if notify:
                    await bot.notify(f"No daily note for {date.isoformat()}; skipping.")
                continue
            if not changed:
                continue

            written += 1
            values = ", ".join(f"{k}={v}" for k, v in changed.items())
            self._logger.info(f"Wrote {values} for {date.isoformat()}")
            if notify:
                await bot.notify(f"Logged heart rate for {date.isoformat()}: {values}.")
        return written
