"""Manifest-owned ledger runtime governance contracts."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine

from app.migrations import run_migrations
from app.models.collection_control import ActiveSeason, CollectionManifest
from app.models.event_catalog import EventCatalogEntry
from app.services.ledger_runtime import ActiveManifestLedgerGovernanceReader


def test_runtime_governance_fails_closed_without_active_manifest(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'missing.sqlite3'}")
    run_migrations(engine)

    with pytest.raises(ValueError, match="active manifest"):
        ActiveManifestLedgerGovernanceReader(engine).read(
            "2025-26", datetime(2025, 11, 1, tzinfo=timezone.utc)
        )


def test_runtime_governance_owns_exact_games_teams_cutoff_and_l15(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'governed.sqlite3'}")
    run_migrations(engine)
    cutoff = datetime(2025, 11, 1, tzinfo=timezone.utc)
    teams = list(range(1, 31))
    events = []
    for round_index in range(15):
        for pair_index in range(15):
            home = teams[pair_index]
            away = teams[-1 - pair_index]
            game_id = f"game-{round_index:02d}-{pair_index:02d}"
            scheduled = cutoff - timedelta(days=15 - round_index)
            events.append({
                "nba_game_id": game_id,
                "season": "2025-26",
                "home_team_id": home,
                "home_team_name": f"Team {home}",
                "home_team_tricode": f"T{home:02d}",
                "away_team_id": away,
                "away_team_name": f"Team {away}",
                "away_team_tricode": f"T{away:02d}",
                "scheduled_at": scheduled,
                "status_text": "Final",
                "status_code": 3,
                "classification": "Regular Season",
                "first_seen_at": scheduled,
                "last_seen_at": cutoff,
            })
        teams = [teams[0], teams[-1], *teams[1:-1]]
    with engine.begin() as connection:
        connection.execute(ActiveSeason.__table__.insert().values(
            season="2025-26", phase="Regular Season", status="active",
            cutoff=cutoff, activated_at=cutoff, activated_by="test",
        ))
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="manifest", season="2025-26", cutoff=cutoff,
            collect_before=cutoff + timedelta(hours=1), accepted_versions="[1]",
            scopes="[\"canonical_game_ledger\"]", checksum="manifest",
            status="active", created_at=cutoff,
        ))
        connection.execute(EventCatalogEntry.__table__.insert(), events)

    governance = ActiveManifestLedgerGovernanceReader(engine).read("2025-26", cutoff)

    assert governance.cutoff == cutoff
    assert len(governance.expected_game_ids) == 225
    assert len(governance.team_ids) == 30
    assert all(len(game_ids) == 15 for game_ids in governance.expected_l15_game_ids.values())
