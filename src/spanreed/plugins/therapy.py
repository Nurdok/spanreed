import asyncio
import spanreed
import datetime
import logging
from typing import List
import os
from spanreed.apis.todoist import Todoist, Task
from spanreed.user import User
import dateutil
import dateutil.tz
import dateutil.rrule
import yaml


logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


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


class TherapyPlugin(spanreed.plugin.Plugin):
    @property
    def name(self) -> str:
        return "Therapy"

    async def run_for_user(self, user: User):
        todoist_api = Todoist.for_user(user)
        tag = "spanreed/therapy"
        israel_tz = dateutil.tz.gettz("Asia/Jerusalem")
        dtstart = datetime.datetime.now(tz=israel_tz)
        recurrence = get_recurrence(dtstart)
        next_session: datetime.datetime = recurrence.after(dtstart)
        logger.info(f'{next_session=}')
        while True:
            wait_time = next_session - datetime.datetime.now(tz=israel_tz)
            logger.info(f'Waiting for {wait_time}')
            logger.info(f'{next_session.date().strftime("%Y-%m-%d")=}')
            await asyncio.sleep(wait_time.total_seconds())
            date_str = next_session.date().strftime("%Y-%m-%d")
            tasks: List[Task] = await todoist_api.get_tasks_with_tag(tag)
            if len(tasks) != 1:
                raise RuntimeError(f'Expected exactly one task with the tag {tag}, got {len(tasks)}')
            task, = tasks
            comment = await todoist_api.get_first_comment_with_yaml(task)
            desc_split = comment.content.split('---')
            logger.info(desc_split)
            assert len(desc_split) == 3, len(desc_split)
            comment_yaml = desc_split[1]
            logger.info(f'{comment_yaml=}')
            therapy_sd = yaml.safe_load(comment_yaml)
            dates: List[str] = therapy_sd['dates']

            if date_str not in dates:
                dates.append(date_str)
                total_cost = therapy_sd['session_cost'] * len(dates)
                therapy_sd['total_cost'] = total_cost
                new_task_content = f'Pay {therapy_sd["therapist"]} {total_cost}₪ for {", ".join(dates)}'
                new_comment_content = '---\n'.join([desc_split[0], yaml.safe_dump(therapy_sd), desc_split[2]])
                logger.info(f'{new_task_content=}\n{new_comment_content=}')
                await todoist_api.update_comment(comment, content=new_comment_content)
                await todoist_api.update_task(task, content=new_task_content)
                await todoist_api.set_due_date_to_today(task)





if __name__ == '__main__':
    asyncio.run(main(Todoist(os.environ['TODOIST_API_TOKEN'])))



