"""Sign in with Google for the web UI (OpenID Connect, authorization code).

A Google account is *linked* to a Spanreed user from a session that is
already logged in (the first login comes from a one-time login link). After
that, "Sign in with Google" on the login page starts a session for whichever
user that Google account is linked to. A Google account that isn't linked
can't sign in: Google proves who someone is, the link says which Spanreed
user they are.

Client credentials: the OAuth client JSON (the ``{"web": {...}}`` file from
Google Cloud) under the Redis key ``web_ui_google_credentials``, falling back
to the client the Gmail / Google Fit integrations already use. The client
must list ``https://spanreed.ink/app/google/callback`` as an authorized
redirect URI.

Redis keys:

- ``web-ui:google-state:{state}``  pending sign-in/link (10 min, single use)
- ``web-ui:google-sub:{sub}``      Google account id -> Spanreed user id
- ``web-ui:user:{id}:google``      the user's linked account {sub, email}
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import secrets
import urllib.parse
from dataclasses import dataclass
from typing import Any

import aiohttp

from spanreed.storage import redis_api
from spanreed.web import auth

_logger = logging.getLogger(__name__)

CREDENTIALS_KEYS = (
    "web_ui_google_credentials",
    "gmail_credentials",
    "googlefit_credentials",
)
AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
STATE_TTL_S = 10 * 60
STATE_COOKIE = "spanreed_google_state"


def redirect_uri(base_url: str = auth.BASE_URL) -> str:
    return f"{base_url}/app/google/callback"


class Mode(enum.StrEnum):
    LOGIN = "login"
    LINK = "link"


class GoogleAuthError(Exception):
    """A sign-in or link attempt failed; the message is shown to the user."""


@dataclass(frozen=True)
class ClientConfig:
    client_id: str
    client_secret: str


@dataclass(frozen=True)
class GoogleIdentity:
    sub: str
    email: str


def _decode(raw: Any) -> str:
    return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)


async def get_client_config() -> ClientConfig | None:
    for key in CREDENTIALS_KEYS:
        raw = await redis_api.get(key)
        if raw is None:
            continue
        try:
            data = json.loads(_decode(raw))
            section = data.get("web") or data.get("installed") or data
            return ClientConfig(section["client_id"], section["client_secret"])
        except (ValueError, KeyError, TypeError, AttributeError):
            _logger.exception(f"Unusable Google client config under {key!r}")
    return None


async def is_configured() -> bool:
    return await get_client_config() is not None


# ---------------------------------------------------------------------------
# Starting a flow
# ---------------------------------------------------------------------------


def _state_key(state: str) -> str:
    return f"web-ui:google-state:{state}"


async def start(mode: Mode, user_id: int | None = None) -> tuple[str, str]:
    """Begin a sign-in (or link, for ``user_id``). Returns (url, state).

    The caller must also put ``state`` in the browser's state cookie, so
    the callback only completes in the browser that started the flow.
    """
    config = await get_client_config()
    if config is None:
        raise GoogleAuthError("Sign in with Google isn't set up on this server.")
    if mode == Mode.LINK and user_id is None:
        raise ValueError("Linking needs a logged-in user")
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    await redis_api.set(
        _state_key(state),
        json.dumps({"mode": str(mode), "user_id": user_id, "nonce": nonce}),
        ex=STATE_TTL_S,
    )
    params = {
        "client_id": config.client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": "openid email",
        "state": state,
        "nonce": nonce,
        "prompt": "select_account",
    }
    return f"{AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}", state


# ---------------------------------------------------------------------------
# Completing a flow
# ---------------------------------------------------------------------------


async def exchange_code(config: ClientConfig, code: str) -> dict[str, Any]:
    """Trade the authorization code for tokens at Google's token endpoint."""
    async with aiohttp.ClientSession() as session:
        async with session.post(
            TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": config.client_id,
                "client_secret": config.client_secret,
                "redirect_uri": redirect_uri(),
                "grant_type": "authorization_code",
            },
            timeout=aiohttp.ClientTimeout(total=15),
        ) as response:
            body: dict[str, Any] = await response.json(content_type=None)
            if response.status != 200:
                _logger.warning(f"Google token exchange failed: {body!r}")
                raise GoogleAuthError("Google didn't accept the sign-in. Try again.")
            return body


def _verify_id_token_sync(token: str, client_id: str) -> dict[str, Any]:
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token

    claims: dict[str, Any] = id_token.verify_oauth2_token(
        token, google_requests.Request(), client_id
    )
    return claims


async def verify_id_token(token: str, client_id: str) -> dict[str, Any]:
    """Check the ID token's signature, issuer, audience and expiry."""
    # verify_oauth2_token fetches Google's certs with a blocking HTTP call.
    return await asyncio.to_thread(_verify_id_token_sync, token, client_id)


@dataclass(frozen=True)
class CallbackResult:
    mode: Mode
    user_id: int
    identity: GoogleIdentity


async def complete(code: str, state: str, state_cookie: str | None) -> CallbackResult:
    """Finish a flow started by ``start``: verify, then sign in or link."""
    if not state or not state_cookie or not secrets.compare_digest(state, state_cookie):
        raise GoogleAuthError(
            "This sign-in didn't start in this browser. Start it again."
        )
    raw = await redis_api.get(_state_key(state))
    if raw is None:
        raise GoogleAuthError("This sign-in expired. Start it again.")
    await redis_api.delete(_state_key(state))
    pending = json.loads(_decode(raw))
    mode = Mode(pending["mode"])

    config = await get_client_config()
    if config is None:
        raise GoogleAuthError("Sign in with Google isn't set up on this server.")
    tokens = await exchange_code(config, code)
    try:
        claims = await verify_id_token(tokens["id_token"], config.client_id)
    except (KeyError, ValueError) as e:
        _logger.warning(f"Rejected Google ID token: {e!r}")
        raise GoogleAuthError("Google's answer couldn't be verified.") from e
    if claims.get("nonce") != pending["nonce"]:
        raise GoogleAuthError("Google's answer couldn't be verified.")
    if not claims.get("email_verified"):
        raise GoogleAuthError("That Google account's email isn't verified.")
    identity = GoogleIdentity(sub=str(claims["sub"]), email=str(claims["email"]))

    if mode == Mode.LINK:
        user_id = int(pending["user_id"])
        await link(user_id, identity)
        return CallbackResult(mode, user_id, identity)

    linked_user_id = await user_id_for_google(identity.sub)
    if linked_user_id is None:
        raise GoogleAuthError(
            f"{identity.email} isn't linked to a Spanreed account yet. Log in"
            " once with a login link, then choose “Link Google account”."
        )
    return CallbackResult(mode, linked_user_id, identity)


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------


def _sub_key(sub: str) -> str:
    return f"web-ui:google-sub:{sub}"


def _user_key(user_id: int) -> str:
    return f"web-ui:user:{user_id}:google"


async def user_id_for_google(sub: str) -> int | None:
    raw = await redis_api.get(_sub_key(sub))
    return None if raw is None else int(_decode(raw))


async def linked_identity(user_id: int) -> GoogleIdentity | None:
    raw = await redis_api.get(_user_key(user_id))
    if raw is None:
        return None
    data = json.loads(_decode(raw))
    return GoogleIdentity(sub=data["sub"], email=data["email"])


async def link(user_id: int, identity: GoogleIdentity) -> None:
    """Link a Google account to a user (replacing the user's old link)."""
    owner = await user_id_for_google(identity.sub)
    if owner is not None and owner != user_id:
        raise GoogleAuthError(
            f"{identity.email} is already linked to another Spanreed account."
        )
    await unlink(user_id)
    await redis_api.set(_sub_key(identity.sub), str(user_id))
    await redis_api.set(
        _user_key(user_id), json.dumps({"sub": identity.sub, "email": identity.email})
    )
    _logger.info(f"Linked a Google account to user {user_id}")


async def unlink(user_id: int) -> bool:
    current = await linked_identity(user_id)
    if current is None:
        return False
    await redis_api.delete(_sub_key(current.sub), _user_key(user_id))
    return True
