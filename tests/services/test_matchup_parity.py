"""Offline contract tests for the matchup materializer dual-run parity."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine

from app.migrations import run_migrations
from app.models.collection_control import PublicationVersion

from app.services.ledger_parity import LedgerParityArtifactRepository
from app.services.matchup_parity import (
    CLASSIFICATION_AVAILABILITY_DIFFERENCE,
    CLASSIFICATION_CUTOFF_MISMATCH,
    CLASSIFICATION_DENOMINATOR_TOLERANCE_EXCEEDED,
    CLASSIFICATION_GAME_SET_MISMATCH,
    CLASSIFICATION_INTEGER_COUNT_DIFFERENCE,
    CLASSIFICATION_LEAGUE_INCOMPLETE,
    CLASSIFICATION_NON_INTEGER_COUNT,
    CLASSIFICATION_RANKING_DIFFERENCE,
    MATCHUP_PARITY_TOLERANCE,
    compare_matchup_materializations,
)
from app.services.team_matchup_repository import (
    TeamMatchupFact,
    TeamMatchupObservation,
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


def _traditional_facts(team_ids, *, provider, offset=0, game_ids=None, minutes=48.0):
    facts = []
    for team_id in team_ids:
        for stat in TRADITIONAL_STATS:
            facts.append(TeamMatchupFact(
                team_id=team_id,
                base="traditional",
                slice_key=stat,
                stat_key=stat,
                raw_value=float(team_id + offset),
                denominator_value=minutes,
                denominator_unit="minutes",
                provider=provider,
                game_ids=tuple(game_ids[team_id]) if game_ids else (),
            ))
    return facts


def _assist_facts(team_ids, *, provider, offset=0, game_ids=None, minutes=48.0, unit="minutes"):
    facts = []
    for team_id in team_ids:
        for stat in ASSIST_STATS:
            facts.append(TeamMatchupFact(
                team_id=team_id,
                base="assist_locations",
                slice_key=stat,
                stat_key=stat,
                raw_value=float(team_id + offset),
                denominator_value=minutes,
                denominator_unit=unit,
                provider=provider,
                game_ids=tuple(game_ids[team_id]) if game_ids else (),
            ))
    return facts


def _observations(surfaces=("traditional", "assist_locations"), status="available"):
    return tuple(
        TeamMatchupObservation(surface=surface, status=status)
        for surface in surfaces
    )


def _game_ids_by_team(team_ids, *, prefix="g"):
    return {
        team_id: (f"{prefix}{team_id}-1", f"{prefix}{team_id}-2")
        for team_id in team_ids
    }


def _compare(*, legacy_extra=(), ledger_extra=(), legacy_game_ids=None, ledger_game_ids=None,
             legacy_as_of=None, legacy_status="available"):
    game_ids = legacy_game_ids or _game_ids_by_team(TEAM_IDS)
    ledger_ids = ledger_game_ids or game_ids
    legacy_facts = (
        *_traditional_facts(TEAM_IDS, provider="nba_stats"),
        *_assist_facts(TEAM_IDS, provider="pbp_stats"),
        *legacy_extra,
    )
    ledger_facts = (
        *_traditional_facts(TEAM_IDS, provider="ledger", game_ids=ledger_ids),
        *_assist_facts(TEAM_IDS, provider="ledger", game_ids=ledger_ids),
        *ledger_extra,
    )
    return compare_matchup_materializations(
        legacy_facts,
        _observations(status=legacy_status),
        ledger_facts,
        _observations(),
        season="2024-25",
        window="season",
        as_of=date(2024, 11, 15),
        legacy_as_of=legacy_as_of or date(2024, 11, 15),
        expected_team_ids=TEAM_IDS,
        legacy_game_ids_by_team=game_ids,
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


def test_integer_count_difference_requires_adjudication():
    report = _compare(ledger_extra=(
        TeamMatchupFact(1, "traditional", "OPP_REB", "OPP_REB", 999.0, 48.0, "minutes", "ledger", game_ids=("g1-1", "g1-2")),
    ))

    assert not report.exact
    assert report.adjudication_required
    assert any(
        difference.classification == CLASSIFICATION_INTEGER_COUNT_DIFFERENCE
        and difference.team_id == 1
        and difference.field == "OPP_REB"
        for difference in report.differences
    )


def test_game_set_mismatch_is_classified():
    ledger_ids = _game_ids_by_team(TEAM_IDS)
    ledger_ids[1] = ("different-1", "different-2")

    report = _compare(ledger_game_ids=ledger_ids)

    assert not report.game_sets_exact
    assert any(
        difference.classification == CLASSIFICATION_GAME_SET_MISMATCH
        and difference.team_id == 1
        for difference in report.differences
    )


def test_seconds_denominator_is_normalized_to_minutes():
    report = _compare(
        legacy_extra=(
            TeamMatchupFact(1, "assist_locations", "Assists", "Assists", 1.0, 2880.0, "seconds", "pbp_stats"),
        ),
    )

    assert report.exact
    assert not report.differences


def test_denominator_tolerance_exceeded_is_classified():
    report = _compare(
        legacy_extra=(
            TeamMatchupFact(1, "traditional", "OPP_REB", "OPP_REB", 1.0, 100.0, "minutes", "nba_stats"),
        ),
    )

    assert not report.exact
    assert any(
        difference.classification == CLASSIFICATION_DENOMINATOR_TOLERANCE_EXCEEDED
        for difference in report.differences
    )


def test_league_incomplete_is_classified():
    report = compare_matchup_materializations(
        _traditional_facts(TEAM_IDS[1:], provider="nba_stats"),
        _observations(),
        _traditional_facts(TEAM_IDS, provider="ledger", game_ids=_game_ids_by_team(TEAM_IDS)),
        _observations(),
        season="2024-25",
        window="season",
        as_of=date(2024, 11, 15),
        legacy_as_of=date(2024, 11, 15),
        expected_team_ids=TEAM_IDS,
        legacy_game_ids_by_team=_game_ids_by_team(TEAM_IDS[1:]),
    )

    assert not report.league_complete
    assert any(
        difference.classification == CLASSIFICATION_LEAGUE_INCOMPLETE
        for difference in report.differences
    )


def test_availability_difference_is_classified():
    report = _compare(legacy_status="unavailable")

    assert any(
        difference.classification == CLASSIFICATION_AVAILABILITY_DIFFERENCE
        for difference in report.differences
    )


def test_cutoff_mismatch_is_classified():
    report = _compare(legacy_as_of=date(2024, 11, 14))

    assert not report.cutoffs_aligned
    assert any(
        difference.classification == CLASSIFICATION_CUTOFF_MISMATCH
        for difference in report.differences
    )


def test_non_integer_count_is_classified():
    report = _compare(
        legacy_extra=(
            TeamMatchupFact(1, "traditional", "OPP_REB", "OPP_REB", 1.5, 48.0, "minutes", "nba_stats"),
        ),
    )

    assert any(
        difference.classification == CLASSIFICATION_NON_INTEGER_COUNT
        for difference in report.differences
    )


def test_near_tie_rank_flip_is_classified_without_a_count_difference():
    # Teams 1 and 2 have equal ledger per-48 rates (a tie, ranks 29,29).  A
    # sub-tolerance denominator change on the legacy side flips the ordering
    # (team 2 edges team 1) without tripping the count or tolerance checks.
    game_ids = _game_ids_by_team(TEAM_IDS)
    legacy_facts = (
        *_traditional_facts(TEAM_IDS, provider="nba_stats"),
        *_assist_facts(TEAM_IDS, provider="pbp_stats"),
        TeamMatchupFact(1, "traditional", "OPP_REB", "OPP_REB", 1.0, 1.0, "minutes", "nba_stats"),
        TeamMatchupFact(2, "traditional", "OPP_REB", "OPP_REB", 2.0, 2.000000001, "minutes", "nba_stats"),
    )
    ledger_facts = (
        *_traditional_facts(TEAM_IDS, provider="ledger", game_ids=game_ids),
        *_assist_facts(TEAM_IDS, provider="ledger", game_ids=game_ids),
        TeamMatchupFact(1, "traditional", "OPP_REB", "OPP_REB", 1.0, 1.0, "minutes", "ledger", game_ids=game_ids[1]),
        TeamMatchupFact(2, "traditional", "OPP_REB", "OPP_REB", 2.0, 2.0, "minutes", "ledger", game_ids=game_ids[2]),
    )

    report = compare_matchup_materializations(
        legacy_facts, _observations(),
        ledger_facts, _observations(),
        season="2024-25", window="season",
        as_of=date(2024, 11, 15), legacy_as_of=date(2024, 11, 15),
        expected_team_ids=TEAM_IDS, legacy_game_ids_by_team=game_ids,
    )

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
        compare_matchup_materializations(
            (), (), (), (),
            season="2024-25", window="season",
            as_of=date(2024, 11, 15), legacy_as_of=date(2024, 11, 15),
            expected_team_ids=TEAM_IDS, legacy_game_ids_by_team={},
            tolerance=-1.0,
        )


def _candidate(engine, *, stream_key="traditional_opponent_season", checksum="a" * 64):
    publication_id = f"candidate-{stream_key}"
    with engine.begin() as connection:
        connection.execute(PublicationVersion.__table__.insert().values(
            publication_id=publication_id,
            stream_key=stream_key,
            season="2024-25",
            cutoff=datetime(2024, 11, 15, tzinfo=timezone.utc),
            version=1,
            status="candidate",
            checksum=checksum,
            payload="{}",
            created_at=datetime(2024, 11, 15, tzinfo=timezone.utc),
            fence=0,
        ))
    return publication_id, checksum


def test_matchup_parity_artifact_is_durable_activation_evidence(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'parity.sqlite3'}")
    run_migrations(engine)
    repository = LedgerParityArtifactRepository(engine)
    publication_id, checksum = _candidate(engine)

    report = _compare()
    artifact = repository.record_matchup_parity(
        "traditional_opponent_season",
        cutoff=datetime(2024, 11, 15, tzinfo=timezone.utc),
        report=report,
        publication_id=publication_id,
        payload_checksum=checksum,
    )

    assert artifact.status == "exact"
    stored = repository.latest("traditional_opponent_season", "2024-25")
    assert stored is not None
    assert stored.artifact_id == artifact.artifact_id

    approved = repository.adjudicate(
        artifact.artifact_id,
        decision="approved",
        actor="operator@example.com",
        reason="dual-run reviewed",
    )
    assert approved.decision == "approved"


def test_documented_tolerance_is_finite_and_positive():
    assert MATCHUP_PARITY_TOLERANCE > 0
    assert MATCHUP_PARITY_TOLERANCE < 1
