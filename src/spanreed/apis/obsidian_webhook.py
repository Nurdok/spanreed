import logging

import aiohttp

from spanreed.plugin import Plugin
from dataclasses import dataclass
from spanreed.user import User
from spanreed.apis.telegram_bot import TelegramBotApi


@dataclass
class UserConfig:
    webhook_url: str


class ObsidianWebhookPlugin(Plugin):
    @classmethod
    def name(cls) -> str:
        return "Obsidian Webhook"

    @classmethod
    def has_user_config(cls) -> bool:
        return True

    @classmethod
    def get_config_class(cls) -> type[UserConfig]:
        return UserConfig

    @classmethod
    async def ask_for_user_config(cls, user: User) -> None:
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)
        async with bot.user_interaction():
            webhook_url = await bot.request_user_input(
                "Please enter your Obsidian webhook URL:"
            )
        await cls.set_config(user, UserConfig(webhook_url))


class ObsidianWebhookApi:
    def __init__(self, user_config: UserConfig):
        self._webhook_url = user_config.webhook_url
        self._logger = logging.getLogger(__name__)

    @classmethod
    async def for_user(cls, user: User) -> "ObsidianWebhookApi":
        return ObsidianWebhookApi(await ObsidianWebhookPlugin.get_config(user))

    async def append_to_note(self, note_path: str, content: str) -> None:
        # Use aiohttp to POST the content to the webhook
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self._webhook_url,
                params={
                    "file": note_path,
                },
                data=content,
            ) as response:
                if response.status != 200:
                    self._logger.error(
                        f"Failed to append to note {note_path}: "
                        f"{response.status=}"
                    )
