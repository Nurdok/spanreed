from spanreed.apis.telegram_bot import TelegramBotApi
from spanreed.apis.todoist import UserConfig
from spanreed.plugin import Plugin
from spanreed.user import User


class TodoistPlugin(Plugin[UserConfig]):
    @classmethod
    def name(cls) -> str:
        return "Todoist"

    @classmethod
    def has_user_config(cls) -> bool:
        return True

    @classmethod
    def get_config_class(cls) -> type[UserConfig]:
        return UserConfig

    @classmethod
    async def ask_for_user_config(cls, user: User) -> None:
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)
        api_token = await bot.request_user_input(
            "Please enter your Todoist API token. "
        )
        await cls.set_config(user, UserConfig(api_token))
