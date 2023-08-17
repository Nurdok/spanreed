import asyncio
from spanreed.apis.todoist import Todoist, UserConfig, Task
from spanreed.apis.rpi.rpi import RPi
from spanreed.apis.rpi.i2c_lcd import Lcd
import time
import os


async def marquee(lcd: Lcd, text: str, line: int) -> None:
    """Marquee the text."""
    width = 16
    text = text.strip()
    if len(text) < width:
        await lcd.write_text_line(text.center(width), line)
        return

    for i in range(len(text) - width + 1):
        await lcd.write_text_line(text[i : i + width], line)
        await asyncio.sleep(0.5)


class TodoistIndicator:
    def __init__(self, rpi: RPi, todoist: Todoist):
        self._todoist = todoist
        self._rpi = rpi

    async def run(self) -> None:
        lcd: Lcd = await self._rpi.get_lcd(1)
        while True:
            if tasks := (await self._todoist.get_due_tasks()):
                task: Task = tasks[0]
                await lcd.write_text_line("Tasks are due!".center(16), 1)
                await marquee(lcd, task.content, 2)
            elif tasks := (await self._todoist.get_inbox_tasks()):
                task: Task = tasks[0]
                await lcd.write_text_line("Inbox not empty".center(16), 1)
                await marquee(lcd, task.content, 2)
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
