import logging
import asyncio
import textwrap

from spanreed.plugin import Plugin
from quart import Quart, websocket
from spanreed.storage import redis_api


class WebUiPlugin(Plugin[None]):
    @classmethod
    def name(cls) -> str:
        return "Web UI"

    async def run(self) -> None:
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

        # Start the Quart app.
        await app.run_task(debug=True, host="0.0.0.0", port=80)


class RedisPubSubHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        log_message = self.format(record)

        async def publish() -> None:
            await redis_api.publish("logs", log_message)

        asyncio.create_task(publish())


logging.getLogger().addHandler(RedisPubSubHandler())
