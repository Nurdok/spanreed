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
    """API for interacting with a MAX17043 LiPo fuel gauge.

    Note:
        MAX17044 is currently not supported. It has different units of
        measurement for voltage and battery percentage. It should be easy to
        add support for it if needed.

    """

    DEFAULT_ADDRESS = 0x36

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

    async def get_percentage(self) -> int:
        msb = await self.device.read_byte_data(SOC_REG)
        lsb = await self.device.read_byte_data(SOC_REG + 1)

        # According to the datasheet:
        # > The SOC register is a read-only register that displays the state of
        # > charge of the cell as calculated by the ModelGauge algorithm.
        # > Units of % can be directly determined by observing only the high
        # > byte of the SOC register. The low byte provides additional
        # resolution in units 1/256%.
        return msb + (lsb / 256)

    async def get_voltage_mv(self) -> int:
        msb = await self.device.read_byte_data(VCELL_REG)
        lsb = await self.device.read_byte_data(VCELL_REG + 1)

        # According to the datasheet:
        # > Battery voltage is measured at the CELL pin input with
        # > respect to GND over a 0 to 5.00V range for the
        # > MAX17043 resolutions of 1.25mV and 2.50mV, respectively.
        # In short, we need to multiply the 16bit register value by 1.25.
        return (((msb << 8) + lsb) >> 4) * 1.25


async def main():
    gauge = LiPoFuelGauge(I2cBus(1), 0x36)
    while True:
        percentage: int = await gauge.get_percentage()
        voltage: int = await gauge.get_voltage_mv()
        print(f"{percentage:.4}%, {voltage/1000:.3}V")
        await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(main())
