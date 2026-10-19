"""Reads without a live game-log publication stay on the last published season.

During the 2026-27 calendar a rollback that disables ``player_game_logs`` (or a
failing pointer or authority read) used to hand requests to the calendar
season, whose rows the nightly refresh writes unverified (#392). The fallback
is the season the stream's pointer names, else its pointer history's.
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


def _cut_over(publications):
    first = _compose(publications, LAST)
    second = _compose(publications, CALENDAR, expected_fence=first.fence)
    return first, second


def _revert(publications, first, second):
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


def _sql(engine, statement, **params):
    with engine.begin() as connection:
        connection.execute(text(statement), params)


def _disable_stream(engine):
    _sql(engine, "UPDATE publication_streams SET enabled = :off", off=False)


def _clear_pointer(engine):
    _sql(engine, "UPDATE publication_pointers SET active_publication_id = NULL")


def _revoke_history(engine, *, season=None):
    _sql(
        engine,
        "UPDATE publication_pointer_history SET revoked_at = :now"
        " WHERE (:season IS NULL OR season = :season)",
        now=CUTOFF,
        season=season,
    )


def _capture(reader):
    def capture(season):
        return reader.snapshot((STREAM,), season=season, projection_only_keys=frozenset({STREAM}))

    return capture


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
    scoped = research_service(service, reader)

    assert scoped.settings.nba.current_season == LAST
    assert service.settings.nba.current_season == CALENDAR


def test_a_disabled_stream_snapshot_is_captured_in_the_last_published_season(lifecycle):
    engine, publications, reader = lifecycle
    _compose(publications, LAST)
    _disable_stream(engine)

    season, snapshot = published_capture(_settings(), reader, _capture(reader))

    assert season == LAST
    assert snapshot.season == LAST
    assert not snapshot.read(STREAM).available


def test_an_unreadable_publication_stays_on_the_last_published_season(lifecycle):
    engine, publications, reader = lifecycle
    _compose(publications, LAST)
    _sql(engine, "DELETE FROM publication_player_game_logs")

    season, snapshot = published_capture(_settings(), reader, _capture(reader))

    assert not snapshot.read(STREAM).available
    assert research_season(_settings(), reader) == LAST
    assert (season, snapshot.season) == (LAST, LAST)


def test_an_active_cutover_season_is_the_published_season(lifecycle):
    _engine, publications, reader = lifecycle
    _cut_over(publications)

    season, snapshot = published_capture(_settings(), reader, _capture(reader))

    assert research_season(_settings(), reader) == CALENDAR
    assert (season, snapshot.season) == (CALENDAR, CALENDAR)


def test_a_disabled_stream_after_a_cutover_stays_on_the_cutover_season(lifecycle):
    engine, publications, reader = lifecycle
    _cut_over(publications)
    _disable_stream(engine)

    assert research_season(_settings(), reader) == CALENDAR


def test_a_reverted_cutover_falls_back_to_the_previous_season(lifecycle):
    engine, publications, reader = lifecycle
    first, second = _cut_over(publications)
    _revert(publications, first, second)
    _disable_stream(engine)

    season, snapshot = published_capture(_settings(), reader, _capture(reader))

    assert research_season(_settings(), reader) == LAST
    assert (season, snapshot.season) == (LAST, LAST)


def test_a_revert_between_lookup_and_capture_cannot_split_the_season_from_the_snapshot(lifecycle):
    engine, publications, reader = lifecycle
    first, second = _cut_over(publications)
    _disable_stream(engine)

    def capture(season):
        _revert(publications, first, second)  # lands after the caller chose its fallback
        return _capture(reader)(season)

    season, snapshot = published_capture(_settings(), reader, capture)

    assert (season, snapshot.season) == (LAST, LAST)
    assert snapshot.read(STREAM).fence == 3


def test_corrections_that_revoked_every_activation_keep_the_last_published_season(lifecycle):
    engine, publications, reader = lifecycle
    _compose(publications, LAST)
    _disable_stream(engine)
    _revoke_history(engine)

    assert research_season(_settings(), reader) == LAST
    _clear_pointer(engine)
    assert research_season(_settings(), reader) == LAST


def test_history_prefers_a_standing_activation_over_a_revoked_newer_one(lifecycle):
    engine, publications, reader = lifecycle
    _cut_over(publications)
    _disable_stream(engine)
    _clear_pointer(engine)
    _revoke_history(engine, season=CALENDAR)

    assert research_season(_settings(), reader) == LAST


def test_history_follows_the_latest_activation_across_a_cross_season_rollback(lifecycle):
    engine, publications, reader = lifecycle
    _first, second = _cut_over(publications)
    publications.rollback(STREAM, reason="back to last season", expected_fence=second.fence)
    _disable_stream(engine)
    _clear_pointer(engine)

    assert research_season(_settings(), reader) == LAST


def test_another_stream_never_decides_the_season(lifecycle):
    engine, publications, reader = lifecycle
    first, held = _cut_over(publications)
    _revert(publications, first, held)
    _disable_stream(engine)
    _sql(
        engine,
        "INSERT INTO publication_pointers (stream_key, active_publication_id, fence, updated_at)"
        " VALUES ('other_stream', :pub, 9, :now)",
        pub=held.publication_id,
        now=CUTOFF,
    )
    _sql(
        engine,
        "INSERT INTO publication_pointer_history"
        " (history_id, stream_key, publication_id, season, fence, activated_at)"
        " VALUES ('other-1', 'other_stream', :pub, :season, 9, :now)",
        pub=held.publication_id,
        season=CALENDAR,
        now=CUTOFF,
    )

    assert research_season(_settings(), reader) == LAST
    _clear_pointer(engine)
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


def test_a_snapshot_with_no_pointer_publication_is_captured_in_the_history_season(lifecycle):
    engine, publications, reader = lifecycle
    _compose(publications, LAST)
    _clear_pointer(engine)

    season, snapshot = published_capture(_settings(), reader, _capture(reader))

    assert (season, snapshot.season) == (LAST, LAST)
