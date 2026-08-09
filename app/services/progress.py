"""Shared progress milestones for durable refresh operations."""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from typing import Final

ProgressCallback = Callable[[float, str | None], None]


class RefreshProgressStage(float, Enum):
    """Canonical durable progress values shared by every refresh operation."""

    FETCH = 0.1
    TRANSFORM = 0.75
    PUBLISH = 0.9
    COMPLETE = 1.0


class RefreshProgress:
    """Report named refresh phases through an optional job callback."""

    FETCH_PROGRESS: Final[float] = RefreshProgressStage.FETCH.value
    TRANSFORM_PROGRESS: Final[float] = RefreshProgressStage.TRANSFORM.value
    PUBLISH_PROGRESS: Final[float] = RefreshProgressStage.PUBLISH.value
    COMPLETE_PROGRESS: Final[float] = RefreshProgressStage.COMPLETE.value

    def __init__(self, callback: ProgressCallback | None = None) -> None:
        self._callback = callback

    def report(self, stage: RefreshProgressStage, note: str | None) -> None:
        """Invoke the callback with one canonical stage and explanatory note."""

        if self._callback is not None:
            self._callback(stage.value, note)

    def fetch(self, note: str | None) -> None:
        self.report(RefreshProgressStage.FETCH, note)

    def transform(self, note: str | None) -> None:
        self.report(RefreshProgressStage.TRANSFORM, note)

    def publish(self, note: str | None) -> None:
        self.report(RefreshProgressStage.PUBLISH, note)

    def complete(self, note: str | None = "Completed") -> None:
        self.report(RefreshProgressStage.COMPLETE, note)


__all__ = [
    "ProgressCallback",
    "RefreshProgress",
    "RefreshProgressStage",
]
