import redis
import logging


class RedisPubSubHandler(logging.Handler):
    def __init__(self, channel: str, redis_api: redis.Redis):
        super().__init__()
        self._redis_api = redis_api
        self._channel = channel

    def emit(self, record):
        msg = self.format(record)
        print(f"Publishing to {self._channel}: {msg}")
        self._redis_api.publish(self._channel, msg)
        self._redis_api.flus
