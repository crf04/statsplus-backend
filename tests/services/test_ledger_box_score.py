"""From 2026-27 the ledger publishes NBA's official box score (crf04/statsplus-backend#763)."""

from __future__ import annotations

import copy
import json
import threading
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import requests
from sqlalchemy import create_engine, select

from app.migrations import run_migrations
from app.models.collection_control import CollectionManifest, ReconciliationItem
from app.providers.nba_tracking import NBATrackingProvider, parse_box_score_traditional
from app.services.canonical_game_ledger import CanonicalGameLedgerRepository, record_schema_drift
from app.services.collection_control import CollectionControlService
from app.services.ledger_backfill import LedgerBackfillService, collection_control_reconciliation_sink
from app.services.ledger_derivations import (
    NBA_TEAM_IDS,
    derive_assist_location_facts,
    derive_player_assist_diet_rows,
    derive_player_rebound_type_diet_rows,
    materialize_assist_location_window,
    materialize_rebound_type_window,
)
from app.services.ledger_materialization import LedgerMaterializationService
from app.services.ledger_parity import LedgerParityArtifactRepository
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
COLLECT_BEFORE = CUTOFF + timedelta(days=45)
PROVIDER_PETERSON = {"PTS": 14, "REB": 4, "AST": 6, "FGM": 5, "FGA": 13, "FG3A": 4}
ASSIST_LOCATION_WIRES = (
    "TwoPtAssists",
    "ThreePtAssists",
    "Arc3Assists",
    "Corner3Assists",
    "AtRimAssists",
    "ShortMidRangeAssists",
    "LongMidRangeAssists",
)


def _fixture(path):
    return json.loads(Path(f"tests/fixtures/{path}").read_text())


def _official(game_id=GAME_ID, **peterson):
    document = _fixture(f"nba_stats/boxscoretraditionalv3_{game_id}.json")
    for player in document["boxScoreTraditional"]["awayTeam"]["players"]:
        if player["personId"] == PETERSON:
            player["statistics"].update(peterson)
    return document


def _box_player(box, player_id):
    return next(
        player
        for side in ("homeTeam", "awayTeam")
        for player in box[side]["players"]
        if player["personId"] == player_id
    )


class _BoxScores:
    def __init__(self, document, *, seconds=0.0, ledger=None):
        self.document = document
        self.seconds = seconds
        self.ledger = ledger

    def fetch_game_box_score(self, game_id, *, timeout=None, finish_within=None):
        if self.ledger is not None:
            self.ledger.now += timedelta(seconds=self.seconds)
        if isinstance(self.document, Exception):
            raise self.document
        return parse_box_score_traditional(copy.deepcopy(self.document))


class _RenamedLiveData(_LiveData):
    """Serves the recorded LiveData documents under any requested game ID."""

    def fetch_game_stats(self, game_id, season, *, season_type="Regular Season"):
        document = super().fetch_game_stats(game_id, season, season_type=season_type)
        document["game"]["gameId"] = game_id
        return document


class _Ledger:
    def __init__(self, engine, box_scores, *, sink=None, season=SEASON, game_ids=(GAME_ID,), **options):
        self.engine = engine
        self.season = season
        self.game_ids = game_ids
        run_migrations(self.engine)
        with self.engine.begin() as connection:
            connection.execute(
                CollectionManifest.__table__.insert().values(
                    manifest_id="ledger-manifest",
                    season=season,
                    cutoff=CUTOFF,
                    collect_before=COLLECT_BEFORE,
                    accepted_versions="[1]",
                    scopes='["canonical_game_ledger"]',
                    checksum="ledger-manifest",
                    status="active",
                    created_at=CUTOFF,
                )
            )
        self.payload = {**_fixture(f"pbp_stats/game_stats_box_score_{GAME_ID}.json"), "season": season}
        participants = _participants(self.payload)
        self.repository = CanonicalGameLedgerRepository(self.engine, schema_drift_sink=record_schema_drift)
        self.box_scores = box_scores
        self.reports = []
        self.now = CUTOFF
        self.service = LedgerBackfillService(
            provider=_PBPStats(self.payload),
            live_data_provider=_RenamedLiveData(
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
            **options,
        )

    def _report(self, game_id, details):
        if details["kind"] == "box_score_differences":
            self.reports.append((game_id, details))

    def refresh(self):
        return self.service.refresh(
            self.season,
            cutoff=CUTOFF,
            governed_events=tuple(
                _event(self.season, {**self.payload, "stats_game_id": game_id}) for game_id in self.game_ids
            ),
            manifest_id="ledger-manifest",
            manifest_scope="canonical_game_ledger",
            collect_before=COLLECT_BEFORE,
            schema_version=1,
            accepted_versions=frozenset({1}),
        )

    def game(self, game_id=GAME_ID):
        return self.repository.get_game(game_id)

    def player(self, player_id, game_id=GAME_ID):
        return next(fact for fact in self.game(game_id).player_facts if fact.player_id == player_id)

    def team(self, team_id):
        return next(fact for fact in self.game().team_facts if fact.team_id == team_id)

    def row(self, player_id):
        return next(
            row
            for side in ("Home", "Away")
            for row in self.payload["stats"][side]["FullGame"]
            if str(row.get("EntityId")) == str(player_id)
        )


def _counts(fact):
    return {
        "PTS": fact.points,
        "REB": fact.rebounds,
        "AST": fact.assists,
        "FGM": fact.field_goals_made,
        "FGA": fact.field_goals_attempted,
        "FG3A": fact.three_pointers_attempted,
    }


def _rebound_types(fact):
    return (
        fact.def_two_pt_rebounds,
        fact.def_three_pt_rebounds,
        fact.ft_def_rebounds,
        fact.off_two_pt_rebounds,
        fact.off_three_pt_rebounds,
        fact.ft_off_rebounds,
    )


def _engine(tmp_path, name="box-score"):
    return create_engine(f"sqlite:///{tmp_path / f'{name}.sqlite3'}")


def _difference(player_id, player_name, stat, ledger_value, box_score_value, *, applied=True):
    return {
        "player_id": player_id,
        "player_name": player_name,
        "stat": stat,
        "ledger_value": ledger_value,
        "box_score_value": box_score_value,
        "applied": applied,
    }


def _controlled_ledger(engine, box_scores):
    run_migrations(engine)
    control = CollectionControlService(engine, clock=lambda: CUTOFF)
    ledger = _Ledger(
        engine,
        box_scores,
        sink=collection_control_reconciliation_sink(control, default_season=SEASON),
        open_box_score_differences=lambda season: len(control.open_box_score_differences(season=season)),
    )
    return ledger, control


def _listed(engine, capsys, *arguments):
    assert (
        ledger_box_score_differences.main(
            ["--database-url", engine.url.render_as_string(hide_password=False), *arguments]
        )
        == 0
    )
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


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


def test_a_season_before_2026_27_publishes_its_provider_counts_unchecked(tmp_path):
    ledger = _Ledger(_engine(tmp_path), _BoxScores(_official()), season="2025-26")

    result = ledger.refresh()

    assert result.complete
    assert _counts(ledger.player(PETERSON)) == {"PTS": 14, "REB": 4, "AST": 6, "FGM": 5, "FGA": 14, "FG3A": 5}
    assert (result.box_score_differences, result.unreconciled_game_ids) == (0, ())


def test_a_game_whose_box_score_cannot_be_fetched_is_accepted_and_counted_unreconciled(tmp_path):
    ledger = _Ledger(_engine(tmp_path), _BoxScores(ProviderResponseError("stats.nba.com timed out")))

    result = ledger.refresh()

    assert result.complete
    assert ledger.player(PETERSON).assists == 6
    assert (result.box_score_differences, result.unreconciled_game_ids) == (0, (GAME_ID,))
    assert ledger.reports == []


UNUSABLE_BOX_SCORES = {
    "a team's players are not a list of rows": lambda box: box.update(homeTeam=["malformed"]),
    "a player row is null": lambda box: box["awayTeam"]["players"].__setitem__(0, None),
    "a count is infinite": lambda box: _box_player(box, PETERSON)["statistics"].update(assists=float("inf")),
    "made shots exceed attempts": lambda box: _box_player(box, PETERSON)["statistics"].update(
        fieldGoalsAttempted=1
    ),
    "points disagree with the shots": lambda box: _box_player(box, PETERSON)["statistics"].update(points=15),
    "rebounds disagree with their split": lambda box: _box_player(box, PETERSON)["statistics"].update(
        reboundsTotal=5
    ),
    "a player the ledger has is missing": lambda box: box["awayTeam"].update(
        players=[player for player in box["awayTeam"]["players"] if player["personId"] != PETERSON]
    ),
    "a team is missing": lambda box: box.pop("awayTeam"),
    "a player who scored is not in the ledger": lambda box: box["awayTeam"]["players"].append(
        {**copy.deepcopy(_box_player(box, PETERSON)), "personId": 1999999}
    ),
}


@pytest.mark.parametrize("edit", UNUSABLE_BOX_SCORES.values(), ids=UNUSABLE_BOX_SCORES.keys())
def test_a_box_score_that_is_not_a_whole_reference_leaves_the_game_as_its_provider_has_it(tmp_path, edit):
    document = _official()
    edit(document["boxScoreTraditional"])
    ledger = _Ledger(_engine(tmp_path), _BoxScores(document))

    result = ledger.refresh()

    assert result.complete
    assert _counts(ledger.player(PETERSON)) == PROVIDER_PETERSON
    assert (result.box_score_differences, result.unreconciled_game_ids) == (0, (GAME_ID,))
    assert ledger.reports == []


def test_a_box_score_listing_players_who_did_not_play_is_still_a_reference(tmp_path):
    document = _official()
    document["boxScoreTraditional"]["awayTeam"]["players"].append(
        {
            **copy.deepcopy(_box_player(document["boxScoreTraditional"], PETERSON)),
            "personId": 1999999,
            "statistics": dict.fromkeys(
                _box_player(document["boxScoreTraditional"], PETERSON)["statistics"], 0
            ),
        }
    )
    ledger = _Ledger(_engine(tmp_path), _BoxScores(document))

    result = ledger.refresh()

    assert ledger.player(PETERSON).assists == 5
    assert (result.box_score_differences, result.unreconciled_game_ids) == (3, ())


def test_a_partial_box_score_resolves_no_open_difference(tmp_path, capsys):
    engine = _engine(tmp_path)
    box_scores = _BoxScores(_official())
    ledger, _control = _controlled_ledger(engine, box_scores)
    ledger.refresh()

    box_scores.document = _official()
    UNUSABLE_BOX_SCORES["a player the ledger has is missing"](box_scores.document["boxScoreTraditional"])
    ledger.now = CUTOFF + timedelta(hours=25)
    result = ledger.refresh()

    assert (result.box_score_differences, result.unreconciled_game_ids) == (0, (GAME_ID,))
    assert ledger.player(PETERSON).assists == 5
    assert [(item["player_name"], item["stat"]) for item in _listed(engine, capsys)] == [
        ("Brice Sensabaugh", "AST"),
        ("Brice Sensabaugh", "FG3A"),
        ("Darryn Peterson", "AST"),
    ]


def test_a_recheck_whose_rebounds_disagree_with_their_split_changes_and_resolves_nothing(tmp_path, capsys):
    engine = _engine(tmp_path)
    box_scores = _BoxScores(_official())
    ledger, _control = _controlled_ledger(engine, box_scores)
    ledger.refresh()
    stored, listed = ledger.game().checksum, _listed(engine, capsys)

    box_scores.document = _official(assists=6, reboundsTotal=5)
    ledger.now = CUTOFF + timedelta(hours=25)
    result = ledger.refresh()

    assert (result.games_replaced, result.box_score_differences, result.unreconciled_game_ids) == (
        0,
        0,
        (GAME_ID,),
    )
    assert (ledger.game().checksum, ledger.player(PETERSON).assists) == (stored, 5)
    assert [(item["player_name"], item["stat"]) for item in listed] == [
        ("Brice Sensabaugh", "AST"),
        ("Brice Sensabaugh", "FG3A"),
        ("Darryn Peterson", "AST"),
    ]
    assert _listed(engine, capsys) == listed


def test_the_ledger_requests_each_box_score_with_its_timeout(tmp_path, monkeypatch):
    from nba_api.stats.library.http import STATS_HEADERS, NBAStatsHTTP

    calls = []
    monkeypatch.setattr(NBAStatsHTTP, "send_api_request", _send_recording(calls))
    ledger = _Ledger(_engine(tmp_path), NBATrackingProvider(pace_seconds=0.6, sleep=lambda _seconds: None))

    result = ledger.refresh()

    assert calls == [("boxscoretraditionalv3", GAME_ID, STATS_HEADERS, 10.0)]
    assert (result.box_score_differences, ledger.player(PETERSON).assists) == (3, 5)


@pytest.mark.parametrize(
    ("seconds_left", "requested"),
    [(15.5, (GAME_ID, "0022600169")), (15.25, (GAME_ID,))],
    ids=["pacing, timeout and margin fit", "pacing does not fit"],
)
def test_a_box_score_check_starts_only_when_its_pacing_wait_and_timeout_end_before_the_deadline(
    tmp_path, monkeypatch, seconds_left, requested
):
    """The second game's check waits 0.5 s for pacing, may then take its 10 s timeout, and keeps a 5 s margin."""

    from nba_api.stats.library.http import NBAStatsHTTP

    second_game = "0022600169"
    ledger = _Ledger(_engine(tmp_path), None, game_ids=(GAME_ID, second_game))
    ledger.service.box_score_provider = NBATrackingProvider(
        pace_seconds=0.75,
        sleep=lambda seconds: setattr(ledger, "now", ledger.now + timedelta(seconds=seconds)),
        monotonic=lambda: (ledger.now - COLLECT_BEFORE).total_seconds(),
    )
    ledger.now = COLLECT_BEFORE - timedelta(seconds=seconds_left + 0.25)
    sent = []

    def send(_http, parameters, timeout=None, **_kwargs):
        sent.append(parameters["GameID"])
        if parameters["GameID"] == GAME_ID:
            ledger.now += timedelta(seconds=0.25)
            return type("Response", (), {"get_dict": staticmethod(_official)})()
        ledger.now += timedelta(seconds=timeout)
        raise requests.Timeout("stats.nba.com did not answer")

    monkeypatch.setattr(NBAStatsHTTP, "send_api_request", send)

    result = ledger.refresh()

    assert tuple(sent) == requested
    assert (result.complete, result.failed_game_ids, result.unreconciled_game_ids) == (
        True,
        (),
        (second_game,),
    )
    assert [ledger.player(PETERSON, game_id).assists for game_id in (GAME_ID, second_game)] == [5, 6]


@pytest.mark.parametrize("seconds_left", [60, 5])
def test_a_box_score_request_that_times_out_near_the_deadline_still_accepts_the_game(
    tmp_path, monkeypatch, seconds_left
):
    from nba_api.stats.library.http import NBAStatsHTTP

    ledger = _Ledger(_engine(tmp_path), NBATrackingProvider(pace_seconds=0.6, sleep=lambda _seconds: None))
    ledger.now = COLLECT_BEFORE - timedelta(seconds=seconds_left)

    def send(_http, timeout=None, **_kwargs):
        ledger.now += timedelta(seconds=timeout)
        raise requests.Timeout("stats.nba.com did not answer")

    monkeypatch.setattr(NBAStatsHTTP, "send_api_request", send)

    result = ledger.refresh()

    assert result.complete
    assert ledger.player(PETERSON).assists == 6
    assert result.unreconciled_game_ids == (GAME_ID,)


def test_once_the_runs_box_score_budget_is_spent_the_remaining_games_are_accepted_unreconciled(tmp_path):
    second_game = "0022600169"
    ledger = _Ledger(
        _engine(tmp_path),
        None,
        game_ids=(GAME_ID, second_game),
        box_score_budget_seconds=30,
    )
    ledger.service.box_score_provider = _BoxScores(_official(), seconds=40, ledger=ledger)

    result = ledger.refresh()

    assert result.complete
    assert result.box_score_differences == 3
    assert len(result.unreconciled_game_ids) == 1
    assert sorted(ledger.player(PETERSON, game_id).assists for game_id in (GAME_ID, second_game)) == [5, 6]


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


def test_a_rebound_difference_is_recorded_unapplied_and_the_providers_rebounds_stay_published(tmp_path):
    ledger = _Ledger(_engine(tmp_path), _BoxScores(_official(reboundsDefensive=5, reboundsTotal=5)))

    result = ledger.refresh()

    peterson = ledger.player(PETERSON)
    assert (peterson.rebounds, peterson.defensive_rebounds, peterson.assists) == (4, 4, 5)
    assert _rebound_types(peterson) == (2, 2, 0, 0, 0, 0)
    assert (ledger.team(UTA).rebounds, ledger.team(UTA).defensive_rebounds) == (56, 37)
    assert (result.box_score_differences, result.unreconciled_game_ids) == (5, ())
    ((_, report),) = ledger.reports
    assert [difference for difference in report["differences"] if difference["player_id"] == PETERSON] == [
        _difference(PETERSON, "Darryn Peterson", "REB", 4, 5, applied=False),
        _difference(PETERSON, "Darryn Peterson", "DREB", 4, 5, applied=False),
        _difference(PETERSON, "Darryn Peterson", "AST", 6, 5),
    ]


def test_a_corrected_assist_count_keeps_the_providers_assist_locations(tmp_path):
    ledger = _Ledger(_engine(tmp_path), _BoxScores(_official()))
    for wire in ASSIST_LOCATION_WIRES:
        ledger.row(SENSABAUGH).setdefault(wire, 0)

    ledger.refresh()

    peterson, sensabaugh = ledger.player(PETERSON), ledger.player(SENSABAUGH)
    assert (peterson.assists, sensabaugh.assists) == (5, 2)
    assert _assist_locations(peterson) == (2, 4, 4, None, 2, None, None)
    assert _assist_locations(sensabaugh) == (0, 1, 1, 0, 0, 0, 0)


def _assist_locations(fact):
    return (
        fact.two_point_assists,
        fact.three_point_assists,
        fact.arc3_assists,
        fact.corner3_assists,
        fact.at_rim_assists,
        fact.short_mid_range_assists,
        fact.long_mid_range_assists,
    )


def _relabelled(game, game_id, home_team_id, away_team_id):
    team = {game.home_team_id: home_team_id, game.away_team_id: away_team_id}
    return replace(
        game,
        game_id=game_id,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        team_facts=tuple(
            replace(fact, team_id=team[fact.team_id], opponent_team_id=team[fact.opponent_team_id])
            for fact in game.team_facts
        ),
        player_facts=tuple(replace(fact, team_id=team[fact.team_id]) for fact in game.player_facts),
        raw_rows=tuple(replace(row, game_id=game_id, team_id=team[row.team_id]) for row in game.raw_rows),
        period_minutes_by_team=tuple(
            (team[team_id], minutes) for team_id, minutes in game.period_minutes_by_team
        ),
    )


def _season_windows(game):
    """The assist-location and rebound-type Season windows of a league whose other 28 teams replay the game."""

    others = sorted(NBA_TEAM_IDS - {game.home_team_id, game.away_team_id})
    games = (
        game,
        *(
            _relabelled(game, f"00999{index:05d}", home, away)
            for index, (home, away) in enumerate(zip(others[::2], others[1::2], strict=True))
        ),
    )
    window = {
        "season": SEASON,
        "as_of": date(2026, 11, 3),
        "expected_game_ids": frozenset(item.game_id for item in games),
        "team_ids": NBA_TEAM_IDS,
    }
    return materialize_assist_location_window(games, **window), materialize_rebound_type_window(
        games, **window
    )


def _composed(ledger):
    """Every ledger stream of a league that replays the ledger's game for 15 rounds."""

    game = ledger.game()
    teams = sorted(NBA_TEAM_IDS)
    games = (
        game,
        *(
            replace(
                relabelled,
                source_observation_id=relabelled.game_id,
                participant_ids_by_team=tuple(
                    (home if team_id == game.home_team_id else away, players)
                    for team_id, players in game.participant_ids_by_team
                ),
            ).with_checksum()
            for round_number in range(15)
            for pair, (home, away) in enumerate(zip(teams[::2], teams[1::2], strict=True))
            for relabelled in (_relabelled(game, f"00998{round_number:02d}{pair:03d}", home, away),)
        ),
    )
    ledger.repository.replace_games_atomic(games[1:])
    return LedgerMaterializationService(
        ledger.repository,
        parity_repository=LedgerParityArtifactRepository(ledger.engine),
        parity_reader=None,
        clock=lambda: CUTOFF,
    ).compose(
        games,
        season=SEASON,
        as_of=date(2026, 11, 3),
        expected_game_ids=frozenset(item.game_id for item in games),
        expected_l15_game_ids={
            team_id: frozenset(
                sorted(
                    (item.game_id for item in games if team_id in {item.home_team_id, item.away_team_id}),
                    reverse=True,
                )[:15]
            )
            for team_id in NBA_TEAM_IDS
        },
        team_ids=NBA_TEAM_IDS,
        require_assist_locations=True,
    )


def _peterson_slices(rows):
    return {row["slice_key"]: row["volume"] for row in rows if row["player_id"] == PETERSON}


PETERSON_ASSIST_SLICES = {
    "Arc3Assists": 4.0,
    "Corner3Assists": 0.0,
    "AtRimAssists": 2.0,
    "ShortMidRangeAssists": 0.0,
    "LongMidRangeAssists": 0.0,
}
PETERSON_REBOUND_SLICES = {
    "DefTwoPtRebounds": 2.0,
    "DefThreePtRebounds": 2.0,
    "FTDefRebounds": 0.0,
    "OffTwoPtRebounds": 0.0,
    "OffThreePtRebounds": 0.0,
    "FTOffRebounds": 0.0,
}


def test_a_corrected_game_keeps_the_assist_location_and_rebound_type_bases_available(tmp_path):
    corrected = _Ledger(_engine(tmp_path), _BoxScores(_official(reboundsDefensive=5, reboundsTotal=5)))
    provider = _Ledger(
        _engine(tmp_path, "provider"), _BoxScores(ProviderResponseError("stats.nba.com timed out"))
    )
    corrected.refresh()
    provider.refresh()
    assert (corrected.player(PETERSON).assists, provider.player(PETERSON).assists) == (5, 6)

    assists, rebounds = _season_windows(corrected.game())
    provider_assists, provider_rebounds = _season_windows(provider.game())
    facts = derive_assist_location_facts((corrected.game(),))
    assist_diet = derive_player_assist_diet_rows((corrected.game(),))
    rebound_diet = derive_player_rebound_type_diet_rows((corrected.game(),))
    composed = _composed(corrected)

    assert (assists.complete, rebounds.complete) == (True, True)
    assert [team.counts for team in assists.teams] == [team.counts for team in provider_assists.teams]
    assert [team.counts for team in rebounds.teams] == [team.counts for team in provider_rebounds.teams]
    assert [(fact.assists, *_assist_locations(fact)) for fact in facts if fact.player_id == PETERSON] == [
        (6, 2, 4, 4, 0, 2, 0, 0)
    ]
    assert _peterson_slices(assist_diet) == PETERSON_ASSIST_SLICES
    assert _peterson_slices(rebound_diet) == PETERSON_REBOUND_SLICES
    assert facts == derive_assist_location_facts((provider.game(),))
    assert assist_diet == derive_player_assist_diet_rows((provider.game(),))
    assert rebound_diet == derive_player_rebound_type_diet_rows((provider.game(),))
    assert [
        window.complete
        for window in (
            composed.season_window,
            composed.l15_window,
            composed.assist_location_season,
            composed.assist_location_l15,
            composed.rebound_type_season,
            composed.rebound_type_l15,
        )
    ] == [True] * 6
    assert _peterson_slices(composed.player_assist_diet["rows"]) == {
        "Arc3Assists": 904.0,
        "Corner3Assists": 0.0,
        "AtRimAssists": 452.0,
        "ShortMidRangeAssists": 0.0,
        "LongMidRangeAssists": 0.0,
    }
    assert _peterson_slices(composed.player_rebound_diet["rows"]) == {
        "DefTwoPtRebounds": 452.0,
        "DefThreePtRebounds": 452.0,
        "FTDefRebounds": 0.0,
        "OffTwoPtRebounds": 0.0,
        "OffThreePtRebounds": 0.0,
        "FTOffRebounds": 0.0,
    }


NO_REFERENCE_RECHECKS = {
    "the request fails": lambda ledger, box_scores: setattr(
        box_scores, "document", ProviderResponseError("stats.nba.com timed out")
    ),
    "the document leaves out a player": lambda ledger, box_scores: UNUSABLE_BOX_SCORES[
        "a player the ledger has is missing"
    ](box_scores.document["boxScoreTraditional"]),
    "the run's budget is spent": lambda ledger, box_scores: setattr(
        ledger.service, "box_score_budget_seconds", 0
    ),
}


@pytest.mark.parametrize("lose_reference", NO_REFERENCE_RECHECKS.values(), ids=NO_REFERENCE_RECHECKS.keys())
def test_a_recheck_without_a_box_score_keeps_the_corrected_game(tmp_path, lose_reference):
    box_scores = _BoxScores(_official())
    ledger = _Ledger(_engine(tmp_path), box_scores)
    ledger.refresh()
    stored = ledger.game().checksum

    box_scores.document = _official()
    lose_reference(ledger, box_scores)
    ledger.now = CUTOFF + timedelta(hours=25)
    result = ledger.refresh()

    assert (result.complete, result.games_replaced, result.unreconciled_game_ids) == (True, 0, (GAME_ID,))
    assert (ledger.game().checksum, ledger.player(PETERSON).assists) == (stored, 5)


def test_a_recheck_without_a_box_score_still_takes_the_providers_update_to_an_uncorrected_game(tmp_path):
    ledger = _Ledger(_engine(tmp_path), _BoxScores(ProviderResponseError("stats.nba.com timed out")))
    ledger.refresh()

    ledger.row(PETERSON)["Usage"] = 31.5
    ledger.now = CUTOFF + timedelta(hours=25)
    result = ledger.refresh()

    assert (result.games_replaced, result.unreconciled_game_ids) == (1, (GAME_ID,))
    assert (ledger.player(PETERSON).usage_pct, ledger.player(PETERSON).assists) == (31.5, 6)


class _UnavailablePBPStats:
    def fetch_game_stats(self, game_id, season, *, season_type="Regular Season"):
        raise ProviderResponseError("PBP Stats is unavailable")


def _open_fallback_items(engine):
    with engine.connect() as connection:
        return connection.execute(
            select(ReconciliationItem.kind).where(
                ReconciliationItem.kind == "ledger_fallback_evidence", ReconciliationItem.status == "open"
            )
        ).all()


def test_a_corrected_pbp_replacement_of_a_fallback_game_resolves_its_fallback_item(tmp_path):
    engine = _engine(tmp_path)
    ledger, _control = _controlled_ledger(engine, _BoxScores(_official()))
    pbp_stats = ledger.service.provider
    ledger.service.provider = _UnavailablePBPStats()
    ledger.refresh()
    assert len(_open_fallback_items(engine)) == 1

    ledger.service.provider = pbp_stats
    ledger.now = CUTOFF + timedelta(hours=25)
    result = ledger.refresh()

    assert (result.games_replaced, ledger.player(PETERSON).assists) == (1, 5)
    assert _open_fallback_items(engine) == []


def test_a_correction_raises_no_schema_drift_alert(tmp_path):
    engine = _engine(tmp_path)
    box_scores = _BoxScores(_official(assists=6))
    ledger = _Ledger(engine, box_scores)
    ledger.refresh()

    box_scores.document = _official(reboundsDefensive=5, reboundsTotal=5)
    _box_player(box_scores.document["boxScoreTraditional"], SENSABAUGH)["statistics"]["blocks"] = 1
    ledger.now = CUTOFF + timedelta(hours=25)
    corrected = ledger.refresh()
    assert (ledger.player(SENSABAUGH).blocks, ledger.player(PETERSON).assists) == (1, 5)
    box_scores.document = _official(assists=6)
    ledger.now = CUTOFF + timedelta(hours=50)
    agreed = ledger.refresh()

    assert (corrected.games_replaced, agreed.games_replaced) == (1, 1)
    assert (ledger.player(SENSABAUGH).blocks, ledger.player(PETERSON).assists) == (0, 6)
    with engine.connect() as connection:
        assert connection.execute(select(ReconciliationItem.kind)).scalars().all() == []


def test_the_command_lists_open_differences_until_the_provider_agrees(tmp_path, capsys):
    assert_the_command_lists_open_differences_until_the_provider_agrees(_engine(tmp_path), capsys)


def assert_the_command_lists_open_differences_until_the_provider_agrees(engine, capsys):
    box_scores = _BoxScores(_official())
    ledger, _control = _controlled_ledger(engine, box_scores)

    first = ledger.refresh()
    again = ledger.refresh()

    listed = _listed(engine, capsys)
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
    assert (first.box_score_differences, first.open_box_score_differences) == (3, 3)
    assert (again.games_processed, again.box_score_differences, again.open_box_score_differences) == (0, 0, 3)

    box_scores.document = _official(assists=6)
    ledger.now = CUTOFF + timedelta(hours=25)
    result = ledger.refresh()
    listed = _listed(engine, capsys, "--season", SEASON)
    assert [(item["player_name"], item["stat"]) for item in listed] == [
        ("Brice Sensabaugh", "AST"),
        ("Brice Sensabaugh", "FG3A"),
    ]
    assert (result.box_score_differences, result.open_box_score_differences) == (2, 2)


def test_the_listing_and_the_run_summary_leave_out_other_seasons(tmp_path, capsys):
    engine = _engine(tmp_path)
    ledger, control = _controlled_ledger(engine, _BoxScores(_official()))
    control.reconcile_box_score_differences(
        season="2027-28",
        game_id="0022700001",
        differences=(_difference(PETERSON, "Darryn Peterson", "PTS", 20, 21),),
    )

    result = ledger.refresh()

    assert result.open_box_score_differences == 3
    assert [(item["season"], item["stat"]) for item in _listed(engine, capsys, "--season", SEASON)] == [
        (SEASON, "AST"),
        (SEASON, "FG3A"),
        (SEASON, "AST"),
    ]
    assert [(item["season"], item["stat"]) for item in _listed(engine, capsys, "--season", "2027-28")] == [
        ("2027-28", "PTS")
    ]


def _send_recording(calls, now=None, gates=None):
    def send(_http, endpoint, parameters, headers=None, timeout=None, **_kwargs):
        calls.append((endpoint, parameters["GameID"], headers, timeout))
        if gates is not None:
            gates(parameters["GameID"])
        if now is not None:
            now[0] += 0.25
        return type("Response", (), {"get_dict": staticmethod(lambda: _official(parameters["GameID"]))})()

    return send


def test_the_provider_fetches_one_paced_box_score_per_game_with_the_standard_headers(monkeypatch):
    from nba_api.stats.library.http import STATS_HEADERS, NBAStatsHTTP

    calls, sleeps, now = [], [], [100.0]
    monkeypatch.setattr(NBAStatsHTTP, "send_api_request", _send_recording(calls, now))
    provider = NBATrackingProvider(pace_seconds=0.6, sleep=sleeps.append, monotonic=lambda: now[0])

    first = provider.fetch_game_box_score(GAME_ID, timeout=10, finish_within=60)
    second = provider.fetch_game_box_score("0022600028", timeout=10, finish_within=60)

    assert (first[PETERSON]["AST"], second[BAILEY]["REB"]) == (5, 6)
    assert calls == [
        ("boxscoretraditionalv3", GAME_ID, STATS_HEADERS, 10),
        ("boxscoretraditionalv3", "0022600028", STATS_HEADERS, 10),
    ]
    assert sleeps == [pytest.approx(0.35)]


def test_a_box_score_request_is_made_once_and_never_retried(monkeypatch):
    from nba_api.stats.library.http import NBAStatsHTTP

    attempts = []

    def send(_http, timeout=None, **_kwargs):
        attempts.append(timeout)
        raise requests.Timeout("stats.nba.com did not answer")

    monkeypatch.setattr(NBAStatsHTTP, "send_api_request", send)
    provider = NBATrackingProvider(pace_seconds=0.6, sleep=lambda _seconds: None)

    with pytest.raises(requests.Timeout):
        provider.fetch_game_box_score(GAME_ID, timeout=10, finish_within=60)
    assert attempts == [10]


def test_a_slow_box_score_request_does_not_hold_up_the_next_games_request(monkeypatch):
    from nba_api.stats.library.http import NBAStatsHTTP

    first_started, second_started = threading.Event(), threading.Event()
    failures = []

    def gates(game_id):
        if game_id == GAME_ID:
            first_started.set()
            if not second_started.wait(2):
                failures.append("the second request waited for the first to finish")
        else:
            second_started.set()

    monkeypatch.setattr(NBAStatsHTTP, "send_api_request", _send_recording([], gates=gates))
    provider = NBATrackingProvider(pace_seconds=0.6, sleep=lambda _seconds: None)
    first = threading.Thread(
        target=provider.fetch_game_box_score, args=(GAME_ID,), kwargs={"timeout": 10, "finish_within": 60}
    )
    first.start()
    assert first_started.wait(2)

    provider.fetch_game_box_score("0022600028", timeout=10, finish_within=60)
    first.join()

    assert failures == []
