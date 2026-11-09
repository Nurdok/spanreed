"""Print a web UI login link for a user, from a shell on the server.

    poetry run python -m spanreed.web.login_link <spanreed_user_id>

The fallback for when no chat surface can deliver the link (for example a
Telegram flood-control ban before the user ever logged in to the web UI).
"""

import asyncio
import sys

from spanreed.user import User
from spanreed.web import auth


async def _main(user_id: int) -> None:
    name = await User.get_user_name(user_id)
    link = await auth.create_login_link(user_id)
    print(f"Login link for {name} (user {user_id}), valid for 1 hour:")
    print(link)


def main() -> None:
    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        print(__doc__)
        sys.exit(2)
    asyncio.run(_main(int(sys.argv[1])))


if __name__ == "__main__":
    main()
