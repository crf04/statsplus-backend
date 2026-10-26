"""NBA.com's 2026-27 box scores leave end-of-period heaves out of attempts."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine

from app.migrations import run_migrations
from app.models.collection_control import CollectionManifest
from app.services.canonical_game_ledger import CanonicalGameLedgerRepository
from app.services.ledger_backfill import LedgerBackfillService
from app.utils.telemetry import ProviderResponseError
from tests.services.test_ledger_backfill import _Recorder

GAME_ID = "0022600099"
CHI, MIL = 1610612741, 1610612749
GIDDEY, JAKUCIONIS = 1630581, 1642857
DONCIC, MAXEY = 1629029, 1630178
CUTOFF = datetime(2026, 10, 24, 12, tzinfo=UTC)


def _pbp_stats(game_id=GAME_ID):
    return json.loads(Path(f"tests/fixtures/pbp_stats/game_stats_heaves_{game_id}.json").read_text())


def _play_by_play(game_id=GAME_ID):
    return json.loads(Path(f"tests/fixtures/nba_live_data/playbyplay_heaves_{game_id}.json").read_text())


def _box_score(game_id=GAME_ID):
    return json.loads(Path(f"tests/fixtures/nba_live_data/boxscore_heaves_{game_id}.json").read_text())


def _participants(payload):
    teams = {"Home": int(payload["home_team_id"]), "Away": int(payload["away_team_id"])}
    return {
        teams[side]: tuple(
            int(row["EntityId"]) for row in payload["stats"][side]["FullGame"] if str(row["EntityId"]) != "0"
        )
        for side in teams
    }


class _Athletes:
    def __init__(self, player_ids):
        self.player_ids = player_ids

    def get_catalog(self, season, *, active_only=False):
        return [{"player_id": player_id} for player_id in self.player_ids]

    def get_freshness(self, season, *, now):
        return {"is_fresh": True, "row_count": len(self.player_ids)}


class _Participants:
    def __init__(self, participants):
        self.participants = participants

    def get_participants(self, observation_id, *, game_id):
        return self.participants


class _PBPStats:
    def __init__(self, payload):
        self.payload = payload

    def fetch_game_stats(self, game_id, season, *, season_type="Regular Season"):
        return json.loads(json.dumps(self.payload))


class _LiveData:
    def __init__(self, play_by_play, box_score):
        self.play_by_play = play_by_play
        self.box_score = box_score

    def fetch_game_stats(self, game_id, season, *, season_type="Regular Season"):
        return self._serve(self.box_score)

    def fetch_play_by_play(self, game_id):
        return self._serve(self.play_by_play)

    @staticmethod
    def _serve(payload):
        if isinstance(payload, Exception):
            raise payload
        return json.loads(json.dumps(payload))


def _event(season, payload):
    return {
        "nba_game_id": payload["stats_game_id"],
        "season": season,
        "classification": "Regular Season",
        "scheduled_at": f"{payload['date']}T00:00:00+00:00",
        "home_team_id": int(payload["home_team_id"]),
        "home_team_tricode": payload["home_team_abbreviation"],
        "away_team_id": int(payload["away_team_id"]),
        "away_team_tricode": payload["away_team_abbreviation"],
        "status_code": 3,
        "status_text": "Final",
    }


def _refresh(
    tmp_path, *, game_id=GAME_ID, season="2026-27", play_by_play=None, pbp_stats=None, box_score=None
):
    tmp_path.mkdir(exist_ok=True)
    engine = create_engine(f"sqlite:///{tmp_path / 'heaves.sqlite3'}")
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(
            CollectionManifest.__table__.insert().values(
                manifest_id="ledger-manifest",
                season=season,
                cutoff=CUTOFF,
                collect_before=CUTOFF + timedelta(days=45),
                accepted_versions="[1]",
                scopes='["canonical_game_ledger"]',
                checksum="ledger-manifest",
                status="active",
                created_at=CUTOFF,
            )
        )
    payload = pbp_stats or _pbp_stats(game_id)
    participants = _participants(payload)
    repository = CanonicalGameLedgerRepository(engine)
    service = LedgerBackfillService(
        provider=_PBPStats(payload),
        live_data_provider=_LiveData(
            _play_by_play(game_id) if play_by_play is None else play_by_play,
            _box_score(game_id) if box_score is None else box_score,
        ),
        athlete_catalog=_Athletes([player for players in participants.values() for player in players]),
        participant_catalog=_Participants(participants),
        reconciliation_sink=lambda game_id, details: None,
        observation_recorder=_Recorder(),
        repository=repository,
        max_concurrency=1,
        clock=lambda: CUTOFF,
    )
    result = service.refresh(
        season,
        cutoff=CUTOFF,
        governed_events=(_event(season, {**_pbp_stats(game_id), "stats_game_id": game_id}),),
        manifest_id="ledger-manifest",
        manifest_scope="canonical_game_ledger",
        collect_before=CUTOFF + timedelta(days=45),
        schema_version=1,
        accepted_versions=frozenset({1}),
    )
    return result, repository.get_game(game_id)


def _shooting(fact):
    return (
        fact.field_goals_made,
        fact.field_goals_attempted,
        fact.three_pointers_made,
        fact.three_pointers_attempted,
        fact.points,
    )


def test_2026_27_ledger_attempts_match_the_nba_box_score_without_heaves(tmp_path):
    result, game = _refresh(tmp_path)

    assert result.complete
    players = {fact.player_id: _shooting(fact) for fact in game.player_facts}
    teams = {fact.team_id: _shooting(fact) for fact in game.team_facts}
    assert players[GIDDEY] == (3, 9, 0, 2, 6)
    assert players[JAKUCIONIS] == (2, 4, 1, 2, 7)
    assert teams[CHI][1:4:2] == (98, 45)
    assert teams[MIL][1:4:2] == (92, 46)


def test_2026_27_heaves_are_excluded_when_pbp_stats_labels_the_sides_backwards(tmp_path):
    payload = _pbp_stats()
    reversed_labels = json.loads(json.dumps(payload))
    for home_key, away_key in (
        ("home_team_id", "away_team_id"),
        ("home_team_abbreviation", "away_team_abbreviation"),
    ):
        reversed_labels[home_key], reversed_labels[away_key] = payload[away_key], payload[home_key]
    for envelope in ("stats", "team_results"):
        reversed_labels[envelope] = {"Home": payload[envelope]["Away"], "Away": payload[envelope]["Home"]}

    result, game = _refresh(tmp_path, pbp_stats=reversed_labels)

    assert result.complete
    teams = {fact.team_id: _shooting(fact) for fact in game.team_facts}
    assert teams[MIL][1:4:2] == (92, 46)
    assert teams[CHI][1:4:2] == (98, 45)
    giddey = next(fact for fact in game.player_facts if fact.player_id == GIDDEY)
    assert giddey.def_three_pt_rebounds == 3


def test_2026_27_game_is_not_accepted_on_pbp_stats_attempts_when_heaves_are_unknown(tmp_path):
    result, game = _refresh(
        tmp_path,
        play_by_play=ProviderResponseError("play-by-play unavailable"),
        box_score=ProviderResponseError("box score unavailable"),
    )

    assert result.failed_game_ids == (GAME_ID,)
    assert game is None


def test_2026_27_unfinished_play_by_play_leaves_the_game_to_the_nba_box_score(tmp_path):
    unfinished = _play_by_play()
    unfinished["game"]["actions"] = [
        action for action in unfinished["game"]["actions"] if action["period"] == 1
    ]

    result, game = _refresh(tmp_path, play_by_play=unfinished)

    assert result.complete
    players = {fact.player_id: _shooting(fact) for fact in game.player_facts}
    assert players[GIDDEY] == (3, 9, 0, 2, 6)
    assert players[JAKUCIONIS] == (2, 4, 1, 2, 7)


def test_2026_27_already_corrected_attempts_are_not_reduced_again(tmp_path):
    corrected = _pbp_stats()
    for side, player_id in (("Away", GIDDEY), ("Home", JAKUCIONIS)):
        player = next(
            row for row in corrected["stats"][side]["FullGame"] if int(row["EntityId"]) == player_id
        )
        for row in (player, corrected["team_results"][side]["FullGame"]):
            row["FG3A"] -= 1
            if "FGA" in row:
                row["FGA"] -= 1

    result, game = _refresh(tmp_path, pbp_stats=corrected)

    assert result.complete
    players = {fact.player_id: _shooting(fact) for fact in game.player_facts}
    assert players[GIDDEY] == (3, 9, 0, 2, 6)
    assert players[JAKUCIONIS] == (2, 4, 1, 2, 7)


def test_2026_27_game_with_heaves_is_not_accepted_without_the_nba_box_score(tmp_path):
    result, game = _refresh(tmp_path, box_score=ProviderResponseError("box score unavailable"))

    assert result.failed_game_ids == (GAME_ID,)
    assert game is None


def test_2026_27_made_long_threes_stay_in_attempts_with_the_pbp_stats_rebound_types(tmp_path):
    _, doncic_game = _refresh(tmp_path / "doncic", game_id="0022600005")
    _, maxey_game = _refresh(tmp_path / "maxey", game_id="0022600106")

    doncic = next(fact for fact in doncic_game.player_facts if fact.player_id == DONCIC)
    maxey = next(fact for fact in maxey_game.player_facts if fact.player_id == MAXEY)
    assert (doncic.field_goals_attempted, doncic.three_pointers_attempted, doncic.def_three_pt_rebounds) == (
        18,
        12,
        4,
    )
    assert (maxey.field_goals_attempted, maxey.three_pointers_attempted, maxey.def_three_pt_rebounds) == (
        22,
        9,
        3,
    )


def test_2025_26_attempts_keep_the_pbp_stats_definition(tmp_path):
    pbp_stats = _pbp_stats()
    pbp_stats["season"] = "2025-26"

    result, game = _refresh(tmp_path, season="2025-26", pbp_stats=pbp_stats)

    assert result.complete
    players = {fact.player_id: _shooting(fact) for fact in game.player_facts}
    assert players[GIDDEY] == (3, 10, 0, 3, 6)
    assert players[JAKUCIONIS] == (2, 5, 1, 3, 7)
