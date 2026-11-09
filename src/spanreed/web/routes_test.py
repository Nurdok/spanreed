import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch

from quart import Quart

from spanreed.interaction import commands, engine
from spanreed.interaction.api import UserInteractionApi
from spanreed.interaction.commands import PluginCommand
from spanreed.interaction.surface import Content
from spanreed.interaction.web_surface import WebSurface
from spanreed.test_utils import FakeRedis, mock_user_find_by_id
from spanreed.web import auth, routes

USER_ID = 5
OTHER_USER_ID = 6
H = {routes.CSRF_HEADER: "1"}


class RecordingSurface(WebSurface):
    """Stands in for Telegram: always reachable, records what it got."""

    name = "telegram"

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[str] = []

    async def is_reachable(self, user: Any) -> bool:
        return True

    async def send_message(self, user: Any, content: Content) -> str:
        self.sent.append(content.text)
        return await super().send_message(user, content)

    async def render_prompt(self, user: Any, prompt: Any, spec: Any) -> None:
        self.sent.append(spec.prompt.text)


def setup_function() -> None:
    engine.reset_state_for_tests()
    commands.reset_registry_for_tests()
    UserInteractionApi.reset_surfaces_for_tests()
    routes._web_flows.clear()


def _user(user_id: int) -> Any:
    user = mock_user_find_by_id(user_id)
    user.plugins = ["demo"]
    return user


@contextlib.asynccontextmanager
async def _app() -> AsyncIterator[tuple[Any, FakeRedis, RecordingSurface]]:
    fake = FakeRedis()
    telegram = RecordingSurface()
    UserInteractionApi.register_surface(telegram)
    UserInteractionApi.register_surface(WebSurface())
    app = Quart(__name__)
    app.register_blueprint(routes.blueprint)
    with (
        patch("spanreed.web.auth.redis_api", new=fake),
        patch("spanreed.interaction.web_surface.redis_api", new=fake),
        patch("spanreed.web.google_auth.redis_api", new=fake),
        patch("spanreed.web.admins.redis_api", new=fake),
        patch(
            "spanreed.web.routes.User.find_by_id",
            new=AsyncMock(side_effect=_user),
        ),
        patch(
            "spanreed.apis.telegram_bot.TelegramBotApi.get_application",
            new=AsyncMock(),
        ),
    ):
        yield app.test_client(), fake, telegram


async def _login(client: Any, user_id: int = USER_ID) -> None:
    token = await auth.create_login_token(user_id)
    response = await client.post("/app/login", form={"token": token})
    assert response.status_code == 303
    assert auth.SESSION_COOKIE in response.headers["Set-Cookie"]


async def _wait_for(predicate: Any, timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


def test_api_requires_login_and_login_tokens_are_single_use() -> None:
    async def run() -> None:
        async with _app() as (client, _, _):
            assert (await client.get("/app/api/state")).status_code == 401
            page = await client.get("/app/")
            assert "login link" in (await page.get_data(as_text=True))

            token = await auth.create_login_token(USER_ID)
            # Opening the link (or a chat app fetching it for a preview)
            # only shows the button; it doesn't use the token up.
            for _ in range(3):
                page = await client.get(f"/app/login?token={token}")
                assert page.status_code == 200
                assert token in await page.get_data(as_text=True)
            assert (await client.get("/app/api/state")).status_code == 401

            login = await client.post("/app/login", form={"token": token})
            assert login.status_code == 303
            # The cookie jar now holds a session.
            assert (await client.get("/app/api/state")).status_code == 200
            # The same link can't be used twice.
            assert (await client.get(f"/app/login?token={token}")).status_code == 403
            again = await client.post("/app/login", form={"token": token})
            assert again.status_code == 403
            assert (await client.get("/app/login?token=nope")).status_code == 403

    asyncio.run(run())


def test_writes_require_the_csrf_header() -> None:
    async def run() -> None:
        async with _app() as (client, _, _):
            await _login(client)
            response = await client.post("/app/api/cancel", json={})
            assert response.status_code == 403
            response = await client.post("/app/api/cancel", json={}, headers=H)
            assert response.status_code == 200

    asyncio.run(run())


def test_state_lists_only_the_users_commands() -> None:
    async def run() -> None:
        commands.register_command("demo", PluginCommand("Say hi", AsyncMock()))
        commands.register_command("other", PluginCommand("Hidden", AsyncMock()))
        async with _app() as (client, _, _):
            await _login(client)
            state = await (await client.get("/app/api/state")).get_json()
            assert [c["text"] for c in state["commands"]] == ["Say hi"]
            assert state["user"]["id"] == USER_ID

            response = await client.post(
                "/app/api/commands/other.hidden/run", headers=H
            )
            assert response.status_code == 404

    asyncio.run(run())


def test_command_flow_runs_on_the_web_surface_only() -> None:
    answers: list[int | str] = []

    async def callback(user: Any) -> None:
        ui = await UserInteractionApi.for_user(user)
        choice = await ui.request_user_choice("Pick one", ["Red", "Blue"])
        answers.append(choice)
        text = await ui.request_user_input("Why?")
        answers.append(text)
        await ui.send_message(f"You picked {choice}: {text}")

    async def run() -> None:
        commands.register_command("demo", PluginCommand("Pick", callback))
        async with _app() as (client, _, telegram):
            await _login(client)
            state = await (await client.get("/app/api/state")).get_json()
            response = await client.post("/app/api/commands/demo.pick/run", headers=H)
            assert response.status_code == 200
            flow_id = (await response.get_json())["flow_id"]

            surface = routes.get_web_surface()
            await _wait_for(lambda: surface.open_prompt(USER_ID) is not None)
            prompt = surface.open_prompt(USER_ID)
            assert prompt is not None and prompt["kind"] == "choice"
            response = await client.post(
                "/app/api/answer",
                json={"interaction_id": prompt["interaction_id"], "value": 1},
                headers=H,
            )
            assert response.status_code == 200

            await _wait_for(
                lambda: (p := surface.open_prompt(USER_ID)) is not None
                and p["kind"] == "input"
            )
            prompt = surface.open_prompt(USER_ID)
            assert prompt is not None
            await client.post(
                "/app/api/answer",
                json={"interaction_id": prompt["interaction_id"], "value": "fun"},
                headers=H,
            )
            await _wait_for(lambda: routes.active_web_flow_id(USER_ID) is None)

            events = await (
                await client.get(f"/app/api/events?after={state['seq']}")
            ).get_json()
            summary = [
                (e["type"], e.get("html") or e.get("text") or e.get("flow_id"))
                for e in events["events"]
                if e["type"] != "close-prompt"
            ]
            assert summary == [
                ("flow-started", flow_id),
                ("prompt", "Pick one"),
                ("answer", "Blue"),
                ("prompt", "Why?"),
                ("answer", "fun"),
                ("message", "You picked 1: fun"),
                ("flow-finished", flow_id),
            ]
            assert answers == [1, "fun"]
            # Scoped to the web: nothing went to the (flood-limited) bot.
            assert telegram.sent == []

    asyncio.run(run())


def test_cannot_answer_another_users_prompt() -> None:
    async def run() -> None:
        async with _app() as (client, _, _):
            await _login(client, OTHER_USER_ID)
            pending = engine.create_prompt(USER_ID, engine.PromptKind.CHOICE)
            response = await client.post(
                "/app/api/answer",
                json={"interaction_id": pending.interaction_id, "value": 0},
                headers=H,
            )
            assert response.status_code == 409
            assert not pending.future.done()

    asyncio.run(run())


def test_cancel_stops_the_running_flow() -> None:

    async def callback(user: Any) -> None:
        ui = await UserInteractionApi.for_user(user)
        await ui.request_user_input("Waiting")

    async def run() -> None:
        commands.register_command("demo", PluginCommand("Wait", callback))
        async with _app() as (client, _, _):
            await _login(client)
            await client.get("/app/api/state")
            await client.post("/app/api/commands/demo.wait/run", headers=H)
            surface = routes.get_web_surface()
            await _wait_for(lambda: surface.open_prompt(USER_ID) is not None)
            prompt = surface.open_prompt(USER_ID)
            assert prompt is not None
            response = await client.post(
                "/app/api/cancel",
                json={"interaction_id": prompt["interaction_id"]},
                headers=H,
            )
            assert (await response.get_json())["ok"] is True
            await _wait_for(lambda: routes.active_web_flow_id(USER_ID) is None)
            assert surface.open_prompt(USER_ID) is None
            last = surface.recent_events(USER_ID)[-1]
            assert (last["type"], last["error"]) == ("flow-finished", "Cancelled.")

    asyncio.run(run())


def test_logout_ends_the_session() -> None:
    async def run() -> None:
        async with _app() as (client, _, _):
            await _login(client)
            response = await client.post("/app/api/logout", headers=H)
            assert response.status_code == 200
            assert (await client.get("/app/api/state")).status_code == 401

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Sign in with Google
# ---------------------------------------------------------------------------

GOOGLE_CONFIG = '{"web": {"client_id": "cid", "client_secret": "secret"}}'


def _claims(nonce: str, sub: str = "g-123") -> dict[str, Any]:
    return {
        "sub": sub,
        "email": "amir@example.com",
        "email_verified": True,
        "nonce": nonce,
    }


async def _google_round_trip(
    client: Any, fake: FakeRedis, start: Any, sub: str = "g-123"
) -> Any:
    """Follow /start (or /link) to Google and come back with a code."""
    import json
    import urllib.parse

    response = await start
    assert response.status_code == 302
    location = response.headers["Location"]
    assert location.startswith("https://accounts.google.com/")
    query = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
    assert query["redirect_uri"] == ["https://spanreed.ink/app/google/callback"]
    state = query["state"][0]
    nonce = json.loads(fake.strings[f"web-ui:google-state:{state}"])["nonce"]
    with (
        patch(
            "spanreed.web.google_auth.exchange_code",
            new=AsyncMock(return_value={"id_token": "tok"}),
        ),
        patch(
            "spanreed.web.google_auth.verify_id_token",
            new=AsyncMock(return_value=_claims(nonce, sub)),
        ) as verify,
    ):
        callback = await client.get(f"/app/google/callback?code=c&state={state}")
        verify.assert_awaited_once_with("tok", "cid")
    return callback


def test_google_login_needs_a_link_first_then_works() -> None:
    async def run() -> None:
        async with _app() as (client, fake, _):
            await fake.set("gmail_credentials", GOOGLE_CONFIG)
            page = await client.get("/app/")
            assert "Sign in with Google" in await page.get_data(as_text=True)

            # Not linked yet: Google proves who you are, but to no one.
            callback = await _google_round_trip(
                client, fake, client.get("/app/google/start")
            )
            assert callback.status_code == 400
            assert "linked to a Spanreed account yet" in await callback.get_data(
                as_text=True
            )
            assert (await client.get("/app/api/state")).status_code == 401

            # Link from a link-login session.
            await _login(client)
            state = await (await client.get("/app/api/state")).get_json()
            assert state["google"] == {"available": True, "email": None}
            callback = await _google_round_trip(
                client,
                fake,
                client.post("/app/google/link", headers={"Origin": auth.BASE_URL}),
            )
            assert callback.status_code == 303
            state = await (await client.get("/app/api/state")).get_json()
            assert state["google"]["email"] == "amir@example.com"

            # Log out, then sign in with Google alone.
            await client.post("/app/api/logout", headers=H)
            assert (await client.get("/app/api/state")).status_code == 401
            callback = await _google_round_trip(
                client, fake, client.get("/app/google/start")
            )
            assert callback.status_code == 303
            state = await (await client.get("/app/api/state")).get_json()
            assert state["user"]["id"] == USER_ID

            # Unlink: Google sign-in stops working.
            await client.post("/app/api/google/unlink", headers=H)
            await client.post("/app/api/logout", headers=H)
            callback = await _google_round_trip(
                client, fake, client.get("/app/google/start")
            )
            assert callback.status_code == 400

    asyncio.run(run())


def test_google_callback_rejects_a_state_from_another_browser() -> None:
    async def run() -> None:
        from spanreed.web import google_auth

        async with _app() as (client, fake, _):
            await fake.set("gmail_credentials", GOOGLE_CONFIG)
            # An attacker's own flow: valid state in Redis, but this browser
            # never got the matching state cookie.
            _, state = await google_auth.start(google_auth.Mode.LOGIN)
            callback = await client.get(f"/app/google/callback?code=c&state={state}")
            assert callback.status_code == 400
            assert "start in this browser" in await callback.get_data(as_text=True)

    asyncio.run(run())


def test_google_account_cannot_be_linked_to_two_users() -> None:
    async def run() -> None:
        from spanreed.web import google_auth

        async with _app() as (client, fake, _):
            identity = google_auth.GoogleIdentity("g-1", "a@example.com")
            await google_auth.link(OTHER_USER_ID, identity)
            try:
                await google_auth.link(USER_ID, identity)
            except google_auth.GoogleAuthError as e:
                assert "another Spanreed account" in str(e)
            else:
                raise AssertionError("expected GoogleAuthError")
            assert await google_auth.user_id_for_google("g-1") == OTHER_USER_ID

    asyncio.run(run())


def test_google_hidden_when_not_configured() -> None:
    async def run() -> None:
        async with _app() as (client, _, _):
            page = await client.get("/app/")
            assert "Sign in with Google" not in await page.get_data(as_text=True)
            response = await client.get("/app/google/start")
            assert response.status_code == 503

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Server logs (issue #30)
# ---------------------------------------------------------------------------


class FakePubSub:
    def __init__(self, messages: list[bytes]) -> None:
        self.messages = list(messages)
        self.subscribed: list[str] = []
        self.closed = False

    async def subscribe(self, channel: str) -> None:
        self.subscribed.append(channel)

    async def unsubscribe(self, channel: str) -> None:
        self.subscribed.remove(channel)

    async def aclose(self) -> None:
        self.closed = True

    async def get_message(self, **_: Any) -> dict[str, Any] | None:
        if self.messages:
            return {"type": "message", "data": self.messages.pop(0)}
        await asyncio.sleep(0)
        return None


def test_logs_page_needs_login_and_admin() -> None:
    async def run() -> None:
        async with _app() as (client, fake, _):
            page = await client.get("/app/logs")
            assert "login link" in await page.get_data(as_text=True)
            assert (await client.get("/app/api/logs/stream")).status_code == 401

            # USER_ID isn't user 1 and the admin set is empty: not an admin.
            await _login(client)
            assert (await client.get("/app/logs")).status_code == 403
            assert (await client.get("/app/api/logs/stream")).status_code == 403
            state = await (await client.get("/app/api/state")).get_json()
            assert state["is_admin"] is False

            await fake.sadd("web-ui:admins", USER_ID)
            page = await client.get("/app/logs")
            assert page.status_code == 200
            assert "/app/api/logs/stream" in await page.get_data(as_text=True)
            state = await (await client.get("/app/api/state")).get_json()
            assert state["is_admin"] is True

    asyncio.run(run())


def test_first_user_is_admin_until_admins_are_set() -> None:
    from spanreed.web import admins

    async def run() -> None:
        fake = FakeRedis()
        with patch("spanreed.web.admins.redis_api", new=fake):
            assert await admins.is_admin(1)
            assert not await admins.is_admin(2)
            await fake.sadd(admins.ADMINS_KEY, 2)
            assert await admins.is_admin(2)
            assert not await admins.is_admin(1)

    asyncio.run(run())


def test_log_stream_relays_lines_as_server_sent_events() -> None:
    async def run() -> None:
        pubsub = FakePubSub([b"line one", b"multi\nline"])
        events = routes._sse_log_events(pubsub)
        chunks = [await events.__anext__() for _ in range(3)]
        assert chunks == [
            b"retry: 3000\n\n",
            b"data: line one\n\n",
            b"data: multi\ndata: line\n\n",
        ]
        assert pubsub.subscribed == ["logs"]
        await events.aclose()
        assert pubsub.subscribed == [] and pubsub.closed

    asyncio.run(run())
