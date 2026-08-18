"""Offline contract tests for the matchup materializer dual-run parity.

The comparator accepts two independently produced, provenance-bound
materializations; the ledger side is always derived from a verified candidate
publication, never from author-written JSON.
"""

from __future__ import annotations

import json
import hashlib
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.domain.nba_teams import NBA_TEAM_ID_TO_TRICODE
from app.domain.publication_integrity import (
    canonical_publication_json,
    publication_payload_checksum,
)
from app.domain.utc import assume_utc
from app.migrations import run_migrations
from app.models.collection_control import (
    ActiveSeason,
    CatalogPublication,
    CollectionManifest,
    PublicationVersion,
)
from app.models.canonical_game_ledger import LedgerParityArtifact
from app.services.collection_control import ControlPlaneError, PublicationService
from app.services.ledger_parity import (
    LEGACY_MATCHUP_DIAGNOSTIC_CAPTURE_STREAM,
    LedgerParityArtifactRepository,
    matchup_parity_artifact_is_activatable,
    matchup_parity_cohort_is_activatable,
)
from app.services.ledger_runtime import ActiveManifestLedgerGovernanceReader
from app.services.ledger_derivations import (
    ASSIST_DERIVED_METRICS,
    TEAM_METRICS,
    competition_ranks,
)
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
    CLASSIFICATION_SERVED_RANK_MISMATCH,
    CLASSIFICATION_SERVED_RATE_MISMATCH,
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
from app.services.team_matchup_refresh import (
    TeamMatchupRefreshService,
    _ProviderWindowUnverified,
    _nba_team_stats_request_descriptor,
    _pbp_totals_request_descriptor,
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


class _AllowTestLegacyWrites:
    def assert_writable(self, stream_key, *, connection=None):
        return None


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
             expected_game_ids=None, tolerance=MATCHUP_PARITY_TOLERANCE,
             semantic_rule=None, semantic_rule_reason=None):
    return compare_matchup_materializations(
        legacy or _materialization(),
        ledger or _materialization(),
        surface=surface,
        expected_team_ids=TEAM_IDS,
        expected_game_ids_by_team=expected_game_ids or _game_ids_by_team(TEAM_IDS),
        tolerance=tolerance,
        semantic_rule=semantic_rule,
        semantic_rule_reason=semantic_rule_reason,
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


def _ledger_with_served_derivations():
    base = _materialization()
    rates = {
        (fact.team_id, fact.stat_key): fact.raw_value * 48.0 / fact.denominator_value
        for fact in base.facts
    }
    ranks = {
        (team_id, stat_key): rank
        for stat_key in TRADITIONAL_STATS + ASSIST_STATS
        for team_id, rank in competition_ranks(
            {
                fact.team_id: rates[(fact.team_id, stat_key)]
                for fact in base.facts
                if fact.stat_key == stat_key
            },
            descending=False,
        ).items()
    }
    return replace(
        base,
        producer=PRODUCER_LEDGER,
        publication_id="publication",
        payload_checksum="payload-checksum",
        served_per48=rates,
        served_ranks=ranks,
    )


def test_wrong_served_per48_cannot_activate():
    ledger = _ledger_with_served_derivations()
    served_per48 = dict(ledger.served_per48)
    served_per48[(TEAM_A, "OPP_REB")] += 10_000.0

    report = _compare(ledger=replace(ledger, served_per48=served_per48))

    assert report.hard_failure
    assert any(
        difference.classification == CLASSIFICATION_SERVED_RATE_MISMATCH
        for difference in report.differences
    )


def test_wrong_served_rank_cannot_activate():
    ledger = _ledger_with_served_derivations()
    served_ranks = dict(ledger.served_ranks)
    served_ranks[(TEAM_A, "OPP_REB")] = 99

    report = _compare(ledger=replace(ledger, served_ranks=served_ranks))

    assert report.hard_failure
    assert any(
        difference.classification == CLASSIFICATION_SERVED_RANK_MISMATCH
        for difference in report.differences
    )


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


def test_unexplained_floating_denominator_difference_is_hard():
    legacy = _replace_fact(_materialization(), surface="traditional", team_id=TEAM_A, stat="OPP_REB", denominator_value=100.0)

    report = _compare(legacy=legacy)

    assert report.hard_failure
    assert not report.adjudication_required
    assert not report.exact
    assert {d.classification for d in report.differences} <= SOFT_CLASSIFICATIONS | {
        CLASSIFICATION_DERIVED_RATE_DIFFERENCE,
    }


def test_provider_rounding_cannot_soften_public_minutes_or_rates():
    legacy = _replace_fact(
        _materialization(),
        surface="traditional",
        team_id=TEAM_A,
        stat="OPP_REB",
        denominator_value=100.0,
    )

    report = _compare(
        legacy=legacy,
        semantic_rule="provider_rounding",
        semantic_rule_reason="provider rounding in the legacy denominator",
    )

    assert report.hard_failure
    assert not report.adjudication_required



def test_matchup_tolerance_is_exactly_one_nanounit():
    assert MATCHUP_PARITY_TOLERANCE == 1e-9

    within = _replace_fact(
        _materialization(),
        surface="traditional",
        team_id=TEAM_A,
        stat="OPP_REB",
        denominator_value=48.0 * (1.0 + 0.9e-9),
    )
    assert not _compare(legacy=within).hard_failure

    outside = _replace_fact(
        _materialization(),
        surface="traditional",
        team_id=TEAM_A,
        stat="OPP_REB",
        denominator_value=48.0 * (1.0 + 1.1e-9),
    )
    assert _compare(legacy=outside).hard_failure
    assert any(
        difference.classification == "denominator_tolerance_exceeded"
        for difference in _compare(legacy=outside).differences
    )

    with pytest.raises(ValueError, match="rel_tol=abs_tol=1e-9"):
        _compare(tolerance=1e-6)


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


def test_near_tie_rank_flip_is_hard_and_not_adjudicable():
    base = _materialization()
    ledger = _replace_fact(base, surface="traditional", team_id=TEAM_A, stat="OPP_REB",
                           raw_value=1.0, denominator_value=1.0)
    ledger = _replace_fact(ledger, surface="traditional", team_id=TEAM_B, stat="OPP_REB",
                           raw_value=2.0, denominator_value=2.0)
    legacy = _replace_fact(base, surface="traditional", team_id=TEAM_A, stat="OPP_REB",
                           raw_value=1.0, denominator_value=1.0)
    legacy = _replace_fact(legacy, surface="traditional", team_id=TEAM_B, stat="OPP_REB",
                           raw_value=2.0, denominator_value=4.0)

    report = _compare(legacy=legacy, ledger=ledger)

    assert report.hard_failure
    assert not report.adjudication_required
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
        competition_rank = {
            metric: TEAM_IDS.index(team_id) + 1 for metric in payload_metrics
        }
        rows.append({
            "team_id": team_id,
            "team_tricode": NBA_TEAM_ID_TO_TRICODE[team_id],
            "game_ids": sorted(game_ids_by_team[team_id]),
            "game_count": len(game_ids_by_team[team_id]),
            "per48": per48,
            "league_average": {metric: 1.0 for metric in payload_metrics},
            "population_sigma": {metric: 1.0 for metric in payload_metrics},
            "competition_rank": competition_rank,
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
        connection.execute(ActiveSeason.__table__.insert().values(
            season="2025-26", phase="Regular Season", status="active",
            cutoff=CUTOFF, activated_at=CUTOFF, activated_by="test",
        ))
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
        connection.execute(ActiveSeason.__table__.insert().values(
            season="2025-26", phase="Regular Season", status="active",
            cutoff=CUTOFF, activated_at=CUTOFF, activated_by="test",
        ))
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


def test_refresh_manifest_resolution_ignores_superseded_history_and_fails_closed(
    tmp_path,
):
    engine, _, _ = _runner_world(tmp_path)
    with engine.begin() as connection:
        original = connection.execute(select(CollectionManifest.__table__).where(
            CollectionManifest.manifest_id == MANIFEST
        )).mappings().one()
        historical = dict(original)
        historical.update({
            "manifest_id": "superseded-history", "status": "superseded",
            "checksum": "superseded-manifest-checksum",
            "created_at": CUTOFF + timedelta(seconds=1),
        })
        connection.execute(CollectionManifest.__table__.insert().values(**historical))
    service = object.__new__(TeamMatchupRefreshService)
    service.repository = TeamMatchupRepository(engine)
    service._clock = lambda: CUTOFF

    assert service._provenance_for_snapshot("2025-26", CUTOFF.date()).manifest_id == MANIFEST

    with engine.begin() as connection:
        connection.execute(CollectionManifest.__table__.update().where(
            CollectionManifest.manifest_id == MANIFEST
        ).values(status="superseded"))
    with pytest.raises(_ProviderWindowUnverified, match="authority"):
        service._provenance_for_snapshot("2025-26", CUTOFF.date())

    with engine.begin() as connection:
        connection.execute(CollectionManifest.__table__.update().where(
            CollectionManifest.manifest_id.in_((MANIFEST, "superseded-history"))
        ).values(status="active"))
    with pytest.raises(_ProviderWindowUnverified, match="authority"):
        service._provenance_for_snapshot("2025-26", CUTOFF.date())


def _write_legacy_facts(engine, *, game_ids_by_team, window="season"):
    repository = TeamMatchupRepository(engine, write_fence=_AllowTestLegacyWrites())
    scope = TeamMatchupSnapshotScope(
        "2025-26", CUTOFF.date(), 15 if window == "l15" else None
    )
    with engine.connect() as connection:
        authority = connection.execute(
            select(
                CollectionManifest.event_catalog_publication_id,
                CollectionManifest.event_catalog_checksum,
            ).where(CollectionManifest.manifest_id == MANIFEST)
        ).mappings().one()
    if window == "season":
        aggregate_requests = {
            "traditional:league": _nba_team_stats_request_descriptor(
                season="2025-26", season_type="Regular Season", team_id=None,
                last_n_games=0, date_from=None, date_to="11/15/2024",
            ),
            "assist_locations:league": _pbp_totals_request_descriptor(
                season="2025-26", season_type="Regular Season", team_id=None,
                from_date=None, to_date="2024-11-15",
            ),
        }
    else:
        aggregate_requests = {
            **{
                f"traditional:{team_id}": _nba_team_stats_request_descriptor(
                    season="2025-26", season_type="Regular Season",
                    team_id=team_id, last_n_games=15,
                    date_from="10/31/2024", date_to="11/15/2024",
                )
                for team_id in TEAM_IDS
            },
            **{
                f"assist_locations:{team_id}": _pbp_totals_request_descriptor(
                    season="2025-26", season_type="Regular Season",
                    team_id=team_id, from_date="2024-10-31",
                    to_date="2024-11-15",
                )
                for team_id in TEAM_IDS
            },
        }
    provider_identity = json.dumps({
        "window": window,
        "provider_source": "nba_stats.team_game_log",
        "provider_sources": [
            "nba_stats.team_game_log",
            "pbp_stats.team_game_log",
        ],
        "collect_before": (CUTOFF + timedelta(hours=1)).isoformat(),
        "aggregate_requests": aggregate_requests,
        "aggregate_request_checksum": hashlib.sha256(json.dumps(
            aggregate_requests, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest(),
        "provider_game_ids_by_source": {
            source: {
                str(team_id): sorted(game_ids_by_team[team_id])
                for team_id in TEAM_IDS
            }
            for source in (
                "nba_stats.team_game_log",
                "pbp_stats.team_game_log",
            )
        },
        "teams": {
            str(team_id): {
                "expected_games": len(game_ids_by_team[team_id]),
                "authority_game_ids": sorted(game_ids_by_team[team_id]),
                "provider_game_ids": sorted(game_ids_by_team[team_id]),
            }
            for team_id in TEAM_IDS
        },
    }, sort_keys=True, separators=(",", ":"))
    facts = tuple(
        replace(
            fact,
            cutoff=CUTOFF,
            manifest_id=MANIFEST,
            event_catalog_publication_id=authority["event_catalog_publication_id"],
            event_catalog_checksum=authority["event_catalog_checksum"],
            provider_window_identity=provider_identity,
            window_start_date=(date(2024, 10, 31) if window == "l15" else None),
        )
        for fact in (
            *_surface_facts(TEAM_IDS, surface="traditional", provider="nba_stats", game_ids_by_team=game_ids_by_team),
            *_surface_facts(TEAM_IDS, surface="assist_locations", provider="pbp_stats", game_ids_by_team=game_ids_by_team),
        )
    )
    observations = tuple(
        replace(
            observation,
            cutoff=CUTOFF,
            manifest_id=MANIFEST,
            event_catalog_publication_id=authority["event_catalog_publication_id"],
            event_catalog_checksum=authority["event_catalog_checksum"],
            provider_window_identity=provider_identity,
        )
        for observation in _observations()
    )
    repository.replace_snapshots(
        (
            (
                scope,
                (
                    *facts,
                ),
                observations,
            ),
        ),
        retrieved_at=CUTOFF,
    )


def _insert_runner_publication(
    engine, *, stream_key, surface, window, game_ids_by_team, binding,
    publication_suffix="", created_at=CUTOFF,
):
    payload = _publication_payload(game_ids_by_team, surface=surface)
    checksum = publication_payload_checksum(payload)
    with engine.begin() as connection:
        connection.execute(PublicationVersion.__table__.insert().values(
            publication_id=f"pub-{stream_key}{publication_suffix}", stream_key=stream_key,
            season="2025-26", cutoff=CUTOFF, version=1, status="candidate",
            checksum=checksum, payload=payload,
            manifest_id=MANIFEST,
            event_catalog_publication_id=binding["event_catalog_publication_id"],
            event_catalog_checksum=binding["event_catalog_checksum"],
            created_at=created_at, fence=0,
        ))
    return f"pub-{stream_key}{publication_suffix}"


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
        captures = connection.execute(text(
            "SELECT artifact_id, payload_checksum, report "
            "FROM canonical_game_ledger_parity_artifacts WHERE stream_key = :stream"
        ), {"stream": LEGACY_MATCHUP_DIAGNOSTIC_CAPTURE_STREAM}).mappings().all()
    assert len(captures) == 2
    assert {json.loads(row["report"])["surface"] for row in captures} == {
        "traditional", "assist_locations",
    }
    assert all(
        json.loads(row["report"])["publication_id"] in publications.values()
        for row in captures
    )
    capture = captures[0]
    original_report = capture["report"]
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE team_matchup_facts SET raw_value = raw_value + 1"
        ))
    with engine.connect() as connection:
        preserved = connection.execute(text(
            "SELECT payload_checksum, report FROM canonical_game_ledger_parity_artifacts "
            "WHERE artifact_id = :artifact_id"
        ), {"artifact_id": capture["artifact_id"]}).mappings().one()
    assert preserved["report"] == original_report
    assert preserved["payload_checksum"] == capture["payload_checksum"]
    repository = LedgerParityArtifactRepository(engine)
    for stream in ("traditional_opponent_season", "assist_locations_season"):
        artifact = repository.latest(stream, "2025-26")
        assert artifact is not None
        assert artifact.status == "exact"
        assert assume_utc(artifact.cutoff) == CUTOFF
    tampered = json.loads(original_report)
    tampered["facts"][0]["raw_value"] += 1
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE canonical_game_ledger_parity_artifacts SET report = :report "
            "WHERE artifact_id = :artifact_id"
        ), {
            "report": json.dumps(tampered, sort_keys=True, separators=(",", ":")),
            "artifact_id": capture["artifact_id"],
        })
    stream = matchup_stream_key(tampered["surface"], tampered["window"])
    artifact = repository.latest(stream, "2025-26")
    with Session(engine) as session:
        assert not matchup_parity_artifact_is_activatable(
            artifact, stream_key=stream, session=session
        )


def test_legacy_provider_aggregate_request_checksum_is_required(tmp_path):
    engine, governance, _ = _runner_world(tmp_path)
    _write_legacy_facts(
        engine, game_ids_by_team=governance.expected_season_game_ids
    )
    with engine.begin() as connection:
        identity = json.loads(connection.execute(text(
            "SELECT provider_window_identity FROM team_matchup_facts LIMIT 1"
        )).scalar_one())
        identity["aggregate_request_checksum"] = "0" * 64
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        connection.execute(text(
            "UPDATE team_matchup_facts SET provider_window_identity = :identity"
        ), {"identity": encoded})
        connection.execute(text(
            "UPDATE team_matchup_surface_observations SET provider_window_identity = :identity"
        ), {"identity": encoded})

    with pytest.raises(MatchupParityError, match="window identity"):
        StoredLegacyMatchupSource(TeamMatchupRepository(engine)).produce(
            season="2025-26", window="season", cutoff=CUTOFF,
            governance=governance,
        )


def test_legacy_request_rejects_changed_wire_default_even_with_valid_checksum(tmp_path):
    engine, governance, _ = _runner_world(tmp_path)
    _write_legacy_facts(engine, game_ids_by_team=governance.expected_season_game_ids)
    with engine.begin() as connection:
        identity = json.loads(connection.execute(text(
            "SELECT provider_window_identity FROM team_matchup_facts LIMIT 1"
        )).scalar_one())
        identity["aggregate_requests"]["traditional:league"]["parameters"][
            "Month"
        ] = "1"
        identity["aggregate_request_checksum"] = hashlib.sha256(json.dumps(
            identity["aggregate_requests"], sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        connection.execute(text(
            "UPDATE team_matchup_facts SET provider_window_identity = :identity"
        ), {"identity": encoded})
        connection.execute(text(
            "UPDATE team_matchup_surface_observations "
            "SET provider_window_identity = :identity"
        ), {"identity": encoded})

    with pytest.raises(MatchupParityError, match="window identity"):
        StoredLegacyMatchupSource(TeamMatchupRepository(engine)).produce(
            season="2025-26", window="season", cutoff=CUTOFF,
            governance=governance,
        )


def test_legacy_season_and_l15_use_one_locked_caller_snapshot(tmp_path):
    engine, governance, _ = _runner_world(tmp_path)
    _write_legacy_facts(
        engine, game_ids_by_team=governance.expected_season_game_ids,
        window="season",
    )
    _write_legacy_facts(
        engine, game_ids_by_team=governance.expected_l15_game_ids,
        window="l15",
    )
    delegate = TeamMatchupRepository(engine)
    calls = []

    class SnapshotRepository:
        def get_snapshot(self, scope, *, connection=None, lock=False):
            calls.append((connection, lock, scope.window_games))
            return delegate.get_snapshot(
                scope, connection=connection, lock=lock
            )

    source = StoredLegacyMatchupSource(SnapshotRepository())
    with Session(engine) as session, session.begin():
        source.produce(
            season="2025-26", window="season", cutoff=CUTOFF,
            governance=governance, session=session,
        )
        source.produce(
            season="2025-26", window="l15", cutoff=CUTOFF,
            governance=governance, session=session,
        )

    assert [window for _, _, window in calls] == [None, 15]
    assert all(lock is True for _, lock, _ in calls)
    assert calls[0][0] is calls[1][0]


def test_stored_source_accepts_authority_bound_unavailable_l15_surface(tmp_path):
    engine, governance, binding = _runner_world(tmp_path)
    game_ids = governance.expected_l15_game_ids
    requests = {
        **{
            f"traditional:{team_id}": _nba_team_stats_request_descriptor(
                season="2025-26", season_type="Regular Season", team_id=team_id,
                last_n_games=15, date_from="10/31/2024", date_to="11/15/2024",
            )
            for team_id in TEAM_IDS
        },
        **{
            f"assist_locations:{team_id}": _pbp_totals_request_descriptor(
                season="2025-26", season_type="Regular Season", team_id=team_id,
                from_date="2024-10-31", to_date="2024-11-15",
            )
            for team_id in TEAM_IDS
        },
    }
    identities = TeamMatchupRefreshService._surface_window_identities(
        window="l15", game_ids_by_team=game_ids,
        provider_game_ids_by_surface={"assist_locations": game_ids},
        expected_counts={team_id: 15 for team_id in TEAM_IDS},
        collect_before=governance.collect_before, aggregate_requests=requests,
    )
    observation = TeamMatchupObservation(
        surface="traditional", status="unavailable",
        unavailable_reason="provider_window_unverified", cutoff=CUTOFF,
        manifest_id=governance.manifest_id,
        event_catalog_publication_id=governance.event_catalog_publication_id,
        event_catalog_checksum=governance.event_catalog_checksum,
        provider_window_identity=identities["traditional"],
    )
    assist_observation = replace(
        observation, surface="assist_locations", status="available",
        unavailable_reason=None,
        provider_window_identity=identities["assist_locations"],
    )
    assist_facts = tuple(
        replace(
            fact, cutoff=CUTOFF, manifest_id=governance.manifest_id,
            event_catalog_publication_id=governance.event_catalog_publication_id,
            event_catalog_checksum=governance.event_catalog_checksum,
            provider_window_identity=identities["assist_locations"],
            window_start_date=date(2024, 10, 31),
        )
        for fact in _surface_facts(
            TEAM_IDS, surface="assist_locations", provider="pbp_stats",
            game_ids_by_team=game_ids,
        )
    )

    class SnapshotRepository:
        def get_snapshot(self, scope, **kwargs):
            assert scope.window_games == 15
            return SimpleNamespace(
                facts=assist_facts,
                observations=(observation, assist_observation),
            )

    materialization = StoredLegacyMatchupSource(SnapshotRepository()).produce(
        season="2025-26", window="l15", cutoff=CUTOFF,
        governance=governance, surface="traditional",
    )

    assert materialization.facts == ()
    assert materialization.observations == (observation,)
    assert set(materialization.game_ids_by_team) == set(TEAM_IDS)
    assert all(not ids for ids in materialization.game_ids_by_team.values())

    publications = {
        stream: _insert_runner_publication(
            engine, stream_key=stream,
            surface=("traditional" if stream.startswith("traditional") else "assist_locations"),
            window="l15", game_ids_by_team=game_ids, binding=binding,
        )
        for stream in ("traditional_opponent_l15", "assist_locations_l15")
    }
    reports = MatchupParityRunner(
        engine, governance=ActiveManifestLedgerGovernanceReader(engine),
        legacy_source=StoredLegacyMatchupSource(SnapshotRepository()),
    ).run("2025-26", "l15", cutoff=CUTOFF, publications=publications)

    assert reports[0].hard_failure
    assert reports[1].exact
    assert LedgerParityArtifactRepository(engine).latest(
        "traditional_opponent_l15", "2025-26"
    ).status == "pending_adjudication"
    assert LedgerParityArtifactRepository(engine).latest(
        "assist_locations_l15", "2025-26"
    ).status == "exact"


def test_cohort_selects_latest_valid_reruns_and_rejects_mixed_authority(tmp_path):
    engine, governance, binding = _runner_world(tmp_path)
    season_ids = governance.expected_season_game_ids
    l15_ids = governance.expected_l15_game_ids
    _write_legacy_facts(engine, game_ids_by_team=season_ids, window="season")
    season_publications = {
        stream: _insert_runner_publication(
            engine,
            stream_key=stream,
            surface=("traditional" if stream.startswith("traditional") else "assist_locations"),
            window="season",
            game_ids_by_team=season_ids,
            binding=binding,
        )
        for stream in (
            "traditional_opponent_season", "assist_locations_season",
        )
    }
    runner = MatchupParityRunner(
        engine,
        governance=ActiveManifestLedgerGovernanceReader(engine),
        legacy_source=StoredLegacyMatchupSource(TeamMatchupRepository(engine)),
    )
    runner.run("2025-26", "season", cutoff=CUTOFF, publications=season_publications)

    _write_legacy_facts(engine, game_ids_by_team=l15_ids, window="l15")
    l15_publications = {
        stream: _insert_runner_publication(
            engine,
            stream_key=stream,
            surface=("traditional" if stream.startswith("traditional") else "assist_locations"),
            window="l15",
            game_ids_by_team=l15_ids,
            binding=binding,
            publication_suffix="-l15",
        )
        for stream in (
            "traditional_opponent_l15", "assist_locations_l15",
        )
    }
    runner.run("2025-26", "l15", cutoff=CUTOFF, publications=l15_publications)

    # A fifth artifact is the newest valid traditional Season rerun.  The
    # cohort must choose it, while a newer rejected historical row is ignored.
    _write_legacy_facts(engine, game_ids_by_team=season_ids, window="season")
    rerun_publication = _insert_runner_publication(
        engine,
        stream_key="traditional_opponent_season",
        surface="traditional",
        window="season",
        game_ids_by_team=season_ids,
        binding=binding,
        publication_suffix="-rerun",
        created_at=CUTOFF + timedelta(minutes=5),
    )
    runner.run(
        "2025-26", "season", cutoff=CUTOFF,
        publications={
            "traditional_opponent_season": rerun_publication,
            "assist_locations_season": season_publications["assist_locations_season"],
        },
    )
    with create_session(engine) as session:
        latest = session.scalar(select(LedgerParityArtifact).where(
            LedgerParityArtifact.publication_id == rerun_publication,
            LedgerParityArtifact.stream_key == "traditional_opponent_season",
        ))
        assert latest is not None
        session.add(LedgerParityArtifact(
            artifact_id="rejected-newer-history",
            publication_id=rerun_publication,
            payload_checksum=latest.payload_checksum,
            stream_key=latest.stream_key,
            season=latest.season,
            cutoff=latest.cutoff,
            status="pending_adjudication",
            decision="rejected",
            report="{}",
            created_at=CUTOFF + timedelta(minutes=6),
        ))
        session.commit()
        assert matchup_parity_cohort_is_activatable(
            session,
            season="2025-26",
            cutoff=CUTOFF,
            candidate_publication_id=rerun_publication,
            artifact_id=latest.artifact_id,
        )

        # Bind one otherwise-valid selected artifact to a second immutable
        # catalog/manifest authority.  The cohort must fail closed rather than
        # mixing authority generations across its four streams.
        assist_l15 = session.scalar(select(LedgerParityArtifact).where(
            LedgerParityArtifact.stream_key == "assist_locations_l15",
            LedgerParityArtifact.season == "2025-26",
        ))
        assert assist_l15 is not None
        publication = session.get(PublicationVersion, assist_l15.publication_id)
        assert publication is not None
        document = json.loads(assist_l15.report)
        document.update({
            "ledger_publication_id": "pub-assist-l15-mixed",
            "ledger_manifest_id": "mixed-manifest",
            "ledger_event_catalog_publication_id": "mixed-catalog",
            "legacy_manifest_id": "mixed-manifest",
            "legacy_event_catalog_publication_id": "mixed-catalog",
        })
        catalog = session.get(CatalogPublication, binding["event_catalog_publication_id"])
        assert catalog is not None
        session.add(CatalogPublication(
            publication_id="mixed-catalog",
            season=catalog.season,
            catalog_type=catalog.catalog_type,
            cutoff=catalog.cutoff,
            version=catalog.version,
            checksum=catalog.checksum,
            payload=catalog.payload,
            complete=True,
            published_at=catalog.published_at,
            expires_at=catalog.expires_at,
        ))
        session.flush()
        session.add(CollectionManifest(
            manifest_id="mixed-manifest",
            season="2025-26",
            cutoff=CUTOFF,
            collect_before=CUTOFF + timedelta(hours=1),
            accepted_versions="[1]",
            scopes='["canonical_game_ledger"]',
            checksum="mixed-manifest-checksum",
            event_catalog_publication_id="mixed-catalog",
            event_catalog_checksum=binding["event_catalog_checksum"],
            status="active",
            created_at=CUTOFF,
        ))
        session.flush()
        session.add(PublicationVersion(
            publication_id="pub-assist-l15-mixed",
            stream_key=publication.stream_key,
            season=publication.season,
            cutoff=publication.cutoff,
            version=2,
            status="candidate",
            checksum=publication.checksum,
            payload=publication.payload,
            manifest_id="mixed-manifest",
            event_catalog_publication_id="mixed-catalog",
            event_catalog_checksum=catalog.checksum,
            created_at=datetime.now(timezone.utc) + timedelta(minutes=7),
            fence=0,
        ))
        session.flush()
        session.add(LedgerParityArtifact(
            artifact_id="mixed-authority-artifact",
            publication_id="pub-assist-l15-mixed",
            payload_checksum=publication.checksum,
            stream_key=assist_l15.stream_key,
            season=assist_l15.season,
            cutoff=assist_l15.cutoff,
            status="exact",
            report=json.dumps(document, sort_keys=True),
            created_at=datetime.now(timezone.utc) + timedelta(minutes=8),
        ))
        session.commit()
        mixed = session.get(LedgerParityArtifact, "mixed-authority-artifact")
        assert mixed is not None
        assert not matchup_parity_artifact_is_activatable(mixed, session=session)
        # A second qualifying authority at the same cutoff invalidates even
        # the previously complete cohort.
        assert not matchup_parity_cohort_is_activatable(
            session,
            season="2025-26",
            cutoff=CUTOFF,
            candidate_publication_id=rerun_publication,
            artifact_id=latest.artifact_id,
        )


def test_stored_legacy_rejects_nullable_date_only_rows(tmp_path):
    engine, governance, _ = _runner_world(tmp_path)
    repository = TeamMatchupRepository(engine, write_fence=_AllowTestLegacyWrites())
    repository.replace_snapshots(
        (
            (
                TeamMatchupSnapshotScope("2025-26", CUTOFF.date()),
                (
                    *_surface_facts(
                        TEAM_IDS, surface="traditional", provider="nba_stats",
                    ),
                    *_surface_facts(
                        TEAM_IDS, surface="assist_locations", provider="pbp_stats",
                    ),
                ),
                _observations(),
            ),
        ),
        retrieved_at=CUTOFF,
    )
    with pytest.raises(MatchupParityError, match="provenance"):
        StoredLegacyMatchupSource(repository).produce(
            season="2025-26", window="season", cutoff=CUTOFF,
            governance=governance,
        )


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


def test_runner_records_hard_failed_reports_as_pending_and_unapprovable(tmp_path):
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
    artifact = LedgerParityArtifactRepository(engine).latest(
        "traditional_opponent_season", "2025-26"
    )
    assert artifact is not None
    assert artifact.status == "pending_adjudication"
    assert artifact.decision is None
    assert artifact.adjudicated_by is None
    assert artifact.adjudicated_at is None
    assert json.loads(artifact.report)["status"] == "failed"
    with create_session(engine) as session:
        assert not matchup_parity_artifact_is_activatable(
            artifact, stream_key="traditional_opponent_season", session=session
        )


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
    repository = TeamMatchupRepository(engine, write_fence=_AllowTestLegacyWrites())
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


def test_bounded_compare_requires_player_per36_for_season():
    import scripts.matchup_parity as matchup_parity_script

    assert matchup_parity_script._required_streams("season") == frozenset({
        "traditional_opponent_season",
        "assist_locations_season",
        "player_per36",
    })
    assert matchup_parity_script._required_streams("l15") == frozenset({
        "traditional_opponent_l15",
        "assist_locations_l15",
    })


def test_manifest_preflight_requires_one_unique_qualifying_authority(tmp_path):
    import scripts.matchup_parity as matchup_parity_script

    engine, _, _ = _runner_world(tmp_path)
    _, manifest, _ = matchup_parity_script._manifest_preflight(
        engine, season="2025-26", manifest_id=MANIFEST,
    )
    assert manifest["id"] == MANIFEST

    with Session(engine) as session, session.begin():
        authority = session.get(CollectionManifest, MANIFEST)
        original = session.get(
            CatalogPublication, authority.event_catalog_publication_id
        )
        session.add(CatalogPublication(
            publication_id="duplicate-catalog", season=original.season,
            catalog_type=original.catalog_type, cutoff=original.cutoff,
            version="duplicate", checksum=original.checksum,
            payload=original.payload, complete=True, published_at=CUTOFF,
        ))
        session.add(CollectionManifest(
            manifest_id="duplicate-manifest", season="2025-26", cutoff=CUTOFF,
            collect_before=CUTOFF + timedelta(hours=1), accepted_versions="[1]",
            scopes='["canonical_game_ledger"]', checksum="duplicate-manifest-checksum",
            event_catalog_publication_id="duplicate-catalog",
            event_catalog_checksum=original.checksum, status="active",
            created_at=CUTOFF,
        ))

    with pytest.raises(
        matchup_parity_script.InvalidEvidenceError,
        match="manifest_authority_ambiguous",
    ):
        matchup_parity_script._manifest_preflight(
            engine, season="2025-26", manifest_id=MANIFEST,
        )


def test_compare_cli_carries_explicit_safety_contract(monkeypatch, tmp_path):
    import scripts.matchup_parity as matchup_parity_script

    received = {}

    def fake_compare(args, engine):
        received.update(vars(args))
        assert engine == "sqlite:///:memory:"
        return 0

    monkeypatch.setattr(matchup_parity_script, "_compare", fake_compare)
    monkeypatch.setattr(
        matchup_parity_script,
        "create_engine",
        lambda database_url: database_url,
    )
    summary = tmp_path / "summary.json"
    monkeypatch.setattr(sys, "argv", [
        "matchup_parity.py",
        "compare",
        "--database-url", "sqlite:///:memory:",
        "--season", "2025-26",
        "--manifest-id", "manifest",
        "--actor", "operator@example.com",
        "--output", str(summary),
        "--target", "isolated",
        "--per36-capture-id", "capture-id",
    ])

    assert matchup_parity_script.main() == 0
    assert received["target"] == "isolated"
    assert received["actor"] == "operator@example.com"
    assert received["output"] == str(summary)
    assert "publications_json" not in received


def test_capture_per36_cli_uses_audited_recorder_and_sanitized_output(
    monkeypatch, tmp_path
):
    import scripts.matchup_parity as matchup_parity_script

    input_path = tmp_path / "capture-input.json"
    output_path = tmp_path / "capture-output.json"
    evidence = {
        "game_set_checksum": "a" * 64,
        "provider_window_identity": {"window": "season"},
        "request_checksum": "b" * 64,
        "rows": [{"player_id": 1, "points": 99}],
    }
    input_path.write_text(json.dumps(evidence), encoding="utf-8")
    received = {}
    capture = SimpleNamespace(
        capture_id="capture-id", capture_checksum="c" * 64,
        source_observation_id="observation-id",
        publication_id="publication-id", payload_checksum="d" * 64,
        request_checksum="b" * 64,
    )

    class Recorder:
        def __init__(self, engine):
            assert engine == "engine"

        def record_operator_evidence(self, **kwargs):
            received.update(kwargs)
            return capture

    monkeypatch.setattr(
        matchup_parity_script, "Per36DiagnosticCaptureRepository", Recorder
    )
    monkeypatch.setattr(
        matchup_parity_script, "_manifest_preflight",
        lambda *args, **kwargs: (None, {
            "cutoff": CUTOFF.isoformat(),
            "event_catalog_publication_id": "catalog-id",
            "event_catalog_checksum": "e" * 64,
        }, 42),
    )
    args = SimpleNamespace(
        actor="operator@example.com", input=str(input_path),
        output=str(output_path), season="2025-26", manifest_id="manifest-id",
        publication_id="publication-id",
    )

    assert matchup_parity_script._capture_per36(args, "engine") == 0
    summary = json.loads(output_path.read_text())
    assert received["rows"] == evidence["rows"]
    assert summary["capture_id"] == "capture-id"
    assert "rows" not in summary
    assert "actor" not in summary


def test_sanitized_summary_omits_row_values():
    import scripts.matchup_parity as matchup_parity_script

    report = _compare(
        legacy=_replace_fact(
            _materialization(),
            surface="traditional",
            team_id=TEAM_A,
            stat="OPP_REB",
            denominator_value=100.0,
        )
    )
    summary = matchup_parity_script._sanitize_matchup_report(report)
    encoded = json.dumps(summary)

    assert summary["difference_classifications"]
    assert "ledger_value" not in encoded
    assert "legacy_value" not in encoded
    assert "semantic_rule_reason" not in encoded


def test_hard_reports_are_pending_and_protected_ids_stay_out_of_summary(capsys):
    import scripts.matchup_parity as matchup_parity_script

    assert matchup_parity_script._overall_status([
        {"status": "exact"}, {"status": "failed"},
    ]) == "pending_adjudication"
    governance = SimpleNamespace(
        expected_season_game_ids={TEAM_A: ("season-2", "season-1")},
        expected_l15_game_ids={TEAM_A: ("l15-1",)},
    )
    matchup_parity_script._print_protected_game_ids(governance)
    output = capsys.readouterr().out
    assert "PROTECTED OPERATOR OUTPUT" in output
    assert f"{TEAM_A}: season-1,season-2" in output
    assert f"{TEAM_A}: l15-1" in output


def test_invalid_summary_keeps_nonmutation_proof_when_prestate_was_captured():
    import scripts.matchup_parity as matchup_parity_script

    before = {"pointers": {"traditional_opponent_season": None}, "streams": {}}
    args = SimpleNamespace(
        target="candidate",
        season="2025-26",
        _control_state_before=before,
        _control_state_after=before.copy(),
        _artifact_transaction_rolled_back=True,
    )

    summary = matchup_parity_script._invalid_summary(args, "candidate_provenance_invalid")

    assert summary["artifact_writes_rolled_back"] is True
    assert summary["pointer_nonmutation"]["unchanged"] is True
    assert summary["stream_nonmutation"]["unchanged"] is True


def test_invalid_summary_does_not_claim_an_unproven_rollback():
    import scripts.matchup_parity as matchup_parity_script

    before = {"pointers": {}, "streams": {}}
    args = SimpleNamespace(
        target="candidate",
        season="2025-26",
        _control_state_before=before,
        _control_state_after=before.copy(),
    )

    summary = matchup_parity_script._invalid_summary(args, "output_failed")

    assert "artifact_writes_rolled_back" not in summary


def test_summary_is_staged_without_publishing_before_commit(tmp_path):
    import scripts.matchup_parity as matchup_parity_script

    destination = tmp_path / "summary.json"
    staged = matchup_parity_script._stage_summary(
        str(destination), {"status": "exact"}
    )

    assert staged.exists()
    assert not destination.exists()
    matchup_parity_script._publish_summary(str(destination), staged)
    assert json.loads(destination.read_text()) == {"status": "exact"}


def test_postcommit_output_failure_is_not_reported_as_rollback():
    import scripts.matchup_parity as matchup_parity_script

    before = {"pointers": {}, "streams": {}}
    args = SimpleNamespace(
        target="candidate",
        season="2025-26",
        _control_state_before=before,
        _control_state_after=before.copy(),
        _database_transaction_committed=True,
    )

    summary = matchup_parity_script._invalid_summary(args, "output_failed")

    assert summary["artifact_transaction_committed"] is True
    assert "artifact_writes_rolled_back" not in summary


def test_failed_database_commit_never_publishes_staged_summary(
    tmp_path, monkeypatch
):
    import scripts.matchup_parity as matchup_parity_script

    destination = tmp_path / "summary.json"
    staged = matchup_parity_script._stage_summary(
        str(destination), {"status": "exact"}
    )
    args = SimpleNamespace(
        output=str(destination), _artifact_session=object(),
        _staged_summary=staged,
    )
    transaction = SimpleNamespace(
        commit=lambda: (_ for _ in ()).throw(RuntimeError("commit failed"))
    )
    session = SimpleNamespace(close=lambda: None)
    published = []
    monkeypatch.setattr(
        matchup_parity_script, "_publish_summary",
        lambda *values: published.append(values),
    )

    with pytest.raises(RuntimeError, match="commit failed"):
        matchup_parity_script._commit_and_publish_summary(
            transaction, session, args, staged
        )

    assert published == []
    assert not destination.exists()
    assert not getattr(args, "_database_transaction_committed", False)


def test_output_failure_occurs_only_after_commit(monkeypatch, tmp_path):
    import scripts.matchup_parity as matchup_parity_script

    calls = []
    transaction = SimpleNamespace(commit=lambda: calls.append("commit"))
    session = SimpleNamespace(close=lambda: calls.append("close"))
    args = SimpleNamespace(
        output=str(tmp_path / "summary.json"),
        _artifact_session=session,
        _staged_summary=tmp_path / ".summary.tmp",
    )

    def fail_publish(*_args):
        calls.append("publish")
        raise OSError("rename failed")

    monkeypatch.setattr(matchup_parity_script, "_publish_summary", fail_publish)

    with pytest.raises(OSError, match="rename failed"):
        matchup_parity_script._commit_and_publish_summary(
            transaction, session, args, args._staged_summary
        )

    assert calls == ["commit", "close", "publish"]
    assert args._database_transaction_committed is True
    assert args._artifact_session is None
