import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import audible

from spanreed.plugin import Plugin
from spanreed.user import User
from spanreed.apis.telegram_bot import TelegramBotApi


MARKETPLACE_LOCALE = "us"


@dataclass
class UserConfig:
    # The `Authenticator.to_dict()` blob (tokens, device info, locale).
    auth: dict


@dataclass
class AudibleBook:
    asin: str
    title: str
    subtitle: str | None
    percent_complete: float
    is_finished: bool


class AudiblePlugin(Plugin):
    @classmethod
    def name(cls) -> str:
        return "Audible"

    @classmethod
    def has_user_config(cls) -> bool:
        return True

    @classmethod
    def get_config_class(cls) -> type[UserConfig]:
        return UserConfig

    @classmethod
    async def ask_for_user_config(cls, user: User) -> None:
        authenticator = await AudibleApi.authenticate(user)
        await cls.set_config(user, UserConfig(auth=authenticator.to_dict()))


class AudibleApi:
    def __init__(self, authenticator: audible.Authenticator) -> None:
        self._auth = authenticator
        self._logger = logging.getLogger(__name__)

    @classmethod
    async def for_user(cls, user: User) -> "AudibleApi":
        user_config: UserConfig = await AudiblePlugin.get_config(user)
        # `from_dict` mutates the dict it's given, so pass a copy to keep the
        # stored config intact.
        authenticator = audible.Authenticator.from_dict(dict(user_config.auth))
        if authenticator.access_token_expired:
            # The refresh does blocking I/O (httpx sync client).
            await asyncio.to_thread(authenticator.refresh_access_token)
            await AudiblePlugin.set_config(
                user, UserConfig(auth=authenticator.to_dict())
            )
        return cls(authenticator)

    @classmethod
    async def authenticate(cls, user: User) -> audible.Authenticator:
        """Register a new Audible "device" for this user.

        Amazon login happens in the user's own browser: we send them the
        login URL over Telegram, and they paste back the URL of the page
        they land on after logging in (an error page whose URL contains
        the authorization code).
        """
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)
        loop = asyncio.get_running_loop()

        def login_url_callback(login_url: str) -> str:
            # Called from the worker thread `from_login_external` runs in;
            # bridge back into the event loop for the Telegram interaction.
            return asyncio.run_coroutine_threadsafe(
                cls._request_redirect_url_from_user(bot, login_url), loop
            ).result()

        return await asyncio.to_thread(
            lambda: audible.Authenticator.from_login_external(
                locale=MARKETPLACE_LOCALE,
                login_url_callback=login_url_callback,
            )
        )

    @staticmethod
    async def _request_redirect_url_from_user(
        bot: TelegramBotApi, login_url: str
    ) -> str:
        await bot.send_message(
            f'<b><a href="{login_url}">Click here to log in to Amazon</a></b>'
            "\nAfter logging in, you'll land on an error page — that's"
            " expected.",
        )
        return await bot.request_user_input(
            "Paste the full URL of the page you landed on"
            " (it contains the authorization code):"
        )

    async def get_library(self) -> list[AudibleBook]:
        params: dict[str, Any] = {
            "num_results": 1000,
            "response_groups": (
                "product_desc,product_attrs,percent_complete,is_finished"
            ),
        }
        async with audible.AsyncClient(auth=self._auth) as client:
            response = await client.get("1.0/library", **params)

        books: list[AudibleBook] = []
        for item in response["items"]:
            asin: str | None = item.get("asin")
            title: str | None = item.get("title")
            if not asin or not title:
                continue
            books.append(
                AudibleBook(
                    asin=asin,
                    title=title,
                    subtitle=item.get("subtitle"),
                    percent_complete=float(item.get("percent_complete") or 0),
                    is_finished=bool(item.get("is_finished")),
                )
            )
        self._logger.info(f"Fetched {len(books)} books from Audible library")
        return books
