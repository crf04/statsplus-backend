"""From 2026-27 the ledger publishes NBA's official box score (crf04/statsplus-backend#763)."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine

from app.migrations import run_migrations
from app.models.collection_control import CollectionManifest
from app.providers.nba_tracking import parse_box_score_traditional
from app.services.canonical_game_ledger import CanonicalGameLedgerRepository
from app.services.collection_control import CollectionControlService
from app.services.ledger_backfill import LedgerBackfillService, collection_control_reconciliation_sink
from app.utils.telemetry import ProviderResponseError
from scripts import ledger_box_score_differences
from tests.services.test_ledger_backfill import _Recorder
from tests.services.test_ledger_heaves import (
    _Athletes,
    _event,
    _LiveData,
    _Participants,
    _participants,
    _PBPStats,
)

GAME_ID = "0022600168"
SEASON = "2026-27"
PETERSON, SENSABAUGH, BAILEY = 1643408, 1641729, 1642846
UTA = 1610612762
CUTOFF = datetime(2026, 11, 3, 12, tzinfo=UTC)


def _fixture(path):
    return json.loads(Path(f"tests/fixtures/{path}").read_text())


def _official(game_id=GAME_ID, **peterson):
    document = _fixture(f"nba_stats/boxscoretraditionalv3_{game_id}.json")
    for player in document["boxScoreTraditional"]["awayTeam"]["players"]:
        if player["personId"] == PETERSON:
            player["statistics"].update(peterson)
    return document


class _BoxScores:
    def __init__(self, document):
        self.document = document

    def fetch_game_box_score(self, game_id):
        if isinstance(self.document, Exception):
            raise self.document
        assert game_id == GAME_ID
        return parse_box_score_traditional(copy.deepcopy(self.document))


class _Ledger:
    def __init__(self, engine, box_scores, *, sink=None):
        self.engine = engine
        run_migrations(self.engine)
        with self.engine.begin() as connection:
            connection.execute(
                CollectionManifest.__table__.insert().values(
                    manifest_id="ledger-manifest",
                    season=SEASON,
                    cutoff=CUTOFF,
                    collect_before=CUTOFF + timedelta(days=45),
                    accepted_versions="[1]",
                    scopes='["canonical_game_ledger"]',
                    checksum="ledger-manifest",
                    status="active",
                    created_at=CUTOFF,
                )
            )
        self.payload = _fixture(f"pbp_stats/game_stats_box_score_{GAME_ID}.json")
        participants = _participants(self.payload)
        self.repository = CanonicalGameLedgerRepository(self.engine)
        self.box_scores = box_scores
        self.reports = []
        self.now = CUTOFF
        self.service = LedgerBackfillService(
            provider=_PBPStats(self.payload),
            live_data_provider=_LiveData(
                _fixture(f"nba_live_data/playbyplay_box_score_{GAME_ID}.json"),
                _fixture(f"nba_live_data/boxscore_box_score_{GAME_ID}.json"),
            ),
            box_score_provider=box_scores,
            athlete_catalog=_Athletes([player for players in participants.values() for player in players]),
            participant_catalog=_Participants(participants),
            reconciliation_sink=sink or self._report,
            observation_recorder=_Recorder(),
            repository=self.repository,
            max_concurrency=1,
            clock=lambda: self.now,
        )

    def _report(self, game_id, details):
        if details["kind"] == "box_score_differences":
            self.reports.append((game_id, details))

    def refresh(self):
        return self.service.refresh(
            SEASON,
            cutoff=CUTOFF,
            governed_events=(_event(SEASON, {**self.payload, "stats_game_id": GAME_ID}),),
            manifest_id="ledger-manifest",
            manifest_scope="canonical_game_ledger",
            collect_before=CUTOFF + timedelta(days=45),
            schema_version=1,
            accepted_versions=frozenset({1}),
        )

    def player(self, player_id):
        return next(
            fact for fact in self.repository.get_game(GAME_ID).player_facts if fact.player_id == player_id
        )

    def team(self, team_id):
        return next(fact for fact in self.repository.get_game(GAME_ID).team_facts if fact.team_id == team_id)


def _counts(fact):
    return {
        "PTS": fact.points,
        "REB": fact.rebounds,
        "AST": fact.assists,
        "FGM": fact.field_goals_made,
        "FGA": fact.field_goals_attempted,
        "FG3A": fact.three_pointers_attempted,
    }


def _engine(tmp_path):
    return create_engine(f"sqlite:///{tmp_path / 'box-score.sqlite3'}")


def _difference(player_id, player_name, stat, ledger_value, box_score_value, applied=True):
    return {
        "player_id": player_id,
        "player_name": player_name,
        "stat": stat,
        "ledger_value": ledger_value,
        "box_score_value": box_score_value,
        "applied": applied,
    }


def test_the_official_box_score_is_the_reference_not_the_player_game_logs():
    peterson = parse_box_score_traditional(_official())[PETERSON]
    bailey = parse_box_score_traditional(_official("0022600028"))[BAILEY]

    assert (peterson["AST"], peterson["PTS"], peterson["FGA"]) == (5, 14, 13)
    assert (bailey["REB"], bailey["OREB"], bailey["DREB"]) == (6, 2, 4)


def test_the_ledger_publishes_the_box_score_counts_pbp_stats_still_has(tmp_path):
    ledger = _Ledger(_engine(tmp_path), _BoxScores(_official()))

    result = ledger.refresh()

    assert result.complete
    assert _counts(ledger.player(PETERSON)) == {"PTS": 14, "REB": 4, "AST": 5, "FGM": 5, "FGA": 13, "FG3A": 4}
    assert _counts(ledger.player(SENSABAUGH)) == {
        "PTS": 7,
        "REB": 2,
        "AST": 2,
        "FGM": 2,
        "FGA": 10,
        "FG3A": 5,
    }
    uta = ledger.team(UTA)
    assert (uta.assists, uta.three_pointers_attempted, uta.points) == (16, 33, 97)
    assert (result.box_score_differences, result.unreconciled_game_ids) == (3, ())
    assert ledger.reports == [
        (
            GAME_ID,
            {
                "kind": "box_score_differences",
                "season": SEASON,
                "differences": (
                    _difference(SENSABAUGH, "Brice Sensabaugh", "AST", 1, 2),
                    _difference(SENSABAUGH, "Brice Sensabaugh", "FG3A", 6, 5),
                    _difference(PETERSON, "Darryn Peterson", "AST", 6, 5),
                ),
            },
        )
    ]


def test_a_game_whose_box_score_cannot_be_fetched_is_accepted_and_counted_unreconciled(tmp_path):
    ledger = _Ledger(_engine(tmp_path), _BoxScores(ProviderResponseError("stats.nba.com timed out")))

    result = ledger.refresh()

    assert result.complete
    assert ledger.player(PETERSON).assists == 6
    assert (result.box_score_differences, result.unreconciled_game_ids) == (0, (GAME_ID,))
    assert ledger.reports == []


def test_a_rerun_inside_the_daily_window_picks_up_a_box_score_correction(tmp_path):
    box_scores = _BoxScores(_official(assists=6))
    ledger = _Ledger(_engine(tmp_path), box_scores)
    assert ledger.refresh().box_score_differences == 2
    assert ledger.player(PETERSON).assists == 6

    box_scores.document = _official()
    ledger.now = CUTOFF + timedelta(hours=25)
    result = ledger.refresh()

    assert (result.games_replaced, result.box_score_differences) == (1, 3)
    assert ledger.player(PETERSON).assists == 5


def test_a_rebound_difference_is_recorded_without_breaking_the_rebound_types(tmp_path):
    ledger = _Ledger(_engine(tmp_path), _BoxScores(_official(reboundsDefensive=5, reboundsTotal=5)))

    result = ledger.refresh()

    assert result.complete
    peterson = ledger.player(PETERSON)
    assert (peterson.rebounds, peterson.assists, peterson.def_two_pt_rebounds) == (4, 5, 2)
    ((_, report),) = ledger.reports
    assert [difference for difference in report["differences"] if difference["player_id"] == PETERSON] == [
        _difference(PETERSON, "Darryn Peterson", "REB", 4, 5, applied=False),
        _difference(PETERSON, "Darryn Peterson", "DREB", 4, 5, applied=False),
        _difference(PETERSON, "Darryn Peterson", "AST", 6, 5),
    ]


def test_the_command_lists_open_differences_until_the_provider_agrees(tmp_path, capsys):
    assert_the_command_lists_open_differences_until_the_provider_agrees(_engine(tmp_path), capsys)


def assert_the_command_lists_open_differences_until_the_provider_agrees(engine, capsys):
    run_migrations(engine)
    control = CollectionControlService(engine, clock=lambda: CUTOFF)
    box_scores = _BoxScores(_official())
    ledger = _Ledger(
        engine, box_scores, sink=collection_control_reconciliation_sink(control, default_season=SEASON)
    )

    ledger.refresh()
    assert (
        ledger_box_score_differences.main(
            ["--database-url", engine.url.render_as_string(hide_password=False)]
        )
        == 0
    )
    listed = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(item["player_name"], item["stat"]) for item in listed] == [
        ("Brice Sensabaugh", "AST"),
        ("Brice Sensabaugh", "FG3A"),
        ("Darryn Peterson", "AST"),
    ]
    assert listed[2] == {
        "season": SEASON,
        "game_id": GAME_ID,
        **_difference(PETERSON, "Darryn Peterson", "AST", 6, 5),
        "first_seen": "2026-11-03T12:00:00+00:00",
        "last_seen": "2026-11-03T12:00:00+00:00",
    }

    box_scores.document = _official(assists=6)
    ledger.now = CUTOFF + timedelta(hours=25)
    ledger.refresh()
    assert (
        ledger_box_score_differences.main(
            ["--database-url", engine.url.render_as_string(hide_password=False), "--season", SEASON]
        )
        == 0
    )
    listed = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(item["player_name"], item["stat"]) for item in listed] == [
        ("Brice Sensabaugh", "AST"),
        ("Brice Sensabaugh", "FG3A"),
    ]


def test_the_provider_fetches_one_paced_box_score_per_game_with_the_standard_headers(monkeypatch):
    from nba_api.stats.library.http import STATS_HEADERS, NBAStatsHTTP

    from app.providers.nba_tracking import NBATrackingProvider

    calls, sleeps, now = [], [], [100.0]

    def send(_http, endpoint, parameters, headers=None, timeout=None, **_kwargs):
        calls.append((endpoint, parameters["GameID"], headers))
        now[0] += 0.25
        return type("Response", (), {"get_dict": staticmethod(lambda: _official(parameters["GameID"]))})()

    monkeypatch.setattr(NBAStatsHTTP, "send_api_request", send)
    provider = NBATrackingProvider(pace_seconds=0.6, sleep=sleeps.append, monotonic=lambda: now[0])

    first = provider.fetch_game_box_score(GAME_ID)
    second = provider.fetch_game_box_score("0022600028")

    assert (first[PETERSON]["AST"], second[BAILEY]["REB"]) == (5, 6)
    assert calls == [
        ("boxscoretraditionalv3", GAME_ID, STATS_HEADERS),
        ("boxscoretraditionalv3", "0022600028", STATS_HEADERS),
    ]
    assert sleeps == [0.6]
