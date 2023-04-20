import asyncio
import datetime
from typing import Optional
from spanreed.apis.todoist import Todoist, Task, TodoistPlugin
from spanreed.apis.telegram_bot import TelegramBotApi
from spanreed.user import User
from spanreed.plugin import Plugin
from dataclasses import dataclass, asdict
import dateutil
import dateutil.tz
import dateutil.rrule
import yaml


@dataclass
class RecurringPayment:
    todoist_label: str
    todoist_task_template: str
    date_format: str
    recurrence_cost: float

    # Recurrence configuration, see dateutil.rrule.rrule.
    timezone: str
    frequency: int  # See dateutil.rrule.FREQUENCIES
    week_start_day: int  # See dateutil.rrule.WEEKDAYS
    week_day: int  # See dateutil.rrule.WEEKDAYS
    hour: int
    minute: int


@dataclass
class UserConfig:
    recurring_payments: list[RecurringPayment]


# TODO: recurrences should be configurable per-user.
def get_recurrence(dtstart: datetime.datetime) -> dateutil.rrule.rrule:
    return dateutil.rrule.rrule(
        dtstart=dtstart,
        freq=dateutil.rrule.WEEKLY,
        wkst=dateutil.rrule.SU,
        byweekday=dateutil.rrule.WE,
        byhour=14,
        byminute=0,
        bysecond=0,
    )


class TherapyPlugin(Plugin):
    @property
    def name(self) -> str:
        return "Therapy"

    def has_user_config(self) -> bool:
        return True

    async def ask_for_user_config(self, user: User):
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)
        recurring_payments: list[RecurringPayment] = []
        while True:
            if recurring_payments:
                choice = await bot.request_user_choice(
                    f"You currently have {len(recurring_payments)} recurring "
                    f"payments.\nWould you like to add another?",
                    ["Yes", "No"],
                )
                if choice == 1:
                    break

            todoist_label = await bot.request_user_input(
                "Please enter a unique label of the Todoist task where you'd"
                " like to keep track of payments:"
            )
            choice = await bot.request_user_choice(
                "The default date format is %Y-%m-%d. Would you like to "
                "change it?",
                ["Keep as-is", "Change it"],
            )
            if choice == 1:
                date_format = await bot.request_user_input(
                    "Please enter the date format:"
                )
            else:
                date_format = "%Y-%m-%d"

            todoist_task_template = await bot.request_user_input(
                "Please enter a template for the Todoist task name.\n"
                "You can use the following optional placeholders:\n"
                "  <b>{{dates}}</b>: A list of all unpaid dates.\n"
                "  <b>{{total_cost}}</b>: The total cost of all unpaid dates.\n"
                "\n"
                "Examples: \n"
                '  "Pay therapist ${{total_cost}} (for {{dates}})"\n'
                '  "Give my daughter her allowance ({{total_cost}}₪)"\n'
            )

            recurrence_cost = float(
                await bot.request_user_input(
                    "Please enter the cost of a single recurrence (you"
                    " can enter 0 if you only:"
                )
            )

            # TODO: This is horrible - improve.
            timezone = await bot.request_user_choice(
                "Please choose your timezone:",
                ["America/New_York", "Asia/Jerusalem"],
            )

            frequency = await bot.request_user_choice(
                "Please choose the frequency of the recurrence:",
                [freq.capitalize() for freq in dateutil.rrule.FREQNAMES],
            )

            week_start_day = await bot.request_user_choice(
                "Please choose the week start day:",
                [day.capitalize() for day in dateutil.rrule.weekdays],
            )

            weekdays = {
                "Monday": dateutil.rrule.MO,
                "Tuesday": dateutil.rrule.TU,
                "Wednesday": dateutil.rrule.WE,
                "Thursday": dateutil.rrule.TH,
                "Friday": dateutil.rrule.FR,
                "Saturday": dateutil.rrule.SA,
                "Sunday": dateutil.rrule.SU,
            }
            weekday_choices = weekdays.keys()

            week_day = await bot.request_user_choice(
                "Please choose the week day:", weekdays
            )

            hour = int(
                await bot.request_user_input(
                    "Please enter the hour of the day (0-23):"
                )
            )

            minute = int(
                await bot.request_user_input(
                    "Please enter the minute of the day (0-59):"
                )
            )

            recurring_payments.append(
                RecurringPayment(
                    todoist_label=todoist_label,
                    todoist_task_template=todoist_task_template,
                    date_format=date_format,
                    recurrence_cost=recurrence_cost,
                    timezone=timezone,
                    frequency=frequency,
                    week_start_day=week_start_day,
                    week_day=week_day,
                    hour=hour,
                    minute=minute,
                )
            )
        return UserConfig(recurring_payments)

    @classmethod
    def get_prerequisites(cls) -> list[type[Plugin]]:
        return [TodoistPlugin]

    async def run_for_user(self, user: User):
        todoist_api = Todoist.for_user(user)
        tag = "spanreed/therapy"
        israel_tz = dateutil.tz.gettz("Asia/Jerusalem")
        dtstart = datetime.datetime.now(tz=israel_tz)
        recurrence = get_recurrence(dtstart)
        next_session: datetime.datetime = recurrence.after(dtstart)
        self._logger.info(f"{next_session=}")
        while True:
            wait_time = next_session - datetime.datetime.now(tz=israel_tz)
            self._logger.info(f"Waiting for {wait_time}")
            self._logger.info(f'{next_session.date().strftime("%Y-%m-%d")=}')
            await asyncio.sleep(wait_time.total_seconds())
            date_str = next_session.date().strftime("%Y-%m-%d")
            tasks: list[Task] = await todoist_api.get_tasks_with_tag(tag)
            if len(tasks) != 1:
                raise RuntimeError(
                    f"Expected exactly one task with the tag {tag}, got {len(tasks)}"
                )
            (task,) = tasks
            comment = await todoist_api.get_first_comment_with_yaml(task)
            desc_split = comment.content.split("---")
            self._logger.info(desc_split)
            assert len(desc_split) == 3, len(desc_split)
            comment_yaml = desc_split[1]
            self._logger.info(f"{comment_yaml=}")
            therapy_sd = yaml.safe_load(comment_yaml)
            dates: list[str] = therapy_sd["dates"]

            if date_str not in dates:
                dates.append(date_str)
                total_cost = therapy_sd["session_cost"] * len(dates)
                therapy_sd["total_cost"] = total_cost
                new_task_content = f'Pay {therapy_sd["therapist"]} {total_cost}₪ for {", ".join(dates)}'
                new_comment_content = "---\n".join(
                    [desc_split[0], yaml.safe_dump(therapy_sd), desc_split[2]]
                )
                self._logger.info(
                    f"{new_task_content=}\n{new_comment_content=}"
                )
                await todoist_api.update_comment(
                    comment, content=new_comment_content
                )
                await todoist_api.update_task(task, content=new_task_content)
                await todoist_api.set_due_date_to_today(task)
