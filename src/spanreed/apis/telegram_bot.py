import asyncio
import contextlib
import datetime
import os
import logging
import typing
import uuid
from typing import List, NamedTuple, Optional, Dict, Callable, Tuple
from dataclasses import dataclass, asdict

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
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


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


class TelegramBotPlugin(Plugin):
    @property
    def name(self) -> str:
        return "Telegram Bot"

    def has_user_config(self) -> bool:
        # There is actually a UserConfig, but we don't need the user's
        # input to create it, as we take the user's ID from the Telegram
        # API.
        return False

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
        application.add_handler(CommandHandler("start", self.start))
        application.add_handler(
            MessageHandler(filters=None, callback=self.handle_message)
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

    async def get_user_by_telegram_user_id(
        self, telegram_user_id: int, send_message_on_failure: bool = True
    ) -> User:
        for user in await self.get_users():
            self._logger.info(f"Checking {user=}")
            if (
                user.config.get("telegram", {}).get("user_id", 0)
                == telegram_user_id
            ):
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

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
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
                    "Anyway, use the <code>/do</code> command to get started.",
                )
            return

        async def start_task():
            user: User = await User.create()
            telegram_config: UserConfig = UserConfig(user_id=telegram_user_id)

            # Normally we'd edit the existing config, but we just created the
            # user, so we can just set it.
            await user.set_config({"telegram": asdict(telegram_config)})

            # These two plugins are required for the bot to work.
            # Importing here to avoid cicular imports.
            from spanreed.plugins.plugin_manager import PluginManagerPlugin

            required_plugins = [
                await Plugin.get_plugin_by_class(TelegramBotPlugin),
                await Plugin.get_plugin_by_class(PluginManagerPlugin),
            ]

            for plugin in required_plugins:
                await plugin.register_user(user)

            bot: TelegramBotApi = await TelegramBotApi.for_user(user)

            async with bot.user_interaction():
                await bot.send_message("Howdy partner!")
                await asyncio.sleep(1)
                await bot.send_message(
                    "I'm Spanreed, your <i>personal</i> "
                    "personal assistant.\n"
                )
                await asyncio.sleep(1)
                await bot.send_message("Let's set you up in the system.\n")
                await asyncio.sleep(1)
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
                    "To get started, you can use the <code>/do</code> command,\n"
                    "It will show you a list of commands you can use.\n",
                    "Since you're new here, there won't be many commands to "
                    "choose from. The magic happens when you "
                    "<b>install plugins</b>.",
                    "You can install plugins using the same <code>/do</code> "
                    "command.",
                    "Once you've installed a plugin, you'll see the commands "
                    "it provides in the list.",
                    "Some plugins will also send you messages, like asking your "
                    "input on decisions, or sending you reminders.",
                    "Try it now - send me a <code>/do</code> command.",
                    delay=1,
                )

        asyncio.create_task(start_task())

    async def show_command_menu(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ):
        app: Application = context.application
        commands = app.bot_data.setdefault("plugin-commands", {})
        self._logger.info(f"{commands=}")
        telegram_user_id: int = update.effective_user.id
        user: User = await self.get_user_by_telegram_user_id(telegram_user_id)
        self._logger.info(f"{user.plugins=}")

        # We need to do user interaction is a separate task because we can't
        # block the main Telegram bot coroutine.
        async def show_command_menu_task():
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
    ):
        self._logger.info(f"Received message: {update.message.text=}")
        # Get the user's Telegram ID and find the corresponding user.
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
    # TODO: Replace this low-level exposed app with something safer.
    _application: Application = None
    _application_initialized = asyncio.Event()

    def __init__(self, telegram_user_id: str):
        self._logger = logging.getLogger(TelegramBotApi.__name__)
        self._telegram_user_id = telegram_user_id
        self._have_interaction_lock = False

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

    async def send_message(self, text: str, *, parse_html=True):
        app: Application = await self.get_application()
        parse_mode = None
        if parse_html:
            parse_mode = "HTML"
        await app.bot.send_message(
            chat_id=self._telegram_user_id, text=text, parse_mode=parse_mode
        )

    async def send_multiple_messages(
        self, *text: str, delay: int = 1, parse_html=True
    ):
        app: Application = await self.get_application()
        for message in text:
            await app.bot.send_chat_action(
                self._telegram_user_id, action=constants.ChatAction.TYPING
            )
            await asyncio.sleep(delay)
            await self.send_message(message, parse_html=parse_html)

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

    async def request_user_input(self, prompt):
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
    async def user_interaction(self):
        # TODO: Need to mark somehow where to return any user input.
        self._logger.info("Acquiring user interaction lock")
        async with await self.get_user_interaction_lock():
            self._logger.info("Acquired user interaction lock")
            self._have_interaction_lock = True
            yield
            self._have_interaction_lock = False
        self._logger.info("Released user interaction lock")
