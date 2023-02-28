from todoist_api_python.api import TodoistAPI


class Todoist:
    def __init__(self):
        self._api = TodoistAPI("e0d98fafff6c08d1744bb1b1be926f5adac1db16")

    def _get_inbox_project(self):
        projects = self._api.get_projects()
        for project in projects:
            if project.inbox_project:
                return project

    def get_inbox_tasks(self):
        return self._api.get_tasks(project_id=self._get_inbox_project().id)

    def get_due_tasks(self):
        # "due before:+0 hours" means that we don't count tasks that have an
        # associated time-of-day that's in the future - this is because
        # due time-of-day usually indicates _start_ time, not due time.
        query = "overdue | (today & no time) | due before:+0 hours"
        return self._api.get_tasks(filter=query)
