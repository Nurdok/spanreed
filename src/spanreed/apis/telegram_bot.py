import asyncio
import contextlib
import datetime
import json
import os
import logging
import uuid
from typing import NamedTuple, Optional, Callable, cast, Awaitable, Coroutine
from dataclasses import dataclass

from spanreed.plugin import Plugin
from spanreed.storage import redis_api
from spanreed.apis.telegram_dispatcher import OutboundDispatcher
from spanreed.interaction import engine

# Backward-compatible re-exports: the interaction machinery moved to
# spanreed.interaction; import it from there (spanreed.interaction) in new
# code.
from spanreed.interaction.engine import (  # noqa: F401
    UserInteraction,
    UserInteractionPriority,
)
from spanreed.interaction.errors import UserInteractionPreempted  # noqa: F401
from spanreed.user import User

from telegram import (
    Update,
    InlineKeyboardMarkup,
    constants,
    Message,
)
from telegram.ext import (
    ApplicationBuilder,
    AIORateLimiter,
    ContextTypes,
    CommandHandler,
    InvalidCallbackData,
    MessageHandler,
    CallbackQueryHandler,
    Application,
    filters,
)

OUTBOUND_DISPATCHERS = "outbound-dispatchers"

# Durable outbound notification queue. `notify()` enqueues here; the per-user
# OutboundDispatcher (see telegram_dispatcher.py) drains it as its
# low-priority lane, so notifications survive restarts and Telegram
# flood-control bans instead of being dropped.
OUTBOUND_QUEUE_MAX = 200  # keep newest N; drop older on overflow


class CallbackData(NamedTuple):
    # The engine's prompt interaction id (uuid hex).
    callback_id: str
    user_id: int
    position: int


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

            # Start the per-user send dispatcher for each user so queued
            # notifications (see TelegramBotApi.notify) get delivered.
            await self._start_dispatchers()

            try:
                # Wait for cancellation so we can perform the cleanup.
                self._logger.info("Waiting for cancellation...")
                while True:
                    await asyncio.sleep(datetime.timedelta(hours=1).total_seconds())
            except asyncio.CancelledError:
                self._logger.exception("Cancellation received. Stopping updater...")
                await application.updater.stop()
                self._logger.info("Stopping application...")
                await application.stop()
                self._logger.info("Stopped")
                raise

    async def _start_dispatchers(self) -> None:
        for user in await self.get_users():
            bot = await TelegramBotApi.for_user(user)
            await bot._get_dispatcher()
            self._logger.info(f"Started outbound dispatcher for user {user.id}")

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
            # Overall-rate safety net only (~30/s across all chats) for the
            # rare call that doesn't go through the OutboundDispatcher.
            # max_retries MUST stay 0: sleeping exactly `retry_after` inside
            # the limiter synchronizes every blocked send to wake at the ban's
            # expiry instant, re-tripping a fresh ban (the 23:01 cycle). The
            # dispatcher owns RetryAfter handling and resumes with jitter.
            .rate_limiter(AIORateLimiter(max_retries=0))
            .build()
        )

        application.add_handler(CallbackQueryHandler(self.handle_callback_query))
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
        if isinstance(query.data, InvalidCallbackData):
            # A button from before the last restart: the arbitrary-callback-
            # data cache is in-memory, so stale keyboards can't be resolved.
            self._logger.info("Ignoring callback from a stale keyboard")
            with contextlib.suppress(Exception):
                await query.answer(
                    "This button has expired — please re-run the command.",
                    show_alert=True,
                )
            return

        callback_data: CallbackData = cast(CallbackData, query.data)

        self._logger.info(
            f"Resolving prompt {callback_data.callback_id} with"
            f" {callback_data.position}"
        )
        resolution = engine.resolve_prompt(
            callback_data.callback_id, callback_data.position
        )
        if resolution == engine.PromptResolution.ALREADY_ANSWERED:
            with contextlib.suppress(Exception):
                await query.answer("This prompt was already answered.")
        elif resolution == engine.PromptResolution.UNKNOWN:
            # A prompt from before the last restart.
            with contextlib.suppress(Exception):
                await query.answer(
                    "This prompt has expired — please re-run the command.",
                    show_alert=True,
                )
            return

        # Route the delete through the user's send dispatcher; failure just
        # leaves the keyboard message behind, so don't let it kill the
        # handler.
        try:
            bot = TelegramBotApi(callback_data.user_id)
            await bot._dispatch("delete_choice_message", query.delete_message)
        except Exception:
            self._logger.exception("Failed to delete choice message")

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

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        from spanreed.interaction.api import UserInteractionApi

        if update.effective_user is None:
            self._logger.error("No user found in update")
            return

        telegram_user_id: int = update.effective_user.id
        self._logger.info(f"Got a /start command from user {telegram_user_id=}")
        existing_user: Optional[User] = None
        with contextlib.suppress(KeyError):
            existing_user = await self.get_user_by_telegram_user_id(
                telegram_user_id,
                send_message_on_failure=False,
            )
        if existing_user is not None:
            ui = await UserInteractionApi.for_user(existing_user)
            async with ui.user_interaction():
                await ui.send_multiple_messages(
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

            ui = await UserInteractionApi.for_user(user)

            async with ui.user_interaction():
                await ui.send_multiple_messages(
                    "Howdy partner!",
                    "I'm Spanreed, your <i>personal</i> personal assistant.",
                    "Let's set you up in the system.",
                )
                name = await ui.request_user_input("How do you want me to address you?")
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
                    await ui.send_message(
                        f"A bit degrading, but okay, <i>{name}</i>.\n"
                    )
                else:
                    await ui.send_message(f"Cool. Cool cool cool.")
                await asyncio.sleep(1)
                await ui.send_multiple_messages(
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
        from spanreed.interaction import commands as command_registry
        from spanreed.interaction.api import UserInteractionApi

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
            async with suppress_and_log_exception(BaseException):
                ui = await UserInteractionApi.for_user(user)

                shown_commands = command_registry.commands_for_user(user)
                self._logger.info(f"Showing commands: {shown_commands}")

                async with ui.user_interaction(priority=UserInteractionPriority.HIGH):
                    choice = await ui.request_user_choice(
                        "Please choose a command to run:",
                        [command.text for command in shown_commands] + ["Cancel"],
                    )
                    if choice == len(shown_commands):
                        return
                    chosen_command = shown_commands[choice]
                    self._logger.info(
                        f"Running {chosen_command=}: {chosen_command.callback=}"
                    )
                    await chosen_command.callback(user)

        # One user-initiated flow at a time, newest wins — shared with the
        # (phase 3+) palette invocation path.
        engine.start_user_flow(user.id, show_command_menu_task())

    async def handle_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if update.message is None:
            self._logger.error("No message found in update")
            return

        self._logger.info(f"Received message: {update.message.text=}")
        message_text = update.message.text
        if message_text is None:
            # Non-text message (sticker, photo, ...): nothing to route.
            self._logger.info("Ignoring non-text message")
            return
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
        pending = engine.get_current_input_prompt(user.id)
        if pending is None:
            self._logger.info("No pending input prompt found")
            await bot.send_message("Unexpected message, ignoring...")
            return

        self._logger.info(f"Found pending input prompt {pending.interaction_id}")
        engine.resolve_prompt(pending.interaction_id, message_text)
        return


class TelegramBotApi:
    """Telegram delivery layer: application lifecycle + paced sends.

    The user-interaction machinery (queues, priorities, prompts) lives in
    ``spanreed.interaction``; plugins use ``UserInteractionApi`` and never
    this class directly.
    """

    _application: Application
    _application_initialized = asyncio.Event()

    def __init__(self, telegram_user_id: int):
        self._logger = logging.getLogger(TelegramBotApi.__name__)
        self._telegram_user_id = telegram_user_id

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
        return TelegramBotApi((await TelegramBotPlugin.get_config(user)).user_id)

    @classmethod
    def set_application(cls, application: Application) -> None:
        if cls._application is not None:
            raise RuntimeError("Application already set")
        cls._application = application
        cls._application_initialized.set()

    async def _get_dispatcher(self) -> OutboundDispatcher:
        """Get (lazily creating and starting) this chat's send dispatcher.

        Stored in ``bot_data`` so the many short-lived TelegramBotApi
        instances for a user share one dispatcher, and created on first use
        so users registered after startup get one too.
        """
        app = await self.get_application()
        dispatchers: dict[int, tuple[OutboundDispatcher, asyncio.Task]] = (
            app.bot_data.setdefault(OUTBOUND_DISPATCHERS, {})
        )
        entry = dispatchers.get(self._telegram_user_id)
        if entry is None:
            dispatcher = OutboundDispatcher(
                chat_id=self._telegram_user_id,
                redis=redis_api,
                send_text=self._send_text_raw,
            )
            # The task reference is kept alive by the bot_data entry.
            task = asyncio.create_task(dispatcher.run())
            entry = (dispatcher, task)
            dispatchers[self._telegram_user_id] = entry
        return entry[0]

    async def _dispatch(
        self, description: str, api_call: Callable[[], Awaitable]
    ) -> object:
        """Run a Telegram API call through this chat's send dispatcher."""
        dispatcher = await self._get_dispatcher()
        return await dispatcher.enqueue(description, api_call)

    async def send_document(self, file_name: str, data: bytes) -> Message:
        app: Application = await self.get_application()

        async def api_call() -> Message:
            return cast(
                Message,
                await app.bot.send_document(
                    chat_id=self._telegram_user_id,
                    document=data,
                    filename=file_name,
                ),
            )

        return cast(
            Message, await self._dispatch(f"send_document {file_name}", api_call)
        )

    async def _send_text_raw(
        self,
        text: str,
        parse_html: bool = True,
        parse_markdown: bool = False,
    ) -> Message:
        """The actual sendMessage API call. Only the dispatcher's own lanes
        may call this directly; everything else goes through send_message."""
        app: Application = await self.get_application()
        parse_mode = None
        if parse_html:
            parse_mode = "HTML"
        elif parse_markdown:
            parse_mode = "MarkdownV2"
        async with asyncio.timeout(30):
            return cast(
                Message,
                await app.bot.send_message(
                    chat_id=self._telegram_user_id,
                    text=text,
                    parse_mode=parse_mode,
                ),
            )

    async def send_message(
        self,
        text: str,
        *,
        parse_html: bool = True,
        parse_markdown: bool = False,
    ) -> Message:
        return cast(
            Message,
            await self._dispatch(
                "send_message",
                lambda: self._send_text_raw(text, parse_html, parse_markdown),
            ),
        )

    async def send_prompt_with_keyboard(
        self, text: str, reply_markup: InlineKeyboardMarkup
    ) -> Message:
        """Send a choice prompt with an inline keyboard (used by the
        Telegram interaction surface)."""
        app: Application = await self.get_application()

        async def api_call() -> Message:
            return cast(
                Message,
                await app.bot.send_message(
                    chat_id=self._telegram_user_id,
                    text=text,
                    reply_markup=reply_markup,
                ),
            )

        return cast(Message, await self._dispatch("send_choice_prompt", api_call))

    async def delete_message(self, message: Message) -> bool:
        """Delete a message via the dispatcher (never call message.delete()
        directly — Message-object methods bypass send pacing)."""
        return cast(bool, await self._dispatch("delete_message", message.delete))

    async def edit_message_text(self, message: Message, text: str) -> None:
        await self._dispatch("edit_message_text", lambda: message.edit_text(text))

    def _outbound_key(self) -> str:
        return f"outbound-messages:{self._telegram_user_id}"

    def _outbound_inflight_key(self) -> str:
        return f"outbound-messages:inflight:{self._telegram_user_id}"

    async def notify(
        self,
        text: str,
        *,
        parse_html: bool = True,
        parse_markdown: bool = False,
    ) -> None:
        """Fire-and-forget, durable notification.

        Enqueues `text` to a per-user Redis queue that a background consumer
        (see `deliver_outbound_messages`) drains in FIFO order. Delivery
        survives process restarts and Telegram flood-control bans, so a
        rate-limited or failed send never aborts the work that produced it
        nor crashes the calling loop.

        Use this for fire-and-forget confirmations and notices (e.g.
        "Logged X", "Spanreed is starting up"). For interactive messages that
        must arrive immediately or surface their failure (prompts, the auth
        link), use `send_message`.
        """
        entry = json.dumps(
            {
                "id": uuid.uuid4().hex,
                "text": text,
                "parse_html": parse_html,
                "parse_markdown": parse_markdown,
                "enqueued_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
        )
        key = self._outbound_key()
        await redis_api.lpush(key, entry)
        # Bound the backlog so a long outage can't grow it without limit.
        # New entries are pushed on the left, so keeping [0, MAX) retains the
        # newest and drops the oldest undelivered ones.
        await redis_api.ltrim(key, 0, OUTBOUND_QUEUE_MAX - 1)

    async def send_multiple_messages(
        self, *text: str, delay: int = 1, parse_html: bool = True
    ) -> None:
        app: Application = await self.get_application()

        async def typing_action() -> None:
            await app.bot.send_chat_action(
                self._telegram_user_id, action=constants.ChatAction.TYPING
            )

        for message in text:
            await self._dispatch("send_chat_action", typing_action)
            await asyncio.sleep(delay)
            await self.send_message(message, parse_html=parse_html)
