"""Authentication for the web UI.

Two kinds of secrets, both stored only as SHA-256 digests in Redis:

- **Login tokens**: single-use, short-lived. A login link
  (``https://spanreed.ink/app/login?token=...``) carries one; it is created
  by the "Web UI login link" command (from any surface) or by
  ``python -m spanreed.web.login_link <user_id>`` on the server, which works
  even while every chat surface is down.
- **Sessions**: created when a login token is redeemed, held in an
  ``HttpOnly``, ``Secure``, ``SameSite=Lax`` cookie scoped to ``/app``.
  Long-lived, so a user who logged in once keeps access through a Telegram
  ban without needing a new link.
"""

import hashlib
import secrets

from spanreed.storage import redis_api

LOGIN_TOKEN_TTL_S = 60 * 60  # 1 hour
SESSION_TTL_S = 90 * 24 * 60 * 60  # 90 days
SESSION_COOKIE = "spanreed_session"
BASE_URL = "https://spanreed.ink"


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _login_key(token: str) -> str:
    return f"web-ui:login-token:{_digest(token)}"


def _session_key(session_id: str) -> str:
    return f"web-ui:session:{_digest(session_id)}"


def _user_sessions_key(user_id: int) -> str:
    return f"web-ui:user:{user_id}:sessions"


async def create_login_token(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    await redis_api.set(_login_key(token), str(user_id), ex=LOGIN_TOKEN_TTL_S)
    return token


def login_url(token: str, base_url: str = BASE_URL) -> str:
    return f"{base_url}/app/login?token={token}"


async def create_login_link(user_id: int, base_url: str = BASE_URL) -> str:
    return login_url(await create_login_token(user_id), base_url)


async def redeem_login_token(token: str) -> int | None:
    """Exchange a login token for its user id; the token is consumed."""
    if not token:
        return None
    key = _login_key(token)
    raw = await redis_api.get(key)
    if raw is None:
        return None
    await redis_api.delete(key)
    return int(raw.decode() if isinstance(raw, bytes) else raw)


async def create_session(user_id: int) -> str:
    session_id = secrets.token_urlsafe(32)
    await redis_api.set(_session_key(session_id), str(user_id), ex=SESSION_TTL_S)
    await redis_api.sadd(_user_sessions_key(user_id), _digest(session_id))
    return session_id


async def user_id_for_session(session_id: str | None) -> int | None:
    if not session_id:
        return None
    raw = await redis_api.get(_session_key(session_id))
    if raw is None:
        return None
    return int(raw.decode() if isinstance(raw, bytes) else raw)


async def end_session(session_id: str) -> None:
    user_id = await user_id_for_session(session_id)
    await redis_api.delete(_session_key(session_id))
    if user_id is not None:
        await redis_api.srem(_user_sessions_key(user_id), _digest(session_id))


async def end_all_sessions(user_id: int) -> int:
    """Log the user out everywhere. Returns how many sessions were ended."""
    digests = await redis_api.smembers(_user_sessions_key(user_id))
    for digest in digests:
        digest_str = digest.decode() if isinstance(digest, bytes) else digest
        await redis_api.delete(f"web-ui:session:{digest_str}")
    await redis_api.delete(_user_sessions_key(user_id))
    return len(digests)
