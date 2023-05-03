import asyncio
import logging
import datetime
from dataclasses import dataclass

import dateutil
import dateutil.tz
import dateutil.rrule
import yaml
import jinja2

from spanreed.apis.todoist import Todoist, Task, TodoistPlugin
from spanreed.apis.telegram_bot import TelegramBotApi
from spanreed.user import User
from spanreed.plugin import Plugin


@dataclass
class RecurrenceInfo:
    # These fields correspond to dateutil.rrule.rrule parameters.
    timezone: str
    frequency: int  # See dateutil.rrule.FREQUENCIES
    week_start_day: int  # See dateutil.rrule.WEEKDAYS
    week_day: int  # See dateutil.rrule.WEEKDAYS
    hour: int
    minute: int
    second: int

    @property
    def tzinfo(self) -> datetime.tzinfo:
        if (tz := dateutil.tz.gettz(self.timezone)) is None:
            raise ValueError(f"Invalid timezone: {self.timezone}")
        return tz


@dataclass
class RecurringPayment:
    todoist_label: str
    todoist_task_template: str
    date_format: str
    recurrence_cost: float
    recurrence_info: RecurrenceInfo
    verify_recurrence: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.recurrence_info, dict):
            self.recurrence_info = RecurrenceInfo(**self.recurrence_info)


@dataclass
class UserConfig:
    recurring_payments: list[RecurringPayment]

    def __post_init__(self) -> None:
        for index, recurring_payment in enumerate(self.recurring_payments):
            if isinstance(recurring_payment, dict):
                self.recurring_payments[index] = RecurringPayment(
                    **recurring_payment
                )


class RecurringPaymentsPlugin(Plugin[UserConfig]):
    @classmethod
    def name(cls) -> str:
        return "Recurring Payments"

    @classmethod
    def has_user_config(cls) -> bool:
        return True

    @classmethod
    def get_config_class(cls) -> type[UserConfig]:
        return UserConfig

    @classmethod
    async def ask_for_user_config(cls, user: User) -> None:
        bot: TelegramBotApi = await TelegramBotApi.for_user(user)
        recurring_payments: list[RecurringPayment] = []
        logger = logging.getLogger(__name__)
        while True:
            logger.info(
                "Asking for recurring payment. Current: %s", recurring_payments
            )
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
                "  <b>{{total_cost}}</b>: The total cost of unpaid dates.\n"
                "\n"
                "Examples: \n"
                '  "Pay therapist ${{total_cost}} (for {{dates}})"\n'
                '  "Give my daughter her allowance ({{total_cost}}₪)"\n'
            )

            recurrence_cost = float(
                await bot.request_user_input(
                    "Please enter the cost of a single recurrence (you"
                    " can enter 0 if you only want to accumulate the dates):"
                )
            )

            timezones = [
                "Africa/Abidjan",
                "America/New_York",
                "Asia/Jerusalem",
            ]

            # TODO: This is horrible - improve.
            # TODO: use this maybe:
            # import pytz
            # pytz.all_timezones

            timezone: str = timezones[
                await bot.request_user_choice(
                    "Please choose your timezone:", timezones
                )
            ]

            frequencies: dict[str, int] = {
                "Weekly": dateutil.rrule.WEEKLY,
            }
            frequency_choices = sorted(
                frequencies.keys(), key=lambda x: frequencies[x]
            )

            frequency: int = frequencies[
                frequency_choices[
                    await bot.request_user_choice(
                        "Please choose the frequency of the recurrence:",
                        [freq.capitalize() for freq in frequencies],
                    )
                ]
            ]

            weekdays: dict[str, dateutil.rrule.weekday] = {
                "Monday": dateutil.rrule.MO,
                "Tuesday": dateutil.rrule.TU,
                "Wednesday": dateutil.rrule.WE,
                "Thursday": dateutil.rrule.TH,
                "Friday": dateutil.rrule.FR,
                "Saturday": dateutil.rrule.SA,
                "Sunday": dateutil.rrule.SU,
            }

            weekday_choices = sorted(
                weekdays.keys(), key=lambda x: weekdays[x].weekday
            )

            week_start_day = weekdays[
                weekday_choices[
                    await bot.request_user_choice(
                        "Please choose the week start day:", weekday_choices
                    )
                ]
            ]

            week_day = weekdays[
                weekday_choices[
                    await bot.request_user_choice(
                        "Please choose the week day:", weekday_choices
                    )
                ]
            ]

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
                    recurrence_info=RecurrenceInfo(
                        timezone=timezone,
                        frequency=frequency,
                        week_start_day=week_start_day.weekday,
                        week_day=week_day.weekday,
                        hour=hour,
                        minute=minute,
                        second=0,
                    ),
                )
            )
        self = await cls.get_plugin_by_class(cls)
        self._logger.info("Setting config")

        await self.set_config(user, UserConfig(recurring_payments))
        asyncio.create_task(self.run_for_user(user))

    @classmethod
    def get_prerequisites(cls) -> list[type[Plugin]]:
        return [TodoistPlugin]

    @staticmethod
    def get_timezone_from_string(timezone: str) -> datetime.tzinfo:
        if (tz := dateutil.tz.gettz(timezone)) is None:
            raise ValueError(f"Invalid timezone: {timezone}")
        return tz

    @staticmethod
    def get_recurrence(recurrence: RecurrenceInfo) -> dateutil.rrule.rrule:
        return dateutil.rrule.rrule(
            dtstart=datetime.datetime.now(tz=recurrence.tzinfo),
            freq=recurrence.frequency,
            wkst=recurrence.week_start_day,
            byweekday=recurrence.week_day,
            byhour=recurrence.hour,
            byminute=recurrence.minute,
            bysecond=recurrence.second,
        )

    async def run_for_single_recurrence(
        self, user: User, recurring_payment: RecurringPayment
    ) -> None:
        todoist_api: Todoist = await Todoist.for_user(user)

        def now() -> datetime.datetime:
            return datetime.datetime.now(
                tz=recurring_payment.recurrence_info.tzinfo
            )

        self._logger.info(f"{now()=}")
        recurrence = self.get_recurrence(recurring_payment.recurrence_info)
        skip_current_date = False

        while True:
            next_event: datetime.datetime = recurrence.after(now())
            wait_time = next_event - now()
            self._logger.info(f"Waiting for {wait_time}")
            self._logger.info(f'{next_event.date().strftime("%Y-%m-%d")=}')
            await asyncio.sleep(wait_time.total_seconds())
            date_str = next_event.date().strftime("%Y-%m-%d")

            tasks: list[Task] = await todoist_api.get_tasks_with_label(
                recurring_payment.todoist_label
            )
            if len(tasks) == 1:
                (task,) = tasks
            elif len(tasks) == 0:
                self._logger.info("Creating new task")
                task = await todoist_api.add_task(
                    content="placeholder",
                    labels=[recurring_payment.todoist_label],
                )
            else:
                raise RuntimeError(
                    f"Expected either zero or exactly one task with the label"
                    f" {recurring_payment.todoist_label}, got {len(tasks)}"
                )

            comment = await todoist_api.get_first_comment_with_yaml(
                task, create=True
            )
            desc_split = comment.content.split("---")
            self._logger.info(desc_split)
            assert len(desc_split) == 3, len(desc_split)
            comment_yaml = desc_split[1]
            self._logger.info(f"{comment_yaml=}")
            sd = yaml.safe_load(comment_yaml) or {}
            dates: list[str] = sd.setdefault("dates", [])

            if date_str not in dates:
                if recurring_payment.verify_recurrence:
                    bot: TelegramBotApi = await TelegramBotApi.for_user(user)
                    async with bot.user_interaction():
                        choice: int = await bot.request_user_choice(
                            f"You have a recurring payment of {recurring_payment.recurrence_cost}"
                            f" due now (for task with"
                            f" label {recurring_payment.todoist_label}).\n"
                            f"Would you like to add it to the list of dates?",
                            ["Yes (add)", "No (skip)"],
                        )
                    # TODO: this might cause re-asking for the same date
                    if choice == 1:
                        continue

                dates.append(date_str)
                total_cost = recurring_payment.recurrence_cost * len(dates)
                if total_cost.is_integer():
                    total_cost = int(total_cost)

                env = jinja2.Environment()
                template_params = dict(
                    dates=", ".join(dates),
                    total_cost=total_cost,
                )
                new_task_content = env.from_string(
                    recurring_payment.todoist_task_template
                ).render(template_params)

                new_comment_content = "---\n".join(
                    [desc_split[0], yaml.safe_dump(sd), desc_split[2]]
                )
                self._logger.info(
                    f"{new_task_content=}\n{new_comment_content=}"
                )
                await todoist_api.update_comment(
                    comment, content=new_comment_content
                )
                await todoist_api.update_task(task, content=new_task_content)
                await todoist_api.set_due_date_to_today(task)

    async def run_for_user(self, user: User) -> None:
        user_config: UserConfig = await self.get_config(user)
        self._logger.info(f"{user_config=}")

        for recurring_payment in user_config.recurring_payments:
            asyncio.create_task(
                self.run_for_single_recurrence(user, recurring_payment)
            )
