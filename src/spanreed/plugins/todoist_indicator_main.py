import asyncio
from spanreed.apis.todoist import Todoist, UserConfig, Task
from spanreed.apis.rpi.rpi import RPi
from spanreed.apis.rpi.i2c_lcd import Lcd
import os
from gpiozero import AngularServo

from typing import Generator


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
        self._todoist: Todoist = todoist
        self._rpi: RPi = rpi

        self._due_tasks: list[Task] = []
        self._inbox_tasks: list[Task] = []
        self._servo = AngularServo(
            pin=26,
            initial_angle=0,
            min_angle=0,
            max_angle=135,
            min_pulse_width=0.5 / 1000,
            max_pulse_width=2.3 / 1000,
            frame_width=25 / 1000,
        )

    async def update_display(self, lcd: Lcd) -> None:
        def tick_fn() -> Generator[str, None, None]:
            while True:
                yield "/"
                yield "%"

        tick = tick_fn()

        def format_line_with_tick(line: str) -> str:
            return line.ljust(Lcd.MAX_LINE_LENGTH - 1, " ")[
                : Lcd.MAX_LINE_LENGTH - 1
            ] + next(tick)

        while True:
            if self._due_tasks or self._inbox_tasks:
                due_line = f"Due tasks: {len(self._due_tasks)}"
                await lcd.write_text_line(
                    format_line_with_tick(due_line), line=1
                )

                inbox_line = f"Inbox tasks: {len(self._inbox_tasks)}"
                await lcd.write_text_line(inbox_line, trim=True, line=2)
            else:
                await lcd.write_text_line(
                    format_line_with_tick("YOU DA".center(16)), line=1
                )
                await lcd.write_text_line("REAL MVP".center(16), line=2)
            await asyncio.to_thread(self._update_servo)

    async def read_tasks_once(self) -> None:
        self._due_tasks = await self._todoist.get_due_tasks()
        self._inbox_tasks = await self._todoist.get_inbox_tasks()

    async def read_tasks(self) -> None:
        while True:
            await self.read_tasks_once()
            await asyncio.sleep(5)

    def _update_servo(self) -> None:
        if self._due_tasks:
            self._servo.angle = 90
        elif self._inbox_tasks:
            self._servo.angle = 90
        else:
            self._servo.angle = 90

    async def run(self) -> None:
        lcd: Lcd = await self._rpi.get_lcd(1)

        await self.read_tasks_once()

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self.update_display(lcd))
                tg.create_task(self.read_tasks())
        except BaseException:
            self._servo.angle = 0
            raise


async def main() -> None:
    ind = TodoistIndicator(
        RPi(), Todoist(UserConfig(os.environ["TODOIST_API_TOKEN"]))
    )
    await ind.run()


if __name__ == "__main__":
    asyncio.run(main())
