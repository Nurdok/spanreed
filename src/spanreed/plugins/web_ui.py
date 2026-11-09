import logging
import asyncio
from typing import Any

from spanreed import BASE_LOGGER
from spanreed.plugin import Plugin
from quart import Quart, redirect, request
from spanreed.storage import redis_api
from spanreed.apis.withings import WithingsApi
from spanreed.apis.gmail import GmailApi
from spanreed.apis.googlefit import GoogleFitApi
from spanreed.apis.health_connect import HealthConnectApi
from spanreed.web.routes import blueprint as app_blueprint


class WebUiPlugin(Plugin[None]):
    @classmethod
    def name(cls) -> str:
        return "Web UI"

    async def run(self) -> None:
        BASE_LOGGER.addHandler(RedisPubSubHandler())
        app = build_app()
        await app.run_task(debug=False, host="127.0.0.1", port=5000)


def build_app() -> Quart:
    app = Quart(__name__)
    # The command UI (login, command list, live conversation) at /app.
    app.register_blueprint(app_blueprint)

    # The server logs viewer lives at /app/logs, behind the web UI
    # login and limited to admins (it shows every user's activity).
    @app.get("/")
    async def root() -> Any:
        return redirect("/app/")

    # Unauthenticated liveness check for the deploy workflow.
    @app.get("/healthz")
    async def healthz() -> str:
        return "ok"

    @app.get("/withings-oauth")
    async def withings_oauth_redirect() -> str:
        # Extract the code from the query string.
        code: str | None = request.args.get("code")
        if code is None:
            return "No code provided."

        state: str | None = request.args.get("state")
        if state is None or (spanreed_user_id := int(state)) is None:
            return "No state provided."

        # Pass the code to the Withings API without blocking the Quart app.
        await asyncio.create_task(WithingsApi.handle_oauth_redirect(code, state))
        return "Authenticated successfully. You can close this tab."

    @app.get("/gmail-oauth")
    async def gmail_oauth_redirect() -> str:
        # Extract the code from the query string.
        code: str | None = request.args.get("code")
        if code is None:
            return "No code provided."

        state: str | None = request.args.get("state")
        if state is None:
            return "No state provided."

        # Pass the code to the Gmail API without blocking the Quart app.
        await asyncio.create_task(GmailApi.handle_oauth_redirect(code, state))
        return "Gmail authenticated successfully. You can close this tab."

    @app.get("/googlefit-oauth")
    async def googlefit_oauth_redirect() -> str:
        # Extract the code from the query string.
        code: str | None = request.args.get("code")
        if code is None:
            return "No code provided."

        state: str | None = request.args.get("state")
        if state is None:
            return "No state provided."

        # Pass the code to the Google Fit API without blocking the Quart app.
        await asyncio.create_task(GoogleFitApi.handle_oauth_redirect(code, state))
        return "Google Fit authenticated successfully. You can close this tab."

    @app.post("/health-connect")
    async def health_connect_webhook() -> tuple[str, int]:
        # Pushed by the Health Connect bridge app on the user's phone.
        # Only authenticates and queues; the plugin drains the queue.
        token: str = HealthConnectApi.token_from_header(
            request.headers.get("Authorization", "")
        )
        raw: str | bytes = await request.get_data()
        body: str = (
            raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
        )
        status: int = await HealthConnectApi.handle_webhook(token, body)
        return {401: "Unauthorized", 400: "Bad Request"}.get(status, "Accepted"), status

    return app


class RedisPubSubHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        log_message = self.format(record)

        async def publish() -> None:
            await redis_api.publish("logs", log_message)

        asyncio.create_task(publish())
