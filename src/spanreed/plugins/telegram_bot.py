import asyncio
import datetime
import os
import logging
from enum import Enum
from typing import List, NamedTuple, Optional
import redis
import json
import spanreed

import telegram.ext
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, \
    CallbackQueryHandler, Application

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


class UserData:
    @classmethod
    async def create(cls, user_id: int, redis_api: redis.Redis) -> 'UserData':
        self = UserData(user_id)
        self.event_storage = await EventStorageRedis.create(user_id=self.user_id,
                                                      redis_api=redis_api)
        return self

    def __init__(self, user_id: int):
        self.user_id = user_id
        self.event_storage: EventStorageRedis = None


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
        self._redis_key = f'events:user_id={user_id}'
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
                Event(datetime.date.fromisoformat(event_json['date']),
                      ActivityType[event_json['activity_type']],
                      EventType[event_json['event_type']]))
        return events

    async def _write_to_storage(self) -> None:
        events_json = []
        for event in self._events:
            events_json.append({'date': event.date.isoformat(),
                                'activity_type': event.activity_type.name,
                                'event_type': event.event_type.name})
        await self._redis.set(self._redis_key, json.dumps(events_json))

    async def add(self, event: Event):
        self._events.append(event)
        await self._write_to_storage()

    def find_event(self, activity_type: ActivityType, date: datetime.date) -> \
            Optional[EventType]:
        for event in self._events:
            if event.activity_type == activity_type and event.date == date:
                return event.event_type


async def ask_journal(context: ContextTypes.DEFAULT_TYPE):
    user_data: UserData = \
        context.application.user_data[context.job.user_id][
            UserData.__name__]
    activity_type: ActivityType = ActivityType.JOURNAL

    if (event_type := user_data.event_storage.find_event(ActivityType.JOURNAL,
                                                         datetime.date.today())) is not None:
        logger.info(f'Skipping asking for {activity_type} because its '
                    f'status is {event_type}')
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
        reply_markup=reply_markup
    )


async def button(update: Update,
                 context: ContextTypes.DEFAULT_TYPE) -> None:
    """Parses the CallbackQuery and updates the message text."""
    query = update.callback_query

    # CallbackQueries need to be answered, even if no notification to the user is needed
    # Some clients may have trouble otherwise. See https://core.telegram.org/bots/api#callbackquery
    await query.answer()

    if query.data == "1":
        text = "Awesome!"
        await context.user_data[UserData.__name__].event_storage.add(
            Event(datetime.date.today(), ActivityType.JOURNAL, EventType.DONE))
    elif query.data == "2":
        text = "Sure, I'll ask again later."
    elif query.data == "3":
        await context.user_data[UserData.__name__].event_storage.add(
            Event(datetime.date.today(), ActivityType.JOURNAL,
                  EventType.SKIPPED))
        text = "A'ight, I won't bother you again today."

    await query.edit_message_text(text=text)


def remove_job_if_exists(name: str,
                         job_queue: telegram.ext.JobQueue):
    """Remove job with given name. Returns whether job was removed."""
    current_jobs = job_queue.get_jobs_by_name(name)
    if not current_jobs:
        return False
    for job in current_jobs:
        job.schedule_removal()
    return True


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_message.from_user.id
    chat_id = update.effective_message.chat_id

    context.user_data.setdefault(UserData.__name__,
                                 await UserData.create(user_id=user_id,
                                          redis_api=context.bot_data['redis']))
    context.bot_data['redis'].lpush('user_ids', user_id)

    remove_job_if_exists(str(user_id), context.job_queue)
    context.job_queue.run_repeating(ask_journal,
                                    datetime.timedelta(hours=1),
                                    user_id=user_id,
                                    chat_id=chat_id,
                                    name=str(user_id))

    text = "Subscribed to journaling questions!"
    await update.effective_message.reply_text(text)


async def subscribe_existing_users_on_startup(
        application: Application) -> None:
    logger.info("Subscribing existing users on startup")
    redis_api = application.bot_data['redis']
    user_ids = [int(uid) for uid in await redis_api.lrange('user_ids', 0, -1)]

    for user_id in user_ids:
        logger.info(f"Subscribing user_id={user_id}")

        application.user_data[user_id].setdefault(UserData.__name__,
                                                  await UserData.create(user_id=user_id,
                                                           redis_api=redis_api))
        logger.info(f'user_data[user_id]={application.user_data[user_id]}')
        remove_job_if_exists(str(user_id), application.job_queue)

        # We are using the fact that user_id == chat_id when a bot is used in a
        # direct message with a user. This isn't guaranteed by the Telegram
        # API, so let's hope this isn't a stupid idea.
        chat_id = user_id
        application.job_queue.run_repeating(ask_journal,
                                            datetime.timedelta(seconds=10),
                                            user_id=user_id,
                                            chat_id=chat_id,
                                            name=str(user_id))


async def setup_application(redis_api: redis.Redis, app_builder: Optional[ApplicationBuilder] = None) -> Application:
    if app_builder is None:
        app_builder = ApplicationBuilder()

    application = app_builder.token(
        os.environ['TELEGRAM_API_TOKEN']).build()

    application.bot_data['redis'] = redis_api

    start_handler = CommandHandler('s', subscribe)
    application.add_handler(start_handler)
    application.add_handler(CallbackQueryHandler(button))

    await subscribe_existing_users_on_startup(application)
    return application


class TelegramBotPlugin(spanreed.plugin.Plugin):
    def __init__(self, *args, **kwargs):
        super().__init__(name="Telegram Bot", *args, **kwargs)

    async def run(self):
        application = await setup_application(self._redis)

        async with application:  # Calls `initialize` and `shutdown`
            await application.start()
            await application.updater.start_polling()
            try:
                # Wait for cancellation so we can perform the cleanup.
                await asyncio.sleep(datetime.timedelta(hours=1).total_seconds())
            except asyncio.CancelledError:
                await application.updater.stop()
                await application.stop()
                raise