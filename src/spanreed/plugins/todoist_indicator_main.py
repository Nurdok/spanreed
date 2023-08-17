import asyncio
from spanreed.apis.todoist import Todoist, UserConfig, Task
from spanreed.apis.rpi.rpi import RPi
from spanreed.apis.rpi.i2c_lcd import Lcd
import time
import os


class TodoistIndicator:
    def __init__(self, rpi: RPi, todoist: Todoist):
        self._todoist = todoist

    async def run(self) -> None:
        lcd: Lcd = await self._rpi.get_lcd(1)
        while True:
            if tasks := self._todoist.get_due_tasks():
                task: Task = tasks[0]
                await lcd.write_text(
                    ["Tasks are due!".center(16), task.content[:16]]
                )
            elif tasks := self._todoist.get_inbox_tasks():
                task: Task = tasks[0]
                await lcd.write_text(
                    [
                        "Inbox not empty".center(16),
                        task.content[:16],
                    ]
                )
            else:
                await lcd.write_text(
                    ["No tasks".center(16), "Yay!".center(16)]
                )
            await asyncio.sleep(5)


async def main() -> None:
    ind = TodoistIndicator(
        RPi(), Todoist(UserConfig(os.environ["TODOIST_API_TOKEN"]))
    )
    await ind.run()


if __name__ == "__main__":
    asyncio.run(main())
