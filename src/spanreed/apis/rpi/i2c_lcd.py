import asyncio

from spanreed.apis.rpi.i2c import I2cBus, I2cDevice
import enum


class Command(enum.IntEnum):
    CLEAR_DISPLAY = 0x01
    RETURN_HOME = 0x02
    ENTRY_MODE_SET = 0x04
    DISPLAY_CONTROL = 0x08
    CURSOR_SHIFT = 0x10
    FUNCTION_SET = 0x20
    SET_CGRAM_ADDR = 0x40
    SET_DDRAM_ADDR = 0x80

    def with_flags(self, *flag: enum.IntEnum) -> int:
        """Add flags to the command."""
        value = self.value
        for f in flag:
            value |= f.value
        return value


class SetDdramAddrFlag(enum.IntEnum):
    """Flags for the Set DDRAM Address command."""

    LINE_1 = 0x00
    LINE_2 = 0x40


class EntryModeSetFlag(enum.IntEnum):
    """Flags for the Entry Mode Set command."""

    RIGHT = 0x00
    LEFT = 0x02
    SHIFT_INCREMENT = 0x01
    SHIFT_DECREMENT = 0x00


class DisplayControlFlag(enum.IntEnum):
    """Flags for the Display Control command."""

    DISPLAY_ON = 0x04
    DISPLAY_OFF = 0x00
    CURSOR_ON = 0x02
    CURSOR_OFF = 0x00
    BLINK_ON = 0x01
    BLINK_OFF = 0x00


class CursorShiftFlag(enum.IntEnum):
    """Flags for the Cursor Shift command."""

    DISPLAY_MOVE = 0x08
    CURSOR_MOVE = 0x00
    MOVE_RIGHT = 0x04
    MOVE_LEFT = 0x00


class FunctionSetFlag(enum.IntEnum):
    """Flags for the Function Set command."""

    EIGHT_BIT_MODE = 0x10
    FOUR_BIT_MODE = 0x00
    TWO_LINE = 0x08
    ONE_LINE = 0x00
    FIVE_BY_TEN_DOTS = 0x04
    FIVE_BY_EIGHT_DOTS = 0x00


class EnableBit(enum.IntEnum):
    """Flags for the Enable bit."""

    ENABLE = 0x04


class BacklightFlag(enum.IntEnum):
    """Flags for backlight control."""

    ON = 0x08
    OFF = 0x00


class ReadWriteBit(enum.IntEnum):
    """Flags for the Read/Write bit."""

    READ = 0x02
    WRITE = 0x00


class RegisterSelectBit(enum.IntEnum):
    """Flags for the Register Select bit."""

    COMMAND = 0x00
    DATA = 0x01


def to_single_byte(data: int) -> bytes:
    """Convert an integer to a single byte."""
    return data.to_bytes(1, "big")


class Lcd:
    DEFAULT_ADDRESS = 0x27
    MAX_LINE_LENGTH = 16

    def __init__(self, i2c_bus: I2cBus, i2c_addr: int) -> None:
        self.device = i2c_bus.get_i2c_device(i2c_addr)

    async def init(self) -> None:
        """Initialize the LCD."""
        # self.device.write(0x00, 0x38)
        await self._send_data(RegisterSelectBit.COMMAND, 0x03)
        await self._send_data(RegisterSelectBit.COMMAND, 0x03)
        await self._send_data(RegisterSelectBit.COMMAND, 0x03)
        await self._send_data(RegisterSelectBit.COMMAND, 0x02)

        await self._send_command(
            Command.FUNCTION_SET,
            FunctionSetFlag.FOUR_BIT_MODE,
            FunctionSetFlag.TWO_LINE,
            FunctionSetFlag.FIVE_BY_EIGHT_DOTS,
        )
        await self._send_command(
            Command.DISPLAY_CONTROL, DisplayControlFlag.DISPLAY_ON
        )
        await self._send_command(Command.CLEAR_DISPLAY)
        await self._send_command(Command.ENTRY_MODE_SET, EntryModeSetFlag.LEFT)
        await asyncio.sleep(1)

    async def _send_nibble(self, nibble: int) -> None:
        """Send a nibble to the LCD."""
        await self.device.write_byte(
            data=nibble | BacklightFlag.ON.value,
        )

        await self.device.write_byte(
            data=nibble | EnableBit.ENABLE | BacklightFlag.ON.value,
        )
        await asyncio.sleep(0.0005)
        await self.device.write_byte(
            data=(nibble & ~EnableBit.ENABLE) | BacklightFlag.ON.value,
        )
        await asyncio.sleep(0.0001)

    async def _send_data(self, register: RegisterSelectBit, data: int) -> None:
        """Send data to the LCD."""
        nibbles: tuple[int, int] = (
            ((data & 0xF0) | register.value),
            (((data << 4) & 0xF0) | register.value),
        )

        for nibble in nibbles:
            await self._send_nibble(nibble)

    async def _send_command(self, cmd: Command, *flag: enum.IntEnum) -> None:
        """Send a command to the LCD."""
        await self._send_data(RegisterSelectBit.COMMAND, cmd.with_flags(*flag))

    async def clear_display(self) -> None:
        """Clear the display."""
        await self._send_command(Command.CLEAR_DISPLAY)
        await self._send_command(Command.RETURN_HOME)

    async def write_text(self, text: list[str]) -> None:
        if len(text) > 2:
            raise ValueError("Only two lines supported")
        await self.write_text_line(text[0], 1)
        if len(text) > 1:
            await self.write_text_line(text[1], 2)

    async def write_text_line(
        self, text: str, line: int = 1, trim: bool = False
    ) -> None:
        line_flag = (
            SetDdramAddrFlag.LINE_1 if line == 1 else SetDdramAddrFlag.LINE_2
        )
        await self._send_command(Command.SET_DDRAM_ADDR, line_flag)

        text_ascii = text.encode("ascii")
        if len(text_ascii) > self.MAX_LINE_LENGTH:
            if not trim:
                raise ValueError(
                    f"Lines are capped at {self.MAX_LINE_LENGTH} chars, "
                    f"got {len(text_ascii)} ({text_ascii!r}). "
                    f"Trim your string manually or pass trim=True"
                )
            text_ascii = text_ascii[: self.MAX_LINE_LENGTH]

        # Pad the string to max length to "delete" the previous text.
        text_ascii = text_ascii.ljust(self.MAX_LINE_LENGTH, b" ")

        for char in text_ascii:
            await self._send_data(RegisterSelectBit.DATA, char)


async def main() -> None:
    i2c_bus = I2cBus(1)
    lcd = Lcd(i2c_bus, Lcd.DEFAULT_ADDRESS)
    await lcd.init()
    await lcd.write_text(["Hello", "World"])


if __name__ == "__main__":
    asyncio.run(main())
