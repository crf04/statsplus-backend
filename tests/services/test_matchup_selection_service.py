"""Stored matchup selection composition at the application-service seam."""

from datetime import datetime, timedelta, timezone
import hashlib
import json

from sqlalchemy import create_engine, event, text

from app.config.settings import NBASeasonSettings, RuntimeSettings
from app.migrations import run_migrations
from app.services.collection_control import PublicationService
from app.services.database_first_activation import DatabaseFirstPublicationReader
from app.services.matchup_selection import MatchupSelectionService
from app.services.player_game_log_repository import PlayerGameLogRepository
from app.services.player_pool import PlayerPool, PoolPlayer
from app.services.statistic_catalog import StatisticCatalog


NOW = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
RETRIEVED_AT = datetime(2026, 1, 15, 10, tzinfo=timezone.utc)
SEASON = "2025-26"
GAME_ID = "0022500584"
LAL = 1610612747
BOS = 1610612738
PEER_ID = 201939
MARKETS = ("PTS", "FGA")


class RecordedEvents:
    def count_events(self, season):
        return 1

    def get_events(self, season):
        return [
            {
                "nba_game_id": GAME_ID,
                "classification": "Regular Season",
                "home_team_id": BOS,
                "away_team_id": LAL,
            }
        ]


class RecordedPool:
    def get_pool_for_game(self, *, season, game_id):
        return PlayerPool(
            players=(
                PoolPlayer(
                    2544,
                    "LeBron James",
                    LAL,
                    MARKETS,
                    {"prizepicks": MARKETS},
                ),
            ),
            team_counts={LAL: 1},
            freshness={
                "status": "fresh",
                "retrieved_at": RETRIEVED_AT.isoformat(),
                "providers": {},
            },
        )


class RecordedPeers:
    def list_peer_ids(self, player_id):
        return (PEER_ID,)


class RecordingReader:
    """Capture the generation the service asked for, without changing it."""

    def __init__(self, reader):
        self._reader = reader
        self.snapshots = []

    def __getattr__(self, name):
        return getattr(self._reader, name)

    def snapshot(self, *args, **kwargs):
        captured = self._reader.snapshot(*args, **kwargs)
        self.snapshots.append(captured)
        return captured


def _log_row(
    *,
    player_id=2544,
    player_name="LeBron James",
    game_id,
    game_date,
    points,
    minutes,
    opponent_team_id=BOS,
):
    return {
        "season": SEASON,
        "season_type": "Regular Season",
        "player_id": player_id,
        "game_id": game_id,
        "player_name": player_name,
        "game_date": game_date,
        "team_id": LAL,
        "team_tricode": "LAL",
        "opponent_team_id": opponent_team_id,
        "opponent_team_tricode": "BOS",
        "is_home": False,
        "minutes": minutes,
        "points": points,
        "rebounds": 8,
        "assists": 7,
        "field_goals_made": 9,
        "field_goals_attempted": 18,
        "three_pointers_made": 3,
        "three_pointers_attempted": 7,
        "free_throws_made": 4,
        "free_throws_attempted": 5,
        "offensive_rebounds": 2,
        "defensive_rebounds": 6,
        "turnovers": 4,
        "steals": 2,
        "blocks": 1,
        "personal_fouls": 2,
    }


def _default_rows():
    return (
        _log_row(
            game_id="0022500001",
            game_date="2026-01-02",
            points=25,
            minutes=35.0,
        ),
        _log_row(
            game_id="0022500002",
            game_date="2026-01-05",
            points=31,
            minutes=37.0,
            opponent_team_id=1610612744,
        ),
        _log_row(
            player_id=PEER_ID,
            player_name="Paul George",
            game_id="0022500003",
            game_date="2026-01-06",
            points=20,
            minutes=30.0,
        ),
    )


def _published_selection_service(tmp_path, rows):
    """Build a selection service over one real activated player-log publication."""

    engine = create_engine(f"sqlite:///{tmp_path / 'selection-logs.sqlite3'}")
    run_migrations(engine)
    publications = PublicationService(engine, clock=lambda: RETRIEVED_AT)
    publications.register_stream(
        "player_game_logs",
        provider="ledger",
        owner="railway",
        required_observations=(),
        publication_strategy="replace",
        enabled=True,
        freshness_rule="cutoff_current",
    )
    publication = publications.compose(
        "player_game_logs",
        season=SEASON,
        cutoff=RETRIEVED_AT,
        payload={"rows": list(rows)},
    )
    reader = RecordingReader(
        DatabaseFirstPublicationReader(engine, clock=lambda: NOW)
    )
    repository = PlayerGameLogRepository(
        engine,
        statistic_catalog=StatisticCatalog.load_default(),
        stats_surface_season=SEASON,
        clock=lambda: NOW,
        stats_surface_max_age=timedelta(hours=30),
        publication_reader=reader,
    )
    service = MatchupSelectionService(
        event_catalog=RecordedEvents(),
        player_pool=RecordedPool(),
        player_logs=repository,
        archetypes=RecordedPeers(),
        statistic_catalog=StatisticCatalog.load_default(),
        settings=RuntimeSettings(
            environment="testing",
            nba=NBASeasonSettings(current_season=SEASON),
        ),
        publication_reader=reader,
    )
    return engine, service, reader, publication


def _rewrite_payload(engine, publication_id, rows):
    rendered = json.dumps(
        {"rows": list(rows)}, sort_keys=True, separators=(",", ":")
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE publication_versions SET payload = :payload, "
                "checksum = :checksum WHERE publication_id = :publication_id"
            ),
            {
                "payload": rendered,
                "checksum": hashlib.sha256(rendered.encode()).hexdigest(),
                "publication_id": publication_id,
            },
        )


def test_selection_snapshot_defers_the_player_game_log_payload(tmp_path):
    """One card never ships the season-wide rendered game-log payload."""

    _, service, reader, _ = _published_selection_service(
        tmp_path, _default_rows()
    )

    service.get_selection(game_id=GAME_ID, player_id=2544)

    read = reader.snapshots[0].read("player_game_logs")
    assert read.payload is None
    assert read.projection_ready is True


def test_selection_reads_its_tables_through_the_indexed_projection(tmp_path):
    """The opponent-filtered tables are one indexed statement each."""

    engine, service, _, _ = _published_selection_service(
        tmp_path, _default_rows()
    )
    statements = []
    event.listen(
        engine,
        "before_cursor_execute",
        lambda _conn, _cursor, statement, _parameters, _context, _many: (
            statements.append(statement)
        ),
    )

    payload = service.get_selection(game_id=GAME_ID, player_id=2544)

    assert [row["game_date"] for row in payload["h2h"]["rows"]] == [
        "2026-01-02",
        None,
    ]
    assert [row["player_id"] for row in payload["archetype"]["rows"]] == [
        PEER_ID,
        None,
    ]
    # The generation this request captured names only the projected stream, so
    # the large payload value is never fetched at all.
    for statement in statements:
        assert "publication_versions.payload" not in statement
    opponent_reads = [
        statement
        for statement in statements
        if "publication_player_game_logs" in statement
        and "opponent_team_id" in statement
    ]
    assert len(opponent_reads) == 2


def test_selection_serves_the_projection_rather_than_the_payload(tmp_path):
    """Only the indexed rows can explain the card this request returns."""

    engine, service, _, publication = _published_selection_service(
        tmp_path, _default_rows()
    )
    # Re-render the payload with a different scoring fact and keep it
    # self-consistent, so a payload-reading path would return 99 points.
    _rewrite_payload(
        engine,
        publication.publication_id,
        (
            _log_row(
                game_id="0022500001",
                game_date="2026-01-02",
                points=99,
                minutes=35.0,
            ),
            *_default_rows()[1:],
        ),
    )

    payload = service.get_selection(game_id=GAME_ID, player_id=2544)

    assert payload["h2h"]["rows"][0]["stats"]["PTS"] == 25.0


def test_selection_with_a_corrupt_projection_serves_empty_tables(tmp_path):
    engine, service, _, publication = _published_selection_service(
        tmp_path, _default_rows()
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE publication_player_game_logs SET row_payload = 'not-json' "
                "WHERE publication_id = :publication_id"
            ),
            {"publication_id": publication.publication_id},
        )

    payload = service.get_selection(game_id=GAME_ID, player_id=2544)

    assert payload["h2h"] == {"thin": True, "rows": []}
    assert payload["archetype"] == {"thin": True, "rows": []}


def test_selection_without_the_projection_has_no_legacy_fallback(tmp_path):
    engine, service, _, publication = _published_selection_service(
        tmp_path, _default_rows()
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM publication_player_game_logs "
                "WHERE publication_id = :publication_id"
            ),
            {"publication_id": publication.publication_id},
        )

    payload = service.get_selection(game_id=GAME_ID, player_id=2544)

    assert payload["freshness"]["player_game_logs"]["status"] == "unavailable"
    assert payload["h2h"] == {"thin": True, "rows": []}
    assert payload["archetype"] == {"thin": True, "rows": []}


def test_projection_and_payload_reads_render_the_same_card(tmp_path):
    """The projection is the payload's rows indexed, so the card cannot differ.

    The route fixtures under ``tests/routes`` wire no publication reader and
    therefore only prove the legacy path; this pins the projection path to the
    hydrated-payload path over a shape with several opponents and a peer.
    """

    rows = (
        *_default_rows(),
        _log_row(
            game_id="0022500004",
            game_date="2026-01-09",
            points=12,
            minutes=22.5,
        ),
        _log_row(
            player_id=PEER_ID,
            player_name="Paul George",
            game_id="0022500005",
            game_date="2026-01-12",
            points=17,
            minutes=28.0,
            opponent_team_id=1610612744,
        ),
    )
    _, service, reader, _ = _published_selection_service(tmp_path, rows)

    via_projection = service.get_selection(game_id=GAME_ID, player_id=2544)
    assert reader.snapshots[-1].read("player_game_logs").payload is None

    hydrated = reader._reader.snapshot(("player_game_logs",), season=SEASON)
    assert hydrated.read("player_game_logs").payload is not None
    service._publication_snapshot = lambda season, **_scope: hydrated
    via_payload = service.get_selection(game_id=GAME_ID, player_id=2544)

    assert via_projection == via_payload
