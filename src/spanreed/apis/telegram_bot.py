import asyncio
import contextlib
import datetime
import os
import logging
import typing
import uuid
from typing import NamedTuple, Optional, Callable
from dataclasses import dataclass
from collections.abc import AsyncGenerator

from spanreed.plugin import Plugin
from spanreed.user import User

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    constants,
)
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    Application,
    filters,
)

CALLBACK_EVENTS = "callback-events"
CALLBACK_EVENT_RESULTS = "callback-event-results"
PLUGIN_COMMANDS = "plugin-commands"
USER_INTERACTION_LOCKS = "user-interaction-locks"
USER_MESSAGE_CALLBACK_ID = "user-message-callback-id"


class CallbackData(NamedTuple):
    callback_id: int
    user_id: int
    position: int


class PluginCommand(NamedTuple):
    text: str
    callback: Callable


@dataclass
class UserConfig:
    user_id: int


class TelegramBotPlugin(Plugin[UserConfig]):
    @classmethod
    def name(cls) -> str:
        return "Telegram Bot"

    @classmethod
    def has_user_config(cls) -> bool:
        return False

    @classmethod
    def get_config_class(cls) -> type[UserConfig]:
        return UserConfig

    async def run(self) -> None:
        application = await self.setup_application()

        async with application:  # Calls `initialize` and `shutdown`
            await application.start()

            if application.updater is None:
                raise RuntimeError("Updater is None")
            await application.updater.start_polling()
            self._logger.info("Started polling")

            TelegramBotApi._application_initialized.set()
            TelegramBotApi._application = application

            try:
                # Wait for cancellation so we can perform the cleanup.
                self._logger.info("Waiting for cancellation...")
                while True:
                    await asyncio.sleep(
                        datetime.timedelta(hours=1).total_seconds()
                    )
            except asyncio.CancelledError:
                self._logger.info("Cancellation received. Stopping updater...")
                await application.updater.stop()
                self._logger.info("Stopping application...")
                await application.stop()
                self._logger.info("Stopped")
                raise

    async def setup_application(
        self,
        app_builder: Optional[ApplicationBuilder] = None,
    ) -> Application:
        if os.environ.get("TELEGRAM_API_TOKEN") is None:
            raise ValueError("TELEGRAM_API_TOKEN not set")

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
        application.add_handler(CommandHandler("start", self.start))
        application.add_handler(
            MessageHandler(filters=filters.ALL, callback=self.handle_message)
        )

        return application

    async def handle_callback_query(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        self._logger.info("Received callback query")
        query = update.callback_query
        if query is None:
            self._logger.info("No callback query found")
            return

        self._logger.info(f"query.data={query.data}")
        callback_data: CallbackData = typing.cast(CallbackData, query.data)
        cid = callback_data.callback_id

        # Mark the index of the selected button.
        self._logger.info(
            f"Marking callback {cid} event result as {callback_data.position}"
        )
        results = context.bot_data.setdefault(CALLBACK_EVENT_RESULTS, {})
        results[cid] = callback_data.position

        self._logger.info(f"Setting event {cid} as done")
        # Notify the waiting coroutine that we received the user's choice.
        event: asyncio.Event = context.bot_data[CALLBACK_EVENTS][cid]
        event.set()

        await query.delete_message()

    async def get_user_by_telegram_user_id(
        self, telegram_user_id: int, send_message_on_failure: bool = True
    ) -> User:
        for user in await self.get_users():
            self._logger.info(f"Checking {user=}")
            user_config: UserConfig = await self.get_config(user)
            if user_config.user_id == telegram_user_id:
                return user

        if send_message_on_failure:
            app = await TelegramBotApi.get_application()
            self._logger.error(
                f"Got a /do command from unknown user {telegram_user_id=}"
            )
            # We need to use the underlying Telegram API here, because we can't
            # use the bot API wrapper, which requires a user to be registered.
            await app.bot.send_message(
                chat_id=telegram_user_id,
                text=(
                    "You're not registered yet.\n"
                    "Please use the /start command to register."
                ),
            )
        raise KeyError(f"User not found for {telegram_user_id=}")

    async def start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if update.effective_user is None:
            self._logger.error("No user found in update")
            return

        telegram_user_id: int = update.effective_user.id
        self._logger.info(
            f"Got a /start command from user {telegram_user_id=}"
        )
        existing_user: Optional[User] = None
        with contextlib.suppress(KeyError):
            existing_user = await self.get_user_by_telegram_user_id(
                telegram_user_id,
                send_message_on_failure=False,
            )
        if existing_user is not None:
            bot = await TelegramBotApi.for_user(existing_user)
            async with bot.user_interaction():
                await bot.send_multiple_messages(
                    "Eh, this is awkward.",
                    f"We already know each other, {existing_user.name}...",
                    "To be honest, I'm a little bit offended.",
                    f"If {existing_user.name} is even your real name...",
                    "Anyway, use the /do command to get started.",
                )
            return

        async def start_task() -> None:
            user: User = await User.create()
            await self.set_config(user, UserConfig(user_id=telegram_user_id))

            # These two plugins are required for the bot to work.
            # Importing here to avoid circular imports.
            from spanreed.plugins.plugin_manager import PluginManagerPlugin

            required_plugins = [
                self,
                await Plugin.get_plugin_by_class(PluginManagerPlugin),
            ]

            for plugin in required_plugins:
                await plugin.register_user(user)

            bot: TelegramBotApi = await TelegramBotApi.for_user(user)

            async with bot.user_interaction():
                await bot.send_multiple_messages(
                    "Howdy partner!",
                    "I'm Spanreed, your <i>personal</i> personal assistant.",
                    "Let's set you up in the system.",
                )
                name = await bot.request_user_input(
                    "How do you want me to address you?"
                )
                await user.set_name(name)
                insulting_names = [
                    "master",
                    "lord",
                    "lady",
                    "sir",
                    "madam",
                    "boss",
                ]
                if name.lower() in insulting_names:
                    await bot.send_message(
                        f"A bit degrading, but okay, <i>{name}</i>.\n"
                    )
                else:
                    await bot.send_message(f"Cool. Cool cool cool.")
                await asyncio.sleep(1)
                await bot.send_multiple_messages(
                    "To get started, you can use the /do command,\n"
                    "It will show you a list of commands you can use.\n",
                    "Since you're new here, there won't be many commands to "
                    "choose from. The magic happens when you "
                    "<b>install plugins</b>.",
                    "You can install plugins using the same /do " "command.",
                    "Once you've installed a plugin, you'll see the commands "
                    "it provides in the list.",
                    "Some plugins will also send you messages, like asking "
                    "your input on decisions, or sending you reminders.",
                    "Try it now - send me a /do command.",
                    delay=1,
                )

        asyncio.create_task(start_task())

    async def show_command_menu(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        app: Application = context.application
        commands: dict[str, PluginCommand] = app.bot_data.setdefault(
            PLUGIN_COMMANDS, {}
        )

        if update.effective_user is None:
            self._logger.error("No user found in update")
            return

        telegram_user_id: int = update.effective_user.id
        user: User = await self.get_user_by_telegram_user_id(telegram_user_id)
        self._logger.info(f"{user.plugins=}")

        # We need to do user interaction is a separate task because we can't
        # block the main Telegram bot coroutine.
        async def show_command_menu_task() -> None:
            self._logger.info(f"Inside internal task for /do for {user}")
            try:
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
                self._logger.info(
                    f"Running {chosen_command=}: {chosen_command.callback=}"
                )
                await chosen_command.callback(user)
            except:
                self._logger.exception("Error in show_command_menu_task")

        asyncio.create_task(show_command_menu_task())

    async def handle_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if update.message is None:
            self._logger.error("No message found in update")
            return

        self._logger.info(f"Received message: {update.message.text=}")
        # Get the user's Telegram ID and find the corresponding user.

        if update.effective_user is None:
            self._logger.error("No user found in update")
            return

        telegram_user_id: int = update.effective_user.id
        user: User = await self.get_user_by_telegram_user_id(telegram_user_id)
        self._logger.info(f"{user=}")

        # See if there's a plugin that is waiting for a message from this user.
        # If so, notify it.
        bot = await TelegramBotApi.for_user(user)
        callback_id: Optional[int] = context.application.bot_data.setdefault(
            USER_MESSAGE_CALLBACK_ID, {}
        ).get(telegram_user_id, None)
        if callback_id is None:
            self._logger.info("No callback ID found")
            await bot.send_message("Unexpected message, ignoring...")
            return

        self._logger.info(f"Found callback ID {callback_id}")
        context.application.bot_data.setdefault(CALLBACK_EVENT_RESULTS, {})[
            callback_id
        ] = update.message.text
        # Notify the waiting coroutine that we received the user's message.
        context.bot_data[CALLBACK_EVENTS][callback_id].set()
        return


class TelegramBotApi:
    _application: Application
    _application_initialized = asyncio.Event()

    def __init__(self, telegram_user_id: int):
        self._logger = logging.getLogger(TelegramBotApi.__name__)
        self._telegram_user_id = telegram_user_id
        self._have_interaction_lock = False

    @classmethod
    async def register_command(
        cls, plugin: Plugin, command: PluginCommand
    ) -> None:
        _logger = logging.getLogger(cls.__name__)
        _logger.info(f"Registering command '{command.text}'")
        app = await cls.get_application()
        app.bot_data.setdefault(PLUGIN_COMMANDS, {}).setdefault(
            plugin.canonical_name(), []
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
        user_config: UserConfig = await TelegramBotPlugin.get_config(user)
        _logger.info(f"Getting TelegramBotApi for {user=} with {user_config=}")
        return TelegramBotApi(
            (await TelegramBotPlugin.get_config(user)).user_id
        )

    @classmethod
    def set_application(cls, application: Application) -> None:
        if cls._application is not None:
            raise RuntimeError("Application already set")
        cls._application = application
        cls._application_initialized.set()

    async def send_message(
        self, text: str, *, parse_html: bool = True
    ) -> None:
        app: Application = await self.get_application()
        parse_mode = None
        if parse_html:
            parse_mode = "HTML"
        await app.bot.send_message(
            chat_id=self._telegram_user_id, text=text, parse_mode=parse_mode
        )

    async def send_multiple_messages(
        self, *text: str, delay: int = 1, parse_html: bool = True
    ) -> None:
        app: Application = await self.get_application()
        for message in text:
            await app.bot.send_chat_action(
                self._telegram_user_id, action=constants.ChatAction.TYPING
            )
            await asyncio.sleep(delay)
            await self.send_message(message, parse_html=parse_html)

    @classmethod
    async def init_callback(cls) -> tuple[int, asyncio.Event]:
        callback_id = uuid.uuid4().int
        app = await cls.get_application()
        event = asyncio.Event()
        app.bot_data.setdefault(CALLBACK_EVENTS, {})[callback_id] = event
        return callback_id, event

    async def request_user_choice(
        self, prompt: str, choices: list[str]
    ) -> int:
        app = await self.get_application()

        # Generate a random callback ID to avoid collisions.
        callback_id, callback_event = await self.init_callback()

        def make_callback_data(position: int) -> CallbackData:
            return CallbackData(callback_id, self._telegram_user_id, position)

        # Set up the keyboard.
        keyboard = []
        for i, choice in enumerate(choices):
            keyboard.append(
                [
                    InlineKeyboardButton(
                        choice, callback_data=make_callback_data(i)
                    )
                ]
            )

        await app.bot.send_message(
            chat_id=self._telegram_user_id,
            text=prompt,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

        # Wait for the user to select a choice.
        self._logger.info(f"Waiting for callback {callback_id} to be done")
        await callback_event.wait()
        self._logger.info(f"Callback {callback_id} done")
        return app.bot_data[CALLBACK_EVENT_RESULTS][callback_id]

    async def request_user_input(self, prompt: str) -> str:
        app: Application = await self.get_application()

        # Generate a random callback ID to avoid collisions.
        callback_id, callback_event = await self.init_callback()

        app.bot_data.setdefault(USER_MESSAGE_CALLBACK_ID, {})[
            self._telegram_user_id
        ] = callback_id

        await self.send_message(prompt)
        self._logger.info(f"Waiting for user input")
        await callback_event.wait()
        return app.bot_data[CALLBACK_EVENT_RESULTS][callback_id]

    async def get_user_interaction_lock(self) -> asyncio.Lock:
        app = await self.get_application()
        return app.bot_data.setdefault(USER_INTERACTION_LOCKS, {}).setdefault(
            self._telegram_user_id, asyncio.Lock()
        )

    @contextlib.asynccontextmanager
    async def user_interaction(self) -> AsyncGenerator[None, None]:
        # TODO: Need to mark somehow where to return any user input.
        self._logger.info("Acquiring user interaction lock")
        async with await self.get_user_interaction_lock():
            self._logger.info("Acquired user interaction lock")
            self._have_interaction_lock = True
            yield
            self._have_interaction_lock = False
        self._logger.info("Released user interaction lock")
