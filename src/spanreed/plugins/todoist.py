from spanreed.interaction import UserInteractionApi
from spanreed.apis.todoist import UserConfig
from spanreed.apis.todoist_blocks import todoist_blocks
from spanreed.plugin import Plugin
from spanreed.user import User
from spanreed.workflows.blocks import Block


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

    def blocks(self) -> list[Block]:
        """Return the Todoist blocks for workflows."""
        return todoist_blocks()

    @classmethod
    async def ask_for_user_config(cls, user: User) -> None:
        bot: UserInteractionApi = await UserInteractionApi.for_user(user)
        api_token = await bot.request_user_input(
            "Please enter your Todoist API token. "
        )
        await cls.set_config(user, UserConfig(api_token))
