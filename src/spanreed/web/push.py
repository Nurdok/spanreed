"""Web Push notifications for the web UI.

The web UI is installable as an app (see ``static/manifest.webmanifest``).
Web Push is what lets it stand in for Telegram: the server pushes when a
plugin prompts or messages the user, and the app's service worker shows a
notification that opens the app, even when the app is closed.

How it fits together:

- **Server key pair (VAPID).** Generated on first use and kept in Redis
  under ``web-ui:vapid``. The browser gets the public key when it subscribes
  and the push service checks each push against it.
- **Subscriptions.** A browser that turns notifications on sends its push
  subscription (an endpoint URL at the browser's push service, plus two
  keys). They are kept per user in the Redis hash ``web-ui:user:{id}:push``.
- **Sending.** The payload is encrypted for that browser, so the push
  service cannot read it. A subscription that the push service reports as
  gone is deleted.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import aiohttp

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid02, b64urlencode  # type: ignore[import-untyped]
from pywebpush import (  # type: ignore[import-untyped]
    WebPushException,
    webpush_async,
)

from spanreed.storage import redis_api
from spanreed.web import auth

_logger = logging.getLogger(__name__)

VAPID_KEY = "web-ui:vapid"
# How long the push service keeps a push for a phone that is off or offline.
TTL_S = 12 * 60 * 60
SEND_TIMEOUT_S = 15.0
MAX_SUBSCRIPTIONS_PER_USER = 10
# The server posts to the endpoint a browser gives it, so only the push
# services of the browsers are accepted.
_PUSH_SERVICE_HOSTS = (
    "fcm.googleapis.com",
    "updates.push.services.mozilla.com",
    "web.push.apple.com",
    "notify.windows.com",
)


class PushError(Exception):
    """A subscription is not usable, with a message for the user."""


def _subscriptions_key(user_id: int) -> str:
    return f"web-ui:user:{user_id}:push"


def _field(endpoint: str) -> str:
    return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()


def _decode(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


async def _vapid_pem() -> str:
    """Return the server's private key, creating it on first use."""
    stored = await redis_api.get(VAPID_KEY)
    if stored is not None:
        return _decode(stored)
    vapid = Vapid02()
    vapid.generate_keys()
    pem = vapid.private_pem().decode("ascii")
    # ``nx``: if two requests race here, both end up with the same key.
    await redis_api.set(VAPID_KEY, pem, nx=True)
    return _decode(await redis_api.get(VAPID_KEY))


async def public_key() -> str:
    """Return the server's public key in the form a browser subscribes with."""
    vapid = Vapid02.from_pem((await _vapid_pem()).encode("ascii"))
    raw = vapid.public_key.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    return str(b64urlencode(raw))


def _checked_subscription(subscription: Any) -> dict[str, Any]:
    """Return the parts of a browser's push subscription that are stored.

    Raise ``PushError`` if it is not a subscription at a known push service.
    """
    if not isinstance(subscription, dict):
        raise PushError("Not a push subscription.")
    endpoint = subscription.get("endpoint")
    keys = subscription.get("keys")
    if not isinstance(endpoint, str) or not isinstance(keys, dict):
        raise PushError("Not a push subscription.")
    p256dh, auth_secret = keys.get("p256dh"), keys.get("auth")
    if not isinstance(p256dh, str) or not isinstance(auth_secret, str):
        raise PushError("Not a push subscription.")
    url = urlsplit(endpoint)
    host = url.hostname or ""
    if url.scheme != "https" or not any(
        host == allowed or host.endswith("." + allowed)
        for allowed in _PUSH_SERVICE_HOSTS
    ):
        raise PushError("This browser's push service is not supported.")
    return {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth_secret}}


async def subscribe(user_id: int, subscription: Any) -> None:
    """Remember a browser's push subscription for a user."""
    checked = _checked_subscription(subscription)
    key = _subscriptions_key(user_id)
    field = _field(checked["endpoint"])
    existing = await redis_api.hgetall(key)
    if len(existing) >= MAX_SUBSCRIPTIONS_PER_USER and field not in {
        _decode(name) for name in existing
    }:
        raise PushError("Too many devices have notifications on. Turn one off.")
    await redis_api.hset(key, field, json.dumps({**checked, "created": time.time()}))


async def unsubscribe(user_id: int, endpoint: str) -> bool:
    """Forget a browser's push subscription. Return whether it was known."""
    return bool(await redis_api.hdel(_subscriptions_key(user_id), _field(endpoint)))


async def _subscriptions(user_id: int) -> dict[str, dict[str, Any]]:
    stored: dict = await redis_api.hgetall(_subscriptions_key(user_id))
    subscriptions = {}
    for field, raw in stored.items():
        try:
            subscriptions[_decode(field)] = json.loads(_decode(raw))
        except ValueError:
            continue
    return subscriptions


async def has_subscription(user_id: int) -> bool:
    """Return whether any browser of the user has notifications turned on."""
    return bool(await redis_api.hlen(_subscriptions_key(user_id)))


async def is_subscribed(user_id: int, endpoint: str) -> bool:
    """Return whether this browser's subscription is known for the user."""
    return bool(await redis_api.hexists(_subscriptions_key(user_id), _field(endpoint)))


async def _post(subscription: dict[str, Any], data: str, vapid_pem: str) -> None:
    """Send one push. Raise ``WebPushException`` if the service refuses it."""
    await webpush_async(
        subscription_info={
            "endpoint": subscription["endpoint"],
            "keys": subscription["keys"],
        },
        data=data,
        vapid_private_key=Vapid02.from_pem(vapid_pem.encode("ascii")),
        # The claims are changed by the call, so each push gets its own.
        vapid_claims={"sub": auth.BASE_URL},
        ttl=TTL_S,
        timeout=aiohttp.ClientTimeout(total=SEND_TIMEOUT_S),
        # Deliver at once, even to a phone that is saving battery.
        headers={"Urgency": "high"},
    )


# Replaced in tests.
_sender: Callable[[dict[str, Any], str, str], Awaitable[None]] = _post


def _is_gone(error: Exception) -> bool:
    """Return whether the push service says the subscription no longer exists."""
    response = getattr(error, "response", None)
    status = getattr(response, "status", None) or getattr(response, "status_code", None)
    return status in (404, 410)


async def send(user_id: int, payload: dict[str, Any]) -> int:
    """Push a notification to every subscribed browser of a user.

    Return how many pushes the push services accepted. Never raise: a push
    is a hint to look at the app, and whatever it announces is in the app
    whether or not the push arrives.
    """
    try:
        subscriptions = await _subscriptions(user_id)
        if not subscriptions:
            return 0
        vapid_pem = await _vapid_pem()
    except asyncio.CancelledError:
        raise
    except Exception:
        _logger.exception(f"Could not prepare a push for user {user_id}")
        return 0

    data = json.dumps(payload)
    accepted = 0
    for field, subscription in subscriptions.items():
        try:
            await _sender(subscription, data, vapid_pem)
        except asyncio.CancelledError:
            raise
        except WebPushException as e:
            if _is_gone(e):
                _logger.info(f"Push subscription of user {user_id} is gone; removed.")
                await redis_api.hdel(_subscriptions_key(user_id), field)
            else:
                _logger.warning(f"Push to user {user_id} was refused: {e!r}")
        except Exception as e:
            _logger.warning(f"Push to user {user_id} failed: {e!r}")
        else:
            accepted += 1
    _logger.info(
        f"Pushed {payload.get('kind', 'notification')!r} to user {user_id}:"
        f" {accepted} of {len(subscriptions)} accepted"
    )
    return accepted
