"""Offline contract tests for the matchup materializer dual-run parity.

The comparator accepts two independently produced, provenance-bound
materializations; the ledger side is always derived from a verified candidate
publication, never from author-written JSON.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from app.domain.nba_teams import NBA_TEAM_ID_TO_TRICODE
from app.domain.publication_integrity import (
    canonical_publication_json,
    publication_payload_checksum,
)
from app.domain.utc import assume_utc
from app.migrations import run_migrations
from app.models.collection_control import (
    CatalogPublication,
    CollectionManifest,
    PublicationVersion,
)
from app.services.collection_control import ControlPlaneError, PublicationService
from app.services.ledger_parity import (
    LedgerParityArtifactRepository,
    matchup_parity_artifact_is_activatable,
)
from app.services.ledger_runtime import ActiveManifestLedgerGovernanceReader
from app.services.ledger_derivations import ASSIST_DERIVED_METRICS, TEAM_METRICS
from app.services.matchup_parity import (
    CLASSIFICATION_AVAILABILITY_DIFFERENCE,
    CLASSIFICATION_AUTHORITY_MISMATCH,
    CLASSIFICATION_CUTOFF_MISMATCH,
    CLASSIFICATION_DERIVED_RATE_DIFFERENCE,
    CLASSIFICATION_DUPLICATE_METRIC,
    CLASSIFICATION_GAME_SET_MISMATCH,
    CLASSIFICATION_INTEGER_COUNT_DIFFERENCE,
    CLASSIFICATION_INVALID_DENOMINATOR,
    CLASSIFICATION_LEAGUE_INCOMPLETE,
    CLASSIFICATION_MISSING_METRIC,
    CLASSIFICATION_MISSING_SURFACE,
    CLASSIFICATION_RANKING_DIFFERENCE,
    HARD_CLASSIFICATIONS,
    MATCHUP_PARITY_TOLERANCE,
    MatchupMaterialization,
    MatchupParityError,
    MatchupParityRunner,
    PRODUCER_LEDGER,
    PRODUCER_LEGACY,
    SOFT_CLASSIFICATIONS,
    StoredLegacyMatchupSource,
    compare_matchup_materializations,
    materialization_from_publication,
    matchup_stream_key,
    resolve_matchup_publication,
)
from app.services.team_matchup_publications import PublicationGovernanceUnavailable
from app.services.team_matchup_query import TeamMatchupQueryService
from app.services.team_matchup_repository import (
    TeamMatchupFact,
    TeamMatchupObservation,
    TeamMatchupRepository,
    TeamMatchupSnapshotScope,
)
from tests.services.test_ledger_runtime import (
    _immutable_event_catalog,
    _manifest_catalog_binding,
)

TEAM_IDS = tuple(NBA_TEAM_ID_TO_TRICODE)
TEAM_A, TEAM_B = TEAM_IDS[:2]
TRADITIONAL_STATS = ("OPP_REB", "OPP_TOV", "OPP_STL", "OPP_BLK")
ASSIST_STATS = (
    "Assists",
    "Arc3Assists",
    "Corner3Assists",
    "AtRimAssists",
    "ShortMidRangeAssists",
    "LongMidRangeAssists",
)
TRADITIONAL_METRICS = {"OPP_REB": "rebounds", "OPP_TOV": "turnovers", "OPP_STL": "steals", "OPP_BLK": "blocks"}
ASSIST_METRICS = {
    "Assists": "assists", "Arc3Assists": "arc3_assists", "Corner3Assists": "corner3_assists",
    "AtRimAssists": "at_rim_assists", "ShortMidRangeAssists": "short_mid_range_assists",
    "LongMidRangeAssists": "long_mid_range_assists",
}
CUTOFF = datetime(2024, 11, 15, 18, 0, tzinfo=timezone.utc)
MANIFEST = "manifest"
CATALOG_ID = "event-catalog"
CATALOG_CHECKSUM = "c" * 64


def _surface_facts(team_ids, *, surface, offset=0, minutes=48.0, unit="minutes", provider="recorded", game_ids_by_team=None):
    stats = TRADITIONAL_STATS if surface == "traditional" else ASSIST_STATS
    return tuple(
        TeamMatchupFact(
            team_id=team_id, base=surface, slice_key=stat, stat_key=stat,
            raw_value=float(team_id + offset), denominator_value=minutes,
            denominator_unit=unit, provider=provider,
            game_ids=tuple(sorted((game_ids_by_team or {}).get(team_id, ()))),
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


def _materialization(*, window="season", cutoff=CUTOFF, offset=0, game_ids=None,
                     facts=None, observations=None, producer=PRODUCER_LEGACY,
                     manifest_id=MANIFEST, catalog_id=CATALOG_ID, catalog_checksum=CATALOG_CHECKSUM):
    return MatchupMaterialization(
        season="2025-26",
        window=window,
        cutoff=cutoff,
        facts=facts if facts is not None else (
            *_surface_facts(TEAM_IDS, surface="traditional", offset=offset),
            *_surface_facts(TEAM_IDS, surface="assist_locations", offset=offset),
        ),
        observations=observations if observations is not None else _observations(),
        game_ids_by_team=game_ids or _game_ids_by_team(TEAM_IDS),
        producer=producer,
        manifest_id=manifest_id,
        event_catalog_publication_id=catalog_id,
        event_catalog_checksum=catalog_checksum,
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
        producer=materialization.producer, manifest_id=materialization.manifest_id,
        event_catalog_publication_id=materialization.event_catalog_publication_id,
        event_catalog_checksum=materialization.event_catalog_checksum,
    )


# --- Pure comparator tests -------------------------------------------------

def test_exact_parity_when_counts_game_sets_and_denominators_match():
    report = _compare()

    assert report.exact
    assert not report.hard_failure
    assert not report.adjudication_required
    assert report.status == "exact"
    assert report.compared_count == 30 * len(TRADITIONAL_STATS)


def test_integer_count_difference_is_a_hard_failure():
    ledger = _replace_fact(_materialization(), surface="traditional", team_id=TEAM_A, stat="OPP_REB", raw_value=999.0)

    report = _compare(ledger=ledger)

    assert report.hard_failure
    assert not report.adjudication_required
    assert not report.exact
    assert any(
        d.classification == CLASSIFICATION_INTEGER_COUNT_DIFFERENCE
        and d.classification in HARD_CLASSIFICATIONS
        for d in report.differences
    )


def test_floating_denominator_difference_is_adjudicable_not_hard():
    legacy = _replace_fact(_materialization(), surface="traditional", team_id=TEAM_A, stat="OPP_REB", denominator_value=100.0)

    report = _compare(legacy=legacy)

    assert not report.hard_failure
    assert report.adjudication_required
    assert not report.exact
    assert {d.classification for d in report.differences} <= SOFT_CLASSIFICATIONS | {
        CLASSIFICATION_DERIVED_RATE_DIFFERENCE,
    }


def test_game_set_mismatch_is_a_hard_failure():
    legacy_ids = _game_ids_by_team(TEAM_IDS)
    legacy_ids[TEAM_A] = frozenset({"different-1", "different-2"})
    legacy = _materialization(game_ids=legacy_ids)

    report = _compare(legacy=legacy)

    assert report.hard_failure
    assert any(
        d.classification == CLASSIFICATION_GAME_SET_MISMATCH and d.team_id == TEAM_A
        for d in report.differences
    )


def test_league_incomplete_is_a_hard_failure():
    legacy = _materialization(
        facts=(
            *_surface_facts(TEAM_IDS[1:], surface="traditional"),
            *_surface_facts(TEAM_IDS[1:], surface="assist_locations"),
        ),
        game_ids=_game_ids_by_team(TEAM_IDS[1:]),
    )

    report = _compare(legacy=legacy, expected_game_ids=_game_ids_by_team(TEAM_IDS))

    assert report.hard_failure
    assert any(
        d.classification == CLASSIFICATION_LEAGUE_INCOMPLETE for d in report.differences
    )


def test_both_sides_unavailable_cannot_be_exact():
    legacy = _materialization(observations=_observations(status="unavailable"))
    ledger = _materialization(observations=_observations(status="unavailable"))

    report = _compare(legacy=legacy, ledger=ledger)

    assert report.hard_failure
    assert any(
        d.classification == CLASSIFICATION_AVAILABILITY_DIFFERENCE for d in report.differences
    )


def test_missing_surface_is_a_hard_failure():
    legacy = _materialization(
        facts=_surface_facts(TEAM_IDS, surface="traditional"),
        observations=_observations(surfaces=("traditional",)),
    )

    report = _compare(surface="assist_locations", legacy=legacy)

    assert report.hard_failure
    assert any(
        d.classification == CLASSIFICATION_MISSING_SURFACE for d in report.differences
    )


def test_single_missing_metric_is_a_hard_failure():
    facts = tuple(
        fact for fact in _materialization().facts
        if not (fact.base == "traditional" and fact.team_id == TEAM_A and fact.stat_key == "OPP_TOV")
    )
    legacy = _materialization(facts=facts)

    report = _compare(legacy=legacy)

    assert report.hard_failure
    assert any(
        d.classification == CLASSIFICATION_MISSING_METRIC
        and d.team_id == TEAM_A and d.field == "OPP_TOV"
        for d in report.differences
    )


def test_cutoff_mismatch_is_a_hard_failure():
    legacy = _materialization(cutoff=datetime(2024, 11, 14, tzinfo=timezone.utc))

    report = _compare(legacy=legacy)

    assert report.hard_failure
    assert any(
        d.classification == CLASSIFICATION_CUTOFF_MISMATCH for d in report.differences
    )


def test_authority_mismatch_is_a_hard_failure():
    ledger = _materialization(manifest_id="other-manifest")

    report = _compare(ledger=ledger)

    assert report.hard_failure
    assert any(
        d.classification == CLASSIFICATION_AUTHORITY_MISMATCH
        for d in report.differences
    )


def test_near_tie_rank_flip_is_soft_not_hard():
    base = _materialization()
    ledger = _replace_fact(base, surface="traditional", team_id=TEAM_A, stat="OPP_REB",
                           raw_value=1.0, denominator_value=1.0)
    ledger = _replace_fact(ledger, surface="traditional", team_id=TEAM_B, stat="OPP_REB",
                           raw_value=2.0, denominator_value=2.0)
    legacy = _replace_fact(base, surface="traditional", team_id=TEAM_A, stat="OPP_REB",
                           raw_value=1.0, denominator_value=1.0)
    legacy = _replace_fact(legacy, surface="traditional", team_id=TEAM_B, stat="OPP_REB",
                           raw_value=2.0, denominator_value=2.000000001)

    report = _compare(legacy=legacy, ledger=ledger)

    assert not report.hard_failure
    assert report.adjudication_required
    assert any(
        d.classification == CLASSIFICATION_RANKING_DIFFERENCE for d in report.differences
    )


def test_soft_and_hard_classifications_are_disjoint():
    assert not (HARD_CLASSIFICATIONS & SOFT_CLASSIFICATIONS)


def test_duplicate_metric_is_a_hard_failure():
    base = _materialization()
    legacy = MatchupMaterialization(
        base.season,
        base.window,
        base.cutoff,
        (*base.facts, base.facts[0]),
        base.observations,
        base.game_ids_by_team,
        producer=base.producer,
        manifest_id=base.manifest_id,
        event_catalog_publication_id=base.event_catalog_publication_id,
        event_catalog_checksum=base.event_catalog_checksum,
    )

    report = _compare(legacy=legacy)

    assert report.hard_failure
    assert any(d.classification == CLASSIFICATION_DUPLICATE_METRIC for d in report.differences)


def test_invalid_denominator_unit_is_a_hard_failure():
    legacy = _replace_fact(
        _materialization(),
        surface="traditional",
        team_id=TEAM_A,
        stat="OPP_REB",
        denominator_unit="hours",
    )

    report = _compare(legacy=legacy)

    assert report.hard_failure
    assert any(d.classification == CLASSIFICATION_INVALID_DENOMINATOR for d in report.differences)


def test_hard_matchup_artifact_cannot_be_activated_or_manually_approved():
    artifact = SimpleNamespace(
        stream_key="traditional_opponent_season",
        report=json.dumps({
            "status": "failed",
            "differences": [{"classification": CLASSIFICATION_GAME_SET_MISMATCH}],
        }),
    )

    assert not matchup_parity_artifact_is_activatable(artifact)


def test_l15_requires_exactly_15_games_per_team():
    short_ids = {
        team_id: frozenset((f"g{team_id}-1",)) for team_id in TEAM_IDS
    }
    legacy = _materialization(window="l15", game_ids=short_ids)
    ledger = _materialization(window="l15", game_ids=short_ids)

    report = compare_matchup_materializations(
        legacy, ledger, surface="traditional",
        expected_team_ids=TEAM_IDS, expected_game_ids_by_team=short_ids,
    )

    assert report.hard_failure
    assert any(
        d.classification == "l15_game_count_mismatch" for d in report.differences
    )


# --- Publication resolution tests ------------------------------------------

def _publication_payload(game_ids_by_team, *, surface, offset=0, minutes=48.0):
    payload_metrics = TEAM_METRICS if surface == "traditional" else ASSIST_DERIVED_METRICS
    rows = []
    for team_id in TEAM_IDS:
        counts = {metric: float(team_id + offset) for metric in payload_metrics}
        per48 = {metric: value * 48.0 / minutes for metric, value in counts.items()}
        rows.append({
            "team_id": team_id,
            "team_tricode": NBA_TEAM_ID_TO_TRICODE[team_id],
            "game_ids": sorted(game_ids_by_team[team_id]),
            "game_count": len(game_ids_by_team[team_id]),
            "per48": per48,
            "league_average": {metric: 1.0 for metric in payload_metrics},
            "population_sigma": {metric: 1.0 for metric in payload_metrics},
            "competition_rank": {metric: 1 for metric in payload_metrics},
            "counts": counts,
            "team_minutes": minutes,
        })
    return canonical_publication_json(rows)


def _publication_world(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'pub.sqlite3'}")
    run_migrations(engine)
    events = [
        {
            "nba_game_id": f"g{team_id}-{game_index}",
            "season": "2025-26",
            "home_team_id": team_id,
            "home_team_name": f"Team {team_id}",
            "home_team_tricode": NBA_TEAM_ID_TO_TRICODE[team_id],
            "away_team_id": TEAM_IDS[(TEAM_IDS.index(team_id) + 1) % len(TEAM_IDS)],
            "away_team_name": "Away",
            "away_team_tricode": NBA_TEAM_ID_TO_TRICODE[
                TEAM_IDS[(TEAM_IDS.index(team_id) + 1) % len(TEAM_IDS)]
            ],
            "scheduled_at": CUTOFF - timedelta(days=game_index),
            "status_text": "Final",
            "status_code": 3,
            "classification": "Regular Season",
            "first_seen_at": CUTOFF - timedelta(days=game_index),
            "last_seen_at": CUTOFF,
        }
        for team_id in TEAM_IDS
        for game_index in (1, 2)
    ]
    catalog = _immutable_event_catalog(events, CUTOFF)
    binding = _manifest_catalog_binding(events, CUTOFF)
    with engine.begin() as connection:
        connection.execute(CatalogPublication.__table__.insert().values(**catalog))
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id=MANIFEST, season="2025-26", cutoff=CUTOFF,
            collect_before=CUTOFF + timedelta(hours=1), accepted_versions="[1]",
            scopes='["canonical_game_ledger"]', checksum="manifest",
            event_catalog_publication_id=binding["event_catalog_publication_id"],
            event_catalog_checksum=binding["event_catalog_checksum"],
            status="active", created_at=CUTOFF,
        ))
    return engine, catalog, binding


def _insert_publication(engine, *, stream_key, surface, window, game_ids_by_team, binding, offset=0):
    payload = _publication_payload(game_ids_by_team, surface=surface, offset=offset)
    checksum = publication_payload_checksum(payload)
    with engine.begin() as connection:
        connection.execute(PublicationVersion.__table__.insert().values(
            publication_id=f"pub-{stream_key}", stream_key=stream_key,
            season="2025-26", cutoff=CUTOFF, version=1, status="candidate",
            checksum=checksum, payload=payload,
            manifest_id=MANIFEST,
            event_catalog_publication_id=binding["event_catalog_publication_id"],
            event_catalog_checksum=binding["event_catalog_checksum"],
            created_at=CUTOFF, fence=0,
        ))

    return f"pub-{stream_key}", checksum


def test_materialization_from_publication_derives_provenance_bound_facts(tmp_path):
    engine, catalog, binding = _publication_world(tmp_path)
    game_ids = _game_ids_by_team(TEAM_IDS)
    publication_id, checksum = _insert_publication(
        engine, stream_key="traditional_opponent_season", surface="traditional",
        window="season", game_ids_by_team=game_ids, binding=binding,
    )

    with create_session(engine) as session:
        publication = resolve_matchup_publication(
            session,
            publication_id=publication_id,
            stream_key="traditional_opponent_season",
            season="2025-26",
            cutoff=CUTOFF,
            manifest_id=MANIFEST,
            event_catalog_publication_id=binding["event_catalog_publication_id"],
            event_catalog_checksum=binding["event_catalog_checksum"],
        )
    assert publication.payload_checksum == checksum
    materialization = materialization_from_publication(publication, surface="traditional")
    assert materialization.producer == PRODUCER_LEDGER
    assert materialization.publication_id == publication_id
    assert materialization.game_ids_by_team[TEAM_A] == frozenset(game_ids[TEAM_A])
    fact = next(f for f in materialization.facts if f.team_id == TEAM_A and f.stat_key == "OPP_REB")
    assert fact.raw_value == float(TEAM_A)
    assert fact.denominator_value == 48.0


def test_resolve_publication_rejects_authority_mismatch(tmp_path):
    engine, catalog, binding = _publication_world(tmp_path)
    game_ids = _game_ids_by_team(TEAM_IDS)
    publication_id, _ = _insert_publication(
        engine, stream_key="traditional_opponent_season", surface="traditional",
        window="season", game_ids_by_team=game_ids, binding=binding,
    )

    with create_session(engine) as session:
        with pytest.raises(MatchupParityError, match="authority"):
            resolve_matchup_publication(
                session,
                publication_id=publication_id,
                stream_key="traditional_opponent_season",
                season="2025-26",
                cutoff=CUTOFF,
                manifest_id="wrong-manifest",
                event_catalog_publication_id=binding["event_catalog_publication_id"],
                event_catalog_checksum=binding["event_catalog_checksum"],
            )


def test_resolve_publication_rejects_checksum_or_scope_mismatch(tmp_path):
    engine, catalog, binding = _publication_world(tmp_path)
    game_ids = _game_ids_by_team(TEAM_IDS)
    publication_id, _ = _insert_publication(
        engine, stream_key="traditional_opponent_season", surface="traditional",
        window="season", game_ids_by_team=game_ids, binding=binding,
    )
    with engine.begin() as connection:
        connection.execute(
            PublicationVersion.__table__.update().where(
                PublicationVersion.publication_id == publication_id,
            ).values(checksum="a" * 64)
        )

    with create_session(engine) as session:
        with pytest.raises(MatchupParityError, match="candidate"):
            resolve_matchup_publication(
                session,
                publication_id=publication_id,
                stream_key="traditional_opponent_season",
                season="2025-26",
                cutoff=CUTOFF,
                manifest_id=MANIFEST,
                event_catalog_publication_id=binding["event_catalog_publication_id"],
                event_catalog_checksum=binding["event_catalog_checksum"],
            )


def test_resolve_publication_rejects_noncanonical_team_identity(tmp_path):
    engine, catalog, binding = _publication_world(tmp_path)
    game_ids = _game_ids_by_team(TEAM_IDS)
    publication_id, _ = _insert_publication(
        engine,
        stream_key="traditional_opponent_season",
        surface="traditional",
        window="season",
        game_ids_by_team=game_ids,
        binding=binding,
    )
    rows = json.loads(
        _publication_payload(game_ids, surface="traditional")
    )
    rows[0]["team_id"] = 999
    rows[0]["team_tricode"] = "XXX"
    payload = canonical_publication_json(rows)
    with engine.begin() as connection:
        connection.execute(
            PublicationVersion.__table__.update().where(
                PublicationVersion.publication_id == publication_id,
            ).values(
                payload=payload,
                checksum=publication_payload_checksum(payload),
            )
        )

    with create_session(engine) as session:
        with pytest.raises(MatchupParityError, match="payload"):
            resolve_matchup_publication(
                session,
                publication_id=publication_id,
                stream_key="traditional_opponent_season",
                season="2025-26",
                cutoff=CUTOFF,
                manifest_id=MANIFEST,
                event_catalog_publication_id=binding["event_catalog_publication_id"],
                event_catalog_checksum=binding["event_catalog_checksum"],
            )


# --- Runner + stored legacy source integration -----------------------------

def create_session(engine):
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=engine, expire_on_commit=False)()


def _runner_world(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'runner.sqlite3'}")
    run_migrations(engine)
    events = []
    teams = list(TEAM_IDS)
    for round_index in range(15):
        for pair_index in range(15):
            home = teams[pair_index]
            away = teams[-1 - pair_index]
            game_id = f"game-{round_index:02d}-{pair_index:02d}"
            scheduled = CUTOFF - timedelta(days=15 - round_index)
            events.append({
                "nba_game_id": game_id,
                "season": "2025-26",
                "home_team_id": home,
                "home_team_name": f"Team {home}",
                "home_team_tricode": NBA_TEAM_ID_TO_TRICODE[home],
                "away_team_id": away,
                "away_team_name": f"Team {away}",
                "away_team_tricode": NBA_TEAM_ID_TO_TRICODE[away],
                "scheduled_at": scheduled,
                "status_text": "Final",
                "status_code": 3,
                "classification": "Regular Season",
                "first_seen_at": scheduled,
                "last_seen_at": CUTOFF,
            })
        teams = [teams[0], teams[-1], *teams[1:-1]]
    catalog = _immutable_event_catalog(events, CUTOFF)
    binding = _manifest_catalog_binding(events, CUTOFF)
    with engine.begin() as connection:
        connection.execute(CatalogPublication.__table__.insert().values(**catalog))
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id=MANIFEST, season="2025-26", cutoff=CUTOFF,
            collect_before=CUTOFF + timedelta(hours=1), accepted_versions="[1]",
            scopes='["canonical_game_ledger"]', checksum="manifest",
            event_catalog_publication_id=binding["event_catalog_publication_id"],
            event_catalog_checksum=binding["event_catalog_checksum"],
            status="active", created_at=CUTOFF,
        ))
    governance = ActiveManifestLedgerGovernanceReader(engine).read("2025-26", CUTOFF)
    return engine, governance, binding


def _write_legacy_facts(engine, *, game_ids_by_team):
    repository = TeamMatchupRepository(engine)
    scope = TeamMatchupSnapshotScope("2025-26", CUTOFF.date())
    repository.replace_snapshots(
        (
            (
                scope,
                (
                    *_surface_facts(TEAM_IDS, surface="traditional", provider="nba_stats", game_ids_by_team=game_ids_by_team),
                    *_surface_facts(TEAM_IDS, surface="assist_locations", provider="pbp_stats", game_ids_by_team=game_ids_by_team),
                ),
                _observations(),
            ),
        ),
        retrieved_at=CUTOFF,
    )


def _insert_runner_publication(engine, *, stream_key, surface, window, game_ids_by_team, binding):
    payload = _publication_payload(game_ids_by_team, surface=surface)
    checksum = publication_payload_checksum(payload)
    with engine.begin() as connection:
        connection.execute(PublicationVersion.__table__.insert().values(
            publication_id=f"pub-{stream_key}", stream_key=stream_key,
            season="2025-26", cutoff=CUTOFF, version=1, status="candidate",
            checksum=checksum, payload=payload,
            manifest_id=MANIFEST,
            event_catalog_publication_id=binding["event_catalog_publication_id"],
            event_catalog_checksum=binding["event_catalog_checksum"],
            created_at=CUTOFF, fence=0,
        ))
    return f"pub-{stream_key}"


def test_runner_records_exact_artifacts_and_never_advances_pointers(tmp_path):
    engine, governance, binding = _runner_world(tmp_path)
    season_ids = governance.expected_season_game_ids
    _write_legacy_facts(engine, game_ids_by_team=season_ids)
    publications = {
        "traditional_opponent_season": _insert_runner_publication(
            engine, stream_key="traditional_opponent_season", surface="traditional",
            window="season", game_ids_by_team=season_ids, binding=binding,
        ),
        "assist_locations_season": _insert_runner_publication(
            engine, stream_key="assist_locations_season", surface="assist_locations",
            window="season", game_ids_by_team=season_ids, binding=binding,
        ),
    }

    runner = MatchupParityRunner(
        engine,
        governance=ActiveManifestLedgerGovernanceReader(engine),
        legacy_source=StoredLegacyMatchupSource(TeamMatchupRepository(engine)),
    )
    reports = runner.run("2025-26", "season", cutoff=CUTOFF, publications=publications)

    assert {report.surface for report in reports} == {"traditional", "assist_locations"}
    assert all(report.exact for report in reports)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM publication_pointers")).scalar_one() == 0
    repository = LedgerParityArtifactRepository(engine)
    for stream in ("traditional_opponent_season", "assist_locations_season"):
        artifact = repository.latest(stream, "2025-26")
        assert artifact is not None
        assert artifact.status == "exact"
        assert assume_utc(artifact.cutoff) == CUTOFF


def test_runner_rejects_legacy_provenance_mismatch(tmp_path):
    engine, governance, binding = _runner_world(tmp_path)

    class BadSource:
        def produce(self, *, season, window, cutoff, governance):
            return _materialization(manifest_id="different-manifest")

    runner = MatchupParityRunner(
        engine,
        governance=ActiveManifestLedgerGovernanceReader(engine),
        legacy_source=BadSource(),
    )
    with pytest.raises(MatchupParityError, match="authority mismatch"):
        runner.run("2025-26", "season", cutoff=CUTOFF)


def test_runner_rejects_ledger_publication_authority_mismatch(tmp_path):
    engine, governance, binding = _runner_world(tmp_path)
    season_ids = governance.expected_season_game_ids
    _write_legacy_facts(engine, game_ids_by_team=season_ids)
    # Insert a publication bound to a different manifest.
    payload = _publication_payload(season_ids, surface="traditional")
    checksum = publication_payload_checksum(payload)
    with engine.begin() as connection:
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id="other-manifest", season="2025-26", cutoff=CUTOFF,
            collect_before=CUTOFF + timedelta(hours=1), accepted_versions="[1]",
            scopes='["canonical_game_ledger"]', checksum="other-manifest-checksum",
            event_catalog_publication_id=binding["event_catalog_publication_id"],
            event_catalog_checksum=binding["event_catalog_checksum"],
            status="active", created_at=CUTOFF,
        ))
        connection.execute(PublicationVersion.__table__.insert().values(
            publication_id="pub-bad", stream_key="traditional_opponent_season",
            season="2025-26", cutoff=CUTOFF, version=1, status="candidate",
            checksum=checksum, payload=payload,
            manifest_id="other-manifest",
            event_catalog_publication_id=binding["event_catalog_publication_id"],
            event_catalog_checksum=binding["event_catalog_checksum"],
            created_at=CUTOFF, fence=0,
        ))

    assist_publication_id = _insert_runner_publication(
        engine, stream_key="assist_locations_season", surface="assist_locations",
        window="season", game_ids_by_team=season_ids, binding=binding,
    )

    runner = MatchupParityRunner(
        engine,
        governance=ActiveManifestLedgerGovernanceReader(engine),
        legacy_source=StoredLegacyMatchupSource(TeamMatchupRepository(engine)),
    )
    with pytest.raises(MatchupParityError, match="authority"):
        runner.run("2025-26", "season", cutoff=CUTOFF,
                   publications={
                       "traditional_opponent_season": "pub-bad",
                       "assist_locations_season": assist_publication_id,
                   })


def test_runner_does_not_record_hard_failed_reports(tmp_path):
    engine, governance, binding = _runner_world(tmp_path)
    season_ids = governance.expected_season_game_ids
    _write_legacy_facts(engine, game_ids_by_team=season_ids)
    # Ledger publication with an off-by-one count is a hard integer difference.
    publication_id = _insert_runner_publication(
        engine, stream_key="traditional_opponent_season", surface="traditional",
        window="season", game_ids_by_team=season_ids, binding=binding,
    )
    assist_publication_id = _insert_runner_publication(
        engine, stream_key="assist_locations_season", surface="assist_locations",
        window="season", game_ids_by_team=season_ids, binding=binding,
    )
    # Override the ledger count by rewriting the payload.
    payload = _publication_payload(season_ids, surface="traditional", offset=999)
    checksum = publication_payload_checksum(payload)
    with engine.begin() as connection:
        connection.execute(
            PublicationVersion.__table__.update().where(
                PublicationVersion.publication_id == publication_id,
            ).values(checksum=checksum, payload=payload)
        )

    runner = MatchupParityRunner(
        engine,
        governance=ActiveManifestLedgerGovernanceReader(engine),
        legacy_source=StoredLegacyMatchupSource(TeamMatchupRepository(engine)),
    )
    reports = runner.run("2025-26", "season", cutoff=CUTOFF,
                         publications={
                             "traditional_opponent_season": publication_id,
                             "assist_locations_season": assist_publication_id,
                         })

    assert reports[0].hard_failure
    assert LedgerParityArtifactRepository(engine).latest(
        "traditional_opponent_season", "2025-26"
    ) is None


# --- Activation revalidation -----------------------------------------------

def test_assist_stream_activation_requires_parity(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'activation.sqlite3'}")
    run_migrations(engine)
    publications = PublicationService(engine)
    publications.register_default_streams()

    for stream in ("assist_locations_season", "assist_locations_l15",
                   "traditional_opponent_season", "traditional_opponent_l15"):
        with pytest.raises(ControlPlaneError, match="ledger_parity_evidence_required"):
            publications.activate_stream(stream, reason="unproven ledger candidate")


# --- CLI integration -------------------------------------------------------

def test_script_compare_uses_actual_publications_and_stored_legacy(tmp_path):
    import scripts.matchup_parity as matchup_parity_script

    engine, governance, binding = _runner_world(tmp_path)
    season_ids = governance.expected_season_game_ids
    _write_legacy_facts(engine, game_ids_by_team=season_ids)
    pub_trad = _insert_runner_publication(
        engine, stream_key="traditional_opponent_season", surface="traditional",
        window="season", game_ids_by_team=season_ids, binding=binding,
    )
    pub_assist = _insert_runner_publication(
        engine, stream_key="assist_locations_season", surface="assist_locations",
        window="season", game_ids_by_team=season_ids, binding=binding,
    )
    publications_path = tmp_path / "publications.json"
    publications_path.write_text(json.dumps({
        "traditional_opponent_season": pub_trad,
        "assist_locations_season": pub_assist,
    }), encoding="utf-8")

    args = SimpleNamespace(
        season="2025-26", window="season",
        cutoff=CUTOFF.isoformat(),
        publications_json=str(publications_path),
    )
    exit_code = matchup_parity_script._compare(args, engine)

    assert exit_code == 0
    repository = LedgerParityArtifactRepository(engine)
    for stream in ("traditional_opponent_season", "assist_locations_season"):
        assert repository.latest(stream, "2025-26") is not None


def test_script_rejects_mismatched_command_scope(tmp_path):
    import scripts.matchup_parity as matchup_parity_script

    engine, governance, binding = _runner_world(tmp_path)
    season_ids = governance.expected_season_game_ids
    _write_legacy_facts(engine, game_ids_by_team=season_ids)
    pub_trad = _insert_runner_publication(
        engine, stream_key="traditional_opponent_season", surface="traditional",
        window="season", game_ids_by_team=season_ids, binding=binding,
    )
    publications_path = tmp_path / "publications.json"
    publications_path.write_text(json.dumps({
        "traditional_opponent_season": pub_trad,
    }), encoding="utf-8")

    # A wrong season is rejected by the immutable governance resolution.
    args = SimpleNamespace(
        season="2023-24", window="season",
        cutoff=CUTOFF.isoformat(),
        publications_json=str(publications_path),
    )
    with pytest.raises(PublicationGovernanceUnavailable):
        matchup_parity_script._compare(args, engine)


def test_script_rejects_non_aware_cutoff(tmp_path):
    import scripts.matchup_parity as matchup_parity_script

    engine, governance, binding = _runner_world(tmp_path)
    with pytest.raises(SystemExit):
        matchup_parity_script._aware_utc("2024-11-15T00:00:00")


# --- Byte-contract compatibility over the public read model ----------------

def test_public_read_model_is_byte_identical_for_legacy_and_ledger_facts(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'byte.sqlite3'}")
    run_migrations(engine)
    repository = TeamMatchupRepository(engine)
    scope = TeamMatchupSnapshotScope("2025-26", CUTOFF.date())
    query = TeamMatchupQueryService(repository)

    def serialize():
        window = query.get_window(scope)
        def key(d):
            return d["base"], d["slice_key"], d["stat_key"]
        metrics = {
            "league": sorted(
                (asdict(metric) for metric in window.league_metrics), key=key
            ),
            "team": {
                str(team_id): sorted(
                    (asdict(metric) for metric in team_metrics), key=key
                )
                for team_id, team_metrics in sorted(window.team_metrics.items())
            },
        }
        return canonical_publication_json(metrics)

    repository.replace_snapshots(
        (
            (
                scope,
                (
                    *_surface_facts(TEAM_IDS, surface="traditional", provider="nba_stats"),
                    *_surface_facts(TEAM_IDS, surface="assist_locations", provider="pbp_stats"),
                ),
                _observations(),
            ),
        ),
        retrieved_at=CUTOFF,
    )
    legacy_bytes = serialize()

    repository.replace_snapshots(
        (
            (
                scope,
                (
                    *_surface_facts(TEAM_IDS, surface="traditional", provider="ledger"),
                    *_surface_facts(TEAM_IDS, surface="assist_locations", provider="ledger"),
                ),
                _observations(),
            ),
        ),
        retrieved_at=CUTOFF,
    )
    ledger_bytes = serialize()

    assert legacy_bytes == ledger_bytes


def test_matchup_stream_key_maps_surfaces_and_windows():
    assert matchup_stream_key("traditional", "season") == "traditional_opponent_season"
    assert matchup_stream_key("assist_locations", "l15") == "assist_locations_l15"
    with pytest.raises(ValueError):
        matchup_stream_key("shot_zones", "season")
