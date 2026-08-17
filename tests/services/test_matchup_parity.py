"""Offline contract tests for the matchup materializer dual-run parity.

The comparator accepts two independently produced materializations and never
reads a merged stored snapshot, because the legacy writer and the ledger
materializer replace the same ``team_matchup_facts`` surface rows.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from app.migrations import run_migrations
from app.domain.utc import assume_utc
from app.models.collection_control import (
    CatalogPublication,
    CollectionManifest,
    PublicationVersion,
)
from app.services.ledger_parity import LedgerParityArtifactRepository
from app.services.ledger_runtime import ActiveManifestLedgerGovernanceReader
from app.services.matchup_parity import (
    CLASSIFICATION_AVAILABILITY_DIFFERENCE,
    CLASSIFICATION_CUTOFF_MISMATCH,
    CLASSIFICATION_DENOMINATOR_TOLERANCE_EXCEEDED,
    CLASSIFICATION_GAME_SET_MISMATCH,
    CLASSIFICATION_INTEGER_COUNT_DIFFERENCE,
    CLASSIFICATION_LEAGUE_INCOMPLETE,
    CLASSIFICATION_MISSING_METRIC,
    CLASSIFICATION_MISSING_SURFACE,
    CLASSIFICATION_NON_INTEGER_COUNT,
    CLASSIFICATION_RANKING_DIFFERENCE,
    MATCHUP_PARITY_TOLERANCE,
    MatchupMaterialization,
    MatchupParityRunner,
    compare_matchup_materializations,
    matchup_stream_key,
)
from app.services.team_matchup_repository import (
    TeamMatchupFact,
    TeamMatchupObservation,
)
from tests.services.test_ledger_runtime import (
    _immutable_event_catalog,
    _manifest_catalog_binding,
)

TEAM_IDS = tuple(range(1, 31))
TRADITIONAL_STATS = ("OPP_REB", "OPP_TOV", "OPP_STL", "OPP_BLK")
ASSIST_STATS = (
    "Assists",
    "Arc3Assists",
    "Corner3Assists",
    "AtRimAssists",
    "ShortMidRangeAssists",
    "LongMidRangeAssists",
)
CUTOFF = datetime(2024, 11, 15, tzinfo=timezone.utc)


def _surface_facts(team_ids, *, surface, offset=0, minutes=48.0, unit="minutes"):
    stats = TRADITIONAL_STATS if surface == "traditional" else ASSIST_STATS
    return tuple(
        TeamMatchupFact(
            team_id=team_id,
            base=surface,
            slice_key=stat,
            stat_key=stat,
            raw_value=float(team_id + offset),
            denominator_value=minutes,
            denominator_unit=unit,
            provider="recorded",
        )
        for team_id in team_ids
        for stat in stats
    )


def _observations(surfaces=("traditional", "assist_locations"), status="available"):
    return tuple(
        TeamMatchupObservation(surface=surface, status=status)
        for surface in surfaces
    )


def _game_ids_by_team(team_ids, *, prefix="g"):
    return {
        team_id: frozenset((f"{prefix}{team_id}-1", f"{prefix}{team_id}-2"))
        for team_id in team_ids
    }


def _materialization(*, window="season", cutoff=CUTOFF, game_ids=None, offset=0):
    return MatchupMaterialization(
        season="2024-25",
        window=window,
        cutoff=cutoff,
        facts=(
            *_surface_facts(TEAM_IDS, surface="traditional", offset=offset),
            *_surface_facts(TEAM_IDS, surface="assist_locations", offset=offset),
        ),
        observations=_observations(),
        game_ids_by_team=game_ids or _game_ids_by_team(TEAM_IDS),
    )


def _compare(*, surface="traditional", legacy=None, ledger=None,
             expected_game_ids=None, tolerance=MATCHUP_PARITY_TOLERANCE):
    return compare_matchup_materializations(
        legacy or _materialization(),
        ledger or _materialization(),
        surface=surface,
        expected_team_ids=TEAM_IDS,
        expected_game_ids_by_team=expected_game_ids or _game_ids_by_team(TEAM_IDS),
        tolerance=tolerance,
    )


def _replace_fact(materialization, *, surface, team_id, stat, **overrides):
    from dataclasses import replace

    facts = tuple(
        replace(fact, **overrides)
        if fact.base == surface and fact.team_id == team_id and fact.stat_key == stat
        else fact
        for fact in materialization.facts
    )
    return MatchupMaterialization(
        materialization.season, materialization.window, materialization.cutoff,
        facts, materialization.observations, materialization.game_ids_by_team,
    )


def test_exact_parity_when_counts_game_sets_and_denominators_match():
    report = _compare()

    assert report.exact
    assert not report.adjudication_required
    assert report.league_complete
    assert report.team_identities_exact
    assert report.game_sets_exact
    assert report.cutoffs_aligned
    assert report.rankings_deterministic
    assert report.differences == ()
    assert report.compared_count == 30 * len(TRADITIONAL_STATS)


def test_assist_surface_compares_exactly():
    report = _compare(surface="assist_locations")

    assert report.exact
    assert report.compared_count == 30 * len(ASSIST_STATS)


def test_integer_count_difference_requires_adjudication():
    ledger = _replace_fact(_materialization(), surface="traditional", team_id=1, stat="OPP_REB", raw_value=999.0)

    report = _compare(ledger=ledger)

    assert not report.exact
    assert report.adjudication_required
    assert any(
        difference.classification == CLASSIFICATION_INTEGER_COUNT_DIFFERENCE
        and difference.team_id == 1
        and difference.field == "OPP_REB"
        for difference in report.differences
    )


def test_game_set_mismatch_is_classified():
    legacy_ids = _game_ids_by_team(TEAM_IDS)
    legacy_ids[1] = frozenset({"different-1", "different-2"})
    legacy = _materialization(game_ids=legacy_ids)

    report = _compare(legacy=legacy)

    assert not report.game_sets_exact
    assert any(
        difference.classification == CLASSIFICATION_GAME_SET_MISMATCH
        and difference.team_id == 1
        for difference in report.differences
    )


def test_seconds_denominator_is_normalized_to_minutes():
    legacy = _replace_fact(
        _materialization(),
        surface="assist_locations", team_id=1, stat="Assists",
        denominator_value=2880.0, denominator_unit="seconds",
    )

    report = _compare(surface="assist_locations", legacy=legacy)

    assert report.exact
    assert not report.differences


def test_denominator_tolerance_exceeded_is_classified():
    legacy = _replace_fact(
        _materialization(),
        surface="traditional", team_id=1, stat="OPP_REB",
        denominator_value=100.0,
    )

    report = _compare(legacy=legacy)

    assert not report.exact
    assert any(
        difference.classification == CLASSIFICATION_DENOMINATOR_TOLERANCE_EXCEEDED
        for difference in report.differences
    )


def test_league_incomplete_is_classified():
    legacy = MatchupMaterialization(
        "2024-25", "season", CUTOFF,
        (
            *_surface_facts(TEAM_IDS[1:], surface="traditional"),
            *_surface_facts(TEAM_IDS[1:], surface="assist_locations"),
        ),
        _observations(),
        _game_ids_by_team(TEAM_IDS[1:]),
    )

    report = _compare(legacy=legacy, expected_game_ids=_game_ids_by_team(TEAM_IDS))

    assert not report.league_complete
    assert any(
        difference.classification == CLASSIFICATION_LEAGUE_INCOMPLETE
        for difference in report.differences
    )


def test_availability_difference_is_classified():
    legacy = MatchupMaterialization(
        "2024-25", "season", CUTOFF,
        _materialization().facts,
        _observations(status="unavailable"),
        _game_ids_by_team(TEAM_IDS),
    )

    report = _compare(legacy=legacy)

    assert any(
        difference.classification == CLASSIFICATION_AVAILABILITY_DIFFERENCE
        for difference in report.differences
    )


def test_cutoff_mismatch_is_classified():
    legacy = _materialization(cutoff=datetime(2024, 11, 14, tzinfo=timezone.utc))
    ledger = _materialization()

    report = _compare(legacy=legacy, ledger=ledger)

    assert not report.cutoffs_aligned
    assert any(
        difference.classification == CLASSIFICATION_CUTOFF_MISMATCH
        for difference in report.differences
    )


def test_non_integer_count_is_classified():
    legacy = _replace_fact(_materialization(), surface="traditional", team_id=1, stat="OPP_REB", raw_value=1.5)

    report = _compare(legacy=legacy)

    assert any(
        difference.classification == CLASSIFICATION_NON_INTEGER_COUNT
        for difference in report.differences
    )


def test_missing_surface_cannot_pass():
    legacy = MatchupMaterialization(
        "2024-25", "season", CUTOFF,
        _surface_facts(TEAM_IDS, surface="traditional"),
        _observations(surfaces=("traditional",)),
        _game_ids_by_team(TEAM_IDS),
    )

    report = _compare(surface="assist_locations", legacy=legacy)

    assert not report.exact
    assert any(
        difference.classification == CLASSIFICATION_MISSING_SURFACE
        for difference in report.differences
    )


def test_single_missing_metric_cannot_pass():
    facts = tuple(
        fact for fact in _materialization().facts
        if not (fact.base == "traditional" and fact.team_id == 1 and fact.stat_key == "OPP_TOV")
    )
    legacy = MatchupMaterialization(
        "2024-25", "season", CUTOFF, facts, _observations(), _game_ids_by_team(TEAM_IDS),
    )

    report = _compare(legacy=legacy)

    assert not report.exact
    assert any(
        difference.classification == CLASSIFICATION_MISSING_METRIC
        and difference.team_id == 1
        and difference.field == "OPP_TOV"
        for difference in report.differences
    )


def test_near_tie_rank_flip_is_classified_without_a_count_difference():
    # Teams 1 and 2 share a per-48 rate of 48.0 on the ledger side (a tie,
    # ranks 29,29).  A sub-tolerance denominator change on the legacy side
    # flips the ordering (team 2 edges team 1) without tripping the count or
    # tolerance checks.
    base = _materialization()
    ledger = _replace_fact(base, surface="traditional", team_id=1, stat="OPP_REB",
                           raw_value=1.0, denominator_value=1.0)
    ledger = _replace_fact(ledger, surface="traditional", team_id=2, stat="OPP_REB",
                           raw_value=2.0, denominator_value=2.0)
    legacy = _replace_fact(base, surface="traditional", team_id=1, stat="OPP_REB",
                           raw_value=1.0, denominator_value=1.0)
    legacy = _replace_fact(legacy, surface="traditional", team_id=2, stat="OPP_REB",
                           raw_value=2.0, denominator_value=2.000000001)

    report = _compare(legacy=legacy, ledger=ledger)

    assert not report.rankings_deterministic
    assert any(
        difference.classification == CLASSIFICATION_RANKING_DIFFERENCE
        for difference in report.differences
    )
    assert not any(
        difference.classification in {
            CLASSIFICATION_INTEGER_COUNT_DIFFERENCE,
            CLASSIFICATION_DENOMINATOR_TOLERANCE_EXCEEDED,
        }
        for difference in report.differences
    )


def test_tolerance_must_be_finite_and_non_negative():
    with pytest.raises(ValueError):
        _compare(tolerance=-1.0)


def test_report_serialization_is_byte_stable():
    first = json.dumps(_compare().to_dict(), sort_keys=True, separators=(",", ":"))
    second = json.dumps(_compare().to_dict(), sort_keys=True, separators=(",", ":"))

    assert first == second


def test_matchup_stream_key_maps_surfaces_and_windows():
    assert matchup_stream_key("traditional", "season") == "traditional_opponent_season"
    assert matchup_stream_key("traditional", "l15") == "traditional_opponent_l15"
    assert matchup_stream_key("assist_locations", "season") == "assist_locations_season"
    assert matchup_stream_key("assist_locations", "l15") == "assist_locations_l15"
    with pytest.raises(ValueError):
        matchup_stream_key("shot_zones", "season")


class _FakeGovernance:
    def __init__(self, team_ids, season_ids, l15_ids):
        self.team_ids = frozenset(team_ids)
        self.expected_season_game_ids = season_ids
        self.expected_l15_game_ids = l15_ids

    def read_for_composition(self, season, cutoff):
        return self


def _runner(tmp_path, *, window="season"):
    engine = create_engine(f"sqlite:///{tmp_path / 'runner.sqlite3'}")
    run_migrations(engine)
    season_ids = _game_ids_by_team(TEAM_IDS, prefix="season")
    l15_ids = _game_ids_by_team(TEAM_IDS, prefix="l15")
    governance = _FakeGovernance(TEAM_IDS, season_ids, l15_ids)
    runner = MatchupParityRunner(engine, governance=governance)
    game_ids = season_ids if window == "season" else l15_ids
    legacy = _materialization(window=window, game_ids=game_ids)
    ledger = _materialization(window=window, game_ids=game_ids)
    return runner, engine, legacy, ledger


def test_runner_compares_both_surfaces_without_advancing_pointers(tmp_path):
    from sqlalchemy import text

    runner, engine, legacy, ledger = _runner(tmp_path)

    reports = runner.run(legacy, ledger)

    assert {report.surface for report in reports} == {"traditional", "assist_locations"}
    assert all(report.exact for report in reports)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM publication_pointers")).scalar_one() == 0


def test_runner_records_per_stream_artifacts_bound_to_exact_cutoff(tmp_path):
    runner, engine, legacy, ledger = _runner(tmp_path)
    with engine.begin() as connection:
        connection.execute(PublicationVersion.__table__.insert(), [
            {"publication_id": f"pub-{stream}", "stream_key": stream,
             "season": "2024-25", "cutoff": CUTOFF, "version": 1,
             "status": "candidate", "checksum": "a" * 64, "payload": "{}",
             "created_at": CUTOFF, "fence": 0}
            for stream in ("traditional_opponent_season", "assist_locations_season")
        ])

    runner.run(legacy, ledger, publications={
        "traditional_opponent_season": ("pub-traditional_opponent_season", "a" * 64),
        "assist_locations_season": ("pub-assist_locations_season", "a" * 64),
    })

    repository = LedgerParityArtifactRepository(engine)
    for stream in ("traditional_opponent_season", "assist_locations_season"):
        artifact = repository.latest(stream, "2024-25")
        assert artifact is not None
        assert assume_utc(artifact.cutoff) == CUTOFF
        assert artifact.status == "exact"
        report = json.loads(artifact.report)
        assert report["window"] == "season"
        assert report["cutoff"] == CUTOFF.isoformat()


def test_runner_rejects_mismatched_cutoffs(tmp_path):
    runner, engine, legacy, ledger = _runner(tmp_path)
    legacy = _materialization(cutoff=datetime(2024, 11, 14, tzinfo=timezone.utc))

    with pytest.raises(ValueError, match="different cutoffs"):
        runner.run(legacy, ledger)


def test_record_matchup_parity_rejects_wrong_stream_window(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'bind.sqlite3'}")
    run_migrations(engine)
    repository = LedgerParityArtifactRepository(engine)
    report = _compare()

    with pytest.raises(ValueError, match="does not match report surface/window"):
        repository.record_matchup_parity(
            "traditional_opponent_l15",
            cutoff=CUTOFF,
            report=report,
            publication_id="pub",
            payload_checksum="a" * 64,
        )


def test_record_matchup_parity_requires_aware_cutoff(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'aware.sqlite3'}")
    run_migrations(engine)
    repository = LedgerParityArtifactRepository(engine)
    report = _compare()

    with pytest.raises(ValueError, match="aware immutable cutoff"):
        repository.record_matchup_parity(
            "traditional_opponent_season",
            cutoff=datetime(2024, 11, 15),
            report=report,
            publication_id="pub",
            payload_checksum="a" * 64,
        )


def test_script_uses_immutable_authority_not_mutable_catalog():
    import scripts.matchup_parity as matchup_parity_script

    assert not hasattr(matchup_parity_script, "EventCatalogRepository")
    assert hasattr(matchup_parity_script, "ActiveManifestLedgerGovernanceReader")


def _manifest_world(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'world.sqlite3'}")
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
        connection.execute(CatalogPublication.__table__.insert().values(
            **_immutable_event_catalog(events, cutoff)
        ))
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="manifest", season="2025-26", cutoff=cutoff,
            collect_before=cutoff + timedelta(hours=1), accepted_versions="[1]",
            scopes='["canonical_game_ledger"]', checksum="manifest",
            **_manifest_catalog_binding(events, cutoff),
            status="active", created_at=cutoff,
        ))
    governance = ActiveManifestLedgerGovernanceReader(engine).read("2025-26", cutoff)
    return engine, cutoff, governance


def _write_materialization_json(path, *, window, cutoff, game_ids_by_team):
    facts = []
    for team_id in TEAM_IDS:
        for stat in TRADITIONAL_STATS:
            facts.append({"team_id": team_id, "base": "traditional", "stat_key": stat,
                          "raw_value": team_id, "denominator_value": 48.0,
                          "denominator_unit": "minutes"})
        for stat in ASSIST_STATS:
            facts.append({"team_id": team_id, "base": "assist_locations", "stat_key": stat,
                          "raw_value": team_id, "denominator_value": 48.0,
                          "denominator_unit": "minutes"})
    document = {
        "season": "2025-26",
        "window": window,
        "cutoff": cutoff.isoformat(),
        "facts": facts,
        "observations": [
            {"surface": "traditional", "status": "available"},
            {"surface": "assist_locations", "status": "available"},
        ],
        "game_ids_by_team": {
            str(team_id): sorted(game_ids)
            for team_id, game_ids in game_ids_by_team.items()
        },
    }
    path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")


def test_script_compare_records_bound_artifacts_from_immutable_authority(tmp_path):
    import scripts.matchup_parity as matchup_parity_script

    engine, cutoff, governance = _manifest_world(tmp_path)
    season_ids = governance.expected_season_game_ids
    legacy_path = tmp_path / "legacy.json"
    ledger_path = tmp_path / "ledger.json"
    publications_path = tmp_path / "publications.json"
    _write_materialization_json(legacy_path, window="season", cutoff=cutoff, game_ids_by_team=season_ids)
    _write_materialization_json(ledger_path, window="season", cutoff=cutoff, game_ids_by_team=season_ids)
    with engine.begin() as connection:
        connection.execute(PublicationVersion.__table__.insert(), [
            {"publication_id": f"pub-{stream}", "stream_key": stream,
             "season": "2025-26", "cutoff": cutoff, "version": 1,
             "status": "candidate", "checksum": "a" * 64, "payload": "{}",
             "created_at": cutoff, "fence": 0}
            for stream in ("traditional_opponent_season", "assist_locations_season")
        ])
    publications_path.write_text(json.dumps({
        "traditional_opponent_season": {"publication_id": "pub-traditional_opponent_season", "payload_checksum": "a" * 64},
        "assist_locations_season": {"publication_id": "pub-assist_locations_season", "payload_checksum": "a" * 64},
    }), encoding="utf-8")

    args = SimpleNamespace(
        season="2025-26", window="season",
        cutoff=cutoff.isoformat(),
        legacy_json=str(legacy_path), ledger_json=str(ledger_path),
        publications_json=str(publications_path),
    )
    exit_code = matchup_parity_script._compare(args, engine)

    assert exit_code == 0
    repository = LedgerParityArtifactRepository(engine)
    for stream in ("traditional_opponent_season", "assist_locations_season"):
        artifact = repository.latest(stream, "2025-26")
        assert artifact is not None
        assert artifact.status == "exact"
        assert assume_utc(artifact.cutoff) == cutoff


def test_assist_stream_activation_requires_parity(tmp_path):
    from app.services.collection_control import ControlPlaneError, PublicationService

    engine = create_engine(f"sqlite:///{tmp_path / 'activation.sqlite3'}")
    run_migrations(engine)
    publications = PublicationService(engine)
    publications.register_default_streams()

    with pytest.raises(ControlPlaneError, match="ledger_parity_evidence_required"):
        publications.activate_stream("assist_locations_season", reason="unproven ledger candidate")

    with pytest.raises(ControlPlaneError, match="ledger_parity_evidence_required"):
        publications.activate_stream("assist_locations_l15", reason="unproven ledger candidate")
