from todoist import Todoist
from rpi import RPi
import time
import os


class TodoistIndicator:
    def __init__(self, rpi, todoist):
        self._todoist = todoist
        self._red = rpi.get_led(4)
        self._yellow = rpi.get_led(17)
        self._green = rpi.get_led(18)

    def run(self):
        while True:
            if self._todoist.get_due_tasks():
                self._red.turn_on()
                self._yellow.turn_off()
                self._green.turn_off()
            elif self._todoist.get_inbox_tasks():
                self._red.turn_off()
                self._yellow.turn_on()
                self._green.turn_off()
            else:
                self._red.turn_off()
                self._yellow.turn_off()
                self._green.turn_on()
            time.sleep(5)


if __name__ == '__main__':
    ind = TodoistIndicator(RPi(), Todoist(os.environ['TODOIST_API_TOKEN'])
    ind.run()
