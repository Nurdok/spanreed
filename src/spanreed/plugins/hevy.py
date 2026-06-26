import asyncio
import collections
import datetime
import json
import pathlib
import re

import yaml

from spanreed.plugin import Plugin
from spanreed.user import User
from spanreed.apis.telegram_bot import TelegramBotApi, PluginCommand
from spanreed.apis.hevy import HevyApi, Workout, Exercise, WorkoutSet
from spanreed.apis.obsidian_webhook import (
    ObsidianWebhookApi,
    ObsidianWebhookPlugin,
)
from dataclasses import dataclass

# Cursor used the first time we sync for a user (the Unix epoch), which makes
# Hevy return the entire workout history.
EPOCH = "1970-01-01T00:00:00Z"

# Keys for per-user state stored via Plugin user-data: the ISO timestamp of the
# last successful sync, and the set of workout ids we've already written notes
# for (so re-runs/restarts never duplicate notes).
SINCE_KEY = "since"
SYNCED_IDS_KEY = "synced_ids"


@dataclass
class UserConfig:
    api_key: str
    vault_dir: str


class HevyPlugin(Plugin):
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
    def get_prerequisites(cls) -> list[type[Plugin]]:
        return [ObsidianWebhookPlugin]

    @classmethod
    async def ask_for_user_config(cls, user: User) -> None:
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)
        api_key = await bot.request_user_input(
            "Please enter your Hevy API key.\n"
            "You can generate one (Hevy Pro required) at "
            "https://hevy.com/settings?developer"
        )
        vault_dir = await bot.request_user_input(
            "Which folder in your vault should set notes go in? "
            "(e.g. Fitness/Sets)"
        )
        await cls.set_config(
            user,
            UserConfig(
                api_key=api_key.strip(),
                vault_dir=vault_dir.strip().strip("/"),
            ),
        )

    async def _get_synced_ids(self, user: User) -> set[str]:
        raw = await self.get_user_data(user, SYNCED_IDS_KEY)
        if not raw:
            return set()
        return set(json.loads(raw))

    async def _set_synced_ids(self, user: User, ids: set[str]) -> None:
        await self.set_user_data(user, SYNCED_IDS_KEY, json.dumps(sorted(ids)))

    async def run(self) -> None:
        await TelegramBotApi.register_command(
            self,
            PluginCommand(
                text="Sync Hevy workouts", callback=self._sync_now
            ),
        )
        await super().run()

    async def run_for_user(self, user: User) -> None:
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)
        while True:
            await self._sync(user, bot)
            await asyncio.sleep(datetime.timedelta(hours=1).total_seconds())

    async def _sync_now(self, user: User) -> None:
        """Manually triggered sync via the Telegram command."""
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)
        await bot.send_message("Checking Hevy for new workouts…")
        if await self._sync(user, bot) == 0:
            await bot.send_message("No new Hevy workouts found.")

    async def _sync(self, user: User, bot: TelegramBotApi) -> int:
        """Fetch and write any new workouts. Returns the number written."""
        config: UserConfig = await self.get_config(user)
        hevy = await HevyApi.for_user(user)
        webhook = await ObsidianWebhookApi.for_user(user)

        # Capture the cursor *before* fetching so we never miss workouts
        # logged while a sync is running. Overlap is harmless: the synced
        # id set prevents duplicate notes.
        poll_start = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        since = (await self.get_user_data(user, SINCE_KEY)) or EPOCH
        synced_ids: set[str] = await self._get_synced_ids(user)

        workouts = await hevy.get_updated_workouts_since(since)
        new_count = 0
        for workout in workouts:
            if workout.id in synced_ids:
                # Already written. (Edits made in Hevy after the fact are
                # not re-synced, since the webhook only appends.)
                continue

            set_count = await self._write_workout(config, webhook, workout)
            synced_ids.add(workout.id)
            await self._set_synced_ids(user, synced_ids)
            await bot.send_message(
                f"Logged Hevy workout: {workout.title} "
                f"({set_count} sets across "
                f"{len(workout.exercises)} exercises)."
            )
            new_count += 1

        await self.set_user_data(user, SINCE_KEY, poll_start)
        return new_count

    async def _write_workout(
        self,
        config: UserConfig,
        webhook: ObsidianWebhookApi,
        workout: Workout,
    ) -> int:
        """Write one note per set; return the number of sets written."""
        date = self._workout_date(workout)
        date_str = date.strftime("%Y-%m-%d") if date else workout.id

        # The same exercise can appear more than once in a session; track
        # occurrences so we can disambiguate file names only when needed.
        title_counts: dict[str, int] = collections.Counter(
            e.title for e in workout.exercises
        )
        seen: dict[str, int] = collections.defaultdict(int)

        set_ordinal = 0
        for exercise_index, exercise in enumerate(
            workout.exercises, start=1
        ):
            seen[exercise.title] += 1
            occurrence = seen[exercise.title]
            suffix = (
                f" ({occurrence})" if title_counts[exercise.title] > 1 else ""
            )
            for set_number, workout_set in enumerate(exercise.sets, start=1):
                set_ordinal += 1
                filename = (
                    f"{self._sanitize(exercise.title)}{suffix} "
                    f"set {set_number}.md"
                )
                note_path = str(
                    pathlib.PurePosixPath(config.vault_dir)
                    / date_str
                    / filename
                )
                content = self._render_set_note(
                    workout,
                    exercise,
                    workout_set,
                    set_number,
                    date,
                    exercise_index,
                    set_ordinal,
                )
                await webhook.append_to_note(note_path, content)
        return set_ordinal

    @staticmethod
    def _sanitize(name: str) -> str:
        # Strip characters that are illegal in file names on common filesystems.
        return re.sub(r'[\\/:*?"<>|]', "", name).strip() or "Exercise"

    @staticmethod
    def _parse_dt(value: str) -> datetime.datetime | None:
        if not value:
            return None
        try:
            return datetime.datetime.fromisoformat(
                value.replace("Z", "+00:00")
            )
        except ValueError:
            return None

    def _workout_date(self, workout: Workout) -> datetime.date | None:
        dt = self._parse_dt(workout.start_time) or self._parse_dt(
            workout.created_at
        )
        return dt.date() if dt else None

    @staticmethod
    def _est_1rm(weight_kg: float | None, reps: int | None) -> float | None:
        # Epley formula.
        if not weight_kg or not reps:
            return None
        return round(weight_kg * (1 + reps / 30), 1)

    def _render_set_note(
        self,
        workout: Workout,
        exercise: Exercise,
        workout_set: WorkoutSet,
        set_number: int,
        date: datetime.date | None,
        exercise_index: int,
        set_ordinal: int,
    ) -> str:
        volume_kg = None
        if workout_set.weight_kg is not None and workout_set.reps is not None:
            volume_kg = round(workout_set.weight_kg * workout_set.reps, 1)

        frontmatter: dict = {"type": "workout-set"}
        if date:
            # A real date object so Obsidian renders it as a Date property.
            frontmatter["date"] = date
        frontmatter["exercise"] = exercise.title
        frontmatter["workout"] = workout.title
        frontmatter["workout_id"] = workout.id
        # exercise_index: position of this exercise in the session (1-based).
        # set_ordinal: position of this set across the whole session (1-based).
        # Sort a Base by date, exercise_index, set_number — or just
        # set_ordinal — to reproduce the order performed.
        frontmatter["exercise_index"] = exercise_index
        frontmatter["set_number"] = set_number
        frontmatter["set_ordinal"] = set_ordinal
        frontmatter["set_type"] = workout_set.type
        frontmatter["weight_kg"] = workout_set.weight_kg
        frontmatter["reps"] = workout_set.reps
        if workout_set.rpe is not None:
            frontmatter["rpe"] = workout_set.rpe
        if volume_kg is not None:
            frontmatter["volume_kg"] = volume_kg
        est_1rm = self._est_1rm(workout_set.weight_kg, workout_set.reps)
        if est_1rm is not None:
            frontmatter["est_1rm_kg"] = est_1rm
        frontmatter["tags"] = ["workout-set"]

        yaml_block = yaml.safe_dump(
            frontmatter, sort_keys=False, allow_unicode=True
        ).strip()

        weight = (
            f"{workout_set.weight_kg:g}"
            if workout_set.weight_kg is not None
            else "–"
        )
        reps = workout_set.reps if workout_set.reps is not None else "–"
        summary = f"{exercise.title} — set {set_number}: {weight} kg × {reps}"
        if workout_set.rpe is not None:
            summary += f" @ RPE {workout_set.rpe:g}"

        return f"---\n{yaml_block}\n---\n\n{summary}\n"
