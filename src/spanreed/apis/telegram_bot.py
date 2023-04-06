import asyncio
import datetime
import os
import logging
import typing
import uuid
from typing import List, NamedTuple, Optional, Dict
from dataclasses import dataclass

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


CALLBACK_EVENTS = "callback-events"
CALLBACK_EVENT_RESULTS = "callback-event-results"


class CallbackData(NamedTuple):
    callback_id: int
    user_id: int
    position: int


class TelegramBotPlugin(Plugin):
    @property
    def name(self) -> str:
        return "Telegram Bot"

    async def run(self):
        application = await self.setup_application()

        async with application:  # Calls `initialize` and `shutdown`
            await application.start()
            await application.updater.start_polling()
            logger.info("Started polling")

            TelegramBotApi._application_initialized.set()
            TelegramBotApi._application = application

            try:
                # Wait for cancellation so we can perform the cleanup.
                logger.info("Waiting for cancellation...")
                while True:
                    await asyncio.sleep(
                        datetime.timedelta(hours=1).total_seconds()
                    )
            except asyncio.CancelledError:
                logger.info("Cancellation received. Stopping updater...")
                await application.updater.stop()
                logger.info("Stopping application...")
                await application.stop()
                logger.info("Stopped")
                raise

    async def setup_application(
        self,
        app_builder: Optional[ApplicationBuilder] = None,
    ) -> Application:
        if app_builder is None:
            app_builder = ApplicationBuilder()

        application = (
            app_builder.token(os.environ["TELEGRAM_API_TOKEN"])
            .arbitrary_callback_data(True)
            .build()
        )

        application.add_handler(
            CallbackQueryHandler(self.handle_callback_query)
        )

        return application

    @staticmethod
    async def handle_callback_query(
        update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        logger.info("Received callback query")
        query = update.callback_query

        logger.info(f"query.data={query.data}")
        callback_data: CallbackData = typing.cast(CallbackData, query.data)
        cid = callback_data.callback_id

        # Mark the index of the selected button.
        logger.info(
            f"Marking callback {cid} event result as {callback_data.position}"
        )
        results = context.bot_data.setdefault(CALLBACK_EVENT_RESULTS, {})
        results[cid] = callback_data.position

        logger.info(f"Setting event {cid} as done")
        # Notify the waiting coroutine that we received the user's choice.
        event: asyncio.Event = context.bot_data[CALLBACK_EVENTS][cid]
        event.set()

        await query.delete_message()


class TelegramBotApi:
    # TODO: Replace this low-level exposed app with something safer.
    _application: Application = None
    _application_initialized = asyncio.Event()

    def __init__(self, telegram_user_id: str):
        self._logger = logging.getLogger(TelegramBotApi.__name__)
        self._user_id = telegram_user_id

    @classmethod
    async def get_application(cls) -> Application:
        await cls._application_initialized.wait()
        return cls._application

    @classmethod
    async def for_user(cls, user: User) -> "TelegramBotApi":
        # Make sure the app is initialized before returning.
        _logger = logging.getLogger(cls.__name__)
        _logger.info("Waiting for application to initialize...")
        await cls.get_application()
        _logger.info("Application initialized")
        return TelegramBotApi(user.config["telegram"]["user_id"])

    @classmethod
    def set_application(cls, application: Application):
        if cls._application is not None:
            raise RuntimeError("Application already set")
        cls._application = application
        cls._application_initialized.set()

    async def send_message(self, text: str):
        app = await self.get_application()
        await app.bot.send_message(chat_id=self._user_id, text=text)

    def _init_callback(self, callback_id: int) -> asyncio.Event:
        event = asyncio.Event()
        self._application.bot_data.setdefault(CALLBACK_EVENTS, {})[
            callback_id
        ] = event
        return event

    async def request_user_choice(
        self, prompt: str, choices: List[str]
    ) -> int:
        app = await self.get_application()

        # Generate a random callback ID to avoid collisions.
        callback_id = uuid.uuid4().int
        callback_event: asyncio.Event = self._init_callback(callback_id)

        def make_callback_data(position) -> CallbackData:
            return CallbackData(callback_id, self._user_id, position)

        # Set up the keyboard.
        keyboard = []
        for i, choice in enumerate(choices):
            keyboard.append(
                InlineKeyboardButton(
                    choice, callback_data=make_callback_data(i)
                )
            )

        await app.bot.send_message(
            chat_id=self._user_id,
            text=prompt,
            reply_markup=InlineKeyboardMarkup([keyboard]),
        )

        # Wait for the user to select a choice.
        self._logger.info(f"Waiting for callback {callback_id} to be done")
        await callback_event.wait()
        self._logger.info(f"Callback {callback_id} done")
        return app.bot_data[CALLBACK_EVENT_RESULTS][callback_id]
