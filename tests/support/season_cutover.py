"""A control plane that has published 2025-26 and collected 2026-27.

Each season has its own completed Event Catalog, ``canonical_game_ledger``
manifest, accepted ledger observations, and canonical games: fifteen final
games that give all thirty NBA teams one game each.  Payloads name exactly
those governed games and the real team identities, and ``PublicationService``
resolves governance through the production
``ActiveManifestLedgerGovernanceReader``, so every publication rests on the
same kind of authority production uses.

``player_game_logs`` and both traditional-opponent windows are enabled and
active on 2025-26; ``player_per36`` stays registered but disabled.  A second
2026-27 manifest a day later (one more game) supplies candidates that rest on
other authority.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select

from app.domain.nba_teams import NBA_TEAM_ID_TO_TRICODE
from app.migrations import run_migrations
from app.models.canonical_game_ledger import (
    CanonicalGameLedgerGame,
    LedgerObservationEvidence,
)
from app.models.collection_control import (
    CatalogPublication,
    CollectionManifest,
    CollectionObservation,
    PublicationPointer,
    PublicationVersion,
)
from app.services.collection_control import NBA_TEAM_IDS, PublicationService
from app.services.ledger_runtime import ActiveManifestLedgerGovernanceReader
from tests.services.test_matchup_selection_service import _log_row
from tests.support.traditional_opponent_family import payload as team_window_payload

UTC = timezone.utc
OLD = "2025-26"
NEW = "2026-27"
OLD_CUTOFF = datetime(2026, 4, 13, 4, tzinfo=UTC)
NEW_CUTOFF = datetime(2026, 10, 24, 4, tzinfo=UTC)
#: A second 2026-27 manifest, for candidates that rest on other authority.
OTHER_CUTOFF = datetime(2026, 10, 25, 4, tzinfo=UTC)
#: A later season whose only manifest governs two teams.
SHORT = "2027-28"
SHORT_CUTOFF = datetime(2026, 10, 25, 5, tzinfo=UTC)
NOW = datetime(2026, 10, 25, 12, tzinfo=UTC)
TEAM_IDS = tuple(sorted(int(team_id) for team_id in NBA_TEAM_IDS))
TRICODES = NBA_TEAM_ID_TO_TRICODE

PLAYER_LOGS = "player_game_logs"
SEASON_STREAM = "traditional_opponent_season"
L15_STREAM = "traditional_opponent_l15"
PER36 = "player_per36"
ENABLED_STREAMS = (PLAYER_LOGS, SEASON_STREAM, L15_STREAM)


def _games(prefix: str) -> tuple[tuple[str, int, int], ...]:
    """Fifteen ``(game_id, home_team_id, away_team_id)``: every team once."""

    return tuple(
        (f"{prefix}{index + 1:05d}", TEAM_IDS[index * 2], TEAM_IDS[index * 2 + 1]) for index in range(15)
    )


OLD_GAMES = _games("00225")
NEW_GAMES = _games("00226")
#: The later manifest's extra game: the first two teams meet again.
OTHER_GAME = ("0022600016", TEAM_IDS[0], TEAM_IDS[1])
SHORT_GAME = ("0022700001", TEAM_IDS[0], TEAM_IDS[1])


def _authority(connection, *, season, manifest_id, cutoff, games, catalog_games):
    """Insert one completed Event Catalog, manifest, and its accepted games."""

    events = [
        {
            "nba_game_id": game_id,
            "home_team_id": home,
            "away_team_id": away,
            "phase": "Regular Season",
            "status": "Final",
            "status_code": 3,
            "scheduled_at": (cutoff - timedelta(hours=6)).isoformat(),
        }
        for game_id, home, away in catalog_games
    ]
    catalog_payload = json.dumps({"events": events}, sort_keys=True, separators=(",", ":"))
    catalog_checksum = hashlib.sha256(catalog_payload.encode()).hexdigest()
    catalog_id = f"catalog-{manifest_id}"
    connection.execute(
        CatalogPublication.__table__.insert().values(
            publication_id=catalog_id,
            season=season,
            catalog_type="event",
            cutoff=cutoff,
            version="v1",
            checksum=catalog_checksum,
            payload=catalog_payload,
            complete=True,
            published_at=cutoff,
        )
    )
    connection.execute(
        CollectionManifest.__table__.insert().values(
            manifest_id=manifest_id,
            season=season,
            cutoff=cutoff,
            collect_before=NOW + timedelta(days=1),
            accepted_versions="[1]",
            scopes='["canonical_game_ledger"]',
            checksum=manifest_id,
            event_catalog_publication_id=catalog_id,
            event_catalog_checksum=catalog_checksum,
            status="active",
            created_at=cutoff,
        )
    )
    for game_id, home, away in games:
        observation_id = f"pbp:{game_id}"
        connection.execute(
            CollectionObservation.__table__.insert().values(
                observation_id=observation_id,
                client_observation_id=observation_id,
                collector_id="test",
                manifest_id=manifest_id,
                environment="testing",
                provider="pbp+nba_live_data",
                observation_type="canonical_game_ledger",
                scope=json.dumps(
                    {
                        "game_id": game_id,
                        "surface": "canonical_game_ledger",
                    }
                ),
                season=season,
                cutoff=cutoff,
                schema_version=1,
                checksum=observation_id,
                payload="{}",
                payload_bytes=2,
                retrieved_at=cutoff,
                accepted_at=cutoff,
            )
        )
        connection.execute(
            CanonicalGameLedgerGame.__table__.insert().values(
                game_id=game_id,
                season=season,
                season_type="Regular Season",
                game_date=(cutoff - timedelta(hours=6)).date(),
                home_team_id=int(home),
                home_team_tricode=TRICODES[home],
                away_team_id=int(away),
                away_team_tricode=TRICODES[away],
                status="final",
                source_observation_id=observation_id,
                checksum=hashlib.sha256(game_id.encode()).hexdigest(),
                raw_checksum=hashlib.sha256(game_id.encode()).hexdigest(),
                retrieved_at=cutoff,
                updated_at=cutoff,
            )
        )
        connection.execute(
            LedgerObservationEvidence.__table__.insert().values(
                observation_id=observation_id,
                game_id=game_id,
                created_at=cutoff,
            )
        )


def provenance(games) -> dict[str, str]:
    return {f"pbp:{game_id}": game_id for game_id, _home, _away in games}


OLD_PROVENANCE = provenance(OLD_GAMES)
NEW_PROVENANCE = provenance(NEW_GAMES)
OTHER_PROVENANCE = provenance((OTHER_GAME,))
SHORT_PROVENANCE = provenance((SHORT_GAME,))


def log_rows(season: str, games, *, points: int = 10):
    """One player's line for each team of each game in ``games``."""

    game_date = (OLD_CUTOFF if season == OLD else NEW_CUTOFF).date().isoformat()
    rows = []
    for index, (game_id, home, away) in enumerate(games):
        for side, (team, opponent) in enumerate(((home, away), (away, home))):
            rows.append(
                {
                    **_log_row(
                        player_id=1000 + index * 2 + side,
                        player_name=f"Player {index * 2 + side}",
                        game_id=game_id,
                        game_date=game_date,
                        minutes=30.0,
                        points=points,
                    ),
                    "season": season,
                    "is_home": side == 0,
                    "team_id": int(team),
                    "team_tricode": TRICODES[team],
                    "opponent_team_id": int(opponent),
                    "opponent_team_tricode": TRICODES[opponent],
                }
            )
    return rows


def payload_for(stream_key: str, season: str, *, points: int = 10, games=None):
    """A payload naming exactly ``games`` (the season's governed games)."""

    games = games or (OLD_GAMES if season == OLD else NEW_GAMES)
    if stream_key == PLAYER_LOGS:
        return {"rows": log_rows(season, games, points=points)}
    by_team = {}
    for game_id, home, away in games:
        by_team.setdefault(int(home), []).append(game_id)
        by_team.setdefault(int(away), []).append(game_id)

    def governed(rows):
        for row in rows:
            row["team_tricode"] = TRICODES[row["team_id"]]
            row["game_ids"] = sorted(by_team.get(row["team_id"], ()))
            row["game_count"] = len(row["game_ids"])

    return team_window_payload(mutate=governed)


def seed_world(tmp_path, *, engine=None):
    """Return ``(engine, publications)`` with 2025-26 published."""

    if engine is None:
        engine = create_engine(f"sqlite:///{tmp_path / 'cutover.sqlite3'}")
    run_migrations(engine)
    with engine.begin() as connection:
        _authority(
            connection,
            season=OLD,
            manifest_id="manifest-old",
            cutoff=OLD_CUTOFF,
            games=OLD_GAMES,
            catalog_games=OLD_GAMES,
        )
        _authority(
            connection,
            season=NEW,
            manifest_id="manifest-new",
            cutoff=NEW_CUTOFF,
            games=NEW_GAMES,
            catalog_games=NEW_GAMES,
        )
        _authority(
            connection,
            season=NEW,
            manifest_id="manifest-other",
            cutoff=OTHER_CUTOFF,
            games=(OTHER_GAME,),
            catalog_games=NEW_GAMES + (OTHER_GAME,),
        )
        _authority(
            connection,
            season=SHORT,
            manifest_id="manifest-short",
            cutoff=SHORT_CUTOFF,
            games=(SHORT_GAME,),
            catalog_games=(SHORT_GAME,),
        )
    publications = PublicationService(
        engine,
        clock=lambda: NOW,
        l15_expectation_resolver=ActiveManifestLedgerGovernanceReader(
            engine,
            clock=lambda: NOW,
        ),
    )
    publications.register_default_streams()
    for stream_key in ENABLED_STREAMS:
        publications.register_stream(
            stream_key,
            provider="ledger",
            owner="railway",
            required_observations=["canonical_game_ledger"],
            publication_strategy="ledger_compose",
            enabled=True,
        )
        publications.recompose_ledger(
            stream_key,
            season=OLD,
            cutoff=OLD_CUTOFF,
            payload=payload_for(stream_key, OLD),
            provenance=OLD_PROVENANCE,
            reason="2025-26 ledger composition",
        )
    return engine, publications


def compose_new(publications, stream_key, *, points=11, payload=None, provenance=None):
    """Compose ``stream_key`` from the 2026-27 ledger, as the nightly job does."""

    return publications.recompose_ledger(
        stream_key,
        season=NEW,
        cutoff=NEW_CUTOFF,
        payload=payload or payload_for(stream_key, NEW, points=points),
        provenance=provenance or NEW_PROVENANCE,
        reason="2026-27 ledger composition",
    )


def compose_other(publications, stream_key):
    """Compose ``stream_key`` under the later 2026-27 manifest."""

    return publications.recompose_ledger(
        stream_key,
        season=NEW,
        cutoff=OTHER_CUTOFF,
        payload=payload_for(stream_key, NEW, games=(OTHER_GAME,)),
        provenance=OTHER_PROVENANCE,
        reason="2026-27 ledger composition",
    )


def pointers(engine):
    """``{stream_key: (active publication_id, season, status, fence)}``."""

    with engine.connect() as connection:
        rows = connection.execute(
            select(
                PublicationPointer.stream_key,
                PublicationPointer.active_publication_id,
                PublicationVersion.season,
                PublicationVersion.status,
                PublicationPointer.fence,
            ).join(
                PublicationVersion,
                PublicationVersion.publication_id == PublicationPointer.active_publication_id,
            )
        ).all()
    return {row[0]: tuple(row[1:]) for row in rows}
