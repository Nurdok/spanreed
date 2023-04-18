import abc
from spanreed.user import User


class Registrable(abc.ABC):
    @property
    @abc.abstractmethod
    def name(self) -> str:
        pass

    @property
    def canonical_name(self) -> str:
        return self.name.replace(" ", "-").lower()

    @abc.abstractmethod
    def has_user_config(self) -> bool:
        pass

    async def ask_for_user_config(self, user: User) -> None:
        if self.has_user_config():
            raise NotImplementedError(
                "Plugin has user config, but no implementation for "
                "asking for user config."
            )

    @abc.abstractmethod
    async def register_user(self, user: User):
        pass

    @abc.abstractmethod
    async def unregister_user(self, user: User):
        pass
