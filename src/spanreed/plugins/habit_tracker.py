import datetime
from enum import Enum
import json
from typing import List, NamedTuple, Optional

import redis

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
    def __init__(self, *, user_id, redis_api: redis.Redis):
        self._redis: redis.Redis = redis_api
        self._redis_key = f"events:user_id={user_id}"
        self._events: List[Event] = []

    @classmethod
    async def create(cls, *, user_id, redis_api: redis.Redis):
        self = EventStorageRedis(user_id=user_id, redis_api=redis_api)
        self._events: List[Event] = await self._load_from_storage()
        return self

    async def _load_from_storage(self) -> List[Event]:
        events: List[Event] = []
        redis_response = await self._redis.get(self._redis_key)
        logger.info(f"key={self._redis_key},value={redis_response}")
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


class HabitTrackerPlugin(Plugin):
    @property
    def name(self) -> str:
        return "Habit Tracker"

    async def run_for_user(self, user: User):
        bot = TelegramBotApi.for_user(user)
        event_storage = await EventStorageRedis.create(user.id, self._redis)
        self.subscribe(user, bot, event_storage)
        pass

    async def subscribe(
        self, user: User, bot: TelegramBotApi, event_storage: EventStorageRedis
    ) -> None:
        self._logger.info(f"Subscribing user_id={user.id}")

        app = await bot.get_application()
        bot.remove_job_if_exists(str(user.id))

        # We are using the fact that user_id == chat_id when a bot is used in a
        # direct message with a user. This isn't guaranteed by the Telegram
        # API, so let's hope this isn't a stupid idea.
        app.job_queue.run_repeating(
            self.ask_journal,
            interval=datetime.timedelta(hours=3),
            first=datetime.timedelta(seconds=10),
            user_id=user.config["telegram"]["user_id"],
            chat_id=user.config["telegram"]["user_id"],
            name=str(user.id),
        )

    def ask_journal_per_user(
        self, user: User, event_storage: EventStorageRedis
    ):
        async def ask_journal(context: ContextTypes.DEFAULT_TYPE):
            activity_type: ActivityType = ActivityType.JOURNAL

            if (event_type := event_storage.find_event(activity_type, datetime.date.today())) is not None:
                self._logger.info(
                    f"Skipping asking for {activity_type} because its "
                    f"status is {event_type}"
                )
                return

            keyboard = [
                [
                    InlineKeyboardButton("Yes", callback_data="1"),
                    InlineKeyboardButton("Not yet", callback_data="2"),
                    InlineKeyboardButton("Not happening", callback_data="3"),
                ],
            ]

            reply_markup = InlineKeyboardMarkup(keyboard)

            await context.bot.send_message(
                context.job.chat_id,
                text="Did you journal today?",
                reply_markup=reply_markup,
            )

        return ask_journal

    async def button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Parses the CallbackQuery and updates the message text."""
        query = update.callback_query

        # CallbackQueries need to be answered, even if no notification to the user is needed
        # Some clients may have trouble otherwise. See https://core.telegram.org/bots/api#callbackquery
        await query.answer()

        if query.data == "1":
            text = "Awesome!"
            await context.user_data[UserData.__name__].event_storage.add(
                Event(datetime.date.today(), ActivityType.JOURNAL, EventType.DONE)
            )
        elif query.data == "2":
            text = "Sure, I'll ask again later."
        elif query.data == "3":
            await context.user_data[UserData.__name__].event_storage.add(
                Event(
                    datetime.date.today(), ActivityType.JOURNAL, EventType.SKIPPED
                )
            )
            text = "A'ight, I won't bother you again today."

        await query.edit_message_text(text=text)
