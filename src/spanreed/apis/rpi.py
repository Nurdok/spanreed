import RPi.GPIO as GPIO


class RPi:
    def __init__(self):
        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False)
        self._gpio_pins = {}

    def get_led(self, gpio_pin):
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
