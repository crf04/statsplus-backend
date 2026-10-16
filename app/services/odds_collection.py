"""Scheduled collection of player-prop odds from The Odds API (#377).

One ``run()`` is one five-minute beat.  It lists the next 36 hours of events
(free), maps them to canonical games, asks the pure planner in
``odds_schedule`` which paid per-event calls are due under the credit guard,
makes them, and stores the quotes.  It owns its own lease row, quota state,
and poll history and never touches the DFS collector's state.

It serves one feed, chosen by ``ODDS_API_REGIONS``: ``us_dfs`` (the default)
records PrizePicks lines in the DFS projection archive as provider
``prizepicks`` (crf04/statsplus#215); ``us`` stores sportsbook ``book_lines``
(#377) and is deferred.

Secrets: the API key lives only in ``OddsApiClient``.  Nothing here formats a
URL, and every message that leaves this module is built from closed reasons.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from typing import Any

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from app.config.settings import OddsApiSettings
from app.errors import redact_public_value
from app.models.projection_collection import ProjectionCollectionLease
from app.models.sportsbook_odds import OddsApiPoll, OddsApiQuotaState
from app.providers.dfs import NBAMarketQuery
from app.providers.odds_api import (
    DFS_REGION,
    OddsApiClient,
    OddsApiError,
    OddsQuota,
)
from app.services.event_resolver import stored_timestamp
from app.services.odds_dfs_projection import (
    CLOSING_LOOKBACK,
    PROVIDER,
    PrizePicksProjectionWriter,
)
from app.services.odds_identity import OddsEventMapper, RosterNameResolver
from app.services.odds_schedule import (
    CALL_COST,
    CLOSING_CLOSES,
    CLOSING_OPENS,
    POLL_WINDOW_OPENS,
    EventState,
    GameClock,
    Tier,
    next_reset,
    plan_cycle,
)
from app.services.projection_archive import (
    ProjectionArchiveReadScope,
    ProjectionRecordingService,
)
from app.services.sportsbook_quotes import ResolvedQuote, SportsbookQuoteStore
from app.services.statistic_catalog import StatisticResolver

logger = logging.getLogger(__name__)

LEASE_KEY = "odds"
STATE_KEY = "odds"
EVENT_HORIZON = timedelta(hours=36)
LEASE_DURATION = timedelta(minutes=5)
AUTH_BACKOFF = timedelta(hours=6)
BACKOFF_BASE = timedelta(minutes=1)
BACKOFF_MAX = timedelta(minutes=30)
#: Backoff after a paid response could not be published.  The poll already
#: counts toward the hourly cadence, so this only spares the next beats a call
#: that would meet the same fault.
STORAGE_BACKOFF = BACKOFF_MAX
#: ``odds_api_polls.failure_reason`` of a ``complete`` DFS poll whose lines have
#: not (yet) reached the archive; cleared once they have.
UNPUBLISHED = "unpublished"
_QUOTA_WARN_FRACTION = 0.25
_QUOTA_ALERT_FRACTION = 0.10


class LeaseLost(Exception):
    """This worker's lease was taken over; its writes must not commit."""


@dataclass(frozen=True, slots=True)
class OddsRunResult:
    """Bounded, operator-safe summary of one beat."""

    status: str  # complete | partial | no_work | busy | backoff | halted | failed
    reason: str
    tier: str = Tier.NORMAL.value
    calls: int = 0
    credits: int = 0
    unresolved_events: int = 0
    unresolved_players: int = 0
    remaining: int | None = None


def _utc(value: datetime | str | None) -> datetime | None:
    return stored_timestamp(value) if value is not None else None


def _default_notify(level: int, message: str) -> None:
    """Log one structured line; mirror warnings and worse to Sentry."""

    logger.log(level, message)
    if level < logging.WARNING:
        return
    try:
        import sentry_sdk
    except ImportError:  # pragma: no cover - sentry is a runtime dependency
        return
    if sentry_sdk.get_client().is_active():
        sentry_sdk.capture_message(message, level="warning")


class OddsCollectionCoordinator:
    def __init__(
        self,
        engine: Engine,
        *,
        client: OddsApiClient,
        event_mapper: OddsEventMapper,
        name_resolver: RosterNameResolver,
        events_between: Callable[[str, datetime, datetime], list[dict[str, Any]]],
        store: SportsbookQuoteStore | None = None,
        season: str,
        settings: OddsApiSettings,
        clock: Callable[[], datetime] | None = None,
        owner: str = "odds-collector",
        notify: Callable[[int, str], None] | None = None,
        dfs_writer: PrizePicksProjectionWriter | None = None,
    ) -> None:
        self.engine = engine
        self.client = client
        self.event_mapper = event_mapper
        self.name_resolver = name_resolver
        self.events_between = events_between
        self.store = store or SportsbookQuoteStore(engine)
        self.season = season
        self.settings = settings
        if (dfs_writer is not None) != (settings.regions == DFS_REGION):
            raise ValueError(
                "a PrizePicks writer is required for ODDS_API_REGIONS=us_dfs and "
                "refused for any other region"
            )
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.owner = owner
        self.notify = notify or _default_notify
        #: Set for the ``us_dfs`` feed: where PrizePicks quotes are recorded.
        self.dfs_writer = dfs_writer
        self._fence: int | None = None

    # -- one beat ----------------------------------------------------------

    def run(self) -> OddsRunResult:
        now = self.clock()
        fence = self._acquire_lease(now)
        if fence is None:
            return OddsRunResult("busy", "lease_held")
        self._fence = fence
        try:
            result = self._run_locked(now, fence)
            self._freeze_closing(now)
            return result
        except LeaseLost:
            return OddsRunResult("failed", "lease_lost")
        finally:
            self._fence = None
            self._release_lease(fence)

    def _freeze_closing(self, now: datetime) -> None:
        """Close the DFS feed's started games, as the DFS collector does.

        Independent of any paid call, so it runs on every beat; a failure is
        logged and never fails the beat (the next beat retries).
        """

        if self.dfs_writer is None:
            return
        try:
            events = self.events_between(self.season, now - CLOSING_LOOKBACK, now)
            self.dfs_writer.freeze_started(events, created_at=self.clock())
        except Exception as error:
            logger.error(
                "Odds API closing freeze failed: %s",
                redact_public_value(f"{type(error).__name__}: {error}")[:300],
            )

    def _assert_lease(self, connection: Any) -> None:
        """Fail the transaction unless this worker still holds the lease.

        Runs first in every write transaction, locking the lease row, so a
        worker that resumes after another took over (the fence moved on)
        commits nothing -- the same owner-and-fence check ``_renew_lease``
        makes, but at commit rather than before a long network call.
        """

        if self._fence is None:
            return
        table = ProjectionCollectionLease.__table__
        row = connection.execute(
            select(table.c.owner, table.c.fence)
            .where(table.c.lease_key == LEASE_KEY)
            .with_for_update()
        ).one_or_none()
        if row is None or row.owner != self.owner or int(row.fence) != self._fence:
            raise LeaseLost()

    def _run_locked(self, now: datetime, fence: int) -> OddsRunResult:
        state = self._load_state(now)
        backoff_until = _utc(state["backoff_until"])
        if backoff_until is not None and backoff_until > now:
            return OddsRunResult("backoff", "backoff_active", tier=state["tier"])

        try:
            listed = self.client.list_events(now, now + EVENT_HORIZON)
        except OddsApiError as error:
            return self._failed(now, state, error)
        if listed.quota.remaining is not None:
            # Free discovery reports the quota too.  Feeding it to the guard
            # before any paid call keeps a fresh state with 5 credits from
            # spending to 0, and lets a halted collector see a monthly reset.
            self._save_quota(
                OddsQuota(listed.quota.remaining, listed.quota.used, None),
                now=self.clock(),
            )
            self._warn_on_quota(state["remaining"], listed.quota.remaining)
            state["remaining"] = listed.quota.remaining
        mapped, unresolved = self.event_mapper.map(listed.events)

        by_event = {item.event.event_id: item for item in mapped}
        event_states = self._event_states(by_event)
        upcoming = self._upcoming(now)
        plan = plan_cycle(
            now=now,
            events=event_states,
            upcoming=upcoming,
            tier=Tier(state["tier"]),
            tier_evaluated_at=_utc(state["tier_evaluated_at"]),
            remaining=state["remaining"],
            reserve=self.settings.reserve_credits,
            lead_hours=self.settings.assumed_lead_hours,
        )
        self._save_tier(state, plan.tier, plan.tier_evaluated_at, now)
        if plan.tier.value != state["tier"]:
            self.notify(
                logging.WARNING,
                f"Odds API budget guard tier {state['tier']} -> {plan.tier.value} "
                f"(remaining={state['remaining']})",
            )

        calls = credits = players_unresolved = 0
        remaining = state["remaining"]
        status, reason = "no_work", "nothing_due"
        for call in plan.calls:
            item = by_event[call.odds_event_id]
            tip = item.event.commence_time
            if not self._renew_lease(fence):
                return OddsRunResult("failed", "lease_lost", tier=plan.tier.value, calls=calls, credits=credits)
            started = self.clock()
            if started >= tip:
                # The plan was made earlier in this beat; never call at tip.
                status, reason = "partial", "tip_passed"
                continue
            # Eligibility is re-checked at dispatch: discovery latency can
            # carry a call planned inside a window out of it, and a closing
            # flag must never be stored outside tip-10m..tip-5m.
            if call.kind == "closing":
                eligible = tip - CLOSING_OPENS <= started < tip - CLOSING_CLOSES
            else:
                eligible = tip - POLL_WINDOW_OPENS <= started < tip - CLOSING_OPENS
            if not eligible:
                status, reason = "partial", "window_passed"
                continue
            # The guard reads the quota as of the previous response (and any
            # shared-key spend it revealed) before every paid call.
            floor = 1 if call.kind == "closing" else max(self.settings.reserve_credits, 1)
            if remaining is not None and remaining - CALL_COST < floor:
                status, reason = "partial", "quota_floor"
                continue
            try:
                result = self.client.event_odds(call.odds_event_id)
            except OddsApiError as error:
                self.store.record_unstored_poll(
                    odds_event_id=call.odds_event_id,
                    game_id=item.game_id,
                    started_at=started,
                    completed_at=self.clock(),
                    outcome="failed",
                    is_closing=call.kind == "closing",
                    failure_reason=error.reason,
                    commence_time=tip,
                    fence=self._assert_lease,
                )
                if error.reason == "event_not_found":
                    status, reason = "partial", error.reason
                    continue
                outcome = self._failed(now, self._load_state(now), error)
                return OddsRunResult(
                    outcome.status, outcome.reason, tier=plan.tier.value,
                    calls=calls, credits=credits,
                    unresolved_events=len(unresolved),
                    unresolved_players=players_unresolved,
                )
            calls += 1
            credits += result.cost
            remaining = (
                result.quota.remaining if result.quota.remaining is not None else remaining
            )
            received = self.clock()
            self._save_quota(result.quota, now=received)
            self._warn_on_quota(state["remaining"], remaining)
            state["remaining"] = remaining
            if received >= tip:
                # Credits were spent, but a response that crossed tip-off is
                # in-play data and is never stored.
                self.store.record_unstored_poll(
                    odds_event_id=call.odds_event_id,
                    game_id=item.game_id,
                    started_at=started,
                    completed_at=received,
                    outcome="failed",
                    is_closing=call.kind == "closing",
                    cost=result.cost,
                    remaining=remaining,
                    failure_reason="received_after_tip",
                    commence_time=tip,
                    fence=self._assert_lease,
                )
                status, reason = "partial", "received_after_tip"
                continue

            parsed = result.parsed
            names = {quote.player_name for quote in parsed.quotes}
            athletes = {}
            if self.dfs_writer is not None:
                athletes = (
                    self.name_resolver.resolve_athletes(names, item.team_ids)
                    if names
                    else {}
                )
                resolved_ids = {name: athlete.player_id for name, athlete in athletes.items()}
            else:
                resolved_ids = (
                    self.name_resolver.resolve(names, item.team_ids) if names else {}
                )
            unresolved_names = len(names) - len(resolved_ids)
            players_unresolved += unresolved_names
            quotes = [
                ResolvedQuote(
                    canonical_player_id=resolved_ids[quote.player_name],
                    category=quote.category,
                    book=quote.book,
                    line=quote.line,
                    over=quote.over,
                    under=quote.under,
                    last_update=quote.last_update,
                )
                for quote in parsed.quotes
                if quote.player_name in resolved_ids
            ]
            stored = len(quotes)
            if parsed.bookmaker_count == 0:
                # Upstream answered empty: props are not posted (yet).  Nothing
                # stored is contradicted, so latest stays as it is.
                self.store.record_unstored_poll(
                    odds_event_id=call.odds_event_id,
                    game_id=item.game_id,
                    started_at=started,
                    completed_at=self.clock(),
                    outcome="empty",
                    is_closing=call.kind == "closing",
                    cost=result.cost,
                    remaining=remaining,
                    commence_time=tip,
                    fence=self._assert_lease,
                )
            elif self.dfs_writer is not None:
                # Write-ahead.  The paid response counts as this game's poll
                # (cadence and credits) before anything is published, so a
                # fault in the archive can cost an hour of fresh lines but never
                # a second purchase; the fence refuses a superseded worker here,
                # before it can publish; and the row reads "unpublished" until
                # the lines are in the archive.
                poll_id = self.store.record_unstored_poll(
                    odds_event_id=call.odds_event_id,
                    game_id=item.game_id,
                    started_at=started,
                    completed_at=self.clock(),
                    outcome="complete",
                    is_closing=call.kind == "closing",
                    cost=result.cost,
                    remaining=remaining,
                    failure_reason=UNPUBLISHED,
                    unresolved_count=unresolved_names,
                    commence_time=tip,
                    fence=self._assert_lease,
                )
                if not self._renew_lease(fence):
                    return OddsRunResult(
                        "failed", "lease_lost", tier=plan.tier.value,
                        calls=calls, credits=credits,
                    )
                try:
                    stored = self.dfs_writer.record(
                        item,
                        [quote for quote in parsed.quotes if quote.player_name in athletes],
                        athletes,
                        retrieved_at=received,
                        accepted_at=self.clock(),
                        poll_started_at=started,
                    )
                except Exception as error:
                    logger.error(
                        "Odds API could not record PrizePicks lines: %s",
                        redact_public_value(f"{type(error).__name__}: {error}")[:300],
                    )
                    self.notify(
                        logging.ERROR,
                        "Odds API could not record PrizePicks lines; backing off",
                    )
                    self._settle_poll(poll_id, 0, "storage_error")
                    outcome = self._backoff(
                        now,
                        self._load_state(now),
                        "storage_error",
                        halts=False,
                        minimum=STORAGE_BACKOFF,
                    )
                    return OddsRunResult(
                        outcome.status, outcome.reason, tier=plan.tier.value,
                        calls=calls, credits=credits,
                        unresolved_events=len(unresolved),
                        unresolved_players=players_unresolved, remaining=remaining,
                    )
                self._settle_poll(poll_id, stored, None)
            else:
                self.store.record_poll(
                    odds_event_id=call.odds_event_id,
                    game_id=item.game_id,
                    started_at=started,
                    completed_at=self.clock(),
                    cost=result.cost,
                    remaining=remaining,
                    is_closing=call.kind == "closing",
                    quotes=quotes,
                    unresolved_count=unresolved_names,
                    commence_time=tip,
                    fence=self._assert_lease,
                )
            self.notify(
                logging.INFO,
                f"Odds API poll region={self.settings.regions} game={item.game_id} "
                f"kind={call.kind} cost={result.cost} remaining={remaining} "
                f"quotes={stored} unresolved_players={unresolved_names}",
            )
            self._clear_failures(now, unresolved_players=unresolved_names)
            status, reason = "complete", "collected"
            if remaining is not None and remaining < 6:
                self.notify(logging.ERROR, "Odds API credits exhausted; collection halted")
                self._save_tier(state, Tier.HALTED, self.clock(), now)
                status, reason = "halted", "credits_exhausted"
                break
        if calls or unresolved or plan.tier is not Tier.NORMAL:
            self.notify(
                logging.INFO,
                f"Odds API cycle tier={plan.tier.value} calls={calls} credits={credits} "
                f"remaining={remaining} unresolved_events={len(unresolved)} "
                f"unresolved_players={players_unresolved}",
            )
        return OddsRunResult(
            status, reason, tier=plan.tier.value, calls=calls, credits=credits,
            unresolved_events=len(unresolved), unresolved_players=players_unresolved,
            remaining=remaining,
        )

    def _settle_poll(self, poll_id: int, quote_count: int, failure_reason: str | None) -> None:
        """Mark what became of a write-ahead poll's lines.

        The paid response is already counted, so a fault here only leaves the
        row reading "unpublished" and is logged, never retried by buying again.
        A lost lease still stops the worker.
        """

        try:
            self.store.settle_poll(
                poll_id,
                quote_count=quote_count,
                failure_reason=failure_reason,
                fence=self._assert_lease,
            )
        except LeaseLost:
            raise
        except Exception as error:
            logger.warning(
                "Odds API could not settle poll %s: %s",
                poll_id,
                redact_public_value(f"{type(error).__name__}: {error}")[:200],
            )

    # -- failures ----------------------------------------------------------

    def _failed(self, now: datetime, state: dict[str, Any], error: OddsApiError) -> OddsRunResult:
        return self._backoff(now, state, error.reason, halts=error.halts)

    def _backoff(
        self,
        now: datetime,
        state: dict[str, Any],
        reason: str,
        *,
        halts: bool,
        minimum: timedelta = timedelta(0),
    ) -> OddsRunResult:
        failures = int(state["consecutive_failures"] or 0) + 1
        if halts:
            backoff = AUTH_BACKOFF
            self.notify(
                logging.ERROR,
                f"Odds API rejected the key or plan ({reason}); halted for 6h",
            )
        else:
            backoff = max(minimum, min(BACKOFF_MAX, BACKOFF_BASE * (2 ** (failures - 1))))
        table = OddsApiQuotaState.__table__
        with self.engine.begin() as connection:
            self._assert_lease(connection)
            connection.execute(
                update(table)
                .where(table.c.state_key == STATE_KEY)
                .values(
                    consecutive_failures=failures,
                    backoff_until=now + backoff,
                    last_failure_reason=reason,
                    updated_at=now,
                )
            )
        return OddsRunResult("halted" if halts else "failed", reason, tier=state["tier"])

    def _clear_failures(self, now: datetime, *, unresolved_players: int) -> None:
        table = OddsApiQuotaState.__table__
        with self.engine.begin() as connection:
            self._assert_lease(connection)
            connection.execute(
                update(table)
                .where(table.c.state_key == STATE_KEY)
                .values(
                    consecutive_failures=0,
                    backoff_until=None,
                    last_failure_reason=None,
                    unresolved_total=table.c.unresolved_total + unresolved_players,
                    updated_at=now,
                )
            )

    # -- quota state -------------------------------------------------------

    def _load_state(self, now: datetime) -> dict[str, Any]:
        table = OddsApiQuotaState.__table__
        with self.engine.begin() as connection:
            row = connection.execute(
                select(table).where(table.c.state_key == STATE_KEY)
            ).mappings().one_or_none()
            if row is None:
                try:
                    with connection.begin_nested():
                        connection.execute(
                            insert(table).values(
                                state_key=STATE_KEY,
                                tier=Tier.NORMAL.value,
                                reset_day=self.settings.reset_day,
                                consecutive_failures=0,
                                unresolved_total=0,
                                updated_at=now,
                            )
                        )
                except IntegrityError:
                    pass
                row = connection.execute(
                    select(table).where(table.c.state_key == STATE_KEY)
                ).mappings().one()
            if row["reset_day"] != self.settings.reset_day:
                connection.execute(
                    update(table)
                    .where(table.c.state_key == STATE_KEY)
                    .values(reset_day=self.settings.reset_day)
                )
            return dict(row)

    def _save_tier(
        self, state: dict[str, Any], tier: Tier, evaluated_at: datetime | None, now: datetime
    ) -> None:
        table = OddsApiQuotaState.__table__
        with self.engine.begin() as connection:
            self._assert_lease(connection)
            connection.execute(
                update(table)
                .where(table.c.state_key == STATE_KEY)
                .values(tier=tier.value, tier_evaluated_at=evaluated_at, updated_at=now)
            )

    def _save_quota(self, quota: Any, *, now: datetime) -> None:
        values: dict[str, Any] = {"observed_at": now, "updated_at": now}
        for column, value in (
            ("remaining", quota.remaining),
            ("used", quota.used),
            ("last_cost", quota.last_cost),
        ):
            if value is not None:
                values[column] = value
        table = OddsApiQuotaState.__table__
        with self.engine.begin() as connection:
            self._assert_lease(connection)
            connection.execute(
                update(table).where(table.c.state_key == STATE_KEY).values(**values)
            )

    def _warn_on_quota(self, before: int | None, after: int | None) -> None:
        if after is None:
            return
        plan = self.settings.plan_credits
        for fraction, level, label in (
            (_QUOTA_ALERT_FRACTION, logging.ERROR, "10%"),
            (_QUOTA_WARN_FRACTION, logging.WARNING, "25%"),
        ):
            threshold = plan * fraction
            if after < threshold and (before is None or before >= threshold):
                self.notify(level, f"Odds API credits below {label} of plan (remaining={after})")
                return

    # -- schedule facts ----------------------------------------------------

    def _event_states(self, by_event: dict[str, Any]) -> list[EventState]:
        if not by_event:
            return []
        polls = OddsApiPoll.__table__
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(
                    polls.c.odds_event_id,
                    polls.c.completed_at,
                    polls.c.is_closing,
                    polls.c.commence_time,
                ).where(
                    polls.c.odds_event_id.in_(list(by_event)),
                    polls.c.outcome.in_(("complete", "empty")),
                )
            ).all()
        last: dict[str, datetime] = {}
        closing: set[str] = set()
        for row in rows:
            completed = _utc(row.completed_at)
            if completed is not None and (
                row.odds_event_id not in last or completed > last[row.odds_event_id]
            ):
                last[row.odds_event_id] = completed
            # A closing poll closes the game only for the tip it was taken
            # against; a postponement moves the tip and reopens collection.
            if row.is_closing and _utc(row.commence_time) == by_event[
                row.odds_event_id
            ].event.commence_time:
                closing.add(row.odds_event_id)
        return [
            EventState(
                odds_event_id=event_id,
                commence_time=item.event.commence_time,
                last_poll_at=last.get(event_id),
                closing_done=event_id in closing,
            )
            for event_id, item in by_event.items()
        ]

    def _upcoming(self, now: datetime) -> list[GameClock]:
        reset = next_reset(now, self.settings.reset_day)
        rows = self.events_between(self.season, now, reset)
        polls = OddsApiPoll.__table__
        with self.engine.connect() as connection:
            closed = {
                (row[0], _utc(row[1]))
                for row in connection.execute(
                    select(polls.c.canonical_game_id, polls.c.commence_time).where(
                        polls.c.is_closing.is_(True),
                        polls.c.outcome.in_(("complete", "empty")),
                    )
                )
            }
        games = []
        for row in rows:
            scheduled = _utc(row.get("scheduled_at"))
            if scheduled is None or scheduled <= now:
                continue
            # Closed only for (about) this tip: a postponed game reopens.
            done = any(
                game == str(row.get("nba_game_id"))
                and tip is not None
                and abs(tip - scheduled) <= timedelta(hours=1)
                for game, tip in closed
            )
            games.append(GameClock(scheduled, closing_done=done))
        return games

    # -- lease -------------------------------------------------------------

    def _acquire_lease(self, now: datetime) -> int | None:
        table = ProjectionCollectionLease.__table__
        with self.engine.begin() as connection:
            row = connection.execute(
                select(table).where(table.c.lease_key == LEASE_KEY).with_for_update()
            ).mappings().one_or_none()
            if row is None:
                try:
                    with connection.begin_nested():
                        connection.execute(
                            insert(table).values(
                                lease_key=LEASE_KEY, owner=None,
                                lease_expires_at=now, fence=0, updated_at=now,
                            )
                        )
                except IntegrityError:
                    pass
                row = connection.execute(
                    select(table).where(table.c.lease_key == LEASE_KEY).with_for_update()
                ).mappings().one()
            expires = _utc(row["lease_expires_at"])
            if row["owner"] and expires is not None and expires > now:
                return None
            fence = int(row["fence"] or 0) + 1
            connection.execute(
                update(table).where(table.c.lease_key == LEASE_KEY).values(
                    owner=self.owner, lease_expires_at=now + LEASE_DURATION,
                    fence=fence, updated_at=now,
                )
            )
            return fence

    def _renew_lease(self, fence: int) -> bool:
        table = ProjectionCollectionLease.__table__
        now = self.clock()
        with self.engine.begin() as connection:
            result = connection.execute(
                update(table)
                .where(
                    table.c.lease_key == LEASE_KEY,
                    table.c.owner == self.owner,
                    table.c.fence == fence,
                )
                .values(lease_expires_at=now + LEASE_DURATION, updated_at=now)
            )
            return result.rowcount == 1

    def _release_lease(self, fence: int) -> None:
        table = ProjectionCollectionLease.__table__
        now = self.clock()
        with self.engine.begin() as connection:
            connection.execute(
                update(table)
                .where(
                    table.c.lease_key == LEASE_KEY,
                    table.c.owner == self.owner,
                    table.c.fence == fence,
                )
                .values(owner=None, lease_expires_at=now, updated_at=now)
            )


def build_odds_collection(
    dependencies: Any, settings: Any, *, client: OddsApiClient | None = None
) -> OddsCollectionCoordinator | None:
    """Compose the coordinator for a collector process.

    Returns ``None`` when collection is off.  Enabled without
    ``ODDS_API_KEY`` raises ``RuntimeError`` (naming the variable, never a
    value): this is called by the collector only, so the web application
    never fails over the key.
    """

    odds = getattr(settings, "odds_api", None)
    if odds is None or not odds.enabled:
        return None
    if not odds.api_key:
        raise RuntimeError("ODDS_API_ENABLED is true but ODDS_API_KEY is not set")
    season = settings.nba.current_season
    engine = dependencies.event_catalog_service.engine
    dfs_writer = (
        _build_dfs_writer(dependencies, season) if odds.regions == DFS_REGION else None
    )
    return OddsCollectionCoordinator(
        engine,
        client=client
        or OddsApiClient(
            odds.api_key,
            base_url=odds.base_url,
            timeout=odds.request_timeout_seconds,
            regions=odds.regions,
        ),
        event_mapper=OddsEventMapper(
            resolver=dependencies.event_resolver,
            mapping_repository=dependencies.event_mapping_repository,
            season=season,
        ),
        name_resolver=RosterNameResolver(
            catalog=dependencies.athlete_catalog_service,
            mapping_repository=dependencies.athlete_mapping_repository,
            season=season,
        ),
        events_between=dependencies.event_catalog_service.get_events_between,
        season=season,
        settings=odds,
        dfs_writer=dfs_writer,
    )


def _build_dfs_writer(dependencies: Any, season: str) -> PrizePicksProjectionWriter:
    """The writer for provider ``prizepicks``, authorized for that scope only.

    ``prizepicks`` is deliberately not in ``DFS_ENABLED_PROVIDERS`` (its direct
    API is blocked), so the application's own recording service refuses it;
    this one is built here for the single scope the odds beat owns.
    """

    archive = getattr(dependencies, "projection_archive", None)
    catalog = getattr(dependencies, "statistic_catalog", None)
    if archive is None or catalog is None:
        raise RuntimeError(
            "ODDS_API_REGIONS=us_dfs needs the projection archive; "
            "the read-only demo database has none"
        )
    query = NBAMarketQuery(season=season)
    scope = ProjectionArchiveReadScope(provider=PROVIDER, query=query)
    return PrizePicksProjectionWriter(
        ProjectionRecordingService(archive, (scope,), default_scope=scope),
        query=query,
        statistics=StatisticResolver(catalog),
    )


def run_once(coordinator: OddsCollectionCoordinator | None) -> OddsRunResult | None:
    """One beat, never raising: a failure is described without any URL."""

    if coordinator is None:
        return None
    try:
        return coordinator.run()
    except Exception as error:
        text = redact_public_value(f"{type(error).__name__}: {error}")
        key = coordinator.settings.api_key
        if key:
            text = text.replace(key, "[REDACTED]")
        logger.error("Odds API collection attempt failed: %s", text[:300])
        return OddsRunResult("failed", "unexpected_error")


__all__ = [
    "OddsCollectionCoordinator",
    "OddsRunResult",
    "build_odds_collection",
    "run_once",
]
