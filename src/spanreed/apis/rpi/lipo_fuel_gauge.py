import asyncio

from spanreed.apis.rpi.i2c import I2cBus, I2cDevice
import enum


VCELL_REG = 0x02
SOC_REG = 0x04
MODE_REG = 0x06
VERSION_REG = 0x08
CONFIG_REG = 0x0C
COMMAND_REG = 0xFE


class LiPoFuelGauge:
    def __init__(self, i2c_bus: I2cBus, i2c_addr: int) -> None:
        self.device: I2cDevice = i2c_bus.get_i2c_device(i2c_addr)

    async def init(self) -> None:
        """Initialize the fuel gauge."""
        pass

    async def reset(self) -> None:
        await self.device.write_byte_data(COMMAND_REG, 0x54)
        await self.device.write_byte_data(COMMAND_REG + 1, 0x00)

    async def quick_start(self) -> None:
        await self.device.write_byte_data(MODE_REG, 0x00)
        await self.device.write_byte_data(MODE_REG + 1, 0x40)

    async def get_alert(self) -> None:
        # msb = await self.device.read_byte_data(CONFIG_REG)
        lsb = await self.device.read_byte_data(CONFIG_REG + 1)
        alert_status = (0x20 & lsb) >> 5
        alert_thd = 32 - (0x1F & lsb)
        print("alert status : {}".format(alert_status))
        print("alert thd : {}".format(alert_thd))

    async def get_config(self) -> None:
        msb = await self.device.read_byte_data(CONFIG_REG)
        lsb = await self.device.read_byte_data(CONFIG_REG + 1)
        config = (msb << 8) + lsb
        sleep_status = (0x80 & lsb) >> 7
        print("sleep : {}".format(sleep_status))
        print("config : {}".format(hex(config)))

    async def get_version(self) -> None:
        msb = await self.device.read_byte_data(VERSION_REG)
        lsb = await self.device.read_byte_data(VERSION_REG + 1)
        version = hex(0xFF * msb + lsb)
        print("version : {}".format(version))

    async def get_soc(self) -> None:
        msb = await self.device.read_byte_data(SOC_REG)
        lsb = await self.device.read_byte_data(SOC_REG + 1)
        percentage = (0xFF * msb + lsb) >> 8
        print("---")
        msb = await self.device.read_byte_data(VCELL_REG)
        lsb = await self.device.read_byte_data(VCELL_REG + 1)
        voltage = (msb << 8 + lsb) >> 4
        print("{} %, {} V".format(percentage, voltage))


async def main():
    gauge = LiPoFuelGauge(I2cBus(1), 0x36)
    await gauge.reset()
    await gauge.quick_start()
    await gauge.get_version()
    await gauge.get_config()
    await gauge.get_soc()
    while True:
        await gauge.get_soc()
        await gauge.get_alert()
        await asyncio.sleep(1)


if __name__ == "__main__":
    asyncio.run(main())
