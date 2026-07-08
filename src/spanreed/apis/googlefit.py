import asyncio
import datetime
import json
import logging
from typing import Any, Dict, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from spanreed.user import User
from spanreed.storage import redis_api


# The merged, estimated step count that matches what the Google Fit app shows.
# Aggregating by this derived source (rather than the raw delta type) de-dupes
# overlapping step sources so we don't double-count.
ESTIMATED_STEPS_SOURCE = (
    "derived:com.google.step_count.delta:" "com.google.android.gms:estimated_steps"
)
STEP_COUNT_DATA_TYPE = "com.google.step_count.delta"

REDIRECT_URI = "https://spanreed.ink/googlefit-oauth"


class GoogleFitAuthenticationFlow:
    def __init__(self, user: User) -> None:
        self.user = user
        self._done_event: Optional[asyncio.Event] = None
        self._logger = logging.getLogger("spanreed.apis.googlefit").getChild(
            f"auth.{user.id}"
        )

    async def get_done_event(self) -> asyncio.Event:
        if self._done_event is not None:
            raise ValueError("Done event already set.")
        self._done_event = asyncio.Event()
        return self._done_event

    async def get_auth_url(self) -> str:
        fit = await GoogleFitApi.for_user(self.user)
        return await fit.authenticate()

    async def complete_authentication(self, code: str) -> None:
        if self._done_event is None:
            raise RuntimeError("Done event not set.")

        fit = await GoogleFitApi.for_user(self.user)
        await fit.complete_authentication(code)
        self._done_event.set()


class GoogleFitApi:
    # Read-only access to activity data (which includes step counts).
    SCOPES = ["https://www.googleapis.com/auth/fitness.activity.read"]
    _instances: Dict[int, "GoogleFitApi"] = {}
    _authentication_flows: Dict[int, GoogleFitAuthenticationFlow] = {}

    def __init__(self, user: User) -> None:
        self.user = user
        self._logger = logging.getLogger("spanreed.apis.googlefit").getChild(
            str(user.id)
        )
        self._service: Any | None = None

    @classmethod
    async def for_user(cls, user: User) -> "GoogleFitApi":
        if user.id not in cls._instances:
            cls._instances[user.id] = cls(user)
        return cls._instances[user.id]

    @classmethod
    async def start_authentication(cls, user: User) -> GoogleFitAuthenticationFlow:
        if user.id in cls._authentication_flows:
            raise ValueError("Authentication flow already started.")
        flow = GoogleFitAuthenticationFlow(user)
        cls._authentication_flows[user.id] = flow
        return flow

    @classmethod
    async def handle_oauth_redirect(cls, code: str, state: str) -> None:
        user_id = int(state)
        if user_id not in cls._authentication_flows:
            raise ValueError(f"No authentication flow found for user {user_id}")

        flow = cls._authentication_flows[user_id]
        await flow.complete_authentication(code)
        del cls._authentication_flows[user_id]

    # --- Per-user credential storage (mirrors GmailApi) ---

    def _get_credentials_key(self) -> str:
        return f"googlefit:credentials:user_id={self.user.id}"

    async def _get_stored_credentials(self) -> Optional[Credentials]:
        creds_data = await redis_api.get(self._get_credentials_key())
        if creds_data:
            creds_dict = json.loads(creds_data)
            return Credentials.from_authorized_user_info(creds_dict, self.SCOPES)
        return None

    async def _store_credentials(self, creds: Credentials) -> None:
        creds_data = {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": creds.scopes,
        }
        await redis_api.set(self._get_credentials_key(), json.dumps(creds_data))

    async def _get_credentials(self) -> Optional[Credentials]:
        creds = await self._get_stored_credentials()

        if creds and creds.valid:
            return creds

        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                await self._store_credentials(creds)
                return creds
            except Exception as e:
                self._logger.error(f"Failed to refresh credentials: {e}")

        return None

    # --- Global OAuth client config (mirrors GmailApi) ---

    @staticmethod
    def _get_global_credentials_config_key() -> str:
        return "googlefit_credentials"

    @staticmethod
    async def _get_stored_credentials_config() -> Optional[Dict[str, Any]]:
        config_data = await redis_api.get(
            GoogleFitApi._get_global_credentials_config_key()
        )
        if config_data:
            return json.loads(config_data)
        return None

    @staticmethod
    async def _store_credentials_config(credentials_json: str) -> None:
        await redis_api.set(
            GoogleFitApi._get_global_credentials_config_key(),
            credentials_json,
        )

    @staticmethod
    async def is_app_configured() -> bool:
        """Whether the global Google Fit OAuth client is configured."""
        config = await GoogleFitApi._get_stored_credentials_config()
        return config is not None

    async def authenticate(self, redirect_uri: str = REDIRECT_URI) -> str:
        """Start the OAuth2 flow for this user using global app credentials."""
        credentials_config = await self._get_stored_credentials_config()
        if not credentials_config:
            raise ValueError(
                "No global Google Fit credentials configured. Configure app "
                "credentials first."
            )

        flow = Flow.from_client_config(
            credentials_config, scopes=self.SCOPES, redirect_uri=redirect_uri
        )

        # `access_type=offline` + `prompt=consent` ensures we get a refresh
        # token so the hourly poll keeps working without re-auth.
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            prompt="consent",
            state=str(self.user.id),
        )
        return auth_url

    async def complete_authentication(
        self,
        authorization_code: str,
        redirect_uri: str = REDIRECT_URI,
    ) -> None:
        """Complete the OAuth2 flow and store the user's access tokens."""
        credentials_config = await self._get_stored_credentials_config()
        if not credentials_config:
            raise ValueError(
                "No global Google Fit credentials configured. Configure app "
                "credentials first."
            )

        flow = Flow.from_client_config(
            credentials_config, scopes=self.SCOPES, redirect_uri=redirect_uri
        )

        flow.fetch_token(code=authorization_code)
        await self._store_credentials(flow.credentials)

    async def is_authenticated(self) -> bool:
        creds = await self._get_credentials()
        return creds is not None and creds.valid

    async def _get_service(self) -> Any:
        if self._service is None:
            creds = await self._get_credentials()
            if not creds:
                raise ValueError("Not authenticated with Google Fit")

            # `build` does blocking I/O (fetches the discovery doc), so run it
            # in a thread to avoid stalling the event loop.
            loop = asyncio.get_event_loop()
            self._service = await loop.run_in_executor(
                None, lambda: build("fitness", "v1", credentials=creds)
            )
        return self._service

    async def get_daily_steps(
        self,
        start_date: datetime.date | None = None,
        end_date: datetime.date | None = None,
    ) -> dict[datetime.date, int]:
        """Return the total step count per day, keyed by local date.

        Defaults to today only. Both bounds are inclusive. Days with no step
        data are omitted from the result.
        """
        if start_date is None:
            start_date = datetime.date.today()
        if end_date is None:
            end_date = datetime.date.today()

        # Bucket on local-day boundaries: start at local midnight of the first
        # day and advance one day past the last, so each 24h bucket lines up
        # with a calendar day in the user's local timezone.
        start_dt = datetime.datetime.combine(start_date, datetime.time.min)
        end_dt = datetime.datetime.combine(
            end_date + datetime.timedelta(days=1), datetime.time.min
        )
        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000)

        body = {
            "aggregateBy": [
                {
                    "dataTypeName": STEP_COUNT_DATA_TYPE,
                    "dataSourceId": ESTIMATED_STEPS_SOURCE,
                }
            ],
            "bucketByTime": {"durationMillis": 86_400_000},
            "startTimeMillis": start_ms,
            "endTimeMillis": end_ms,
        }

        service = await self._get_service()
        loop = asyncio.get_event_loop()
        self._logger.info(f"Requesting aggregate steps: {body=}")
        response = await loop.run_in_executor(
            None,
            lambda: service.users()
            .dataset()
            .aggregate(userId="me", body=body)
            .execute(),
        )
        self._logger.info(f"Got aggregate response: {response=}")
        return self.extract_steps_from_aggregate(response)

    @staticmethod
    def extract_steps_from_aggregate(
        data: dict,
    ) -> dict[datetime.date, int]:
        """Parse a Fitness ``dataset:aggregate`` response into steps-per-day.

        Each bucket corresponds to one 24h window; its ``startTimeMillis``
        identifies the day. Points inside a bucket are summed. Buckets with no
        data points are skipped so we never write a spurious ``0``.
        """
        result: dict[datetime.date, int] = {}
        for bucket in data.get("bucket", []):
            total = 0
            has_point = False
            for dataset in bucket.get("dataset", []):
                for point in dataset.get("point", []):
                    for value in point.get("value", []):
                        int_val = value.get("intVal")
                        if int_val is not None:
                            total += int_val
                            has_point = True
            if not has_point:
                continue
            start_ms = int(bucket["startTimeMillis"])
            bucket_date = datetime.datetime.fromtimestamp(start_ms / 1000).date()
            result[bucket_date] = result.get(bucket_date, 0) + total
        return result
