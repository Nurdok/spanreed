# mypy: ignore-errors
import RPi.GPIO as GPIO
import smbus2 as smbus
from spanreed.apis.rpi.i2c_lcd import I2cBus, Lcd


class RPi:
    def __init__(self) -> None:
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        self._gpio_pins = {}

    def get_led(self, gpio_pin) -> "Led":
        if gpio_pin in self._gpio_pins:
            return self._gpio_pins[gpio_pin]
        return Led(gpio_pin)

    async def get_lcd(self, bus_port) -> "Lcd":
        lcd = Lcd(I2cBus(bus_port), Lcd.DEFAULT_ADDRESS)
        await lcd.init()
        return lcd


class Led:
    def __init__(self, gpio_pin):
        self._gpio_pin = gpio_pin
        self._state = GPIO.LOW
        GPIO.setup(gpio_pin, GPIO.OUT)

    def _set_state(self, state):
        self._state = state
        GPIO.output(self._gpio_pin, state)

    def turn_on(self):
        self._set_state(GPIO.HIGH)

    def turn_off(self):
        self._set_state(GPIO.LOW)

    def toggle(self):
        new_state = GPIO.HIGH if self._state == GPIO.LOW else GPIO.HIGH
        self._set_state(new_state)
