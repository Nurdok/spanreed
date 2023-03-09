import os
from todoist import Todoist
import logging
import sys


def log_to_stdout():
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    root.addHandler(handler)


if __name__ == '__main__':
    log_to_stdout()
    todoist = Todoist(os.environ['TODOIST_API_TOKEN'])
    tasks = todoist.get_tasks_with_tag('spanreed/nooverdue')
    for task in tasks:
        todoist.set_due_date_to_today(task)



