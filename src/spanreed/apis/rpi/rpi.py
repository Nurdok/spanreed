# mypy: ignore-errors
import RPi.GPIO as GPIO
import smbus2 as smbus


class RPi:
    def __init__(self):
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        self._gpio_pins = {}

    def get_led(self, gpio_pin) -> "Led":
        if gpio_pin in self._gpio_pins:
            return self._gpio_pins[gpio_pin]
        return Led(gpio_pin)


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


class I2cDevice:
    def __init__(self, i2c_address, bus_port=1):
        self.addr = i2c_address
        self.bus = smbus.SMBus(bus_port)

    def write_cmd(self, cmd):
        self.bus.write_byte(self.addr, cmd)
        # sleep(0.0001)

    def write_cmd_arg(self, cmd, data):
        self.bus.write_byte_data(self.addr, cmd, data)
        # sleep(0.0001)

    def write_block_data(self, cmd, data):
        self.bus.write_block_data(self.addr, cmd, data)
        # sleep(0.0001)

    def read(self):
        return self.bus.read_byte(self.addr)

    def read_data(self, cmd):
        return self.bus.read_byte_data(self.addr, cmd)

    def read_block_data(self, cmd):
        return self.bus.read_block_data(self.addr, cmd)
