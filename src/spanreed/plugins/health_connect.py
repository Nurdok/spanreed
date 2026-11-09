"""Move Health Connect payloads from the webhook inbox into daily notes.

See ``spanreed.apis.health_connect`` for how the data arrives. This plugin
parses each payload, stores the records by local date, and writes the
daily-note properties for the dates that changed. Steps are written now.
Heart-rate metrics come in a later change.
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
    daily_step_total,
    parse_payload,
)


# Frontmatter property written into the daily note.
STEPS_PROPERTY = "steps"
# Folder holding the daily notes (matches the other daily-note plugins).
DAILY_NOTE_FOLDER = "Daily"


@dataclass
class UserConfig:
    # Bearer token the bridge app sends; also registered in Redis so the
    # HTTP route can map it back to the user.
    token: str


@dataclass
class IngestSummary:
    """Counts of what one payload added to the store."""

    heart_rate_samples: int = 0
    resting_heart_rate_samples: int = 0
    steps_records: int = 0
    skipped_records: int = 0
    # Local dates that got new or changed records, for all types together
    # and for steps alone (the steps dates are the ones to rewrite).
    touched_dates: set[datetime.date] = field(default_factory=set)
    steps_dates: set[datetime.date] = field(default_factory=set)


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
            except asyncio.CancelledError:
                raise
            except Exception:
                # One bad payload must not take the drain loop down (the
                # supervisor would back off and the inbox would pile up).
                self._logger.exception("Failed to ingest Health Connect payload.")

    async def _ingest(self, user: User, raw: str) -> IngestSummary:
        """Parse one raw payload, store its records and return a summary."""
        payload: HealthConnectPayload = parse_payload(json.loads(raw))
        store = HealthConnectStore(user)
        summary = IngestSummary(
            heart_rate_samples=len(payload.heart_rate),
            resting_heart_rate_samples=len(payload.resting_heart_rate),
            steps_records=len(payload.steps),
            skipped_records=payload.skipped_records,
        )
        if payload.heart_rate:
            summary.touched_dates |= await store.add_heart_rate(payload.heart_rate)
        if payload.resting_heart_rate:
            summary.touched_dates |= await store.add_resting_heart_rate(
                payload.resting_heart_rate
            )
        if payload.steps:
            summary.steps_dates = await store.add_steps(payload.steps)
            summary.touched_dates |= summary.steps_dates

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
                f"Payload also carried unparsed types: {payload.unknown_types}"
            )
        return summary

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
