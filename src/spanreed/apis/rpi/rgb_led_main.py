import asyncio
import random

from spanreed.apis.rpi.rpi import RPi, RgbLed


async def rgb_led_experiment():
    rpi = RPi()
    rgb_led = rpi.get_rgb_led(4, 27, 22)
    while True:
        rgb_led._set_color(
            random.randrange(0, 255),
            random.randrange(0, 255),
            random.randrange(0, 255),
        )
        await asyncio.sleep(1)


def main():
    asyncio.run(rgb_led_experiment())


if __name__ == "__main__":
    main()
