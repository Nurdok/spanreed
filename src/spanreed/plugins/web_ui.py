import logging
import asyncio
import textwrap

from spanreed import BASE_LOGGER
from spanreed.plugin import Plugin
from quart import Quart, websocket, request
from spanreed.storage import redis_api
from spanreed.apis.withings import WithingsApi
from spanreed.apis.gmail import GmailApi


class WebUiPlugin(Plugin[None]):
    @classmethod
    def name(cls) -> str:
        return "Web UI"

    async def run(self) -> None:
        BASE_LOGGER.addHandler(RedisPubSubHandler())

        # Create a Quart app.
        app = Quart(__name__)

        # Register the `get_logs` route.
        @app.get("/")
        async def get_logs() -> str:
            # Render an HTML page that reads logs from a websocket
            # and displays them.
            return textwrap.dedent(
                """\
                <!DOCTYPE html>
                <html>
                <head>
                  <title>WebSocket Logs</title>
                  <style>
                    #log-container {
                      display: flex;
                      flex-direction: column;
                    }
                  </style>
                </head>
                <body>
                  <div id="log-container"></div>

                  <script>
                    // Create WebSocket connection
                    const socket = new WebSocket(`ws://${window.location.host}/logs-ws`);

                    // Handle incoming messages
                    socket.addEventListener('message', function(event) {
                      const logMessage = event.data;
                      displayLog(logMessage);
                    });

                    // Display log message in the container
                    function displayLog(message) {
                      const logContainer = document.getElementById('log-container');
                      const logElement = document.createElement('code');
                      logElement.textContent = message;
                      logContainer.appendChild(logElement);
                    }
                  </script>
                </body>
                </html>
            """
            )

        @app.websocket("/logs-ws")
        async def logs_ws() -> None:
            # Subscribe to the logs channel.
            pubsub = redis_api.pubsub()
            await pubsub.subscribe("logs")

            # Send the logs to the client.
            while True:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True
                )
                if message is not None:
                    await websocket.send(message["data"].decode("utf-8"))

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
            await asyncio.create_task(
                WithingsApi.handle_oauth_redirect(code, state)
            )
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
            await asyncio.create_task(
                GmailApi.handle_oauth_redirect(code, state)
            )
            return "Gmail authenticated successfully. You can close this tab."

        # Start the Quart app.
        await app.run_task(debug=False, host="127.0.0.1", port=5000)


class RedisPubSubHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        log_message = self.format(record)

        async def publish() -> None:
            await redis_api.publish("logs", log_message)

        asyncio.create_task(publish())
