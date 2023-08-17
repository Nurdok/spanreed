import asyncio

# smbus2 is a pure Python, drop-in replacement of the smbus package.
import smbus2 as smbus
from typing import Optional


class I2cBus:
    def __init__(self, i2c_bus_port: int) -> None:
        self.i2c_bus_port: int = i2c_bus_port
        self.lock: asyncio.Lock = asyncio.Lock()
        self.smbus: Optional[smbus.SMBus] = None

    def create_sync_smbus(self) -> None:
        """Create a sync SMBus."""
        self.smbus = smbus.SMBus(self.i2c_bus_port)

    async def ensure_bus_connection(self) -> None:
        if self.smbus is None:
            await asyncio.to_thread(self.create_sync_smbus)

        if self.smbus is None:
            raise RuntimeError("SMBus not initialized.")

    def get_i2c_device(self, i2c_addr: int) -> "I2cDevice":
        return I2cDevice(self, i2c_addr)

    async def write_byte(
        self, i2c_addr: int, data: int
    ) -> None:
        """Write bytes to a given register."""
        print(f"{data=:02X}")
        await self.ensure_bus_connection()
        assert self.smbus is not None
        async with self.lock:
            return await asyncio.to_thread(
                self.smbus.write_byte,
                i2c_addr,
                data,
            )


class I2cDevice:
    def __init__(self, controller: I2cBus, i2c_addr: int) -> None:
        self.controller: I2cBus = controller
        self.i2c_addr: int = i2c_addr

    async def write_byte(self, data: int) -> None:
        await self.controller.write_byte(self.i2c_addr, data)
