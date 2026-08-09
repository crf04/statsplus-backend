"""Offline contract for the operator athlete mapping CLI."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, insert

from app.config.settings import load_settings
from app.migrations import run_migrations
from app.models.athlete_catalog import AthleteCatalog
from app.providers.dfs import AthleteEvidence, TeamEvidence
from app.services.athlete_catalog_service import AthleteCatalogService
from app.services.athlete_mapping_repository import AthleteMappingRepository
from app.services.athlete_resolver import AthleteResolver
from scripts import athlete_mappings


def _catalog_values(player_id: int, display_name: str) -> dict[str, object]:
    return {
        "season": "2024-25",
        "player_id": player_id,
        "display_name": display_name,
        "roster_status": "active",
        "is_active": True,
        "is_active_for_season": True,
        "team_id": 1610612743,
        "team_name": "Denver Nuggets",
        "team_abbreviation": "DEN",
        "published_at": datetime(2026, 8, 9, tzinfo=timezone.utc),
    }


def _seed_database(tmp_path, *, extra_rows: list[dict[str, object]] | None = None):
    path = tmp_path / "cli.sqlite3"
    engine = create_engine(f"sqlite:///{path}")
    run_migrations(engine)
    rows = [_catalog_values(15, "Nikola Jokić")] + list(extra_rows or [])
    with engine.begin() as connection:
        connection.execute(insert(AthleteCatalog.__table__), rows)
    engine.dispose()
    return f"sqlite:///{path}"


def _run(capsys, *argv: str) -> dict | list:
    assert athlete_mappings.main(list(argv)) == 0
    return json.loads(capsys.readouterr().out)


def test_cli_dry_run_and_audited_actions(tmp_path, capsys):
    database_url = _seed_database(tmp_path)

    dry_run = _run(
        capsys,
        "dry-run",
        "--database-url",
        database_url,
        "--provider",
        "prizepicks",
        "--provider-athlete-id",
        "pp-15",
        "--season",
        "2024-25",
        "--name",
        "Nikola Jokic",
    )
    assert dry_run["state"] == "auto"

    approved = _run(
        capsys,
        "approve",
        "--database-url",
        database_url,
        "--provider",
        "prizepicks",
        "--provider-athlete-id",
        "pp-15",
        "--season",
        "2024-25",
        "--canonical-player-id",
        "15",
        "--operator",
        "ops@example.com",
        "--reason",
        "verified source",
    )
    assert approved["state"] == "manual_approved"

    history = _run(
        capsys,
        "history",
        "--database-url",
        database_url,
        "--provider",
        "prizepicks",
        "--provider-athlete-id",
        "pp-15",
    )
    assert history[-1]["operator_id"] == "ops@example.com"


def test_cli_approve_and_override_retain_provider_evidence(tmp_path, capsys):
    database_url = _seed_database(
        tmp_path, extra_rows=[_catalog_values(23, "LeBron James")]
    )

    approved = _run(
        capsys,
        "approve",
        "--database-url",
        database_url,
        "--provider",
        "prizepicks",
        "--provider-athlete-id",
        "pp-15",
        "--season",
        "2024-25",
        "--canonical-player-id",
        "15",
        "--operator",
        "ops@example.com",
        "--reason",
        "verified source",
        "--name",
        "Nikola Jokic",
        "--provider-team-id",
        "pp-den",
        "--team-name",
        "Denver Nuggets",
        "--team-abbreviation",
        "den",
    )
    assert approved["mapping"]["provider_name"] == "Nikola Jokic"
    assert approved["mapping"]["provider_team_id"] == "pp-den"
    assert approved["mapping"]["provider_team_abbreviation"] == "DEN"
    assert approved["decision"]["provider_team_name"] == "Denver Nuggets"

    overridden = _run(
        capsys,
        "override",
        "--database-url",
        database_url,
        "--provider",
        "prizepicks",
        "--provider-athlete-id",
        "pp-15",
        "--season",
        "2024-25",
        "--canonical-player-id",
        "23",
        "--operator",
        "ops@example.com",
        "--reason",
        "corrected identity",
        "--name",
        "LeBron James",
        "--provider-team-id",
        "pp-lal",
        "--team-name",
        "Los Angeles Lakers",
    )
    assert overridden["state"] == "manual_override"
    assert overridden["mapping"]["canonical_player_id"] == 23
    assert overridden["mapping"]["provider_name"] == "LeBron James"
    assert overridden["mapping"]["provider_team_id"] == "pp-lal"


def test_cli_list_reports_unresolved_and_ambiguous_observations(tmp_path, capsys):
    database_url = _seed_database(
        tmp_path, extra_rows=[_catalog_values(23, "Nikola Jokić")]
    )

    # Two active rows share one normalized official name, so a board read
    # records the ambiguity instead of guessing an identity.
    engine = create_engine(database_url)
    repository = AthleteMappingRepository(engine)
    resolver = AthleteResolver(
        AthleteCatalogService(engine, settings=load_settings(
            overrides={"DATABASE_URL": database_url}
        )),
        mapping_repository=repository,
    )
    for provider_athlete_id, name in (
        ("pp-ambiguous", "Nikola Jokic"),
        ("pp-unknown", "Nobody Here"),
    ):
        repository.record_resolution(
            resolver.resolve(
                "prizepicks",
                AthleteEvidence(provider_id=provider_athlete_id, name=name),
                "2024-25",
            )
        )
    engine.dispose()

    listed = _run(capsys, "list", "--database-url", database_url)

    observations = {
        item["provider_athlete_id"]: item for item in listed["unresolved"]
    }
    assert observations["pp-ambiguous"]["decision_state"] == "ambiguous"
    assert observations["pp-ambiguous"]["provider_name"] == "Nikola Jokic"
    # The operator can only choose between candidates that are shown.
    assert [
        candidate["canonical_player_id"]
        for candidate in observations["pp-ambiguous"]["candidates"]
    ] == [15, 23]
    assert observations["pp-unknown"]["decision_state"] == "unmatched"
    assert observations["pp-unknown"]["candidates"] == []
    assert listed["mappings"] == []


@pytest.mark.parametrize("action", ["approve", "override"])
def test_cli_resolves_an_unresolved_observation_and_keeps_its_evidence(
    tmp_path, capsys, action
):
    database_url = _seed_database(
        tmp_path, extra_rows=[_catalog_values(23, "LeBron James")]
    )

    # An unmatched board read leaves only a durable observation to act on.
    engine = create_engine(database_url)
    repository = AthleteMappingRepository(engine)
    resolver = AthleteResolver(
        AthleteCatalogService(engine, settings=load_settings(
            overrides={"DATABASE_URL": database_url}
        )),
        mapping_repository=repository,
    )
    repository.record_resolution(
        resolver.resolve(
            "prizepicks",
            AthleteEvidence(
                provider_id="pp-77",
                name="King James",
                team=TeamEvidence(
                    provider_id="pp-den",
                    canonical_id=1610612743,
                    name="Denver Nuggets",
                    abbreviation="DEN",
                ),
            ),
            "2024-25",
        )
    )
    engine.dispose()

    resolved = _run(
        capsys,
        action,
        "--database-url",
        database_url,
        "--provider",
        "prizepicks",
        "--provider-athlete-id",
        "pp-77",
        "--season",
        "2024-25",
        "--canonical-player-id",
        "23",
        "--operator",
        "ops@example.com",
        "--reason",
        "the provider uses a nickname",
    )

    # The operator supplied no evidence, so the observed evidence is retained
    # on both the new mapping and its decision.
    assert resolved["mapping"]["provider_name"] == "King James"
    assert resolved["mapping"]["provider_team_id"] == "pp-den"
    assert resolved["mapping"]["provider_team_canonical_id"] == 1610612743
    assert resolved["mapping"]["provider_team_abbreviation"] == "DEN"
    assert resolved["decision"]["provider_name"] == "King James"
    assert resolved["decision"]["provider_team_name"] == "Denver Nuggets"
    assert _run(capsys, "list", "--database-url", database_url)["unresolved"] == []


@pytest.mark.parametrize("action", ["approve", "override"])
def test_cli_keeps_observed_evidence_across_a_reject_and_clear(
    tmp_path, capsys, action
):
    database_url = _seed_database(
        tmp_path, extra_rows=[_catalog_values(23, "LeBron James")]
    )
    engine = create_engine(database_url)
    repository = AthleteMappingRepository(engine)
    resolver = AthleteResolver(
        AthleteCatalogService(engine, settings=load_settings(
            overrides={"DATABASE_URL": database_url}
        )),
        mapping_repository=repository,
    )
    repository.record_resolution(
        resolver.resolve(
            "prizepicks",
            AthleteEvidence(
                provider_id="pp-77",
                name="King James",
                team=TeamEvidence(
                    provider_id="pp-den",
                    canonical_id=1610612743,
                    name="Denver Nuggets",
                    abbreviation="DEN",
                ),
            ),
            "2024-25",
        )
    )
    engine.dispose()
    identity = (
        "--provider",
        "prizepicks",
        "--provider-athlete-id",
        "pp-77",
        "--operator",
        "ops@example.com",
    )
    _run(
        capsys,
        "reject",
        "--database-url",
        database_url,
        *identity,
        "--reason",
        "provider identity is not trusted",
    )
    assert _run(
        capsys,
        "clear",
        "--database-url",
        database_url,
        *identity,
        "--reason",
        "the identity was reinstated",
    ) == {"cleared": True}

    resolved = _run(
        capsys,
        action,
        "--database-url",
        database_url,
        *identity,
        "--season",
        "2024-25",
        "--canonical-player-id",
        "23",
        "--reason",
        "the provider uses a nickname",
    )

    # The rejection and its clearing carry no provider evidence, so the last
    # board observation is still what the provider reported for this identity.
    assert resolved["mapping"]["provider_name"] == "King James"
    assert resolved["mapping"]["provider_team_id"] == "pp-den"
    assert resolved["mapping"]["provider_team_canonical_id"] == 1610612743
    assert resolved["mapping"]["provider_team_abbreviation"] == "DEN"
    assert resolved["decision"]["provider_name"] == "King James"
    assert resolved["decision"]["provider_team_name"] == "Denver Nuggets"


def test_cli_reject_blocks_then_clear_restores(tmp_path, capsys):
    database_url = _seed_database(tmp_path)

    rejected = _run(
        capsys,
        "reject",
        "--database-url",
        database_url,
        "--provider",
        "prizepicks",
        "--provider-athlete-id",
        "pp-15",
        "--operator",
        "ops@example.com",
        "--reason",
        "provider identity is not trusted",
    )
    assert rejected["state"] == "rejected"

    listed = _run(capsys, "list", "--database-url", database_url)
    assert listed["rejections"][0]["provider_athlete_id"] == "pp-15"
    assert listed["rejections"][0]["is_active"] is True

    blocked = _run(
        capsys,
        "dry-run",
        "--database-url",
        database_url,
        "--provider",
        "prizepicks",
        "--provider-athlete-id",
        "pp-15",
        "--season",
        "2024-25",
        "--name",
        "Nikola Jokic",
    )
    assert blocked["state"] == "rejected"

    cleared = _run(
        capsys,
        "clear",
        "--database-url",
        database_url,
        "--provider",
        "prizepicks",
        "--provider-athlete-id",
        "pp-15",
        "--operator",
        "ops@example.com",
        "--reason",
        "new evidence reviewed",
    )
    assert cleared == {"cleared": True}

    restored = _run(
        capsys,
        "dry-run",
        "--database-url",
        database_url,
        "--provider",
        "prizepicks",
        "--provider-athlete-id",
        "pp-15",
        "--season",
        "2024-25",
        "--name",
        "Nikola Jokic",
    )
    assert restored["state"] == "auto"

    history = _run(capsys, "history", "--database-url", database_url)
    assert [item["decision_state"] for item in history] == [
        "rejected",
        "rejection_cleared",
    ]


def test_cli_clear_reports_no_active_rejection(tmp_path, capsys):
    database_url = _seed_database(tmp_path)

    cleared = _run(
        capsys,
        "clear",
        "--database-url",
        database_url,
        "--provider",
        "prizepicks",
        "--provider-athlete-id",
        "pp-15",
        "--operator",
        "ops@example.com",
        "--reason",
        "nothing to clear",
    )

    assert cleared == {"cleared": False}
