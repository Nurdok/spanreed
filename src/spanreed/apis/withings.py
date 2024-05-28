import asyncio

from spanreed.storage import redis_api
from spanreed.user import User
import logging
import contextlib
import requests
import logging
from dataclasses import dataclass


@dataclass
class UserConfig:
    userid: str
    access_token: str
    refresh_token: str
    expires_in: str
    scope: str
    csrf_token: str
    token_type: str


class AuthenticationFlow:
    CLIENT_ID = "ed9577e2d6a8eb2ddd3223c5d251d210f1a9e3b16975c8f7ab4a9f5a32f6f22b"
    REDIRECT_URI = "https://spanreed.ink/withings-oauth"

    def __init__(self, user: User):
        self._user = user
        self._done_event: asyncio.Event | None = None
        self._user_config: UserConfig | None = None
        self._logger = logging.getLogger(__name__)

    def get_url(self) -> str:
        url_args = {
            "client_id": self.CLIENT_ID,
            "redirect_uri": self.REDIRECT_URI,
            "state": str(self._user.id),
        }
        return "https://account.withings.com/oauth2_user/authorize2?response_type=code&client_id={client_id}&scope=user.info,user.metrics,user.activity&redirect_uri={redirect_uri}&state={state}".format(
            **url_args
        )

    async def get_done_event(self) -> asyncio.Event:
        if self._done_event is not None:
            raise ValueError("Done event already set.")
        self._done_event = asyncio.Event()
        return self._done_event

    @staticmethod
    async def get_client_secret() -> str:
        return await redis_api.get("withings_client_secret")

    async def authenticate(self, code: str) -> None:
        # request token
        request_token_url = "https://wbsapi.withings.net/v2/oauth2"
        data = {
            "action": "requesttoken",
            "client_id": self.CLIENT_ID,
            "client_secret": await AuthenticationFlow.get_client_secret(),
            "grant_type": "authorization_code",
            "code": code,
            "uri": self.get_url(),
        }
        response = requests.post(request_token_url, data=data)
        response.raise_for_status()
        self._logger.info(response.json())
        self._user_config = UserConfig(
            userid=response.json()["userid"],
            access_token=response.json()["access_token"],
            refresh_token=response.json()["refresh_token"],
            expires_in=response.json()["expires_in"],
            scope=response.json()["scope"],
            csrf_token=response.json()["csrf_token"],
            token_type=response.json()["token_type"],
        )
        self._done_event.set()

    def get_user_config(self) -> UserConfig:
        if self._user_config is None:
            raise ValueError("User config not set.")
        return self._user_config


class WithingsApi:
    authentication_flows: dict[int, AuthenticationFlow] = {}

    def __init__(self, user_config: UserConfig):
        self._api_token = user_config.api_token
        self._logger = logging.getLogger(__name__)

    @classmethod
    async def for_user(cls, user: User) -> "WithingsApi":
        from spanreed.plugins.withings import WithingsPlugin

        return WithingsApi(await WithingsPlugin.get_config(user))

    @classmethod
    @contextlib.asynccontextmanager
    async def start_authentication(cls, user: User) -> AuthenticationFlow:
        if user.id in cls.authentication_flows:
            raise ValueError("Authentication flow already started.")
        flow = AuthenticationFlow(user)
        cls.authentication_flows[user.id] = flow
        yield flow
        del cls.authentication_flows[user.id]

    @classmethod
    async def handle_oauth_redirect(cls, code: str, state: str) -> None:
        user_id: int = int(state)
        flow: AuthenticationFlow = cls.authentication_flows[user_id]
        await flow.authenticate(code)
