import asyncio
import base64
import datetime
import email
import json
import logging
import os
from typing import Optional, Dict, Any, List
from dataclasses import dataclass

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from spanreed.user import User
from spanreed.storage import redis_api


@dataclass
class EmailMessage:
    id: str
    thread_id: str
    sender: str
    subject: str
    body: str
    snippet: str
    date: datetime.datetime
    labels: List[str]
    has_attachments: bool


class GmailAuthenticationFlow:
    def __init__(self, user: User) -> None:
        self.user = user
        self._done_event: Optional[asyncio.Event] = None
        self._logger = logging.getLogger("spanreed.apis.gmail").getChild(f"auth.{user.id}")

    async def get_done_event(self) -> asyncio.Event:
        if self._done_event is not None:
            raise ValueError("Done event already set.")
        self._done_event = asyncio.Event()
        return self._done_event

    async def get_auth_url(self) -> str:
        gmail = await GmailApi.for_user(self.user)
        return await gmail.authenticate()

    async def complete_authentication(self, code: str) -> None:
        if self._done_event is None:
            raise RuntimeError("Done event not set.")

        gmail = await GmailApi.for_user(self.user)
        await gmail.complete_authentication(code)
        self._done_event.set()


class GmailApi:
    SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']
    _instances: Dict[int, 'GmailApi'] = {}
    _authentication_flows: Dict[int, GmailAuthenticationFlow] = {}

    def __init__(self, user: User) -> None:
        self.user = user
        self._logger = logging.getLogger("spanreed.apis.gmail").getChild(str(user.id))
        self._service = None

    @classmethod
    async def for_user(cls, user: User) -> 'GmailApi':
        if user.id not in cls._instances:
            cls._instances[user.id] = cls(user)
        return cls._instances[user.id]

    @classmethod
    async def start_authentication(cls, user: User) -> GmailAuthenticationFlow:
        if user.id in cls._authentication_flows:
            raise ValueError("Authentication flow already started.")
        flow = GmailAuthenticationFlow(user)
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

    def _get_credentials_key(self) -> str:
        return f"gmail:credentials:user_id={self.user.id}"

    async def _get_stored_credentials(self) -> Optional[Credentials]:
        creds_data = await redis_api.get(self._get_credentials_key())
        if creds_data:
            creds_dict = json.loads(creds_data)
            return Credentials.from_authorized_user_info(creds_dict, self.SCOPES)
        return None

    async def _store_credentials(self, creds: Credentials) -> None:
        creds_data = {
            'token': creds.token,
            'refresh_token': creds.refresh_token,
            'token_uri': creds.token_uri,
            'client_id': creds.client_id,
            'client_secret': creds.client_secret,
            'scopes': creds.scopes
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

    @staticmethod
    def _get_global_credentials_config_key() -> str:
        return "gmail_credentials"

    @staticmethod
    async def _get_stored_credentials_config() -> Optional[Dict[str, Any]]:
        config_data = await redis_api.get(GmailApi._get_global_credentials_config_key())
        if config_data:
            return json.loads(config_data)
        return None

    @staticmethod
    async def _store_credentials_config(credentials_json: str) -> None:
        await redis_api.set(GmailApi._get_global_credentials_config_key(), credentials_json)

    @staticmethod
    async def is_app_configured() -> bool:
        """Check if Gmail client credentials are configured globally."""
        config = await GmailApi._get_stored_credentials_config()
        return config is not None

    async def authenticate(self, redirect_uri: str = "https://spanreed.ink:5000/gmail-oauth") -> str:
        """Start OAuth2 flow for this user using global app credentials."""
        credentials_config = await self._get_stored_credentials_config()
        if not credentials_config:
            raise ValueError("No global Gmail credentials configured. Configure app credentials first.")

        flow = Flow.from_client_config(
            credentials_config,
            scopes=self.SCOPES,
            redirect_uri=redirect_uri
        )

        auth_url, _ = flow.authorization_url(prompt='consent', state=str(self.user.id))
        return auth_url

    async def complete_authentication(self, authorization_code: str, redirect_uri: str = "https://spanreed.ink:5000/gmail-oauth") -> None:
        """Complete OAuth2 flow and store user's access tokens."""
        credentials_config = await self._get_stored_credentials_config()
        if not credentials_config:
            raise ValueError("No global Gmail credentials configured. Configure app credentials first.")

        flow = Flow.from_client_config(
            credentials_config,
            scopes=self.SCOPES,
            redirect_uri=redirect_uri
        )

        flow.fetch_token(code=authorization_code)
        await self._store_credentials(flow.credentials)


    async def is_authenticated(self) -> bool:
        creds = await self._get_credentials()
        return creds is not None and creds.valid

    async def _get_service(self):
        if self._service is None:
            creds = await self._get_credentials()
            if not creds:
                raise ValueError("Not authenticated with Gmail")

            # Run in thread pool to avoid blocking
            loop = asyncio.get_event_loop()
            self._service = await loop.run_in_executor(
                None, lambda: build('gmail', 'v1', credentials=creds)
            )
        return self._service

    def _parse_email_body(self, payload: Dict[str, Any]) -> str:
        body = ""

        if 'parts' in payload:
            for part in payload['parts']:
                if part['mimeType'] == 'text/plain':
                    data = part['body']['data']
                    body = base64.urlsafe_b64decode(data).decode('utf-8')
                    break
        elif payload['mimeType'] == 'text/plain' and 'data' in payload['body']:
            data = payload['body']['data']
            body = base64.urlsafe_b64decode(data).decode('utf-8')

        return body

    def _has_attachments(self, payload: Dict[str, Any]) -> bool:
        if 'parts' in payload:
            for part in payload['parts']:
                if 'filename' in part and part['filename']:
                    return True
        return False

    async def get_recent_messages(self, query: str = "", max_results: int = 100) -> List[EmailMessage]:
        service = await self._get_service()

        try:
            # Search for messages
            loop = asyncio.get_event_loop()
            results = await loop.run_in_executor(
                None,
                lambda: service.users().messages().list(
                    userId='me',
                    q=query,
                    maxResults=max_results
                ).execute()
            )

            messages = results.get('messages', [])
            email_messages = []

            # Fetch details for each message
            for message in messages:
                msg_detail = await loop.run_in_executor(
                    None,
                    lambda msg_id=message['id']: service.users().messages().get(
                        userId='me',
                        id=msg_id
                    ).execute()
                )

                payload = msg_detail['payload']
                headers = payload.get('headers', [])

                # Extract headers
                sender = next((h['value'] for h in headers if h['name'] == 'From'), '')
                subject = next((h['value'] for h in headers if h['name'] == 'Subject'), '')
                date_str = next((h['value'] for h in headers if h['name'] == 'Date'), '')

                # Parse date
                try:
                    date = email.utils.parsedate_to_datetime(date_str)
                except:
                    date = datetime.datetime.now()

                # Extract body
                body = self._parse_email_body(payload)

                # Check for attachments
                has_attachments = self._has_attachments(payload)

                email_message = EmailMessage(
                    id=message['id'],
                    thread_id=message['threadId'],
                    sender=sender,
                    subject=subject,
                    body=body,
                    snippet=msg_detail.get('snippet', ''),
                    date=date,
                    labels=msg_detail.get('labelIds', []),
                    has_attachments=has_attachments
                )

                email_messages.append(email_message)

            return email_messages

        except HttpError as error:
            self._logger.error(f"Gmail API error: {error}")
            raise
        except Exception as error:
            self._logger.error(f"Unexpected error fetching messages: {error}")
            raise

    async def get_messages_since(self, since_date: datetime.datetime) -> List[EmailMessage]:
        # Gmail query format for date
        date_query = f"after:{since_date.strftime('%Y/%m/%d')}"
        return await self.get_recent_messages(query=date_query)