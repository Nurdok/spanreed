import logging
import abc
from abc import ABC
from typing import List

import redis.asyncio as redis

from spanreed.registrable import Registrable


class Service(Registrable, ABC):
    service: List["Service"] = []

    def __init__(self, redis_api: redis.Redis):
        Service.register(self)
        self._logger = logging.getLogger(self.name)
        self._redis: redis.Redis = redis_api


Registrable.register(Service)
