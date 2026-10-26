"""A split-field regression must not erase stored complete evidence (#446).

Driven through the real refresh service: the primary PBP observation loses a
miss-type field, and the same rows would otherwise be composed through the
LiveData fallback and archived as split-less fallback evidence.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine

from app.migrations import run_migrations
from app.providers.nba_live_data import compose_pbp_live_data_observation
from app.services.canonical_game_ledger import (
    CanonicalGameLedgerRepository,
    ReboundTypeRegressionError,
    canonical_game_from_pbp,
)
from app.services.ledger_backfill import LedgerBackfillService
from app.utils.telemetry import ProviderResponseError
from tests.providers.test_nba_live_data import _event, _live_payload
from tests.services.test_ledger_backfill import (
    _Athletes,
    _authorized,
    _install_manifest,
    _Participants,
    _Provider,
    _Recorder,
)

CUTOFF = datetime(2024, 11, 16, tzinfo=UTC)
SPLIT = (
    "def_two_pt_rebounds",
    "def_three_pt_rebounds",
    "ft_def_rebounds",
    "off_two_pt_rebounds",
    "off_three_pt_rebounds",
    "ft_off_rebounds",
)


def _documents():
    live = _live_payload()
    live["game"]["homeTeam"]["players"][0]["statistics"].update(reboundsDefensive=2, reboundsTotal=2)
    live["game"]["homeTeam"]["statistics"].update(reboundsDefensive=2, reboundsTotal=2)
    ordinary = compose_pbp_live_data_observation(None, live, event=_event())
    ordinary.pop("_ledger_provenance")
    for side in ("Home", "Away"):
        for row in ordinary["stats"][side]["FullGame"]:
            row.pop("LedgerEvidenceProvider", None)
    complete = deepcopy(ordinary)
    complete["stats"]["Home"]["FullGame"][1]["DefTwoPtRebounds"] = 2
    return live, ordinary, complete


def _setup(tmp_path):
    live, regressed, complete = _documents()
    engine = create_engine(f"sqlite:///{tmp_path / 'refresh.sqlite3'}")
    run_migrations(engine)
    _install_manifest(engine, CUTOFF)
    repository = CanonicalGameLedgerRepository(engine)
    repository.replace_game(
        canonical_game_from_pbp(complete, event=_event(), retrieved_at=datetime(2024, 11, 15, tzinfo=UTC))
    )
    return live, regressed, repository


def _stored(repository):
    return [
        (p.player_id, p.defensive_rebounds, p.def_two_pt_rebounds)
        for p in repository.get_game("0022400001").player_facts
    ]


class _Fallback:
    def __init__(self, live):
        self.live = live
        self.calls = 0

    def fetch_game_stats(self, *args, **kwargs):
        self.calls += 1
        return self.live


class _UnavailablePrimary:
    def fetch_game_stats(self, *args, **kwargs):
        raise ProviderResponseError("PBP is down")


def _refresh(repository, primary, fallback):
    service = LedgerBackfillService(
        provider=primary,
        live_data_provider=fallback,
        repository=repository,
        athlete_catalog=_Athletes(),
        participant_catalog=_Participants(),
        reconciliation_sink=lambda *args: None,
        observation_recorder=_Recorder(),
        max_concurrency=1,
        clock=lambda: CUTOFF,
    )
    return service.refresh("2024-25", historical_repair=True, **_authorized(_event(), CUTOFF))


def test_a_primary_split_regression_fails_the_game_and_never_reaches_livedata(tmp_path, monkeypatch):
    live, regressed, repository = _setup(tmp_path)
    assert _stored(repository)[0] == (2544, 2, 2)
    fallback = _Fallback(live)
    composed = []
    monkeypatch.setattr(
        "app.services.ledger_backfill.compose_pbp_live_data_observation",
        lambda *args, **kwargs: composed.append(args),
    )

    result = _refresh(repository, _Provider(regressed), fallback)

    assert result.failed_game_ids == ("0022400001",)
    assert result.games_replaced == 0
    # LiveData supplies the final score only; its box score is never evidence.
    assert composed == []
    assert _stored(repository)[0] == (2544, 2, 2)


def test_livedata_only_evidence_cannot_replace_a_game_that_carries_the_split(tmp_path):
    live, _regressed, repository = _setup(tmp_path)
    fallback = _Fallback(live)

    # PBP is down altogether: the fallback is legitimately consulted, but its
    # split-less evidence must not displace the stored complete facts.
    result = _refresh(repository, _UnavailablePrimary(), fallback)

    assert fallback.calls == 1
    assert result.failed_game_ids == ("0022400001",)
    assert result.games_replaced == 0
    assert _stored(repository)[0] == (2544, 2, 2)


def test_the_repository_refuses_to_drop_the_split_with_a_named_reason(tmp_path):
    live, _regressed, repository = _setup(tmp_path)
    stored = repository.get_game("0022400001")
    fallback_game = canonical_game_from_pbp(
        compose_pbp_live_data_observation(None, live, event=_event()),
        event=_event(),
        retrieved_at=datetime(2024, 11, 17, tzinfo=UTC),
        source_observation_id="obs-fallback",
    )
    assert all(getattr(p, SPLIT[0]) is None for p in fallback_game.player_facts)

    with pytest.raises(ReboundTypeRegressionError, match="rebound_types_regression"):
        repository.replace_game(fallback_game)

    assert repository.get_game("0022400001") == stored
