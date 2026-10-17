"""Slate game-day neighbours against a real migrated Event Catalog.

The oracle is the Slate itself: a neighbour is correct when its own Slate lists
games and no closer Slate Date does (crf04/statsplus#368).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest
from sqlalchemy import create_engine, event

from app.config.settings import NBASeasonSettings, RuntimeSettings
from app.errors import ResourceNotFoundError
from app.migrations import run_migrations
from app.services.event_catalog_repository import EventCatalogRepository
from app.services.event_catalog_service import EventCatalogService
from app.services.slate_service import SlateService

REFRESHED = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _row(game_id: str, season: str, scheduled_at: str, classification="Regular Season"):
    return {
        "nba_game_id": game_id,
        "season": season,
        "home_team_id": 2,
        "home_team_name": "Home",
        "home_team_tricode": "HME",
        "away_team_id": 1,
        "away_team_name": "Away",
        "away_team_tricode": "AWY",
        "scheduled_at": datetime.fromisoformat(scheduled_at),
        "status_text": "7:00 pm ET",
        "status_code": 1,
        "postponed_status": None,
        "postponement_evidence": None,
        "classification": classification,
    }


class _World:
    def __init__(self, engine, *, pin: str | None = None):
        self.engine = engine
        run_migrations(self.engine)
        self.repository = EventCatalogRepository(self.engine)
        self.catalog = EventCatalogService(self.engine, provider=object())
        nba = NBASeasonSettings(current_season=pin) if pin else NBASeasonSettings()
        self.slate = SlateService(
            self.catalog,
            settings=RuntimeSettings(environment="testing", nba=nba),
            clock=lambda: datetime(2026, 7, 15, 16, tzinfo=timezone.utc),
        )
        self._rows: dict[str, list[dict]] = {}

    def store(self, season: str, *rows: dict) -> None:
        self._rows.setdefault(season, []).extend(rows)
        self.repository.publish(season, pd.DataFrame(self._rows[season]), REFRESHED)

    def neighbours(self, on: str) -> tuple[str | None, str | None]:
        body = self.slate.get_slate(on)
        return body["previous_game_date"], body["next_game_date"]

    def listed(self, on: str) -> bool:
        try:
            return bool(self.slate.get_slate(on)["games"])
        except ResourceNotFoundError:
            return False

    def assert_matches_the_slate_itself(self, first: str, last: str) -> None:
        """Every date's neighbours are the nearest dates whose Slate lists games."""

        days = []
        day = date.fromisoformat(first)
        while day <= date.fromisoformat(last):
            days.append(day.isoformat())
            day += timedelta(days=1)
        non_empty = [d for d in days if self.listed(d)]
        for current in days:
            before = [d for d in non_empty if d < current]
            after = [d for d in non_empty if d > current]
            expected = (before[-1] if before else None, after[0] if after else None)
            got = self.neighbours(current)
            # The oracle only sees the window; a neighbour outside it is unknown.
            clipped = (
                got[0] if got[0] is not None and got[0] >= first else None,
                got[1] if got[1] is not None and got[1] <= last else None,
            )
            assert clipped == expected, current
            for neighbour in got:
                if neighbour is not None:
                    assert self.listed(neighbour), (current, neighbour)


def _sqlite(tmp_path):
    return create_engine(f"sqlite:///{tmp_path / 'events.sqlite3'}")


@pytest.fixture
def world(tmp_path):
    return _World(_sqlite(tmp_path))


def test_an_off_day_points_at_the_game_days_around_it(world):
    world.store(
        "2025-26",
        _row("0022500001", "2025-26", "2026-01-03T00:00:00+00:00"),  # Jan 2 ET
        _row("0022500002", "2025-26", "2026-01-06T00:30:00+00:00"),  # Jan 5 ET
    )

    assert world.neighbours("2026-01-03") == ("2026-01-02", "2026-01-05")
    assert world.neighbours("2026-01-02") == (None, "2026-01-05")
    assert world.neighbours("2026-01-05") == ("2026-01-02", None)


def test_neighbours_are_bucketed_by_eastern_day_like_the_slate(world):
    world.store(
        "2025-26",
        # 11:30 pm ET on Jan 2 and 12:30 am ET on Jan 4 (UTC dates differ).
        _row("0022500001", "2025-26", "2026-01-03T04:30:00+00:00"),
        _row("0022500002", "2025-26", "2026-01-04T05:30:00+00:00"),
    )

    assert world.neighbours("2026-01-03") == ("2026-01-02", "2026-01-04")
    assert [g["game_id"] for g in world.slate.get_slate("2026-01-04")["games"]] == ["0022500002"]


def test_an_all_star_only_day_is_not_a_game_day(world):
    world.store(
        "2025-26",
        _row("0022500001", "2025-26", "2026-02-12T00:00:00+00:00"),
        _row("0032500001", "2025-26", "2026-02-16T01:00:00+00:00", "All-Star Game"),
        _row("0022500002", "2025-26", "2026-02-20T00:00:00+00:00"),
    )

    assert world.neighbours("2026-02-16") == ("2026-02-11", "2026-02-19")
    assert world.slate.get_slate("2026-02-15")["games"] == []


def _unlisted(n: int, at: str):
    """An All-Star event the SQL prefix filter cannot see (an unknown ID prefix)."""

    return _row(f"ASG{n:07d}", "2025-26", at, "All-Star Game")


def test_a_long_run_of_unlisted_events_does_not_hide_the_nearest_game_day(world):
    exhibitions = [_unlisted(n, f"2026-02-{15 + n // 60:02d}T0{n % 6}:00:00+00:00") for n in range(1, 130)]
    world.store(
        "2025-26",
        _row("0022500001", "2025-26", "2026-02-10T00:00:00+00:00"),
        *exhibitions,
        _row("0022500002", "2025-26", "2026-02-25T00:00:00+00:00"),
    )

    assert world.neighbours("2026-02-20") == ("2026-02-09", "2026-02-24")


class _Counting:
    """A catalog proxy that counts the rows and seeks a search reads."""

    def __init__(self, inner):
        self.inner = inner
        self.rows = 0
        self.pages = 0
        self.seasons = 0

    def get_events_before(self, *args, **kwargs):
        page = self.inner.get_events_before(*args, **kwargs)
        self.pages += 1
        self.rows += len(page)
        return page

    def get_events_after(self, *args, **kwargs):
        page = self.inner.get_events_after(*args, **kwargs)
        self.pages += 1
        self.rows += len(page)
        return page

    def get_adjacent_season(self, *args, **kwargs):
        self.seasons += 1
        return self.inner.get_adjacent_season(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.inner, name)


def test_skipping_events_on_one_timestamp_reads_each_once(world):
    same_instant = "2026-02-14T01:00:00+00:00"
    world.store(
        "2025-26",
        _row("0022500001", "2025-26", "2026-02-10T00:00:00+00:00"),
        *[_unlisted(n, same_instant) for n in range(1, 601)],
    )
    counting = _Counting(world.catalog)
    world.slate.event_catalog = counting

    assert world.neighbours("2026-02-20") == ("2026-02-09", None)

    # 600 excluded events, one listed: each is fetched once, 50 to a page.
    assert counting.rows == 601
    assert counting.pages == 14  # 13 pages back (12 full), 1 empty page forward


def test_a_non_numeric_003_id_is_a_game_when_its_own_slate_lists_it(world):
    world.store(
        "2025-26",
        _row("0022500001", "2025-26", "2026-02-10T00:00:00+00:00"),
        # Only a numeric 003 prefix means All-Star; this is a Regular Season game.
        _row("003ABC0001", "2025-26", "2026-02-14T01:00:00+00:00"),
        _row("0032500001", "2025-26", "2026-02-15T01:00:00+00:00", "All-Star Game"),
    )

    assert [g["game_id"] for g in world.slate.get_slate("2026-02-13")["games"]] == ["003ABC0001"]
    assert world.neighbours("2026-02-20") == ("2026-02-13", None)


def test_skipping_events_on_one_timestamp_forward_reads_each_once(world):
    same_instant = "2026-02-25T01:00:00+00:00"
    world.store(
        "2025-26",
        _row("0022500001", "2025-26", "2026-02-10T00:00:00+00:00"),
        *[_unlisted(n, same_instant) for n in range(1, 601)],
        _row("0022500002", "2025-26", "2026-03-05T00:00:00+00:00"),
    )
    counting = _Counting(world.catalog)
    world.slate.event_catalog = counting

    assert world.neighbours("2026-02-20") == ("2026-02-09", "2026-03-04")

    # Forward: 600 excluded events on one instant plus the listed one.
    assert counting.rows == 1 + 601


def test_a_neighbour_search_issues_only_seeks_never_a_season_scan(world):
    for start in range(2018, 2026):
        season = f"{start}-{str(start + 1)[-2:]}"
        world.store(
            season,
            _row(f"002{str(start)[2:]}00001", season, f"{start + 1}-01-03T00:00:00+00:00"),
        )
    statements = []

    def record(conn, cursor, statement, *rest):
        statements.append(statement.upper())

    event.listen(world.engine, "before_cursor_execute", record)
    try:
        assert world.neighbours("2026-01-10")[0] == "2026-01-02"
    finally:
        event.remove(world.engine, "before_cursor_execute", record)

    season_reads = [s for s in statements if s.startswith("SELECT EVENT_CATALOG.SEASON")]
    assert not any("DISTINCT" in s for s in statements)
    assert season_reads and all("LIMIT" in s for s in season_reads)
    assert len(season_reads) <= 4


def test_the_season_search_does_not_grow_with_stored_history(world):
    for start in range(2018, 2026):
        season = f"{start}-{str(start + 1)[-2:]}"
        world.store(
            season,
            _row(f"00{str(start)[2:]}00001".ljust(10, "0")[:10], season, f"{start + 1}-01-03T00:00:00+00:00"),
        )
    counting = _Counting(world.catalog)
    world.slate.event_catalog = counting

    assert world.neighbours("2026-01-10")[0] == "2026-01-02"
    assert counting.seasons <= 4


def test_summer_events_bucket_on_daylight_time(world):
    world.store(
        "2025-26",
        # 12:30 am EDT June 15 and June 20: a fixed UTC-5 would say the 14th and 19th.
        _row("0042500001", "2025-26", "2026-06-15T04:30:00+00:00"),
        _row("0042500002", "2025-26", "2026-06-20T04:30:00+00:00"),
    )

    assert world.neighbours("2026-06-16") == ("2026-06-15", "2026-06-20")
    assert [g["game_id"] for g in world.slate.get_slate("2026-06-15")["games"]] == ["0042500001"]


def test_neighbours_across_the_spring_forward_day(world):
    world.store(
        "2025-26",
        _row("0022500001", "2025-26", "2026-03-07T04:30:00+00:00"),  # Mar 6, 11:30 pm EST
        _row("0022500002", "2025-26", "2026-03-09T04:30:00+00:00"),  # Mar 9, 12:30 am EDT
    )

    assert world.neighbours("2026-03-08") == ("2026-03-06", "2026-03-09")
    world.assert_matches_the_slate_itself("2026-03-05", "2026-03-10")


def test_neighbours_across_the_fall_back_day(world):
    world.store(
        "2026-27",
        _row("0022600001", "2026-27", "2026-11-01T03:30:00+00:00"),  # Oct 31, 11:30 pm EDT
        _row("0022600002", "2026-27", "2026-11-02T04:30:00+00:00"),  # Nov 1, 11:30 pm EST
        _row("0022600003", "2026-27", "2026-11-04T01:00:00+00:00"),  # Nov 3, 8 pm EST
    )

    assert world.neighbours("2026-11-02") == ("2026-11-01", "2026-11-03")
    assert world.neighbours("2026-11-01") == ("2026-10-31", "2026-11-03")
    world.assert_matches_the_slate_itself("2026-10-29", "2026-11-05")


def test_across_a_season_boundary_each_neighbour_is_listed_by_its_own_slate(world):
    world.store(
        "2025-26",
        _row("0042500001", "2025-26", "2026-06-14T00:00:00+00:00"),  # Jun 13 ET
    )
    world.store(
        "2026-27",
        _row("0022600001", "2026-27", "2026-10-22T00:00:00+00:00"),  # Oct 21 ET
    )

    assert world.neighbours("2026-07-15") == ("2026-06-13", "2026-10-21")
    assert world.neighbours("2026-10-21") == ("2026-06-13", None)
    assert world.neighbours("2026-06-13") == (None, "2026-10-21")
    world.assert_matches_the_slate_itself("2026-06-01", "2026-10-30")


def test_the_offseason_before_the_new_schedule_reads_the_previous_season(world):
    world.store(
        "2025-26",
        _row("0042500001", "2025-26", "2026-06-14T00:00:00+00:00"),
    )

    # October 25 contains no 2026-27 events, so it reads 2025-26 (empty there).
    assert world.neighbours("2026-10-25") == ("2026-06-13", None)
    assert world.listed("2026-10-25") is False
    world.assert_matches_the_slate_itself("2026-06-10", "2026-10-30")


def test_a_preseason_day_its_own_slate_does_not_read_is_not_a_neighbour(world):
    world.store(
        "2025-26",
        _row("0042500001", "2025-26", "2026-06-14T00:00:00+00:00"),
    )
    world.store(
        "2026-27",
        # Sept 30 ET belongs to the 2025-26 season window, whose Slate lists
        # only 2025-26 events, so this 2026-27 preseason game never appears.
        _row("0012600001", "2026-27", "2026-09-30T23:00:00+00:00"),
        _row("0022600001", "2026-27", "2026-10-22T00:00:00+00:00"),
    )

    assert world.listed("2026-09-30") is False
    assert world.neighbours("2026-07-15") == ("2026-06-13", "2026-10-21")
    world.assert_matches_the_slate_itself("2026-06-10", "2026-10-30")


def test_a_pin_counts_only_the_pinned_seasons_events(tmp_path):
    pinned = _World(_sqlite(tmp_path), pin="2026-27")
    pinned.store("2025-26", _row("0042500001", "2025-26", "2026-06-14T00:00:00+00:00"))
    pinned.store(
        "2026-27",
        _row("0022600001", "2026-27", "2026-10-22T00:00:00+00:00"),
        _row("0022600002", "2026-27", "2026-10-25T00:00:00+00:00"),
    )

    assert pinned.neighbours("2026-10-23") == ("2026-10-21", "2026-10-24")
    assert pinned.neighbours("2026-10-21") == (None, "2026-10-24")
    assert pinned.neighbours("2026-07-15") == (None, "2026-10-21")
    pinned.assert_matches_the_slate_itself("2026-06-10", "2026-10-30")


def test_a_single_game_day_has_no_neighbours(world):
    world.store("2025-26", _row("0022500001", "2025-26", "2026-01-03T00:00:00+00:00"))

    assert world.neighbours("2026-01-02") == (None, None)
    assert world.neighbours("2026-03-01") == ("2026-01-02", None)


def test_no_stored_schedule_is_not_found(world):
    with pytest.raises(ResourceNotFoundError):
        world.slate.get_slate("2026-01-02")
