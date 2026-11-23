import asyncio
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pywebpush import WebPushException  # type: ignore[import-untyped]

from spanreed.test_utils import FakeRedis
from spanreed.web import push

USER_ID = 5
ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc"
OTHER_ENDPOINT = "https://updates.push.services.mozilla.com/wpush/v2/xyz"
SUBSCRIPTION: dict[str, Any] = {
    "endpoint": ENDPOINT,
    "keys": {"p256dh": "p", "auth": "a"},
}
OTHER: dict[str, Any] = {
    "endpoint": OTHER_ENDPOINT,
    "keys": {"p256dh": "p2", "auth": "a2"},
}


@pytest.fixture(autouse=True)
def redis() -> Iterator[FakeRedis]:
    fake = FakeRedis()
    with patch("spanreed.web.push.redis_api", new=fake):
        yield fake


def _refused(status: int) -> WebPushException:
    response = MagicMock()
    response.status = status
    return WebPushException("refused", response=response)


def test_public_key_is_created_once_and_kept() -> None:
    first = asyncio.run(push.public_key())
    second = asyncio.run(push.public_key())

    assert first == second
    # An uncompressed P-256 point, base64url without padding.
    assert len(first) == 87 and "=" not in first


def test_subscribe_keeps_only_what_a_push_needs() -> None:
    async def run() -> dict[str, Any]:
        await push.subscribe(USER_ID, {**SUBSCRIPTION, "expirationTime": None, "x": 1})
        await push.subscribe(USER_ID, SUBSCRIPTION)  # same browser again
        assert await push.is_subscribed(USER_ID, SUBSCRIPTION["endpoint"])
        assert not await push.is_subscribed(USER_ID, OTHER["endpoint"])
        return await push._subscriptions(USER_ID)

    (stored,) = asyncio.run(run()).values()

    assert stored["endpoint"] == SUBSCRIPTION["endpoint"]
    assert stored["keys"] == SUBSCRIPTION["keys"]
    assert "x" not in stored


@pytest.mark.parametrize(
    "subscription",
    [
        None,
        "text",
        {"endpoint": SUBSCRIPTION["endpoint"]},
        {"endpoint": 5, "keys": SUBSCRIPTION["keys"]},
        {"endpoint": SUBSCRIPTION["endpoint"], "keys": {"p256dh": "p"}},
        {**SUBSCRIPTION, "endpoint": "https://example.com/push"},
        {**SUBSCRIPTION, "endpoint": "http://fcm.googleapis.com/fcm/send/abc"},
        {**SUBSCRIPTION, "endpoint": "https://fcm.googleapis.com.evil.test/x"},
    ],
)
def test_subscribe_rejects_other_things(subscription: Any) -> None:
    with pytest.raises(push.PushError):
        asyncio.run(push.subscribe(USER_ID, subscription))
    assert not asyncio.run(push.has_subscription(USER_ID))


def test_subscribe_limits_the_devices_of_a_user() -> None:
    async def run() -> None:
        for i in range(push.MAX_SUBSCRIPTIONS_PER_USER):
            await push.subscribe(
                USER_ID,
                {**SUBSCRIPTION, "endpoint": f"https://fcm.googleapis.com/fcm/{i}"},
            )
        with pytest.raises(push.PushError):
            await push.subscribe(USER_ID, OTHER)
        # A device that is already known can still refresh its subscription.
        await push.subscribe(
            USER_ID, {**SUBSCRIPTION, "endpoint": "https://fcm.googleapis.com/fcm/0"}
        )

    asyncio.run(run())


def test_send_pushes_to_every_device_and_counts_the_accepted() -> None:
    sent: list[tuple[str, dict]] = []

    async def sender(subscription: dict, data: str, key: str) -> None:
        assert "PRIVATE KEY" in key
        sent.append((subscription["endpoint"], json.loads(data)))

    async def run() -> int:
        await push.subscribe(USER_ID, SUBSCRIPTION)
        await push.subscribe(USER_ID, OTHER)
        return await push.send(USER_ID, {"kind": "prompt", "body": "Hi"})

    with patch("spanreed.web.push._sender", new=sender):
        accepted = asyncio.run(run())

    assert accepted == 2
    assert sorted(endpoint for endpoint, _ in sent) == sorted(
        [SUBSCRIPTION["endpoint"], OTHER["endpoint"]]
    )
    assert sent[0][1] == {"kind": "prompt", "body": "Hi"}


def test_send_without_a_subscription_does_nothing() -> None:
    sender = AsyncMock()

    with patch("spanreed.web.push._sender", new=sender):
        assert asyncio.run(push.send(USER_ID, {"body": "Hi"})) == 0

    sender.assert_not_awaited()


def test_send_forgets_a_subscription_that_is_gone() -> None:
    async def sender(subscription: dict, _data: str, _key: str) -> None:
        if subscription["endpoint"] == SUBSCRIPTION["endpoint"]:
            raise _refused(410)

    async def run() -> int:
        await push.subscribe(USER_ID, SUBSCRIPTION)
        await push.subscribe(USER_ID, OTHER)
        accepted = await push.send(USER_ID, {"body": "Hi"})
        assert not await push.is_subscribed(USER_ID, SUBSCRIPTION["endpoint"])
        assert await push.is_subscribed(USER_ID, OTHER["endpoint"])
        return accepted

    with patch("spanreed.web.push._sender", new=sender):
        assert asyncio.run(run()) == 1


def test_send_keeps_a_subscription_after_a_passing_failure() -> None:
    async def run() -> int:
        await push.subscribe(USER_ID, SUBSCRIPTION)
        accepted = await push.send(USER_ID, {"body": "Hi"})
        assert await push.is_subscribed(USER_ID, SUBSCRIPTION["endpoint"])
        return accepted

    for error in (_refused(500), _refused(429), OSError("network down")):
        with patch("spanreed.web.push._sender", new=AsyncMock(side_effect=error)):
            assert asyncio.run(run()) == 0


def test_send_never_raises() -> None:
    broken = MagicMock()
    broken.hgetall = AsyncMock(side_effect=ConnectionError("redis down"))

    with patch("spanreed.web.push.redis_api", new=broken):
        assert asyncio.run(push.send(USER_ID, {"body": "Hi"})) == 0


def test_post_encrypts_for_the_browser_and_signs_with_the_server_key() -> None:
    """Run the real sender up to the network call, against a real key pair."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from py_vapid import b64urlencode  # type: ignore[import-untyped]

    browser_key = ec.generate_private_key(ec.SECP256R1())
    p256dh = b64urlencode(
        browser_key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        )
    )
    subscription = {
        "endpoint": SUBSCRIPTION["endpoint"],
        "keys": {"p256dh": p256dh, "auth": b64urlencode(b"0123456789abcdef")},
    }
    posted: dict[str, Any] = {}

    class Response:
        status = 201

        async def text(self) -> str:
            return ""

    class Session:
        async def post(self, url: str, **kwargs: Any) -> Response:
            posted.update(url=url, **kwargs)
            return Response()

        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

    async def run() -> None:
        pem = await push._vapid_pem()
        await push._post(subscription, json.dumps({"body": "secret text"}), pem)

    with patch("pywebpush.aiohttp.ClientSession", new=Session):
        asyncio.run(run())

    assert posted["url"] == SUBSCRIPTION["endpoint"]
    headers = {k.lower(): v for k, v in posted["headers"].items()}
    assert headers["urgency"] == "high"
    assert str(headers["ttl"]) == str(push.TTL_S)
    assert headers["authorization"].startswith("vapid t=")
    assert headers["content-encoding"] == "aes128gcm"
    # The payload is encrypted: the push service cannot read the text.
    assert b"secret text" not in posted["data"]
    assert posted["timeout"].total == push.SEND_TIMEOUT_S
