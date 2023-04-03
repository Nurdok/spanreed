import asyncio
import datetime
import os
import logging
from typing import List, NamedTuple, Optional
import redis

from spanreed.plugin import Plugin
from spanreed.user import User

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    Application,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_message.from_user.id
    chat_id = update.effective_message.chat_id

    context.user_data.setdefault(
        UserData.__name__,
        await UserData.create(
            user_id=user_id, redis_api=context.bot_data["redis"]
        ),
    )
    context.bot_data["redis"].lpush("user_ids", user_id)

    remove_job_if_exists(str(user_id), context.job_queue)
    context.job_queue.run_repeating(
        ask_journal,
        datetime.timedelta(hours=1),
        user_id=user_id,
        chat_id=chat_id,
        name=str(user_id),
    )

    text = "Subscribed to journaling questions!"
    await update.effective_message.reply_text(text)


async def setup_application(
    redis_api: redis.Redis, app_builder: Optional[ApplicationBuilder] = None
) -> Application:
    if app_builder is None:
        app_builder = ApplicationBuilder()

    application = app_builder.token(os.environ["TELEGRAM_API_TOKEN"]).build()

    application.bot_data["redis"] = redis_api

    start_handler = CommandHandler("s", subscribe)
    application.add_handler(start_handler)
    application.add_handler(CallbackQueryHandler(button))

    await subscribe_existing_users_on_startup(application)
    return application


class TelegramBotPlugin(Plugin):
    @property
    def name(self) -> str:
        return "Telegram Bot"

    async def run(self):
        application = await setup_application(self._redis)

        async with application:  # Calls `initialize` and `shutdown`
            await application.start()
            await application.updater.start_polling()
            try:
                # Wait for cancellation so we can perform the cleanup.
                await asyncio.sleep(
                    datetime.timedelta(hours=1).total_seconds()
                )
            except asyncio.CancelledError:
                await application.updater.stop()
                await application.stop()
                raise


class TelegramBotApi:
    # TODO: Replace this low-level exposed app with something safer.
    _application: Application = None

    def __init__(self, telegram_user_id: str):
        self._logger = logging.getLogger(__name__)
        self._user_id = telegram_user_id

    @classmethod
    async def get_application(cls) -> Application:
        # TODO: Replace with an event condition so we can simply await for this to be available.
        if cls._application is not None:
            raise ValueError(
                "Instantiating the per-user Telegram bot API is illegal before"
                " the bot itself is set up."
            )
        return cls._application

    @classmethod
    async def for_user(cls, user: User) -> "TelegramBotApi":
        # Make sure the app is initialized before returning.
        await cls.get_application()
        return TelegramBotApi(user.config["telegram"]["user_id"])

    @classmethod
    def remove_job_if_exists(cls, name: str):
        """Remove job with given name. Returns whether job was removed."""
        app = await cls.get_application()
        current_jobs = app.job_queue.get_jobs_by_name(name)
        if not current_jobs:
            return False
        for job in current_jobs:
            job.schedule_removal()
        return True
