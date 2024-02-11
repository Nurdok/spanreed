import asyncio

from spanreed.apis.rpi.rpi import RPi, RgbLed


async def rgb_led_experiment():
    rpi = RPi()
    rgb_led = rpi.get_rgb_led(4, 27, 22)
    while True:
        rgb_led.set_color(0xFF0000)
        await asyncio.sleep(1)
        rgb_led.set_color(0x00FF00)
        await asyncio.sleep(1)
        rgb_led.set_color(0x0000FF)
        await asyncio.sleep(1)
        rgb_led.set_color(0xFFFFFF)
        await asyncio.sleep(1)
        rgb_led.set_color(0x000000)
        await asyncio.sleep(1)


def main():
    asyncio.run(rgb_led_experiment())


if __name__ == "__main__":
    main()
