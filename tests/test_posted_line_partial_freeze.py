"""A game whose provider freezes only partly committed (crf04/statsplus#238)."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy import event as sqlalchemy_event

from app.migrations import run_migrations
from app.providers.dfs import AthleteEvidence, NBAMarketQuery, TeamEvidence
from app.services.posted_lines import PostedLinesReader
from app.services.projection_archive import (
    FrozenReplayRefused,
    LatestProjectionPlayerPoolReader,
    ProjectionArchive,
    ProjectionArchiveReadScope,
    projection_query_key,
)
from app.services.statistic_catalog import StatisticCatalog
from tests.services.test_projection_archive import (
    GAME_ID,
    OBSERVED_AT,
    SEASON,
    _line_market,
    _line_snapshot,
)

PROVIDERS = ("dabble", "prizepicks")
TIP = OBSERVED_AT + timedelta(minutes=5)
GSW = TeamEvidence(canonical_id=1610612744, abbreviation="GSW")
LAL = TeamEvidence(canonical_id=1610612747, abbreviation="LAL")
QUERY = NBAMarketQuery(season=SEASON)


def _archive(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'archive.db'}")
    run_migrations(engine)
    catalog = StatisticCatalog.load_default()
    return engine, catalog, ProjectionArchive(engine, catalog)


def _points(catalog, provider, threshold, *, player_id=2544, name="LeBron James", team=LAL):
    market = _line_market(
        catalog,
        provider=provider,
        market_id=f"points-{player_id}",
        statistic_id="points",
        threshold=threshold,
    )
    return replace(market, athlete=AthleteEvidence(canonical_id=player_id, name=name, team=team), team=team)


def _freeze(archive, providers):
    for provider in providers:
        archive.freeze_closing_projection_set(
            provider=provider, query=QUERY, canonical_game_id=GAME_ID, started_at=TIP, created_at=TIP
        )


def _read(engine, *, started_at=TIP, player_id=2544):
    """His posted line from the real reader, and the providers it replayed."""

    event = dict(
        nba_game_id=GAME_ID,
        scheduled_at=TIP.isoformat(),
        classification="",
        status_code=3,
        status_text="Final",
        first_observed_started_at=started_at.isoformat(),
        home_team_id=1610612744,
        home_team_tricode="GSW",
        away_team_id=1610612747,
        away_team_tricode="LAL",
    )

    class Schedule:
        def count_events(self, season):
            return 1

        def get_events_between(self, *args):
            return [event]

        def get_events_by_ids(self, *args):
            return [event]

    schedule = Schedule()
    pool = LatestProjectionPlayerPoolReader(
        engine,
        tuple(ProjectionArchiveReadScope(provider=p, query=QUERY) for p in PROVIDERS),
        event_reader=schedule,
        clock=lambda: OBSERVED_AT + timedelta(days=90),
    )
    settings = SimpleNamespace(
        nba=SimpleNamespace(model_fields_set={"current_season"}, current_season=SEASON)
    )
    replayed = set()

    @sqlalchemy_event.listens_for(engine, "before_cursor_execute")
    def record_replay(_conn, _cursor, statement, parameters, _context, _many):
        if "projection_materialization_generations" in statement:
            replayed.update(value for value in parameters if value in PROVIDERS)

    result = PostedLinesReader(event_catalog=schedule, player_pool=pool, settings=settings).for_player(
        player_id, OBSERVED_AT.date()
    )
    return result, replayed


def _posted_line(tmp_path, frozen, player_id=2544):
    """His posted line once ``frozen`` froze, and the providers replayed for it."""

    engine, catalog, archive = _archive(tmp_path)
    for provider, offers in (
        ("dabble", (("points", "26.5"),)),
        ("prizepicks", (("points", "27.5"), ("rebounds", "7.5"))),
    ):
        archive.ingest_complete_snapshot(
            _line_snapshot(
                provider,
                tuple(
                    _line_market(
                        catalog, provider=provider, market_id=stat, statistic_id=stat, threshold=value
                    )
                    for stat, value in offers
                ),
            ),
            query=QUERY,
            accepted_at=OBSERVED_AT,
        )
    _freeze(archive, frozen)
    return _read(engine, player_id=player_id)


@pytest.mark.parametrize(
    ("frozen", "replayed"),
    (
        # freeze_closing_projection_sets commits each alphabetically sorted provider
        # separately. A crash after dabble commits leaves this legitimate state.
        (("dabble",), {"prizepicks"}),
        # Both froze: their sets are the whole answer and no archive is replayed.
        (PROVIDERS, set()),
    ),
)
def test_a_frozen_game_keeps_prizepicks_preference_and_replays_only_the_unfrozen(tmp_path, frozen, replayed):
    result, read_from_archive = _posted_line(tmp_path, frozen)

    # Dabble's PTS 26.5 comes from its set; PrizePicks' lines, frozen or read
    # from its archive, win the preference for both markets.
    assert result["lines"] == {
        "PTS": {"value": 27.5, "provider": "prizepicks"},
        "REB": {"value": 7.5, "provider": "prizepicks"},
    }
    assert result["provider_lines"] == {
        "dabble": {"PTS": 26.5},
        "prizepicks": {"PTS": 27.5, "REB": 7.5},
    }
    assert read_from_archive == replayed


def test_a_game_the_archive_never_placed_him_in_replays_nothing(tmp_path):
    """The slate probe reads every started game until it finds his line."""

    result, read_from_archive = _posted_line(tmp_path, ("dabble",), player_id=201939)

    assert result is None
    assert read_from_archive == set()


def _prizepicks_line_left_out_of_its_freeze(tmp_path, *, late_mapping):
    """Both providers froze at the scheduled tip without his PrizePicks 27.5.

    PrizePicks either polled it after the freeze but before the later observed
    start, or carried it under an athlete it had not mapped until after the
    freeze.  A replay at the start includes it either way.
    """

    engine, catalog, archive = _archive(tmp_path)
    teammate = _points(catalog, "prizepicks", "9.5", player_id=12345, name="Other Player")
    his = _points(catalog, "prizepicks", "27.5")
    unmapped = replace(
        his, athlete=AthleteEvidence(provider_id="target-athlete", name="Provider Player", team=LAL)
    )
    archive.ingest_complete_snapshot(
        _line_snapshot("prizepicks", (teammate, unmapped) if late_mapping else (teammate,)),
        query=QUERY,
        accepted_at=OBSERVED_AT,
    )
    archive.ingest_complete_snapshot(
        _line_snapshot("dabble", (_points(catalog, "dabble", "26.5"),)), query=QUERY, accepted_at=OBSERVED_AT
    )
    _freeze(archive, PROVIDERS)
    after_freeze = TIP + timedelta(minutes=1)
    if late_mapping:
        archive.replay_athlete_mapping(
            provider="prizepicks",
            provider_athlete_id="target-athlete",
            canonical_player_id=2544,
            canonical_player_name="LeBron James",
            canonical_team_id=LAL.canonical_id,
            replayed_at=after_freeze,
        )
        return _read(engine)
    archive.ingest_complete_snapshot(
        _line_snapshot("prizepicks", (teammate, his), retrieved_at=after_freeze),
        query=QUERY,
        accepted_at=after_freeze,
    )
    return _read(engine, started_at=TIP + timedelta(minutes=5))


@pytest.mark.parametrize("late_mapping", (False, True), ids=("polled-before-start", "mapped-after-freeze"))
def test_a_provider_frozen_without_him_posts_only_its_set(tmp_path, late_mapping):
    """A provider's closing set is its whole answer, as on the Matchup (statsplus-backend#554)."""

    result, read_from_archive = _prizepicks_line_left_out_of_its_freeze(tmp_path, late_mapping=late_mapping)

    assert result["lines"] == {"PTS": {"value": 26.5, "provider": "dabble"}}
    assert result["provider_lines"] == {"dabble": {"PTS": 26.5}}
    assert read_from_archive == set()


def test_providers_placing_him_on_opposite_sides_take_his_team_from_the_frozen_set(tmp_path):
    """His team is his first row's: Dabble's set rows come before PrizePicks' replay."""

    engine, catalog, archive = _archive(tmp_path)
    for provider, threshold, team in (("dabble", "26.5", LAL), ("prizepicks", "27.5", GSW)):
        archive.ingest_complete_snapshot(
            _line_snapshot(provider, (_points(catalog, provider, threshold, team=team),)),
            query=QUERY,
            accepted_at=OBSERVED_AT,
        )
    _freeze(archive, ("dabble",))

    result, read_from_archive = _read(engine)

    assert result["matchup"] == "LAL @ GSW"
    assert result["provider_lines"] == {"dabble": {"PTS": 26.5}, "prizepicks": {"PTS": 27.5}}
    assert read_from_archive == {"prizepicks"}


def test_a_frozen_line_is_his_after_the_boards_drop_him(tmp_path):
    engine, catalog, archive = _archive(tmp_path)
    for provider, threshold in (("dabble", "26.5"), ("prizepicks", "27.5")):
        archive.ingest_complete_snapshot(
            _line_snapshot(provider, (_points(catalog, provider, threshold),)),
            query=QUERY,
            accepted_at=OBSERVED_AT,
        )
    _freeze(archive, PROVIDERS)
    after_freeze = TIP + timedelta(minutes=1)
    for provider in PROVIDERS:
        archive.ingest_complete_snapshot(
            _line_snapshot(
                provider,
                (_points(catalog, provider, "9.5", player_id=12345, name="Other Player"),),
                retrieved_at=after_freeze,
            ),
            query=QUERY,
            accepted_at=after_freeze,
        )

    result, _ = _read(engine)

    assert result["provider_lines"] == {"dabble": {"PTS": 26.5}, "prizepicks": {"PTS": 27.5}}


def test_a_frozen_pair_refuses_a_replay(tmp_path):
    """Retention keeps no replay input for a frozen pair, so nothing may replay it."""

    engine, catalog, archive = _archive(tmp_path)
    for provider in PROVIDERS:
        archive.ingest_complete_snapshot(
            _line_snapshot(provider, (_points(catalog, provider, "26.5"),)),
            query=QUERY,
            accepted_at=OBSERVED_AT,
        )
    _freeze(archive, ("dabble",))

    def replay(provider):
        with engine.connect() as connection:
            rows = ProjectionArchive._closing_materialization_rows(
                connection,
                provider=provider,
                season=SEASON,
                query_key=projection_query_key(QUERY),
                canonical_game_id=GAME_ID,
                started_at=TIP,
            )
        return [(row["canonical_player_id"], row["market_category"]) for row in rows]

    assert replay("prizepicks") == [(2544, "PTS")]
    with pytest.raises(FrozenReplayRefused):
        replay("dabble")
