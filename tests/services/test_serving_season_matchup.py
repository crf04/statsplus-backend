"""A Matchup across an opening-week Season Cutover (crf04/statsplus-backend#547).

One real control plane (``tests/support/season_cutover``) publishes 2025-26
and collects 2026-27.  The game logs cut over in the opening week, when every
team has played one game; team defense and the player's Diet are not ready
and keep serving 2025-26.  The Matchup is composed by the real
``MatchupService`` over the real Publication reader, game-log and Diet
repositories and team-matchup query, and only its response is asserted.

The scored player is ATL's ``Player 0`` (id 1000) against BOS.  BOS allows
more steals per 48 than the rest of the league in each season's team-defense
Publication, and the player's steal and block rates differ by season, so a
Score names which season each of its inputs came from.
"""

from __future__ import annotations

import math
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.config.settings import MatchupScoreSettings, NBASeasonSettings, RuntimeSettings
from app.domain.publication_integrity import canonical_publication_json
from app.domain.team_matchup_taxonomy import SHOT_TYPE_SLICES
from app.services.collection_control import CollectionOperationsService
from app.services.database_first_activation import DatabaseFirstPublicationReader
from app.services.ledger_runtime import ActiveManifestLedgerGovernanceReader
from app.services.matchup import MatchupService
from app.services.player_diet import PlayerDietRepository
from app.services.player_game_log_repository import PlayerGameLogRepository
from app.services.player_pool import PlayerPool, PoolPlayer
from app.services.statistic_catalog import StatisticCatalog
from app.services.stats_freshness_repository import StatsFreshness
from app.services.team_matchup_query import TeamMatchupQueryService
from app.services.team_matchup_repository import TeamMatchupRepository
from tests.support.season_cutover import (
    L15_STREAM,
    NEW,
    NEW_CUTOFF,
    NEW_GAMES,
    NEW_PROVENANCE,
    NOW,
    OLD,
    OLD_CUTOFF,
    OLD_GAMES,
    OLD_PROVENANCE,
    PLAYER_LOGS,
    SEASON_STREAM,
    TEAM_IDS,
    THIN_CUTOFF,
    THIN_GAMES,
    THIN_PROVENANCE,
    TRICODES,
    accept_ledger,
    compose_thin,
    log_rows,
    open_opening_week,
    pointers,
    seed_world,
)
from tests.support.traditional_opponent_family import payload as team_window_payload

ACTOR = "operator@example.com"
PLAYER_ID = 1000
ATL, BOS = TEAM_IDS[0], TEAM_IDS[1]
DIET_STREAM = "grouped_shot_types"
#: The player's own box score each season: the Score's STL/BLK weights.
PLAYER_LINE = {
    OLD: {"points": 30, "steals": 1, "blocks": 2},
    NEW: {"points": 12, "steals": 3, "blocks": 1},
}
#: Steals BOS allows per 48 in each season's team-defense Publication; every
#: other team allows 7, so the league mean is 8 (2025-26) and 9 (2026-27).
BOS_STEALS_ALLOWED = {OLD: 37.0, NEW: 67.0}
OTHER_STEALS_ALLOWED = 7.0

TRAILING_DIETS = {
    "family": "player_diets",
    "serving_season": OLD,
    "rule": "every team has played 10 games",
}
TRAILING_TEAM_DEFENSE = {
    "family": "team_defense",
    "serving_season": OLD,
    "rule": "every team has played 10 games",
}


def _log_payload(season, games):
    rows = log_rows(season, games)
    for row in rows:
        if row["player_id"] == PLAYER_ID:
            row.update(PLAYER_LINE[season])
    return {"rows": rows}


def _defense_payload(season, games):
    """Thirty governed rows; BOS allows ``BOS_STEALS_ALLOWED[season]``.

    The league blocks are the population's own mean, population sigma and
    ascending competition rank, which the publication proves on read.
    """

    by_team = {}
    for game_id, home, away in games:
        by_team.setdefault(int(home), []).append(game_id)
        by_team.setdefault(int(away), []).append(game_id)
    steals = {
        team_id: BOS_STEALS_ALLOWED[season] if team_id == BOS else OTHER_STEALS_ALLOWED
        for team_id in TEAM_IDS
    }
    mean = sum(steals.values()) / len(steals)
    sigma = math.sqrt(sum((value - mean) ** 2 for value in steals.values()) / len(steals))

    def mutate(rows):
        for row in rows:
            team_id = row["team_id"]
            row["game_ids"] = sorted(by_team[team_id])
            row["game_count"] = len(row["game_ids"])
            row["counts"]["steals"] = steals[team_id]
            row["per48"]["steals"] = steals[team_id]
            row["league_average"]["steals"] = mean
            row["population_sigma"]["steals"] = sigma
            row["competition_rank"]["steals"] = 30 if team_id == BOS else 1

    return team_window_payload(mutate=mutate)


def _diet_payload():
    """The 2025-26 shot-type Diet: the player takes 0.4 of his shots in the
    first type, one other player 0.2, so its league baseline is 0.3."""

    rest = round(0.6 / (len(SHOT_TYPE_SLICES) - 1), 6)
    return {
        "base": "shot_types",
        "rows": [
            {
                "player_id": player_id,
                "slice_key": slice_key,
                "share": (0.4 if player_id == PLAYER_ID else 0.2) if index == 0 else rest,
                "volume": 100.0,
                "games_played": 10,
                "volume_unit": "field_goal_attempts",
                "provider": "nba_stats",
            }
            for player_id in (PLAYER_ID, PLAYER_ID + 1)
            for index, slice_key in enumerate(SHOT_TYPE_SLICES)
        ],
    }


class _Catalog:
    """The athlete catalog an Unscheduled Matchup names its player from."""

    def get_catalog(self, season, *, active_only=False):
        return [
            {
                "season": season,
                "player_id": PLAYER_ID,
                "display_name": "Player 0",
                "roster_status": "active",
                "is_active": True,
                "team_id": ATL,
                "team_abbreviation": "ATL",
            }
        ]


#: A 2026-27 ATL @ BOS game the day after ``NOW``, the player on its pool.
GAME_ID = "0022600160"
GAME_TIP = "2026-10-26T23:30:00+00:00"
SLATE_DATE = "2026-10-26"


def _game_event():
    event = {
        "nba_game_id": GAME_ID,
        "season": NEW,
        "scheduled_at": GAME_TIP,
        "status_text": "7:30 pm ET",
        "status_code": 1,
        "postponed_status": None,
        "postponement_evidence": None,
        "classification": "Regular Season",
    }
    for side, team_id in (("away", ATL), ("home", BOS)):
        team = {"id": team_id, "name": f"{TRICODES[team_id]} team", "tricode": TRICODES[team_id]}
        event[f"{side}_team"] = team
        event.update({f"{side}_team_{field}": value for field, value in team.items()})
    return event


class _Schedule:
    """The 2026-27 Event Catalog a game Matchup reads its one game from."""

    def get_event(self, season, game_id):
        return _game_event() if (season, game_id) == (NEW, GAME_ID) else None

    def get_freshness(self, season, *, now):
        return {"last_success_at": NOW.isoformat(), "fresh": True}


class _Pool:
    def get_pool_for_game(self, *, season, game_id):
        return PlayerPool(
            players=(PoolPlayer(PLAYER_ID, "Player 0", ATL, ("STKS",), {"prizepicks": ("STKS",)}),),
            team_counts={ATL: 1},
            freshness={"status": "fresh", "retrieved_at": NOW.isoformat(), "providers": {}},
        )


class World:
    """2025-26 published with varied team defense and one player Diet."""

    def __init__(self, tmp_path, *, engine=None):
        self.engine, self.publications = seed_world(tmp_path, engine=engine)
        self.operations = CollectionOperationsService(
            self.engine, publication_service=self.publications, clock=lambda: NOW
        )
        self.publications.recompose_ledger(
            PLAYER_LOGS,
            season=OLD,
            cutoff=OLD_CUTOFF,
            payload=_log_payload(OLD, OLD_GAMES),
            provenance=OLD_PROVENANCE,
            reason="2025-26 ledger composition",
        )
        self.publications.recompose_ledger(
            SEASON_STREAM,
            season=OLD,
            cutoff=OLD_CUTOFF,
            payload=_defense_payload(OLD, OLD_GAMES),
            provenance=OLD_PROVENANCE,
            reason="2025-26 ledger composition",
        )
        self.publications.register_stream(
            DIET_STREAM,
            provider="nba_stats",
            owner="railway",
            required_observations=(),
            publication_strategy="replace",
            enabled=True,
            freshness_rule="cutoff_current",
        )
        self.diet = self._compose_diet()
        reader = DatabaseFirstPublicationReader(self.engine, clock=lambda: NOW)
        self.service = MatchupService(
            event_catalog=_Schedule(),
            player_pool=_Pool(),
            player_logs=PlayerGameLogRepository(
                self.engine,
                statistic_catalog=StatisticCatalog.load_default(),
                stats_surface_season=NEW,
                clock=lambda: NOW,
                stats_surface_max_age=timedelta(hours=30),
                publication_reader=reader,
            ),
            player_diets=PlayerDietRepository(self.engine, publication_reader=reader),
            team_matchups=TeamMatchupQueryService(
                TeamMatchupRepository(self.engine),
                clock=lambda: NOW,
                publication_reader=reader,
                l15_expectation_resolver=ActiveManifestLedgerGovernanceReader(self.engine, clock=lambda: NOW),
            ),
            # No stats refresh has run, so no event catalog is consulted.
            stats_freshness=SimpleNamespace(get=lambda: StatsFreshness(None)),
            settings=RuntimeSettings(
                environment="testing",
                # No NBA_CURRENT_SEASON pin: the game-log pointer decides.
                nba=NBASeasonSettings.model_construct(_fields_set=set(), current_season=NEW),
                matchup_scores=MatchupScoreSettings(),
            ),
            statistic_catalog=StatisticCatalog.load_default(),
            clock=lambda: NOW,
            database_only=True,
            publication_reader=reader,
            engine=self.engine,
            athlete_catalog=_Catalog(),
        )

    def _compose_diet(self):
        """Activate the 2025-26 Diet through the pointer-advancing write.

        A Diet's public compose also demands the collection manifest and
        observations that authorized it; the Readiness Rule is not about
        those, so the payload takes the write the public compose ends in.
        """

        payload = _diet_payload()
        with Session(self.engine) as session, session.begin():
            version = self.publications._compose_active_in_session(
                session,
                stream_key=DIET_STREAM,
                season=OLD,
                cutoff=OLD_CUTOFF,
                encoded=canonical_publication_json(payload),
                payload=payload,
                expected_fence=None,
                reason=None,
                provenance_ids=set(),
                now=NOW,
                derive_expected_fence_from_lock=True,
            )
            return version.publication_id

    def opening_week_cutover(self):
        """Cut over with one game a team: the game logs move, the rest trail."""

        open_opening_week(self.engine)
        self.publications.recompose_ledger(
            PLAYER_LOGS,
            season=NEW,
            cutoff=THIN_CUTOFF,
            payload=_log_payload(NEW, THIN_GAMES),
            provenance=THIN_PROVENANCE,
            reason="2026-27 ledger composition",
        )
        compose_thin(self.publications, SEASON_STREAM)
        plan = self.operations.cutover_season(NEW, actor=ACTOR, reason="open 2026-27", dry_run=True)
        return self.operations.cutover_season(
            NEW,
            actor=ACTOR,
            reason="open 2026-27",
            leave_behind=[item["stream_key"] for item in plan.to_dict()["leave_behind"]],
        )

    def compose_defense(self, games, *, cutoff, provenance):
        return self.publications.recompose_ledger(
            SEASON_STREAM,
            season=NEW,
            cutoff=cutoff,
            payload=_defense_payload(NEW, games),
            provenance=provenance,
            reason="2026-27 ledger composition",
        )

    def matchup(self):
        return self.service.get_unscheduled_matchup(opponent="BOS", player_id=PLAYER_ID)

    def game_matchup(self):
        return self.service.get_matchup(game_id=GAME_ID)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def _generation(matchup, *stream_keys):
    """``{stream_key: (status, season, publication_id)}`` for the named streams."""

    return {
        entry["stream_key"]: (entry["status"], entry["season"], entry["publication_id"])
        for entry in matchup["provenance"]["generation"]
        if entry["stream_key"] in stream_keys
    }


def _score(matchup, market):
    return matchup["players"][0]["scores"][market]["season"]["components"]["traditional"]["value"]


def _bos_steals(matchup):
    """BOS's OPP_STL cell and the league mean, as the Matchup serves them."""

    [bos] = matchup["teams"]
    [column] = [row for row in bos["defense_sheet"]["traditional"] if row["key"] == "OPP_STL"]
    [league] = [row for row in matchup["league"]["defense_sheet"]["traditional"] if row["key"] == "OPP_STL"]
    return (
        bos["tricode"],
        column["season"]["allowed_per_48"],
        column["season"]["rank"],
        column["season"]["percent_vs_league_average"],
        league["season"]["average_allowed_per_48"],
    )


def _season_defense(season, context="latest"):
    return {
        "status": "available",
        "source": "team_matchup_publication",
        "context": context,
        "unavailable_reason": None,
        "season": season,
        "window_games": None,
    }


def test_after_an_opening_week_cutover_a_matchup_serves_trailing_inputs_labelled_2025_26(world):
    old_defense = pointers(world.engine)[SEASON_STREAM][0]
    cutover = world.opening_week_cutover()
    thin_logs = pointers(world.engine)[PLAYER_LOGS][0]

    matchup = world.matchup()

    assert [dict(item) for item in cutover.resource.left_behind] == [
        {"stream_key": SEASON_STREAM, "reason": "serving_season_not_ready"}
    ]
    assert matchup["as_of"]["season"] == NEW
    assert matchup["experience"]["sections"]["season_defense"] == _season_defense(OLD)
    assert matchup["league"]["surface_availability"]["traditional"]["season"] == {
        "status": "available",
        "unavailable_reason": None,
        "season": OLD,
        "window_games": None,
    }
    assert matchup["trailing"] == [TRAILING_DIETS, TRAILING_TEAM_DEFENSE]
    assert _generation(matchup, PLAYER_LOGS, DIET_STREAM, SEASON_STREAM) == {
        PLAYER_LOGS: ("active", NEW, thin_logs),
        DIET_STREAM: ("prior_season", OLD, world.diet),
        SEASON_STREAM: ("prior_season", OLD, old_defense),
    }
    assert sorted(
        entry["stream_key"]
        for entry in matchup["provenance"]["generation"]
        if entry["status"] == "prior_season"
    ) == [DIET_STREAM, SEASON_STREAM]
    [player] = matchup["players"]
    # The game logs are 2026-27's: one 30-minute game with 12 points (2025-26
    # had 30).
    assert (player["canonical_id"], player["season_scoring"], player["last_10_minutes"]) == (
        PLAYER_ID,
        12.0,
        [30.0],
    )
    # The Diet is 2025-26's, against 2025-26's baseline: mean(0.4, 0.2).
    first_shot_type = player["diet_shares"]["shot_types"][0]
    assert (
        first_shot_type["key"],
        first_shot_type["season"]["share"],
        first_shot_type["season"]["league_average_share"],
    ) == ("Catch and Shoot", 0.4, 0.3)


def test_a_mixed_season_score_is_pinned(world):
    world.opening_week_cutover()

    matchup = world.matchup()

    # Ranks and percent vs league are 2025-26's population: BOS allows 37
    # steals per 48, the 29 others 7, so the mean is (29 * 7 + 37) / 30 = 8,
    # BOS ranks 30th and allows 37 / 8 - 1 = +362.5%.
    assert _bos_steals(matchup) == ("BOS", 37.0, 30, 362.5, 8.0)
    assert (_score(matchup, "STL"), _score(matchup, "BLK")) == (3.625, 0.0)
    # STKS weighs the 2025-26 STL and BLK scores by the player's 2026-27 rates
    # (3 steals, 1 block a game); blocks are uniform, so BLK scores 0:
    #   (3 * (37 / 8 - 1) + 1 * 0) / (3 + 1) = 2.71875
    # 2025-26 rates (1, 2) would give 3.625 / 3 = 1.208333, and 2026-27's
    # population (BOS 67, mean 9) 3 * (67 / 9 - 1) / 4 = 4.833333.
    assert _score(matchup, "STKS") == 2.71875


def test_when_team_defense_is_ready_its_next_composition_activates_and_the_section_reports_2026_27(world):
    world.opening_week_cutover()
    # Still one game a team: this composition is held, and nothing changes.
    world.compose_defense(THIN_GAMES, cutoff=THIN_CUTOFF, provenance=THIN_PROVENANCE)
    held = world.matchup()
    assert (held["trailing"], _score(held, "STKS")) == ([TRAILING_DIETS, TRAILING_TEAM_DEFENSE], 2.71875)

    accept_ledger(world.engine, NEW_GAMES[:15], "pbp")
    ready = world.compose_defense(NEW_GAMES, cutoff=NEW_CUTOFF, provenance=NEW_PROVENANCE)

    matchup = world.matchup()

    # The fixture's 2026-27 Event Catalog lists only final games, so its own
    # governance calls the season complete once a 2026-27 Publication is read.
    assert matchup["experience"]["sections"]["season_defense"] == _season_defense(NEW, "completed_season")
    assert matchup["league"]["surface_availability"]["traditional"]["season"]["season"] == NEW
    assert matchup["trailing"] == [TRAILING_DIETS]
    assert _generation(matchup, SEASON_STREAM) == {SEASON_STREAM: ("active", NEW, ready.publication_id)}
    # 2026-27's population: BOS 67, the others 7, mean (29 * 7 + 67) / 30 = 9;
    # 67 / 9 - 1 = +644.444444%.
    assert _bos_steals(matchup) == ("BOS", 67.0, 30, 644.444444, 9.0)
    # STL 67 / 9 - 1 = 6.444444; STKS (3 * 58 / 9 + 1 * 0) / 4 = 4.833333.
    assert (_score(matchup, "STL"), _score(matchup, "STKS")) == (6.444444, 4.833333)


def test_reverting_the_cutover_restores_every_read_to_2025_26(world):
    before = pointers(world.engine)
    cutover = world.opening_week_cutover()

    world.operations.revert_season_cutover(
        NEW, cutover.job_id, actor=ACTOR, reason="2026-27 opened too early"
    )
    matchup = world.matchup()

    assert matchup["as_of"]["season"] == OLD
    assert matchup["experience"]["sections"]["season_defense"] == _season_defense(OLD, "completed_season")
    assert matchup["trailing"] == []
    # Every Publication the Matchup read is 2025-26's own active one.
    assert {
        entry["stream_key"]: (entry["status"], entry["season"], entry["publication_id"])
        for entry in matchup["provenance"]["generation"]
        if entry["publication_id"] is not None
    } == {
        PLAYER_LOGS: ("active", OLD, before[PLAYER_LOGS][0]),
        DIET_STREAM: ("active", OLD, world.diet),
        SEASON_STREAM: ("active", OLD, before[SEASON_STREAM][0]),
        L15_STREAM: ("active", OLD, before[L15_STREAM][0]),
    }
    # 2025-26 game logs (30 points; 1 steal, 2 blocks a game) against
    # 2025-26 defense: STKS (1 * 3.625 + 2 * 0) / 3 = 1.208333.
    assert (matchup["players"][0]["season_scoring"], _score(matchup, "STKS")) == (30.0, 1.208333)


def test_after_an_opening_week_cutover_a_game_matchup_is_as_of_2026_27_with_its_trailing_sections_2025_26(
    world,
):
    # Before the cutover a 2026-27 game reads 2025-26 evidence, and says so.
    before = world.game_matchup()
    assert (before["as_of"]["season"], before["trailing"]) == (OLD, [])
    world.opening_week_cutover()

    matchup = world.game_matchup()

    # The Published Season, while each section says which season it served.
    # Last 15 waits for its first 2026-27 composition (#548), so it has no date.
    assert matchup["as_of"] == {
        "season": NEW,
        "season_defense": "2026-10-25",
        "last_15_defense": None,
        "player_diets": "2026-10-25",
    }
    assert matchup["experience"]["sections"]["season_defense"] == _season_defense(OLD, "pregame")
    assert matchup["trailing"] == [TRAILING_DIETS, TRAILING_TEAM_DEFENSE]
