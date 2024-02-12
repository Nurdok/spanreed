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

    def get_rgb_led(self, red_pin, green_pin, blue_pin) -> "RgbLed":
        return RgbLed(red_pin, green_pin, blue_pin)

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


class RgbLed:
    SIGNAL_FREQ_HZ = 75

    def __init__(self, red_pin, green_pin, blue_pin):
        pins = (red_pin, green_pin, blue_pin)
        self._rgb_pwms: list[GPIO.PWM] = []

        for pin in pins:
            GPIO.setup(pin, GPIO.OUT)
            pwm = GPIO.PWM(pin, self.SIGNAL_FREQ_HZ)
            self._rgb_pwms.append(pwm)
            pwm.start(0)

    def get_duty_cycle_for_numeric_color(self, color):
        # Convert 0-255 color to 0-100% duty cycle
        return color * 100 / 255

    def set_color(self, hex_color: int | str):
        if isinstance(hex_color, int):
            hex_color = hex(hex_color)
        if hex_color.startswith("#"):
            hex_color = hex_color[1:]
        if hex_color.startswith("0x"):
            hex_color = hex_color[2:]
        hex_color = hex_color.zfill(6)
        print(f"Setting color: {hex_color!r}")
        self._set_color(
            *tuple(int(hex_color[i : i + 2], 16) for i in (0, 2, 4))
        )

    def set_rgb_color(self, red, green, blue):
        for pwm, value in zip(self._rgb_pwms, (red, green, blue)):
            print(
                f"Setting value: {self.get_duty_cycle_for_numeric_color(value)}"
            )
            pwm.ChangeDutyCycle(self.get_duty_cycle_for_numeric_color(value))

    def turn_off(self):
        for pwm in self._rgb_pwms:
            pwm.stop()
