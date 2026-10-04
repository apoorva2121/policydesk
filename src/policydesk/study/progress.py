"""Remembering where you got to, and what you wrote.

Why this is a file on disk and not Streamlit session state
----------------------------------------------------------
Session state vanishes when the process restarts, and you will restart it — to
change a setting, to rebuild the index, because you closed the terminal. Losing
your written predictions each time would quietly train you to stop writing them,
which removes the one habit the whole study plan rests on.

Where it lives
--------------
`study-progress.json` beside the repository, **git-ignored**. Your own answers and
self-scores are yours; they are not part of the project, and committing them would
be odd for anyone else who cloned it.

Note the contrast with `eval-runs/`, which IS committed: that is a measurement of
the system, and the history of every quality decision. This is a record of *you*.
Same question in both cases — can it be regenerated, and whose is it — with
different answers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..config import Settings


class StepRecord(BaseModel):
    """What you wrote and whether you finished a step."""

    prediction: str = ""
    revealed: bool = False
    done: bool = False
    updated_at: str = ""


class DrillRecord(BaseModel):
    """Your answer to one interview question, and your own score of it."""

    answer: str = ""
    score: int | None = Field(default=None, ge=1, le=5)
    revealed: bool = False
    attempts: int = 0
    updated_at: str = ""


class StudyProgress(BaseModel):
    """Everything remembered between sessions."""

    # Keyed by "session-<n>-step-<i>" and by tour file path.
    steps: dict[str, StepRecord] = Field(default_factory=dict)
    tour: dict[str, StepRecord] = Field(default_factory=dict)
    drill: dict[str, DrillRecord] = Field(default_factory=dict)
    started_at: str = ""

    # -- reading ------------------------------------------------------------

    def step(self, key: str) -> StepRecord:
        return self.steps.get(key, StepRecord())

    def tour_entry(self, key: str) -> StepRecord:
        return self.tour.get(key, StepRecord())

    def drill_entry(self, key: str) -> DrillRecord:
        return self.drill.get(key, DrillRecord())

    def completed_steps(self, keys: list[str]) -> int:
        return sum(1 for key in keys if self.step(key).done)

    def completed_tour(self, keys: list[str]) -> int:
        return sum(1 for key in keys if self.tour_entry(key).done)

    @property
    def drill_scores(self) -> list[int]:
        return [r.score for r in self.drill.values() if r.score is not None]

    @property
    def mean_drill_score(self) -> float | None:
        scores = self.drill_scores
        return sum(scores) / len(scores) if scores else None

    def weakest_drill(self, limit: int = 3) -> list[tuple[str, int]]:
        """The lowest-scored questions. What to practise next, by evidence."""
        scored = [(key, r.score) for key, r in self.drill.items() if r.score is not None]
        return sorted(scored, key=lambda pair: pair[1])[:limit]

    # -- writing ------------------------------------------------------------

    def record_step(self, key: str, **changes: Any) -> None:
        current = self.steps.get(key, StepRecord()).model_dump()
        current.update(changes)
        current["updated_at"] = _now()
        self.steps[key] = StepRecord(**current)

    def record_tour(self, key: str, **changes: Any) -> None:
        current = self.tour.get(key, StepRecord()).model_dump()
        current.update(changes)
        current["updated_at"] = _now()
        self.tour[key] = StepRecord(**current)

    def record_drill(self, key: str, **changes: Any) -> None:
        current = self.drill.get(key, DrillRecord()).model_dump()
        current.update(changes)
        current["updated_at"] = _now()
        self.drill[key] = DrillRecord(**current)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def progress_path(settings: Settings) -> Path:
    """Beside the repository, not inside `data/`.

    `data/processed/` is a directory the documentation tells you to delete to
    prove the pipeline rebuilds from nothing. Putting your written answers there
    would mean one honest cleanup erased your study notes -- which is exactly the
    mistake that cost this project its evaluation history once already.
    """
    return settings.data_dir.parent / "study-progress.json"


def load(settings: Settings) -> StudyProgress:
    """Read progress, treating any damage as 'start fresh' rather than crashing."""
    path = progress_path(settings)
    if not path.exists():
        return StudyProgress(started_at=_now())
    try:
        return StudyProgress.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        # A corrupt progress file must never block studying. Losing notes is bad;
        # being unable to open the app at all is worse.
        return StudyProgress(started_at=_now())


def save(progress: StudyProgress, settings: Settings) -> Path:
    path = progress_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write then rename, so a crash mid-write cannot leave a half-written file
    # that looks valid and loses everything.
    temporary = path.with_suffix(".tmp")
    temporary.write_text(progress.model_dump_json(indent=2), encoding="utf-8")
    temporary.replace(path)
    return path
