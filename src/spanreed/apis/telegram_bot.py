import asyncio
import datetime
import os
import logging
import typing
import uuid
from typing import List, NamedTuple, Optional, Dict, Callable, Tuple
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
PLUGIN_COMMANDS = "plugin-commands"


class CallbackData(NamedTuple):
    callback_id: int
    user_id: int
    position: int


class PluginCommand(NamedTuple):
    text: str
    callback: Callable


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
        application.add_handler(CommandHandler("do", self.show_command_menu))

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

    async def get_user_by_telegram_user_id(
        self, telegram_user_id: int
    ) -> User:
        for user in await self.get_users():
            self._logger.info(f"Checking {user=}")
            if (
                user.config.get("telegram", {}).get("user_id", 0)
                == telegram_user_id
            ):
                return user

        raise RuntimeError(f"User not found for {telegram_user_id=}")

    async def show_command_menu(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        app: Application = context.application
        commands = app.bot_data.setdefault("plugin-commands", {})
        self._logger.info(f"{commands=}")
        telegram_user_id: int = update.effective_user.id
        user: User = await self.get_user_by_telegram_user_id(telegram_user_id)
        self._logger.info(f"{user.plugins=}")

        # keyboard = []
        # for plugin_canonical_name, commands in app.bot_data.setdefault(
        #     PLUGIN_COMMANDS, {}
        # ).items():
        #     if plugin_canonical_name not in user.plugins:
        #         continue
        #
        #     self._logger.info(f"Adding commands for {plugin_canonical_name=}")
        #
        #     buttons = []
        #     for command in commands:
        #         self._logger.info(f"Adding command {command=}")
        #         buttons.append(
        #             InlineKeyboardButton(command.text, callback_data=command)
        #         )
        #     keyboard.append(buttons)

        # We need to do user interaction is a separate task because we can't
        # block the main Telegram bot coroutine.
        async def show_command_menu_task():
            bot = await TelegramBotApi.for_user(user)

            shown_commands = []
            for plugin_canonical_name, commands in app.bot_data.setdefault(
                PLUGIN_COMMANDS, {}
            ).items():
                if plugin_canonical_name not in user.plugins:
                    continue

                self._logger.info(
                    f"Adding commands for {plugin_canonical_name=}"
                )

                for command in commands:
                    self._logger.info(f"Adding command {command=}")
                    shown_commands.append(command)

            choice = await bot.request_user_choice(
                "Please choose a command to run:",
                [command.text for command in shown_commands],
            )
            chosen_command = shown_commands[choice]
            await bot.send_message(f"Running {chosen_command.text}...")
            self._logger.info(
                f"Running {chosen_command=}: {chosen_command.callback=}"
            )
            await chosen_command.callback(user)

        asyncio.create_task(show_command_menu_task())


class TelegramBotApi:
    # TODO: Replace this low-level exposed app with something safer.
    _application: Application = None
    _application_initialized = asyncio.Event()

    def __init__(self, telegram_user_id: str):
        self._logger = logging.getLogger(TelegramBotApi.__name__)
        self._user_id = telegram_user_id

    @classmethod
    async def register_command(cls, plugin: Plugin, command: PluginCommand):
        _logger = logging.getLogger(cls.__name__)
        _logger.info(f"Registering command '{command.text}'")
        app = await cls.get_application()
        app.bot_data.setdefault(PLUGIN_COMMANDS, {}).setdefault(
            plugin.canonical_name, []
        ).append(command)

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

    @classmethod
    async def init_callback(cls) -> Tuple[int, asyncio.Event]:
        callback_id = uuid.uuid4().int
        app = await cls.get_application()
        event = asyncio.Event()
        app.bot_data.setdefault(CALLBACK_EVENTS, {})[callback_id] = event
        return callback_id, event

    async def request_user_choice(
        self, prompt: str, choices: List[str]
    ) -> int:
        app = await self.get_application()

        # Generate a random callback ID to avoid collisions.
        callback_id, callback_event = await self.init_callback()

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

    async def request_user_input(self, prompt):
        return "input"
