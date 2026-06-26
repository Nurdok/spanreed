import logging
from dataclasses import dataclass, field

import aiohttp

from spanreed.user import User

BASE_URL = "https://api.hevyapp.com/v1"
# Hevy caps the page size at 10 for the workout endpoints.
MAX_PAGE_SIZE = 10


@dataclass
class WorkoutSet:
    index: int
    type: str
    weight_kg: float | None
    reps: int | None
    distance_meters: float | None
    duration_seconds: int | None
    rpe: float | None

    @classmethod
    def from_json(cls, data: dict) -> "WorkoutSet":
        return cls(
            index=data.get("index", 0),
            type=data.get("type", "normal"),
            weight_kg=data.get("weight_kg"),
            reps=data.get("reps"),
            distance_meters=data.get("distance_meters"),
            duration_seconds=data.get("duration_seconds"),
            rpe=data.get("rpe"),
        )


@dataclass
class Exercise:
    index: int
    title: str
    notes: str
    exercise_template_id: str
    supersets_id: int | None
    sets: list[WorkoutSet] = field(default_factory=list)

    @classmethod
    def from_json(cls, data: dict) -> "Exercise":
        return cls(
            index=data.get("index", 0),
            title=data.get("title", ""),
            notes=data.get("notes", "") or "",
            exercise_template_id=data.get("exercise_template_id", ""),
            supersets_id=data.get("supersets_id"),
            sets=[WorkoutSet.from_json(s) for s in data.get("sets", [])],
        )


@dataclass
class Workout:
    id: str
    title: str
    description: str
    start_time: str
    end_time: str
    updated_at: str
    created_at: str
    exercises: list[Exercise] = field(default_factory=list)

    @classmethod
    def from_json(cls, data: dict) -> "Workout":
        return cls(
            id=data["id"],
            title=data.get("title", "") or "Workout",
            description=data.get("description", "") or "",
            start_time=data.get("start_time", ""),
            end_time=data.get("end_time", ""),
            updated_at=data.get("updated_at", ""),
            created_at=data.get("created_at", ""),
            exercises=[
                Exercise.from_json(e) for e in data.get("exercises", [])
            ],
        )


class HevyApi:
    def __init__(self, api_key: str):
        self._api_key = api_key
        self._logger = logging.getLogger(__name__)

    @classmethod
    async def for_user(cls, user: User) -> "HevyApi":
        from spanreed.plugins.hevy import HevyPlugin

        config = await HevyPlugin.get_config(user)
        return cls(config.api_key)

    @property
    def _headers(self) -> dict[str, str]:
        return {"api-key": self._api_key, "accept": "application/json"}

    async def _get(self, path: str, params: dict) -> dict:
        url = f"{BASE_URL}/{path}"
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=self._headers, params=params
            ) as response:
                response.raise_for_status()
                return await response.json()

    async def get_updated_workouts_since(self, since: str) -> list[Workout]:
        """Return workouts created or updated since `since` (ISO 8601).

        Uses Hevy's `/workouts/events` endpoint, which is purpose-built for
        incremental sync. Deletions are ignored (we never remove notes).
        """
        workouts: list[Workout] = []
        page = 1
        while True:
            data = await self._get(
                "workouts/events",
                {
                    "since": since,
                    "page": page,
                    "pageSize": MAX_PAGE_SIZE,
                },
            )
            for event in data.get("events", []):
                if event.get("type") == "updated" and "workout" in event:
                    workouts.append(Workout.from_json(event["workout"]))
            page_count = data.get("page_count", 1)
            if page >= page_count:
                break
            page += 1

        # Events come newest-first; return oldest-first so notes are written
        # in chronological order.
        workouts.reverse()
        return workouts
