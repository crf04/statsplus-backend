"""Scheduled refresh of the official injury report on game days.

The cron (``railway.injuries.json``) fires every 30 minutes across a UTC span
wide enough for both halves of the year, because the same UTC hours fall at
different Eastern times either side of a daylight-saving change.  This service
is what keeps the contract of crf04/statsplus#220: it acts only from 11:00 to
19:30 Eastern, only on a date with games still to tip, and stores the report
for each of those games.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, time, timezone
from typing import Any

from app.domain.nba_events import is_final_event, is_postponed_event
from app.domain.utc import assume_utc, parse_utc_iso
from app.providers.nba_injury_report import EASTERN
from app.services.injury_listing import slate_events
from app.services.research_season import schedule_season

REFRESH_WINDOW_START = time(11, 0)
REFRESH_WINDOW_END = time(19, 30)


class InjuryReportRefreshService:
    def __init__(
        self,
        *,
        injuries: Any,
        event_catalog: Any | None,
        settings: Any,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.injuries = injuries
        self.event_catalog = event_catalog
        self.settings = settings
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def run(self) -> dict[str, Any]:
        """One scheduled tick; ``state`` says what happened.

        Provider failures propagate so the caller can fail the run.
        """

        now = assume_utc(self._clock())
        eastern = now.astimezone(EASTERN)
        # Minute resolution: a run scheduled for 19:30 starts a few seconds late.
        if not (
            REFRESH_WINDOW_START
            <= eastern.replace(second=0, microsecond=0).time()
            <= REFRESH_WINDOW_END
        ):
            return {"state": "outside_window", "games": 0}
        if self.event_catalog is None:
            return {"state": "no_schedule", "games": 0}
        today = eastern.date()
        events = [
            event
            for event in slate_events(self.event_catalog, self.settings, today)
            if parse_utc_iso(str(event["scheduled_at"])) > now
            and not is_final_event(event)
            and not is_postponed_event(event)
        ]
        if not events:
            return {"state": "no_games", "games": 0}
        season = schedule_season(self.settings, self.event_catalog, today)
        result = self.injuries.refresh_report(events=events, season=season)
        return {
            "state": result.state,
            "games": result.games,
            "report_url": result.report_url,
        }


__all__ = ["InjuryReportRefreshService", "REFRESH_WINDOW_END", "REFRESH_WINDOW_START"]
