import asyncio

from spanreed.apis.rpi.rpi import RPi, RgbLed


async def rgb_led_experiment():
    rpi = RPi()
    rgb_led = rpi.get_rgb_led(4, 27, 22)
    sequence = (0xFF0000, 0x00FF00, 0x0000FF, 0xFFFFFF, 0x000000)
    current = 0x000000
    for color in sequence:
        while current != color:
            if current < color:
                current += 1
            else:
                current -= 1

            rgb_led.set_color(current)
            await asyncio.sleep(0.1)


def main():
    asyncio.run(rgb_led_experiment())


if __name__ == "__main__":
    main()
