"""Who counts as a Spanreed admin (for server-wide views like the logs).

Admins are the user ids in the Redis set ``web-ui:admins``. While that set
is empty, user 1 (the first account ever created, i.e. whoever set up the
server) is the only admin, so the owner has access without any setup.

Manage the set from a shell on the server:

    poetry run python -m spanreed.web.admins list
    poetry run python -m spanreed.web.admins add <user_id>
    poetry run python -m spanreed.web.admins remove <user_id>
"""

import asyncio
import sys

from spanreed.storage import redis_api

ADMINS_KEY = "web-ui:admins"
DEFAULT_ADMIN_ID = 1


async def admin_ids() -> set[int]:
    members = await redis_api.smembers(ADMINS_KEY)
    ids = {int(m.decode() if isinstance(m, bytes) else m) for m in members}
    return ids or {DEFAULT_ADMIN_ID}


async def is_admin(user_id: int) -> bool:
    return user_id in await admin_ids()


async def _main(args: list[str]) -> None:
    if args == ["list"]:
        print(" ".join(str(i) for i in sorted(await admin_ids())))
    elif len(args) == 2 and args[0] in ("add", "remove") and args[1].isdigit():
        if args[0] == "add":
            await redis_api.sadd(ADMINS_KEY, int(args[1]))
        else:
            await redis_api.srem(ADMINS_KEY, int(args[1]))
        print("Admins:", " ".join(str(i) for i in sorted(await admin_ids())))
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    asyncio.run(_main(sys.argv[1:]))
