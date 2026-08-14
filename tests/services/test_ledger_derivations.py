"""Pure ledger derivation and ranking contracts."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json

from app.services.ledger_derivations import (
    competition_ranks,
    derive_assist_location_facts,
    derive_player_per36_facts,
    derive_traditional_opponent_facts,
    materialize_assist_location_window,
    materialize_team_window,
)
from app.services.ledger_materialization import (
    LedgerMaterializationService,
    LedgerMaterializationUnavailable,
)
from app.services.ledger_parity import LedgerParityArtifactRepository, compare_ledger_to_legacy
from app.services.collection_control import PublicationService
from app.services.canonical_game_ledger import (
    CanonicalGame,
    CanonicalGameLedgerRepository,
    PlayerGameFact,
    TeamGameFact,
)
from app.migrations import run_migrations
from sqlalchemy import create_engine, select
from app.models.collection_control import (
    CollectionManifest,
    CollectionObservation,
    PublicationVersion,
)
from app.models.canonical_game_ledger import LedgerParityArtifact, LedgerPublication
from tests.services.test_canonical_game_ledger import _game


class _ParityReader:
    def read(self, stream_key):
        raise ValueError(f"{stream_key} diagnostic unavailable")


def test_traditional_opponent_is_derived_from_the_other_team_fact():
    game = _game()
    facts = derive_traditional_opponent_facts((game,))

    assert len(facts) == 2
    assert facts[0].opponent_team_id != facts[0].team_id
    assert {fact.opponent_points for fact in facts} == {16}


def test_assist_locations_are_counted_without_provider_percentage_sums():
    game = _game()
    players = tuple(
        replace(
            player,
            two_point_assists=1,
            three_point_assists=2,
            arc3_assists=1,
            corner3_assists=0,
            at_rim_assists=1,
            short_mid_range_assists=0,
            long_mid_range_assists=0,
        )
        for player in game.player_facts
    )
    facts = derive_assist_location_facts((replace(game, player_facts=players),))

    assert len(facts) == 4
    assert facts[0].location_total == 2


def test_per36_aggregates_traded_player_counts_and_retains_game_teams():
    game = _game()
    traded = game.player_facts[0]
    second_game = replace(
        game,
        game_id="0022400002",
        game_date=game.game_date.replace(day=16),
        player_facts=(replace(traded, team_id=game.away_team_id, team_tricode=game.away_team_tricode), *game.player_facts[1:]),
    )
    facts = derive_player_per36_facts((game, second_game), season="2024-25")
    result = next(fact for fact in facts if fact.player_id == traded.player_id)

    assert result.game_count == 2
    assert result.team_ids_at_game == (game.home_team_id, game.away_team_id)
    assert result.points_per36 == 14.4


def test_competition_ranks_are_deterministic_and_leave_gaps_after_ties():
    assert competition_ranks({3: 10.0, 2: 10.0, 1: 7.0}) == {2: 1, 3: 1, 1: 3}
    assert competition_ranks({3: 10.0, 2: 10.0, 1: 7.0}, descending=False) == {1: 1, 2: 2, 3: 2}


def _league_games():
    teams = list(range(1, 31))
    games = []
    for round_index in range(15):
        for pair_index in range(15):
            home = teams[pair_index]
            away = teams[-1 - pair_index]
            game_id = f"00225{round_index:02d}{pair_index:03d}"
            facts = []
            team_facts = []
            for team_id, opponent_id, is_home in ((home, away, True), (away, home, False)):
                assists = 4 + (team_id % 3)
                facts.append(PlayerGameFact(
                    player_id=1000 + team_id,
                    player_name=f"Player {team_id}",
                    team_id=team_id,
                    team_tricode=f"T{team_id:02d}",
                    minutes=240.0,
                    points=10 + team_id,
                    field_goals_made=5,
                    field_goals_attempted=10,
                    two_pointers_made=4,
                    two_pointers_attempted=8,
                    three_pointers_made=1,
                    three_pointers_attempted=2,
                    free_throws_made=0,
                    free_throws_attempted=0,
                    offensive_rebounds=1,
                    defensive_rebounds=4,
                    rebounds=5,
                    assists=assists,
                    turnovers=1,
                    steals=1,
                    blocks=0,
                    personal_fouls=1,
                    two_point_assists=2,
                    three_point_assists=2,
                    arc3_assists=1,
                    corner3_assists=1,
                    at_rim_assists=1,
                    short_mid_range_assists=0,
                    long_mid_range_assists=0,
                ))
                team_facts.append(TeamGameFact(
                    team_id=team_id,
                    team_tricode=f"T{team_id:02d}",
                    opponent_team_id=opponent_id,
                    opponent_team_tricode=f"T{opponent_id:02d}",
                    is_home=is_home,
                    points=10 + team_id,
                    field_goals_made=5,
                    field_goals_attempted=10,
                    two_pointers_made=4,
                    two_pointers_attempted=8,
                    three_pointers_made=1,
                    three_pointers_attempted=2,
                    offensive_rebounds=1,
                    defensive_rebounds=4,
                    rebounds=5,
                    assists=assists,
                    turnovers=1,
                    steals=1,
                    personal_fouls=1,
                    team_minutes=48.0,
                ))
            games.append(CanonicalGame(
                game_id=game_id,
                season="2025-26",
                game_date=date(2025, 10, 1) + timedelta(days=round_index),
                home_team_id=home,
                home_team_tricode=f"T{home:02d}",
                away_team_id=away,
                away_team_tricode=f"T{away:02d}",
                team_facts=tuple(team_facts),
                player_facts=tuple(facts),
                source_observation_id=f"obs:{game_id}",
                retrieved_at=datetime(2025, 11, 1, tzinfo=timezone.utc),
                participant_ids_by_team=((home, (1000 + home,)), (away, (1000 + away,))),
            ).with_checksum())
        teams = [teams[0], teams[-1], *teams[1:-1]]
    return tuple(games)


def test_exact_l15_is_league_complete_and_defensive_ranks_are_ascending():
    games = _league_games()
    expected = frozenset(game.game_id for game in games)
    expected_by_team = {
        team_id: frozenset(
            game.game_id for game in games
            if team_id in {game.home_team_id, game.away_team_id}
        )
        for team_id in range(1, 31)
    }
    window = materialize_team_window(
        games,
        season="2025-26",
        as_of=date(2025, 10, 15),
        window_games=15,
        expected_game_ids=expected,
        expected_team_game_ids=expected_by_team,
        team_ids=frozenset(range(1, 31)),
    )
    assert window.complete and len(window.teams) == 30
    lowest = min(window.teams, key=lambda team: team.per48["points"])
    assert lowest.competition_rank["points"] == 1
    assist = materialize_assist_location_window(
        games,
        season="2025-26",
        as_of=date(2025, 10, 15),
        window_games=15,
        expected_game_ids=expected,
        expected_team_game_ids=expected_by_team,
        team_ids=frozenset(range(1, 31)),
    )
    assert assist.complete and len(assist.teams) == 30
    assert all(team.game_count == 15 for team in assist.teams)


def test_materialization_persists_full_payloads_and_inactive_control_versions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'materialization.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    publications = PublicationService(
        engine,
        clock=lambda: datetime(2025, 11, 1, tzinfo=timezone.utc),
    )
    publications.register_default_streams()
    games = _league_games()
    repository.replace_games_atomic(games)
    candidate_cutoff = datetime(2025, 10, 15, tzinfo=timezone.utc)
    with engine.begin() as connection:
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="ledger-manifest", season="2025-26",
            cutoff=candidate_cutoff,
            collect_before=datetime(2025, 11, 2, tzinfo=timezone.utc),
            accepted_versions="[1]", scopes='["canonical_game_ledger"]',
            checksum="ledger-manifest", status="expired",
            created_at=datetime(2025, 10, 15, tzinfo=timezone.utc),
        ))
        connection.execute(CollectionObservation.__table__.insert(), [
            {
                "observation_id": game.source_observation_id,
                "client_observation_id": game.source_observation_id,
                "collector_id": "test",
                "manifest_id": "ledger-manifest",
                "environment": "testing",
                "provider": "pbp",
                "observation_type": "canonical_game_ledger",
                "scope": json.dumps({
                    "game_id": game.game_id,
                    "surface": "canonical_game_ledger",
                }),
                "season": game.season,
                "cutoff": candidate_cutoff,
                "schema_version": 1,
                "checksum": game.checksum,
                "payload": "{}",
                "payload_bytes": 2,
                "retrieved_at": game.retrieved_at,
                "accepted_at": game.retrieved_at,
            }
            for game in games
        ])
    expected = frozenset(game.game_id for game in games)
    expected_by_team = {
        team_id: frozenset(
            game.game_id for game in games
            if team_id in {game.home_team_id, game.away_team_id}
        )
        for team_id in range(1, 31)
    }

    result = LedgerMaterializationService(
        repository,
        parity_repository=LedgerParityArtifactRepository(engine),
        parity_reader=_ParityReader(),
        publication_service=publications,
    ).compose(
        games,
        season="2025-26",
        as_of=date(2025, 10, 15),
        expected_game_ids=expected,
        expected_l15_game_ids=expected_by_team,
        team_ids=frozenset(range(1, 31)),
        require_assist_locations=True,
    )

    assert len(result.assist_location_season.teams) == 30
    with engine.connect() as connection:
        ledger_payloads = connection.execute(select(LedgerPublication.payload)).scalars().all()
        candidates = connection.execute(select(PublicationVersion).where(
            PublicationVersion.status == "candidate",
        )).all()
        parity = connection.execute(
            select(LedgerParityArtifact.__table__)
        ).mappings().all()
    assert len(ledger_payloads) == 8
    assert all(payload not in {"", "{}", "[]"} for payload in ledger_payloads)
    assert len(candidates) == 6
    assert len(parity) == 4
    assert {row["stream_key"] for row in parity} == {
        "player_game_logs",
        "traditional_opponent_season",
        "traditional_opponent_l15",
        "player_per36",
    }
    assert all(row["status"] == "pending_adjudication" for row in parity)
    assert all(
        row["publication_id"] and len(row["payload_checksum"]) == 64
        for row in parity
    )
    traditional_payload = json.loads(repository.get_publication(
        "traditional_opponent_season",
        season="2025-26",
        window_kind="season",
        window_games=0,
        as_of=date(2025, 10, 15),
    ).payload)
    assert len(traditional_payload) == 30
    assert {"per48", "league_average", "population_sigma", "competition_rank"} <= set(
        traditional_payload[0]
    )


def test_missing_assist_evidence_does_not_block_independent_streams(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'independent.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    games = tuple(
        replace(
            game,
            player_facts=tuple(
                replace(
                    player,
                    two_point_assists=None,
                    three_point_assists=None,
                    arc3_assists=None,
                    corner3_assists=None,
                    at_rim_assists=None,
                    short_mid_range_assists=None,
                    long_mid_range_assists=None,
                )
                for player in game.player_facts
            ),
            checksum=None,
        ).with_checksum()
        for game in _league_games()
    )
    repository.replace_games_atomic(games)
    expected = frozenset(game.game_id for game in games)
    expected_by_team = {
        team_id: frozenset(
            game.game_id for game in games
            if team_id in {game.home_team_id, game.away_team_id}
        )
        for team_id in range(1, 31)
    }

    result = LedgerMaterializationService(
        repository,
        parity_repository=LedgerParityArtifactRepository(engine),
        parity_reader=_ParityReader(),
    ).compose(
        games,
        season="2025-26",
        as_of=date(2025, 10, 15),
        expected_game_ids=expected,
        expected_l15_game_ids=expected_by_team,
        team_ids=frozenset(range(1, 31)),
    )

    assert result.assist_location_season is None
    with engine.connect() as connection:
        streams = set(connection.execute(select(LedgerPublication.stream_key)).scalars())
    assert {"player_game_logs", "traditional_opponent_season", "traditional_opponent_l15", "player_per36"} <= streams
    assert not {"assist_locations_season", "assist_locations_l15"} & streams


def test_parity_report_compares_shared_primitives_but_ignores_provider_rates():
    game = _game()
    player = game.player_facts[0]
    report = compare_ledger_to_legacy(
        (game,),
        ({"game_id": game.game_id, "player_id": player.player_id, "PTS": player.points, "FG_PCT": 0.5},),
        season="2024-25",
    )
    assert not report.exact
    assert report.compared_count == 1
    assert any(item.classification == "missing_legacy_identity" for item in report.differences)

    different = compare_ledger_to_legacy(
        (game,),
        ({"game_id": game.game_id, "player_id": player.player_id, "PTS": player.points + 1},),
        season="2024-25",
    )
    assert different.adjudication_required
    assert any(item.field == "points" for item in different.differences)


def test_materialization_fails_closed_before_a_30_team_window(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ledger.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    service = LedgerMaterializationService(
        repository,
        parity_repository=LedgerParityArtifactRepository(engine),
        parity_reader=_ParityReader(),
    )

    try:
        service.compose((_game(),), season="2024-25", as_of=_game().game_date)
    except LedgerMaterializationUnavailable as error:
        assert "governed game IDs" in str(error) or "L15" in str(error)
    else:
        raise AssertionError("partial league window unexpectedly published")


def test_materialization_rejects_extra_stored_game_identity(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'extra.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    game = _game()
    repository.replace_game(game)
    service = LedgerMaterializationService(
        repository,
        parity_repository=LedgerParityArtifactRepository(engine),
        parity_reader=_ParityReader(),
    )

    try:
        service.compose(
            (),
            season=game.season,
            as_of=game.game_date,
            expected_game_ids=frozenset(),
        )
    except LedgerMaterializationUnavailable as error:
        assert "exactly equal" in str(error)
    else:
        raise AssertionError("extra stored game unexpectedly accepted")


def test_historical_materialization_ignores_later_ledger_rows(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'historical.sqlite3'}")
    run_migrations(engine)
    repository = CanonicalGameLedgerRepository(engine)
    games = _league_games()
    later = replace(
        games[0],
        game_id="later-game",
        game_date=date(2025, 10, 16),
        source_observation_id="later-observation",
        checksum=None,
    ).with_checksum()
    repository.replace_games_atomic((*games, later))
    expected = frozenset(game.game_id for game in games)
    expected_by_team = {
        team_id: frozenset(
            game.game_id for game in games
            if team_id in {game.home_team_id, game.away_team_id}
        )
        for team_id in range(1, 31)
    }

    result = LedgerMaterializationService(
        repository,
        parity_repository=LedgerParityArtifactRepository(engine),
        parity_reader=_ParityReader(),
    ).compose(
        (*games, later), season="2025-26", as_of=date(2025, 10, 15),
        expected_game_ids=expected, expected_l15_game_ids=expected_by_team,
        team_ids=frozenset(range(1, 31)),
    )

    assert result.season_window.complete
    assert "later-game" not in result.season_window.governed_game_ids
