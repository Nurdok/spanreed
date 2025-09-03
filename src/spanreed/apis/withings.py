import asyncio
import datetime
from typing import AsyncGenerator

from yaml import YAMLError

from spanreed.storage import redis_api
from spanreed.user import User
import contextlib
import requests
import logging
from dataclasses import dataclass
from enum import IntEnum

from spanreed.plugins.spanreed_monitor import suppress_and_log_exception


@dataclass
class UserConfig:
    userid: str
    access_token: str
    refresh_token: str
    expires_in: str
    scope: str
    token_type: str


class AuthenticationFlow:
    CLIENT_ID = (
        "ed9577e2d6a8eb2ddd3223c5d251d210f1a9e3b16975c8f7ab4a9f5a32f6f22b"
    )
    REDIRECT_URI = "http://spanreed.ink:5000/withings-oauth"

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
        secret: str | None = await redis_api.get("withings_client_secret")
        if secret is None:
            raise RuntimeError("Client secret not set.")
        return secret

    async def authenticate(self, code: str) -> None:
        if self._done_event is None:
            raise RuntimeError("Done event not set.")

        # request token
        request_token_url = "https://wbsapi.withings.net/v2/oauth2"
        data = {
            "action": "requesttoken",
            "client_id": self.CLIENT_ID,
            "client_secret": await AuthenticationFlow.get_client_secret(),
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.REDIRECT_URI,
        }
        logging.info(f"Sending request: {data}")
        response = requests.post(request_token_url, data=data)
        response.raise_for_status()
        self._logger.info(f"Got response: {response.json()}")
        body = response.json()["body"]
        self._user_config = UserConfig(
            userid=body["userid"],
            access_token=body["access_token"],
            refresh_token=body["refresh_token"],
            expires_in=body["expires_in"],
            scope=body["scope"],
            token_type=body["token_type"],
        )
        self._done_event.set()

    def get_user_config(self) -> UserConfig:
        if self._user_config is None:
            raise ValueError("User config not set.")
        return self._user_config

    @staticmethod
    async def refresh_token(user: User, user_config: UserConfig) -> UserConfig:
        request_token_url = "https://wbsapi.withings.net/v2/oauth2"
        data = {
            "action": "requesttoken",
            "client_id": AuthenticationFlow.CLIENT_ID,
            "client_secret": await AuthenticationFlow.get_client_secret(),
            "grant_type": "refresh_token",
            "refresh_token": user_config.refresh_token,
        }
        logging.info(f"Sending request: {data}")
        response = requests.post(request_token_url, data=data)
        logging.info(f"Got response: {response.json()}")
        response.raise_for_status()
        status = response.json()["status"]
        if status != 0 and (status < 200 or status >= 300):
            raise requests.exceptions.HTTPError("Token refresh failed.")
        body = response.json()["body"]

        user_config = UserConfig(
            userid=body["userid"],
            access_token=body["access_token"],
            refresh_token=body["refresh_token"],
            expires_in=body["expires_in"],
            scope=body["scope"],
            token_type=body["token_type"],
        )

        from spanreed.plugins.withings import WithingsPlugin

        await WithingsPlugin.set_config(user, user_config)
        return user_config


class MeasurementType(IntEnum):
    WEIGHT = 1
    FAT_PERCENTAGE = 6
    FAT_MASS = 8
    HEART_PULSE = 11


class WithingsApi:
    authentication_flows: dict[int, AuthenticationFlow] = {}

    def __init__(self, user: User, user_config: UserConfig):
        self._user = user
        self._user_config = user_config
        self._logger = logging.getLogger(__name__)

    @classmethod
    async def for_user(cls, user: User) -> "WithingsApi":
        from spanreed.plugins.withings import WithingsPlugin

        return WithingsApi(user, await WithingsPlugin.get_config(user))

    @classmethod
    @contextlib.asynccontextmanager
    async def start_authentication(
        cls, user: User
    ) -> AsyncGenerator[AuthenticationFlow, None]:
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

    async def get_measurements(self) -> dict | None:
        url = "https://wbsapi.withings.net/measure"
        headers = {
            "Authorization": f"{self._user_config.token_type} {self._user_config.access_token}",
        }
        today = datetime.datetime.now().replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        measurement_types = [
            MeasurementType.WEIGHT,
            MeasurementType.FAT_PERCENTAGE,
            MeasurementType.FAT_MASS,
            MeasurementType.HEART_PULSE,
        ]
        data = {
            "action": "getmeas",
            "meastypes": ",".join([str(t) for t in measurement_types]),
            "category": 1,  # 1 = real measurement, 2 = user goal
            "lastupdate": today.timestamp(),
        }
        self._logger.info(f"Sending request: {url=} {headers=} {data=}")
        response = requests.post(url, headers=headers, data=data)
        response.raise_for_status()
        self._logger.info(f"Got response: {response.json()}")
        if response.json()["status"] == 401:
            self._logger.info("Refreshing token.")
            async with suppress_and_log_exception(
                requests.exceptions.HTTPError
            ) as did_suppress:
                self._user_config = await AuthenticationFlow.refresh_token(
                    self._user, self._user_config
                )
            if did_suppress:
                self._logger.info("Token refresh failed.")
                return None
            headers = {
                "Authorization": f"{self._user_config.token_type} {self._user_config.access_token}",
            }
            response = requests.post(url, headers=headers, data=data)
        self._logger.info(f"Got response: {response.json()}")

        # TODO: handle multiple measurements
        try:
            result = self.extract_measurements_from_json(response.json())
        except YAMLError:
            self._logger.exception(
                f"Failed to parse response: {response.text}"
            )
            return None

        if not result.keys():
            return None
        return result

    def extract_measurements_from_json(
        self, data: dict
    ) -> dict[MeasurementType, float]:
        result: dict[MeasurementType, float] = {}
        for measure_group in data["body"]["measuregrps"]:
            for measure in measure_group["measures"]:
                result[MeasurementType(int(measure["type"]))] = int(
                    measure["value"]
                ) * 10 ** int(measure["unit"])
        return result
