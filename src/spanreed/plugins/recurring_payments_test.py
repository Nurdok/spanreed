import contextlib
import asyncio
import textwrap
import freezegun
from unittest.mock import MagicMock, patch, AsyncMock, call

from spanreed.plugins.recurring_payments import (
    RecurringPaymentsPlugin,
    UserConfig,
    RecurringPayment,
    RecurrenceInfo,
)
from spanreed.plugin import Plugin
from spanreed.test_utils import (
    mock_user_find_by_id,
    EndPluginRun,
    patch_telegram_bot,
)
from spanreed.apis.todoist import Task, Comment, Project
from spanreed.user import User
import dateutil


def test_name() -> None:
    Plugin.reset_registry()
    plugin = RecurringPaymentsPlugin()

    assert plugin.name() == "Recurring Payments"
    assert plugin.canonical_name() == "recurring-payments"


@patch_telegram_bot("spanreed.plugins.recurring_payments")
@patch("spanreed.plugins.recurring_payments.Todoist", autospec=True)
def test_ask_for_user_config(
    mock_bot: AsyncMock,
    mock_todoist: AsyncMock,
) -> None:
    Plugin.reset_registry()
    plugin = RecurringPaymentsPlugin()

    with patch.object(
        User,
        "find_by_id",
        new=AsyncMock(side_effect=mock_user_find_by_id),
    ):
        mock_user = asyncio.run(User.find_by_id(4))

        def fake_user_input(prompt: str) -> str:
            if "unique label" in prompt:
                return "spanreed/recurring"

            if "enter a template" in prompt:
                return "Pay {{total_cost}} for {{dates}}"

            if "cost of a single recurrence" in prompt:
                return "100"

            if "hour" in prompt:
                return "14"

            if "minute" in prompt:
                return "50"

            assert False, f"Unexpected prompt: {prompt}"

        mock_bot.request_user_input.side_effect = fake_user_input

        def fake_user_choice(prompt: str, choices: list[str]) -> int:
            if "default date format" in prompt:
                assert choices == ["Keep as-is", "Change it"]
                return 0

            if "timezone" in prompt:
                return 2  # Asia/Jerusalem

            if "frequency" in prompt:
                return 0  # Weekly

            if "week start day" in prompt:
                return dateutil.rrule.SU.weekday

            if "week day" in prompt:
                return dateutil.rrule.TU.weekday

            if "add another?" in prompt:
                assert choices == ["Yes", "No"]
                return 1

            if "verify" in prompt:
                assert choices == ["Yes", "No"]
                return 1

            if "Obsidian" in prompt:
                assert choices == ["Yes", "No"]
                return 1

            if "project" in prompt:
                assert choices == ["Project A", "Project B"]
                return 0

            assert False, f"Unexpected prompt: {prompt}"

        mock_bot.request_user_choice.side_effect = fake_user_choice
        mock_set_config = AsyncMock(name="set_config")

        project_a = MagicMock(spec=Project, id="pid-A")
        project_a.name = "Project A"
        project_b = MagicMock(spec=Project, id="pid-B")
        project_b.name = "Project B"

        mock_todoist.for_user.return_value.get_projects.return_value = [
            project_a,
            project_b,
        ]

        with patch.object(
            RecurringPaymentsPlugin,
            "set_config",
            new=mock_set_config,
        ), patch.object(
            RecurringPaymentsPlugin,
            "run_for_user",
        ) as mock_run_for_user:
            asyncio.run(plugin.ask_for_user_config(mock_user))

            assert mock_set_config.call_count == 1
            assert mock_set_config.call_args_list[0] == call(
                mock_user,
                UserConfig(
                    [
                        RecurringPayment(
                            todoist_label="spanreed/recurring",
                            todoist_task_template="Pay {{total_cost}} for {{dates}}",
                            date_format="%Y-%m-%d",
                            recurrence_cost=100.0,
                            recurrence_info=RecurrenceInfo(
                                timezone="Asia/Jerusalem",
                                frequency=dateutil.rrule.WEEKLY,
                                week_start_day=dateutil.rrule.SU.weekday,
                                week_day=dateutil.rrule.TU.weekday,
                                hour=14,
                                minute=50,
                                second=0,
                            ),
                            todoist_project_id="pid-A",
                        )
                    ],
                ),
            )
            assert mock_run_for_user.call_count == 1
            mock_run_for_user.assert_called_once_with(mock_user)


@freezegun.freeze_time("2021-01-19")
@patch("spanreed.plugins.recurring_payments.Todoist", autospec=True)
@patch("asyncio.sleep", autospec=True)
@patch_telegram_bot("spanreed.plugins.recurring_payments")
def test_run_for_single_recurrence(
    mock_bot: AsyncMock, mock_sleep: AsyncMock, mock_todoist: AsyncMock
) -> None:
    Plugin.reset_registry()
    plugin = RecurringPaymentsPlugin()

    user: MagicMock = mock_user_find_by_id(3)

    recurring_payment = RecurringPayment(
        todoist_label="spanreed/recurring",
        todoist_task_template="Pay {{total_cost}} for {{dates}}",
        date_format="%Y-%m-%d",
        recurrence_cost=100.0,
        recurrence_info=RecurrenceInfo(
            timezone="Asia/Jerusalem",
            frequency=dateutil.rrule.WEEKLY,
            week_start_day=dateutil.rrule.SU.weekday,
            week_day=dateutil.rrule.TU.weekday,
            hour=14,
            minute=50,
            second=0,
        ),
        verify_recurrence=True,
        todoist_project_id="pid",
    )
    task = MagicMock(name="task", spec=Task)
    # TODO: change to existing task
    mock_todoist.for_user.return_value.get_tasks_with_label.return_value = [
        task
    ]
    comment = MagicMock(name="comment", spec=Comment)
    comment.content = textwrap.dedent(
        """\
            ---
            dates:
                - "2021-01-05"
                - "2021-01-12"
            ---
        """
    )
    mock_todoist.for_user.return_value.get_first_comment_with_yaml.return_value = (
        comment
    )

    # "Yes", for adding the current date to the task.
    mock_bot.request_user_choice.return_value = 0

    mock_sleep.side_effect = ["", EndPluginRun]
    with contextlib.suppress(EndPluginRun):
        asyncio.run(plugin.run_for_single_recurrence(user, recurring_payment))

    mock_todoist.for_user.return_value.update_task.assert_called_once_with(
        task,
        content="Pay 300 for 2021-01-05, 2021-01-12, 2021-01-19",
        project_id="pid",
    )

    mock_bot.request_user_choice.assert_called_once()
    assert (
        "add it to the list" in mock_bot.request_user_choice.call_args.args[0]
    )
