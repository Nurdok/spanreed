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
        if i == 0:
            # Give the user a chance to read the first part of the text.
            await asyncio.sleep(3)
        await asyncio.sleep(0.5)


class TodoistIndicator:
    def __init__(self, rpi: RPi, todoist: Todoist):
        self._todoist = todoist
        self._rpi = rpi

    async def run(self) -> None:
        lcd: Lcd = await self._rpi.get_lcd(1)
        while True:
            due_tasks = await self._todoist.get_due_tasks()
            if due_tasks:
                await lcd.write_text_line(
                    f"Due tasks: {len(due_tasks)}", line=1
                )
            else:
                await lcd.write_text_line("No due tasks :)", line=1)

            inbox_tasks = await self._todoist.get_inbox_tasks()
            if inbox_tasks:
                await lcd.write_text_line(
                    f"Inbox tasks: {len(inbox_tasks)}", line=2
                )
            else:
                await lcd.write_text_line("No inbox tasks :)", line=2)

            await asyncio.sleep(5)


async def main() -> None:
    ind = TodoistIndicator(
        RPi(), Todoist(UserConfig(os.environ["TODOIST_API_TOKEN"]))
    )
    await ind.run()


if __name__ == "__main__":
    asyncio.run(main())
