"""Reads without a live game-log publication stay on the last published season.

During the 2026-27 calendar a rollback that disables ``player_game_logs`` (or a
failing pointer or authority read) used to hand requests to the calendar
season, whose rows the nightly refresh writes unverified (#392). The fallback
is the season of the latest unrevoked activation in the pointer history.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text

from app.config.settings import MatchupScoreSettings, NBASeasonSettings, RuntimeSettings
from app.migrations import run_migrations
from app.services.collection_control import PublicationService
from app.services.database_first_activation import DatabaseFirstPublicationReader
from app.services.research_season import published_capture, research_season, research_service
from tests.services.test_matchup_selection_service import _log_row
from tests.support.season_rollover import cut_over_if_held

STREAM = "player_game_logs"
CUTOFF = datetime(2026, 1, 15, tzinfo=UTC)
LAST = "2025-26"
CALENDAR = "2026-27"


def _settings(*, pinned: str | None = None) -> RuntimeSettings:
    nba = (
        NBASeasonSettings(current_season=pinned)
        if pinned is not None
        # The calendar default after 2026-10-01, without depending on today.
        else NBASeasonSettings.model_construct(_fields_set=set(), current_season=CALENDAR)
    )
    return RuntimeSettings(environment="testing", nba=nba, matchup_scores=MatchupScoreSettings())


@pytest.fixture
def lifecycle(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fallback.sqlite3'}")
    run_migrations(engine)
    publications = PublicationService(engine, clock=lambda: CUTOFF)
    publications.register_stream(
        STREAM,
        provider="ledger",
        owner="railway",
        required_observations=(),
        publication_strategy="replace",
        enabled=True,
        freshness_rule="cutoff_current",
    )
    return engine, publications, DatabaseFirstPublicationReader(engine, clock=lambda: CUTOFF)


def _compose(publications, season, **kwargs):
    row = {
        **_log_row(game_id="0022500001", game_date="2026-01-02", minutes=30.0, points=10),
        "season": season,
    }
    return cut_over_if_held(
        publications,
        publications.compose(STREAM, season=season, cutoff=CUTOFF, payload={"rows": [row]}, **kwargs),
    )


def _disable_stream(engine):
    with engine.begin() as connection:
        connection.execute(text("UPDATE publication_streams SET enabled = :off"), {"off": False})


def test_a_disabled_stream_reads_the_last_published_season_not_the_calendar(lifecycle):
    engine, publications, reader = lifecycle
    _compose(publications, LAST)
    _disable_stream(engine)

    assert research_season(_settings(), reader) == LAST


def test_a_disabled_stream_scopes_the_service_to_the_last_published_season(lifecycle):
    engine, publications, reader = lifecycle
    _compose(publications, LAST)
    _disable_stream(engine)

    class Service:
        settings = _settings()

    service = Service()
    service.publication_reader = reader
    scoped = research_service(service, reader)

    assert scoped.settings.nba.current_season == LAST
    assert service.settings.nba.current_season == CALENDAR


def test_a_disabled_stream_captures_in_the_last_published_season(lifecycle):
    engine, publications, reader = lifecycle
    _compose(publications, LAST)
    _disable_stream(engine)

    season, snapshot = published_capture(_settings(), reader, lambda season: season)

    assert season == LAST
    assert snapshot.fallback == LAST


def test_an_active_cutover_season_is_the_published_season(lifecycle):
    _engine, publications, reader = lifecycle
    first = _compose(publications, LAST)
    _compose(publications, CALENDAR, expected_fence=first.fence)

    assert research_season(_settings(), reader) == CALENDAR


def test_a_disabled_stream_after_a_cutover_stays_on_the_cutover_season(lifecycle):
    engine, publications, reader = lifecycle
    first = _compose(publications, LAST)
    _compose(publications, CALENDAR, expected_fence=first.fence)
    _disable_stream(engine)

    assert research_season(_settings(), reader) == CALENDAR


def test_a_reverted_cutover_falls_back_to_the_previous_season(lifecycle):
    engine, publications, reader = lifecycle
    first = _compose(publications, LAST)
    second = _compose(publications, CALENDAR, expected_fence=first.fence)
    publications.revert_season_cutover(
        {
            "moved": [
                {
                    "stream_key": STREAM,
                    "from_publication_id": first.publication_id,
                    "to_publication_id": second.publication_id,
                    "from_previous_publication_id": None,
                }
            ],
            "fences": {STREAM: second.fence},
        },
        reason="cutover reverted",
    )
    _disable_stream(engine)

    assert research_season(_settings(), reader) == LAST


def test_no_publication_history_keeps_the_settings_season(lifecycle):
    _engine, _publications, reader = lifecycle

    assert research_season(_settings(), reader) == CALENDAR
    assert research_season(_settings(), None) == CALENDAR


def test_a_pin_wins_over_the_publication_history(lifecycle):
    engine, publications, reader = lifecycle
    _compose(publications, LAST)
    _disable_stream(engine)

    assert research_season(_settings(pinned=CALENDAR), reader) == CALENDAR
