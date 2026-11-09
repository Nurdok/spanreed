"""Move Health Connect payloads from the webhook inbox into the store.

See ``spanreed.apis.health_connect`` for how the data arrives. This plugin
only parses and stores records. Later changes add the daily-note
properties (steps, heart-rate metrics) on top of it.
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
from spanreed.apis.health_connect import (
    HealthConnectApi,
    HealthConnectPayload,
    HealthConnectStore,
    WEBHOOK_URL,
    parse_payload,
)


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
    touched_dates: set[datetime.date] = field(default_factory=set)


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
                await self._ingest(user, raw)
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
            summary.touched_dates |= await store.add_steps(payload.steps)

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
