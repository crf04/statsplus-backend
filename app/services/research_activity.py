"""Weekly active researchers (crf04/statsplus#213).

The GTM goal counts accounts outside the founder's circle that are active in
3 of 4 weeks.  This module owns the three pieces that make that measurable:

* which reads are *research actions* (:data:`RESEARCH_ENDPOINTS`);
* a per-account tally of those reads for each US Eastern calendar date (the
  *game day*), with no per-request event log; and
* the admin cohort tags and the 4-week report built from the tally.

A research action is a successful (2xx) authenticated read that loads a
Matchup, a Player profile, an Opposing Team Profile, a Search or game-log
query (including natural-language search), or a Target run or backtest.  The
Slate, the home page and reference lists do not count.  The request hook that
feeds the tally lives in ``app/__init__.py``; ``docs/API_DOCUMENTATION.md``
lists every route and why it is in or out.

An *active week* is a Monday-to-Sunday Eastern week with research actions on
two or more game days (which also means two or more actions).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from typing import Any, Final

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.domain.slate_time import EASTERN, slate_date_for_instant
from app.errors import InvalidInputError, ResourceNotFoundError
from app.models import get_session
from app.models.research_activity import (
    COHORT_FRIEND_OF_FRIEND,
    COHORTS,
    AccountCohort,
    ResearchActivityDay,
)
from app.models.user import User
from app.models.user_attribution import UserAttribution

logger = logging.getLogger(__name__)

#: ``(HTTP method, Flask endpoint)`` of every route whose success is a research
#: action.  Endpoints are ``<blueprint>.<view function>``; a test proves each
#: one still names a real route, so a rename cannot silently stop counting.
#:
#: Deliberately absent: ``users.resolve_targets``.  The Slate page reads it on
#: every load to show live Targets, so counting it would count Slate loads.
#: Target use is counted through the Backtest and Lab (preview) routes instead.
RESEARCH_ENDPOINTS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        # Matchup, from a Slate game or with no game.
        ("GET", "games.get_matchup"),
        ("GET", "games.get_matchup_selection"),
        ("GET", "matchups.get_unscheduled_matchup"),
        # Player profile.
        ("GET", "players.get_player_profile"),
        # Opposing Team Profile (also read beside a Player profile and by a
        # Target's opponent context; the tally only needs one read a day).
        ("GET", "teams.get_team_stats"),
        # Search: the Log Workspace game logs, and natural-language search.
        ("GET", "games.get_game_logs"),
        ("POST", "nl.process_natural_language_query"),
        # Target run (the Lab's Draft Target preview) and Backtest.
        ("POST", "users.preview_target"),
        ("GET", "users.backtest_target"),
        ("GET", "users.backtest_targets"),
    }
)

#: A week is active with research actions on at least this many game days.
ACTIVE_WEEK_MIN_GAME_DAYS: Final[int] = 2
#: The goal counts accounts active in at least this many of the report weeks.
GOAL_MIN_ACTIVE_WEEKS: Final[int] = 3
REPORT_WEEKS: Final[int] = 4
GOAL_TARGET: Final[int] = 30
#: Friends-of-friends count toward the goal, up to this many of the 30.
FRIEND_OF_FRIEND_CAP: Final[int] = 10

#: Upper bound on a count write's wait for a row lock or a slow statement
#: (PostgreSQL only; SQLite has no per-statement timeout).
COUNT_STATEMENT_TIMEOUT_MS: Final[int] = 2000


def week_start(day: date) -> date:
    """The Monday of the Monday-to-Sunday week containing ``day``."""

    return day - timedelta(days=day.weekday())


def report_id(firebase_uid: str) -> str:
    """A stable pseudonym for an account in the report.

    The report can be pasted into an issue without naming anyone.  It is a
    one-way hash, not a secret: an admin who holds the uid (or sees this id in
    the admin user list) can match the two.
    """

    digest = hashlib.sha256(f"statsplus-research-report:{firebase_uid}".encode())
    return digest.hexdigest()[:12]


class ResearchActivityService:
    """Counts research reads per account per Eastern date and reports on them."""

    def __init__(
        self,
        engine,
        *,
        counting: bool = True,
        clock: Callable[[], datetime] | None = None,
    ):
        """Bind to ``engine``.

        ``counting=False`` (the read-only demo database) turns the request-path
        tally off; the admin reads and tags still work.  ``clock`` returns the
        current timezone-aware instant and exists so tests can fix the date.
        """
        self.engine = engine
        self.counting = counting
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def today(self) -> date:
        """The current US Eastern calendar date."""

        return slate_date_for_instant(self._clock())

    # -- counting ---------------------------------------------------------

    def record(self, firebase_uid: str, game_day: date) -> None:
        """Add one research action to the account's count for ``game_day``.

        An UPDATE of the day's row, or the INSERT of its first action; two
        requests racing to make the first one resolve by retrying as an UPDATE.
        Raises on failure -- the request path uses :meth:`record_quietly`.
        """

        session = get_session(self.engine)
        try:
            for attempt in range(2):
                self._bound_the_wait(session)
                updated = (
                    session.query(ResearchActivityDay)
                    .filter_by(firebase_uid=firebase_uid, game_day=game_day)
                    .update(
                        {ResearchActivityDay.actions: ResearchActivityDay.actions + 1},
                        synchronize_session=False,
                    )
                )
                if not updated:
                    session.add(ResearchActivityDay(firebase_uid=firebase_uid, game_day=game_day, actions=1))
                try:
                    session.commit()
                    return
                except IntegrityError:
                    session.rollback()
                    if attempt == 1:
                        raise
        finally:
            session.close()

    def record_quietly(self, firebase_uid: str, game_day: date) -> None:
        """:meth:`record`, but a failure is logged and never raised.

        The tally is a measurement: losing a count is acceptable, failing the
        read it describes is not.
        """

        try:
            self.record(firebase_uid, game_day)
        except Exception as error:
            logger.warning("Could not count a research action: %s", error)

    @staticmethod
    def _bound_the_wait(session) -> None:
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text(f"SET LOCAL statement_timeout = {COUNT_STATEMENT_TIMEOUT_MS}"))

    # -- cohorts ----------------------------------------------------------

    def set_cohort(self, firebase_uid: str, cohort: str | None) -> str | None:
        """Tag an account, or clear its tag with ``None``; return the new tag.

        Idempotent.  An unknown uid is ``ResourceNotFoundError``.
        """

        if cohort is not None and cohort not in COHORTS:
            raise InvalidInputError("cohort must be internal, friend, friend_of_friend or null.")
        session = get_session(self.engine)
        try:
            if session.get(User, firebase_uid) is None:
                raise ResourceNotFoundError("No such user.")
            for attempt in range(2):
                row = session.get(AccountCohort, firebase_uid)
                if cohort is None:
                    if row is not None:
                        session.delete(row)
                elif row is None:
                    session.add(
                        AccountCohort(
                            firebase_uid=firebase_uid,
                            cohort=cohort,
                            tagged_at=datetime.now(timezone.utc),
                        )
                    )
                else:
                    row.cohort = cohort
                    row.tagged_at = datetime.now(timezone.utc)
                try:
                    session.commit()
                    return cohort
                except IntegrityError:
                    # A concurrent first tag won the primary key; update it.
                    session.rollback()
                    if attempt == 1:
                        raise
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- report -----------------------------------------------------------

    def report(self, as_of: date | None = None) -> dict[str, Any]:
        """The last four Monday-to-Sunday Eastern weeks, ending with ``as_of``'s.

        ``as_of`` defaults to today (Eastern), so the newest week is the one in
        progress; pass the last day of a finished week for four complete weeks.
        """

        today = self.today()
        starts = [
            week_start(as_of or today) - timedelta(weeks=REPORT_WEEKS - 1 - offset)
            for offset in range(REPORT_WEEKS)
        ]
        last_day = starts[-1] + timedelta(days=6)

        session = get_session(self.engine)
        try:
            accounts = session.execute(
                select(
                    User.firebase_uid,
                    AccountCohort.cohort,
                    UserAttribution.ref,
                    UserAttribution.utm_source,
                )
                .select_from(User)
                .outerjoin(AccountCohort, AccountCohort.firebase_uid == User.firebase_uid)
                .outerjoin(UserAttribution, UserAttribution.firebase_uid == User.firebase_uid)
            ).all()
            tally = session.execute(
                select(
                    ResearchActivityDay.firebase_uid,
                    ResearchActivityDay.game_day,
                    ResearchActivityDay.actions,
                ).where(
                    ResearchActivityDay.game_day >= starts[0],
                    ResearchActivityDay.game_day <= last_day,
                )
            ).all()
        finally:
            session.close()

        # uid -> week start -> [game days with actions, actions]
        weekly: dict[str, dict[date, list[int]]] = {}
        for uid, game_day, actions in tally:
            totals = weekly.setdefault(uid, {}).setdefault(week_start(game_day), [0, 0])
            totals[0] += 1
            totals[1] += actions

        rows = []
        for uid, cohort, ref, utm_source in accounts:
            weeks = []
            for start in starts:
                game_days, actions = weekly.get(uid, {}).get(start, (0, 0))
                weeks.append(
                    {
                        "start": start.isoformat(),
                        "game_days": game_days,
                        "actions": actions,
                        "active": game_days >= ACTIVE_WEEK_MIN_GAME_DAYS,
                    }
                )
            rows.append(
                {
                    "id": report_id(uid),
                    "cohort": cohort,
                    "source": ref or utm_source,
                    "ref": ref,
                    "utm_source": utm_source,
                    "weeks": weeks,
                    "active_weeks": sum(week["active"] for week in weeks),
                }
            )
        rows.sort(key=lambda row: (-row["active_weeks"], row["id"]))

        retained = [row for row in rows if row["active_weeks"] >= GOAL_MIN_ACTIVE_WEEKS]
        # Only outside (untagged) and friend_of_friend accounts count toward the
        # goal; internal and friend accounts are reported but never counted.
        untagged = sum(row["cohort"] is None for row in retained)
        friends_of_friends = sum(row["cohort"] == COHORT_FRIEND_OF_FRIEND for row in retained)
        return {
            "timezone": EASTERN.key,
            "weeks": [
                {
                    "start": start.isoformat(),
                    "end": (start + timedelta(days=6)).isoformat(),
                    "complete": start + timedelta(days=6) < today,
                }
                for start in starts
            ],
            "accounts": rows,
            "goal": {
                "target": GOAL_TARGET,
                "min_active_weeks": GOAL_MIN_ACTIVE_WEEKS,
                "friend_of_friend_cap": FRIEND_OF_FRIEND_CAP,
                "untagged_accounts": untagged,
                "friend_of_friend_accounts": friends_of_friends,
                "total": untagged + min(friends_of_friends, FRIEND_OF_FRIEND_CAP),
            },
        }
