import asyncio
import contextlib
import datetime
import os
from enum import auto, IntEnum
import logging
import uuid
from typing import NamedTuple, Optional, Callable, cast, Awaitable, Coroutine
from dataclasses import dataclass
from collections.abc import AsyncGenerator

from spanreed.plugin import Plugin
from spanreed.user import User

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    constants,
    Message,
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
USER_INTERACTION_QUEUES = "user-interaction-queue"
CURRENT_USER_INTERACTION = "current-user-interaction"
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
    def __init__(self) -> None:
        super().__init__()
        self.background_tasks: set[asyncio.Task] = set()

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
                self._logger.exception(
                    "Cancellation received. Stopping updater..."
                )
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
        callback_data: CallbackData = cast(CallbackData, query.data)
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

        self.create_task(start_task())

    def create_task(self, coro: Coroutine) -> None:
        task = asyncio.create_task(coro)
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)
        return

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
            from spanreed.plugins.spanreed_monitor import (
                suppress_and_log_exception,
            )

            self._logger.info(f"Inside internal task for /do for {user}")
            self._logger.info(
                f"Current commands: {app.bot_data[PLUGIN_COMMANDS]}"
            )
            async with suppress_and_log_exception(BaseException):
                bot = await TelegramBotApi.for_user(user)

                shown_commands = []
                for plugin_canonical_name, commands in app.bot_data.setdefault(
                    PLUGIN_COMMANDS, {}
                ).items():
                    if plugin_canonical_name not in user.plugins:
                        self._logger.debug(
                            f"Skipping {plugin_canonical_name=}"
                        )
                        continue

                    self._logger.info(
                        f"Adding commands for {plugin_canonical_name=}"
                    )

                    for command in commands:
                        self._logger.info(f"Adding command {command=}")
                        shown_commands.append(command)

                async with bot.user_interaction(
                    priority=UserInteractionPriority.HIGH
                ):
                    choice = await bot.request_user_choice(
                        "Please choose a command to run:",
                        [command.text for command in shown_commands]
                        + ["Cancel"],
                    )
                    if choice == len(shown_commands):
                        return
                    chosen_command = shown_commands[choice]
                    self._logger.info(
                        f"Running {chosen_command=}: {chosen_command.callback=}"
                    )
                    await chosen_command.callback(user)

        self.create_task(show_command_menu_task())

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


class UserInteractionPreempted(Exception):
    pass


class UserInteractionPriority(IntEnum):
    HIGH = auto()
    NORMAL = auto()
    LOW = auto()


class UserInteraction:
    def __init__(
        self, user_id: int, priority: UserInteractionPriority
    ) -> None:
        self.user_id = user_id
        self.priority = priority
        self.event = asyncio.Event()
        task: asyncio.Task | None = asyncio.current_task()
        if task is None:
            raise RuntimeError("No current task")
        self.task: asyncio.Task = task
        self.preempted = False
        self.running = False

    def __repr__(self) -> str:
        return self.__str__()

    def __str__(self) -> str:
        coro = self.task.get_coro()
        task_name = coro.cr_code.co_qualname if coro is not None else "unknown"
        return f"UserInteraction<{task_name} @ {self.priority.name}>"

    def allow_to_run(self) -> None:
        self.event.set()

    async def wait_to_run(self) -> None:
        await self.event.wait()
        self.running = True

    def preempt(self) -> None:
        self.preempted = True
        self.task.cancel()


UserInteractionQueues = dict[UserInteractionPriority, list[UserInteraction]]


class TelegramBotApi:
    _application: Application
    _application_initialized = asyncio.Event()

    def __init__(self, telegram_user_id: int):
        self._logger = logging.getLogger(TelegramBotApi.__name__)
        self._telegram_user_id = telegram_user_id
        self._have_interaction_lock = False
        self._preempted = False

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
        _logger.info(
            f"These plugins have registered commands: {', '.join(name for name in app.bot_data[PLUGIN_COMMANDS])}"
        )

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

    async def send_document(self, file_name: str, data: bytes) -> Message:
        app: Application = await self.get_application()
        return cast(
            Message,
            await app.bot.send_document(
                chat_id=self._telegram_user_id,
                document=data,
                filename=file_name,
            ),
        )

    async def send_message(
        self,
        text: str,
        *,
        parse_html: bool = True,
        parse_markdown: bool = False,
    ) -> Message:
        app: Application = await self.get_application()
        parse_mode = None
        if parse_html:
            parse_mode = "HTML"
        elif parse_markdown:
            parse_mode = "MarkdownV2"
        return cast(
            Message,
            await app.bot.send_message(
                chat_id=self._telegram_user_id,
                text=text,
                parse_mode=parse_mode,
            ),
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
        self, prompt: str, choices: list[str], *, columns: int = 1
    ) -> int:
        app = await self.get_application()

        # Generate a random callback ID to avoid collisions.
        callback_id, callback_event = await self.init_callback()

        def make_callback_data(position: int) -> CallbackData:
            return CallbackData(callback_id, self._telegram_user_id, position)

        # Set up the keyboard.
        keyboard: list[list[InlineKeyboardButton]] = [[]]

        for i, choice in enumerate(choices):
            button_to_append = InlineKeyboardButton(
                choice, callback_data=make_callback_data(i)
            )
            if len(keyboard[-1]) == columns:
                keyboard.append([])
            keyboard[-1].append(button_to_append)

        async def send_message() -> Message:
            return cast(
                Message,
                await app.bot.send_message(
                    chat_id=self._telegram_user_id,
                    text=prompt,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                ),
            )

        # Wait for the user to select a choice.
        interaction_result: int | str = await self.wait_for_user_interaction(
            callback_id, callback_event, send_message
        )
        if not isinstance(interaction_result, int):
            raise ValueError("Expected integer from user")
        return interaction_result

    async def request_user_input(self, prompt: str) -> str:
        app: Application = await self.get_application()

        # Generate a random callback ID to avoid collisions.
        callback_id, callback_event = await self.init_callback()

        app.bot_data.setdefault(USER_MESSAGE_CALLBACK_ID, {})[
            self._telegram_user_id
        ] = callback_id

        async def send_message() -> Message:
            return await self.send_message(prompt)

        interaction_result: int | str = await self.wait_for_user_interaction(
            callback_id, callback_event, send_message
        )
        if not isinstance(interaction_result, str):
            raise ValueError("Expected string from user")
        return interaction_result

    async def wait_for_user_interaction(
        self,
        callback_id: int,
        callback_event: asyncio.Event,
        send_message_fn: Callable[[], Awaitable[Message]],
    ) -> int | str:
        app: Application = await self.get_application()

        # Wait for the user to select a choice.
        self._logger.info(f"Waiting for callback {callback_id} to be done")
        message: Message | None = None
        pending_message: Message | None = None
        try:
            while True:
                message = await send_message_fn()
                try:
                    async with asyncio.timeout(
                        datetime.timedelta(minutes=60).total_seconds()
                    ):
                        await callback_event.wait()
                        break
                except asyncio.TimeoutError:
                    if not await message.delete():
                        raise RuntimeError("Failed to delete message")

                    queues = await self._get_user_interaction_queues()
                    pending_interactions: int = sum(
                        len(queue) for queue in queues.values()
                    )
                    if pending_message is not None:
                        await pending_message.delete()
                    pending_message = await self.send_message(
                        f"You have {pending_interactions + 1} pending interactions, this is the first one:"
                    )

        except asyncio.CancelledError:
            self._logger.info(f"Callback {callback_id} was cancelled")
            if message is not None:
                if not await message.delete():
                    self._logger.error("Failed to delete message")
            raise
        if pending_message is not None:
            await pending_message.delete()
        self._logger.info(f"Callback {callback_id} done")
        return app.bot_data[CALLBACK_EVENT_RESULTS][callback_id]  # type: ignore

    async def get_user_interaction_lock(self) -> asyncio.Lock:
        app = await self.get_application()
        return app.bot_data.setdefault(USER_INTERACTION_LOCKS, {}).setdefault(  # type: ignore
            self._telegram_user_id, asyncio.Lock()
        )

    def _get_default_priority_queue(self) -> UserInteractionQueues:
        return {prio: [] for prio in UserInteractionPriority}

    async def _get_user_interaction_queues(self) -> UserInteractionQueues:
        app = await self.get_application()
        return app.bot_data.setdefault(  # type: ignore
            USER_INTERACTION_QUEUES, {}
        ).setdefault(
            self._telegram_user_id, self._get_default_priority_queue()
        )

    async def _get_current_user_interaction(self) -> UserInteraction | None:
        app = await self.get_application()
        return app.bot_data.setdefault(  # type: ignore
            CURRENT_USER_INTERACTION, {}
        ).setdefault(self._telegram_user_id, None)

    async def _set_current_user_interaction(
        self,
        user_interaction: UserInteraction,
    ) -> None:
        app = await self.get_application()
        if (
            app.bot_data.setdefault(CURRENT_USER_INTERACTION, {}).setdefault(
                self._telegram_user_id, None
            )
        ) is not None:
            raise RuntimeError("User interaction already set")

        app.bot_data[CURRENT_USER_INTERACTION][
            self._telegram_user_id
        ] = user_interaction

    async def _clear_current_user_interaction(self) -> None:
        self._logger.info(
            f"Clearing current user interaction {await self._get_current_user_interaction()=}"
        )
        app = await self.get_application()
        app.bot_data[CURRENT_USER_INTERACTION][self._telegram_user_id] = None

    async def _try_to_allow_next_user_interaction(self) -> None:
        self._logger.info("Trying to allow next user interaction")
        await self._preempt_user_interaction_if_needed()
        if (await self._get_current_user_interaction()) is not None:
            return

        interaction_queue: UserInteractionQueues = (
            await self._get_user_interaction_queues()
        )

        for priority in UserInteractionPriority:  # high to low
            if interaction_queue[priority]:
                user_interaction: UserInteraction = interaction_queue[
                    priority
                ].pop(0)
                self._logger.info(f"Allowing {user_interaction=}")
                user_interaction.allow_to_run()
                await self._set_current_user_interaction(user_interaction)
                return

        self._logger.info("No user interaction to allow")

    async def _preempt_user_interaction_if_needed(self) -> None:
        self._logger.info("Checking if we need to preempt user interaction")
        current_interaction: UserInteraction | None = (
            await self._get_current_user_interaction()
        )
        if current_interaction is None:
            self._logger.info("No current user interaction, so not preempting")
            return

        user_interaction_queues = await self._get_user_interaction_queues()
        # TODO: correct order
        for possible_priority in UserInteractionPriority:  # high to low
            if (
                possible_priority >= current_interaction.priority
                or not user_interaction_queues[possible_priority]
            ):
                return
            self._logger.info(f"Preempting {current_interaction=}")
            if current_interaction.running:
                current_interaction.preempt()
            else:
                # If this task was preempted before starting to run, re-add it
                # to the queue instead of cancelling it.
                await self._add_to_user_interaction_queue(current_interaction)
            return

        self._logger.info(
            "There are awaiting user interactions, but the current one is higher priority"
        )

    async def _add_to_user_interaction_queue(
        self, user_interaction: UserInteraction
    ) -> None:
        user_interaction_queues = await self._get_user_interaction_queues()
        user_interaction_queues[user_interaction.priority].append(
            user_interaction
        )

    @contextlib.asynccontextmanager
    async def user_interaction(
        self,
        *,
        propagate_preemption: bool = True,
        priority: UserInteractionPriority = UserInteractionPriority.NORMAL,
    ) -> AsyncGenerator[None, None]:
        user_interaction = UserInteraction(self._telegram_user_id, priority)
        await self._add_to_user_interaction_queue(user_interaction)

        def log(msg: str) -> None:
            self._logger.info(msg + f" {user_interaction=}")

        # TODO: Need to mark somehow where to return any user input.
        log("Waiting in queue for user interaction")
        lock: asyncio.Lock = await self.get_user_interaction_lock()
        await self._try_to_allow_next_user_interaction()
        # Wait for our turn in the queue.
        await user_interaction.wait_to_run()
        log("Got user interaction permission")
        async with lock:
            try:
                yield
            except asyncio.CancelledError:
                if user_interaction.preempted:
                    log("User interaction was preempted")
                    if propagate_preemption:
                        user_interaction.task.uncancel()
                        raise UserInteractionPreempted()
                raise
            finally:
                await self._clear_current_user_interaction()
                log("Yielded user interaction")
                self._have_interaction_lock = False
                await self._try_to_allow_next_user_interaction()
