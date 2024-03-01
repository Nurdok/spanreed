import RPi.GPIO as GPIO
from spanreed.apis.rpi.i2c_lcd import I2cBus, Lcd
from spanreed.apis.rpi.lipo_fuel_gauge import LiPoFuelGauge
from spanreed.apis.rpi.led import Led, RgbLed


class RPi:
    def __init__(self) -> None:
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)

    def get_led(self, gpio_pin: int) -> "Led":
        return Led(gpio_pin)

    def get_rgb_led(
        self, red_pin: int, green_pin: int, blue_pin: int
    ) -> "RgbLed":
        return RgbLed(red_pin, green_pin, blue_pin)

    async def get_lcd(self, bus_port: int) -> "Lcd":
        lcd = Lcd(I2cBus(bus_port), Lcd.DEFAULT_ADDRESS)
        await lcd.init()
        return lcd

    def get_lipo_fuel_gauge(self, bus_port: int) -> "LiPoFuelGauge":
        return LiPoFuelGauge(I2cBus(bus_port), LiPoFuelGauge.DEFAULT_ADDRESS)
