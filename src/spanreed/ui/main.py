import asyncio
import redis
import logging
from aiohttp import web, WSMsgType


async def subscribe_to_logs(redis_api, channel):
    pubsub = redis_api.pubsub()
    await pubsub.subscribe(channel)
    return pubsub


async def get_logs(pubsub):
    while True:
        msg = await pubsub.get_message(ignore_subscribe_messages=True)
        if msg:
            yield msg['data'].decode('utf-8')


async def log_websocket(request, redis_api: redis.Redis):
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    pubsub = await subscribe_to_logs(redis_api, 'logs')

    async for log_msg in get_logs(pubsub):
        await ws.send_str(log_msg)

    return ws


async def index(request):
    return web.FileResponse(f'{__file__}/../index.html')


async def run_server(redis_api: redis.Redis):
    app = web.Application()
    app.add_routes([
        web.get('/logs', lambda request: log_websocket(request, redis_api)),
        web.get('/', index),
    ])
    # Now run the server using asyncio so that it's non-blocking
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, 'localhost', 8080)
    await site.start()


