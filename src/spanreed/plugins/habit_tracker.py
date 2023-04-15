import asyncio
import dataclasses
import datetime
import logging
from enum import Enum, member
import json
from typing import List, NamedTuple, Optional

import redis.asyncio as redis

from spanreed.plugin import Plugin
from spanreed.user import User
from spanreed.apis.telegram_bot import TelegramBotApi

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    Application,
)


class ActivityType(Enum):
    UNKNOWN = 0
    JOURNAL = 1


class EventType(Enum):
    UNKNOWN = 0
    DONE = 1
    SKIPPED = 2


class Event(NamedTuple):
    date: datetime.date
    activity_type: ActivityType
    event_type: EventType


class EventStorageRedis:
    def __init__(self, *, user: User, redis_api: redis.Redis):
        self._redis: redis.Redis = redis_api
        self._redis_key = f"events:user_id={user.id}"
        self._logger = logging.getLogger(__name__)
        self._events: List[Event] = []

    @classmethod
    async def for_user(cls, user: User, redis_api: redis.Redis):
        self = EventStorageRedis(user=user, redis_api=redis_api)
        self._events: List[Event] = await self._load_from_storage()
        return self

    async def _load_from_storage(self) -> List[Event]:
        events: List[Event] = []
        redis_response = await self._redis.get(self._redis_key)
        if redis_response is None:
            return []
        self._logger.info(f"key={self._redis_key},value={redis_response}")
        try:
            events_json = json.loads(redis_response)
        except json.JSONDecodeError:
            return []
        for event_json in events_json:
            events.append(
                Event(
                    datetime.date.fromisoformat(event_json["date"]),
                    ActivityType[event_json["activity_type"]],
                    EventType[event_json["event_type"]],
                )
            )
        return events

    async def _write_to_storage(self) -> None:
        events_json = []
        for event in self._events:
            events_json.append(
                {
                    "date": event.date.isoformat(),
                    "activity_type": event.activity_type.name,
                    "event_type": event.event_type.name,
                }
            )
        await self._redis.set(self._redis_key, json.dumps(events_json))

    async def add(self, event: Event):
        self._events.append(event)
        await self._write_to_storage()

    def find_event(
        self, activity_type: ActivityType, date: datetime.date
    ) -> Optional[EventType]:
        for event in self._events:
            if event.activity_type == activity_type and event.date == date:
                return event.event_type


@dataclasses.dataclass
class Choice:
    text: str
    event: EventType


class HabitTrackerPlugin(Plugin):
    @property
    def name(self) -> str:
        return "Habit Tracker"

    async def run_for_user(self, user: User):
        self._logger.info(f"Running for user {user}")
        bot = await TelegramBotApi.for_user(user)
        self._logger.info(f"Got bot")
        event_storage = await EventStorageRedis.for_user(user, self._redis)

        # TODO: Allow users to decide on their tracked habits
        activity_type = ActivityType.JOURNAL

        while True:
            self._logger.info(
                f"Checking if we need to ask for {activity_type}"
            )
            if (
                event_type := event_storage.find_event(
                    activity_type, datetime.date.today()
                )
            ) is not None:
                self._logger.info(
                    f"Skipping asking for {activity_type} because its "
                    f"status is {event_type}"
                )
            else:
                await self.poll_user(activity_type, bot, event_storage)

            await asyncio.sleep(datetime.timedelta(hours=4).total_seconds())

    async def poll_user(
        self,
        activity_type: ActivityType,
        bot: TelegramBotApi,
        event_storage: EventStorageRedis,
    ):
        self._logger.info(f"Polling user for {activity_type}")
        prompt = f"Did you {activity_type.name.lower()} today?"
        choices = [
            Choice("Yes", EventType.DONE),
            Choice("Not yet", EventType.UNKNOWN),
            Choice("Not happening", EventType.SKIPPED),
        ]

        choice = choices[
            await bot.request_user_choice(prompt, [c.text for c in choices])
        ]

        self._logger.info(f"User chose {choice.text}")

        replies = {
            EventType.DONE: "Great!",
            EventType.UNKNOWN: "I'll ask again later.",
            EventType.SKIPPED: "FINE, I'll remind you tomorrow, you worthles-- I mean, you're great!",
        }

        if choice.event != EventType.UNKNOWN:
            await event_storage.add(
                Event(
                    datetime.date.today(), ActivityType.JOURNAL, choice.event
                )
            )

        await bot.send_message(replies[choice.event])
