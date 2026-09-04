"""Public refresh/query behavior for durable Season player Diet facts."""

from __future__ import annotations

from datetime import datetime, timezone
from statistics import fmean, pstdev

import pandas as pd
import pytest
from sqlalchemy import create_engine, event

from app.config.settings import PlayerDietBaselineSettings
from app.migrations import run_migrations
from app.models.catalogs import PLAY_TYPES, SHOOTING_TYPES
from app.services.database_first_activation import PublicationRead
from app.services.player_diet import (
    PlayerDietFact,
    PlayerDietObservation,
    PlayerDietRepository,
    PlayerDietService,
    compute_player_diet_baselines,
)


NOW = datetime(2026, 1, 6, 10, 0, tzinfo=timezone.utc)


class FreshAthleteCatalog:
    def __init__(self, *, is_fresh=True, player_ids=(2544,)):
        self.is_fresh = is_fresh
        self.player_ids = player_ids

    def get_freshness(self, season, *, now=None):
        return {"is_fresh": self.is_fresh, "row_count": len(self.player_ids)}

    def get_catalog(self, season, *, active_only=False):
        return [
            {"player_id": player_id, "display_name": f"Player {player_id}"}
            for player_id in self.player_ids
        ]


class RecordedNBAPlayerDiets:
    def __init__(self):
        self.calls = []
        self.player_id = 2544
        self.play_share = 0.2
        self.shot_share = 0.25
        self.shot_type_player_id = None
        self.shot_type_games = {}
        self.restricted_area_attempts = 40
        self.zero_zone_player_id = None
        self.uncovered_positive_zone_player_id = None
        self.missing_play_type = None
        self.malformed_play_type = None
        self.fail_shot_zones = False
        self.duplicate_shot_type_rows = False
        self.stint_player_id = None
        self.stint_play_type = "Isolation"
        self.stints = ()

    def fetch_synergy_play_types(self, play_type, **kwargs):
        self.calls.append(("play_type", play_type, kwargs))
        if play_type == self.malformed_play_type:
            return pd.DataFrame([{"PLAYER_ID": self.player_id}])
        if play_type == self.missing_play_type:
            return pd.DataFrame(
                columns=[
                    "PLAYER_ID", "PLAYER_NAME", "PLAY_TYPE", "TYPE_GROUPING",
                    "GP", "POSS_PCT", "POSS",
                ]
            )
        row = {
            "PLAYER_ID": self.player_id,
            "PLAYER_NAME": "LeBron James",
            "PLAY_TYPE": play_type,
            "TYPE_GROUPING": "Offensive",
            "GP": 40,
            "POSS_PCT": self.play_share,
            "POSS": 120,
        }
        rows = [row]
        if self.stint_player_id is not None and play_type == self.stint_play_type:
            rows.extend(
                {
                    "PLAYER_ID": self.stint_player_id,
                    "PLAYER_NAME": "James Harden",
                    "PLAY_TYPE": play_type,
                    "TYPE_GROUPING": "Offensive",
                    "GP": games,
                    "POSS_PCT": share,
                    "POSS": possessions,
                }
                for games, possessions, share in self.stints
            )
        return pd.DataFrame(rows)

    def fetch_player_shot_type(self, general_range, **kwargs):
        self.calls.append(("shot_type", general_range, kwargs))
        row = {
            "PLAYER_ID": self.shot_type_player_id or self.player_id,
            "PLAYER_NAME": "LeBron James",
            "GP": self.shot_type_games.get(general_range, 40),
            "FGA_FREQUENCY": self.shot_share,
            "FGA": 100,
        }
        return pd.DataFrame([row, row] if self.duplicate_shot_type_rows else [row])

    def fetch_player_shooting_zone(self, **kwargs):
        self.calls.append(("shot_zones", kwargs))
        if self.fail_shot_zones:
            raise RuntimeError("simulated provider transport failure")
        columns = pd.MultiIndex.from_tuples(
            [
                ("", "PLAYER_ID"),
                ("", "PLAYER_NAME"),
                ("Restricted Area", "FGA"),
                ("In The Paint (Non-RA)", "FGA"),
                ("Mid-Range", "FGA"),
                ("Left Corner 3", "FGA"),
                ("Right Corner 3", "FGA"),
                ("Above the Break 3", "FGA"),
                ("Backcourt", "FGA"),
                ("Corner 3", "FGA"),
            ]
        )
        rows = [[
                self.player_id,
                "LeBron James",
                self.restricted_area_attempts,
                20,
                10,
                5,
                5,
                20,
                1,
                10,
            ]]
        if self.zero_zone_player_id is not None:
            rows.append([
                self.zero_zone_player_id,
                "Zero Included Attempts",
                0,
                0,
                0,
                0,
                0,
                0,
                4,
                0,
            ])
        if self.uncovered_positive_zone_player_id is not None:
            rows.append([
                self.uncovered_positive_zone_player_id,
                "Positive Attempts Without GP",
                10,
                0,
                0,
                0,
                0,
                0,
                0,
                0,
            ])
        return pd.DataFrame(rows, columns=columns)


class RecordedPBPPlayerDiets:
    def __init__(self):
        self.calls = []
        self.arc_assists = 20
        self.extra_rows = []
        self.no_location_observations = False

    def fetch_player_diet_totals(self, *, season):
        self.calls.append(season)
        location_values = {
            "Arc3Assists": self.arc_assists,
            "Corner3Assists": 10,
            "AtRimAssists": 30,
            "ShortMidRangeAssists": 25,
            "LongMidRangeAssists": 15,
        }
        if self.no_location_observations:
            location_values = {key: None for key in location_values}
        return pd.DataFrame(
            [{
                "EntityId": "2544",
                "Name": "LeBron James",
                "GamesPlayed": 40,
                "Assists": 100,
                **location_values,
            }, *self.extra_rows]
        )


def _service(tmp_path, *, player_ids=(2544,)):
    engine = create_engine(f"sqlite:///{tmp_path / 'player-diets.sqlite3'}")
    run_migrations(engine)
    nba = RecordedNBAPlayerDiets()
    pbp = RecordedPBPPlayerDiets()
    return (
        PlayerDietService(
            engine,
            athlete_catalog=FreshAthleteCatalog(player_ids=player_ids),
            nba_stats_provider=nba,
            pbp_stats_provider=pbp,
            clock=lambda: NOW,
        ),
        nba,
        pbp,
    )


def test_repository_refuses_the_read_only_demo_database():
    with pytest.raises(ValueError, match="demo database"):
        PlayerDietRepository(create_engine("sqlite:///nba_play_types.db"))


def test_refresh_publishes_four_raw_season_bases_and_bulk_reads_them(tmp_path):
    service, nba, pbp = _service(tmp_path)

    service.refresh("2025-26")
    result = service.get_for_players("2025-26", [2544])

    assert [call[1] for call in nba.calls if call[0] == "play_type"] == list(
        PLAY_TYPES
    )
    assert all(
        call[2]
        == {
            "player_or_team_abbreviation": "P",
            "type_grouping": "Offensive",
            "season": "2025-26",
            "season_type": "Regular Season",
            "per_mode_simple": "Totals",
        }
        for call in nba.calls
        if call[0] == "play_type"
    )
    assert [call[1] for call in nba.calls if call[0] == "shot_type"] == list(
        SHOOTING_TYPES
    )
    assert len([call for call in nba.calls if call[0] == "shot_zones"]) == 1
    assert pbp.calls == ["2025-26"]
    assert {(item.base, item.status) for item in result.observations} == {
        ("play_types", "available"),
        ("shot_zones", "available"),
        ("shot_types", "available"),
        ("assist_locations", "available"),
    }
    facts = result.players[2544]
    assert len([fact for fact in facts if fact.base == "play_types"]) == 11
    assert len([fact for fact in facts if fact.base == "shot_types"]) == 3
    assert {
        fact.slice_key for fact in facts if fact.base == "shot_zones"
    } == {
        "Restricted Area",
        "In The Paint (Non-RA)",
        "Mid-Range",
        "Corner 3",
        "Above the Break 3",
    }
    assert {
        fact.slice_key for fact in facts if fact.base == "assist_locations"
    } == {
        "Arc3Assists",
        "Corner3Assists",
        "AtRimAssists",
        "ShortMidRangeAssists",
        "LongMidRangeAssists",
    }
    isolation = next(
        fact
        for fact in facts
        if fact.base == "play_types" and fact.slice_key == "Isolation"
    )
    assert (isolation.share, isolation.volume, isolation.games_played) == (
        0.2,
        120,
        40,
    )
    assert isolation.volume_unit == "possessions"
    restricted = next(
        fact
        for fact in facts
        if fact.base == "shot_zones" and fact.slice_key == "Restricted Area"
    )
    assert (restricted.share, restricted.volume) == (0.4, 40)
    assert restricted.retrieved_at == NOW


def test_duplicate_provider_identity_degrades_only_its_base(tmp_path):
    service, nba, _ = _service(tmp_path)
    service.refresh("2025-26")
    nba.duplicate_shot_type_rows = True
    nba.play_share = 0.3

    service.refresh("2025-26")

    result = service.get_for_players("2025-26", [2544])
    assert next(
        fact.share
        for fact in result.players[2544]
        if fact.base == "shot_types" and fact.slice_key == "Catch and Shoot"
    ) == 0.25
    observation = {item.base: item for item in result.observations}["shot_types"]
    assert (observation.status, observation.unavailable_reason) == (
        "unavailable",
        "provider_invalid_response",
    )
    assert next(
        fact.share
        for fact in result.players[2544]
        if fact.base == "play_types" and fact.slice_key == "Isolation"
    ) == 0.3


def test_traded_player_play_type_stints_combine_into_one_fact(tmp_path):
    service, nba, _ = _service(tmp_path, player_ids=(2544, 201935))
    nba.stint_player_id = 201935
    nba.stints = ((41, 419, 0.421), (26, 181, 0.365))

    service.refresh("2025-26")
    result = service.get_for_players("2025-26", [2544, 201935])

    isolation = [
        fact
        for fact in result.players[201935]
        if fact.base == "play_types" and fact.slice_key == "Isolation"
    ]
    assert len(isolation) == 1
    assert (isolation[0].volume, isolation[0].games_played) == (600, 67)
    assert isolation[0].share == pytest.approx(
        600 / (419 / 0.421 + 181 / 0.365)
    )
    observation = {item.base: item for item in result.observations}["play_types"]
    assert (observation.status, observation.unavailable_reason) == ("available", None)
    assert next(
        fact.share
        for fact in result.players[2544]
        if fact.base == "play_types" and fact.slice_key == "Isolation"
    ) == 0.2


def test_play_type_stint_without_a_share_degrades_its_base(tmp_path):
    service, nba, _ = _service(tmp_path, player_ids=(2544, 201935))
    nba.stint_player_id = 201935
    nba.stints = ((41, 419, 0.0), (26, 181, 0.365))

    service.refresh("2025-26")
    result = service.get_for_players("2025-26", [201935])

    observation = {item.base: item for item in result.observations}["play_types"]
    assert (observation.status, observation.unavailable_reason) == (
        "unavailable",
        "provider_invalid_response",
    )
    assert not [
        fact for fact in result.players.get(201935, ()) if fact.base == "play_types"
    ]


def test_nonfinite_base_is_explicit_and_preserves_prior_facts(tmp_path):
    service, nba, _ = _service(tmp_path)
    service.refresh("2025-26")
    nba.play_share = float("nan")

    service.refresh("2025-26")
    result = service.get_for_players("2025-26", [2544])

    observation = next(
        item for item in result.observations if item.base == "play_types"
    )
    assert (observation.status, observation.unavailable_reason) == (
        "unavailable",
        "provider_invalid_response",
    )
    assert next(
        fact.share
        for fact in result.players[2544]
        if fact.base == "play_types" and fact.slice_key == "Isolation"
    ) == 0.2


def test_finite_out_of_domain_share_degrades_only_its_provider_base(tmp_path):
    service, nba, _ = _service(tmp_path)
    service.refresh("2025-26")
    nba.play_share = 1.5
    nba.shot_share = 0.3

    service.refresh("2025-26")
    result = service.get_for_players("2025-26", [2544])

    observations = {item.base: item for item in result.observations}
    assert (
        observations["play_types"].status,
        observations["play_types"].unavailable_reason,
    ) == ("unavailable", "provider_invalid_response")
    assert next(
        fact.share
        for fact in result.players[2544]
        if fact.base == "play_types" and fact.slice_key == "Isolation"
    ) == 0.2
    assert next(
        fact.share
        for fact in result.players[2544]
        if fact.base == "shot_types" and fact.slice_key == "Catch and Shoot"
    ) == 0.3


def test_assist_counter_above_total_degrades_only_assist_locations(tmp_path):
    service, nba, pbp = _service(tmp_path)
    service.refresh("2025-26")
    pbp.arc_assists = 101
    nba.shot_share = 0.3

    service.refresh("2025-26")
    result = service.get_for_players("2025-26", [2544])

    observation = {item.base: item for item in result.observations}[
        "assist_locations"
    ]
    assert (observation.status, observation.unavailable_reason) == (
        "unavailable",
        "provider_invalid_response",
    )
    assert next(
        fact.volume
        for fact in result.players[2544]
        if fact.base == "assist_locations" and fact.slice_key == "Arc3Assists"
    ) == 20
    assert next(
        fact.share
        for fact in result.players[2544]
        if fact.base == "shot_types" and fact.slice_key == "Catch and Shoot"
    ) == 0.3


def test_nan_assist_location_is_absent_without_synthetic_zero_fact(tmp_path):
    service, _, pbp = _service(tmp_path)
    pbp.arc_assists = float("nan")

    service.refresh("2025-26")
    result = service.get_for_players("2025-26", [2544])

    observation = {item.base: item for item in result.observations}[
        "assist_locations"
    ]
    assert (observation.status, observation.unavailable_reason) == (
        "available",
        None,
    )
    assert {
        fact.slice_key
        for fact in result.players[2544]
        if fact.base == "assist_locations"
    } == {
        "Corner3Assists",
        "AtRimAssists",
        "ShortMidRangeAssists",
        "LongMidRangeAssists",
    }


def test_zero_assist_player_is_skipped_while_other_players_publish(tmp_path):
    service, _, pbp = _service(tmp_path, player_ids=(2544, 201939))
    pbp.extra_rows = [{
        "EntityId": "201939",
        "Name": "Stephen Curry",
        "GamesPlayed": 40,
        "Assists": 0,
        "Arc3Assists": 0,
        "Corner3Assists": 0,
        "AtRimAssists": 0,
        "ShortMidRangeAssists": 0,
        "LongMidRangeAssists": 0,
    }]

    service.refresh("2025-26")
    result = service.get_for_players("2025-26", [2544, 201939])

    observation = {item.base: item for item in result.observations}[
        "assist_locations"
    ]
    assert (observation.status, observation.unavailable_reason) == (
        "available",
        None,
    )
    assert len(
        [fact for fact in result.players[2544] if fact.base == "assist_locations"]
    ) == 5
    assert 201939 not in result.players


def test_shot_zones_report_missing_games_evidence_when_shot_types_degrade(tmp_path):
    service, nba, _ = _service(tmp_path)
    service.refresh("2025-26")
    nba.shot_share = 1.5
    nba.restricted_area_attempts = 50

    service.refresh("2025-26")
    result = service.get_for_players("2025-26", [2544])

    observations = {item.base: item for item in result.observations}
    assert (
        observations["shot_types"].status,
        observations["shot_types"].unavailable_reason,
    ) == ("unavailable", "provider_invalid_response")
    assert (
        observations["shot_zones"].status,
        observations["shot_zones"].unavailable_reason,
    ) == ("unavailable", "missing_games_played_evidence")
    assert next(
        fact.volume
        for fact in result.players[2544]
        if fact.base == "shot_zones" and fact.slice_key == "Restricted Area"
    ) == 40


def test_shot_zone_dependency_reason_survives_a_shot_type_join_failure(tmp_path):
    service, nba, _ = _service(tmp_path)
    service.refresh("2025-26")
    nba.shot_type_player_id = 999999
    nba.restricted_area_attempts = 50

    service.refresh("2025-26")
    result = service.get_for_players("2025-26", [2544, 999999])

    observations = {item.base: item for item in result.observations}
    assert (
        observations["shot_types"].status,
        observations["shot_types"].unavailable_reason,
    ) == ("unavailable", "athlete_catalog_join_incomplete")
    assert (
        observations["shot_zones"].status,
        observations["shot_zones"].unavailable_reason,
    ) == ("unavailable", "missing_games_played_evidence")
    assert next(
        fact.volume
        for fact in result.players[2544]
        if fact.base == "shot_zones" and fact.slice_key == "Restricted Area"
    ) == 40
    assert 999999 not in result.players


def test_zero_included_zone_attempts_do_not_disable_other_player_facts(tmp_path):
    service, nba, _ = _service(tmp_path, player_ids=(2544, 201939))
    nba.zero_zone_player_id = 201939

    service.refresh("2025-26")
    result = service.get_for_players("2025-26", [2544, 201939])

    observation = {item.base: item for item in result.observations}["shot_zones"]
    assert (observation.status, observation.unavailable_reason) == (
        "available",
        None,
    )
    assert len(
        [fact for fact in result.players[2544] if fact.base == "shot_zones"]
    ) == 5
    assert 201939 not in result.players


def test_positive_zone_player_without_gp_coverage_degrades_and_preserves_base(
    tmp_path,
):
    service, nba, _ = _service(tmp_path, player_ids=(2544, 201939))
    service.refresh("2025-26")
    nba.restricted_area_attempts = 50
    nba.uncovered_positive_zone_player_id = 201939

    service.refresh("2025-26")
    result = service.get_for_players("2025-26", [2544, 201939])

    observations = {item.base: item for item in result.observations}
    assert observations["shot_types"].status == "available"
    assert (
        observations["shot_zones"].status,
        observations["shot_zones"].unavailable_reason,
    ) == ("unavailable", "missing_games_played_evidence")
    assert next(
        fact.volume
        for fact in result.players[2544]
        if fact.base == "shot_zones" and fact.slice_key == "Restricted Area"
    ) == 40
    assert 201939 not in result.players


@pytest.mark.parametrize("attempts", [-1, float("nan")])
def test_negative_or_nonfinite_zone_attempts_still_degrade_the_base(
    tmp_path, attempts
):
    service, nba, _ = _service(tmp_path)
    service.refresh("2025-26")
    nba.restricted_area_attempts = attempts

    service.refresh("2025-26")
    result = service.get_for_players("2025-26", [2544])

    observation = {item.base: item for item in result.observations}["shot_zones"]
    assert (observation.status, observation.unavailable_reason) == (
        "unavailable",
        "provider_invalid_response",
    )
    assert next(
        fact.volume
        for fact in result.players[2544]
        if fact.base == "shot_zones" and fact.slice_key == "Restricted Area"
    ) == 40


def test_positive_zone_attempts_keep_the_gp_dependency_after_inconsistent_gp(tmp_path):
    service, nba, _ = _service(tmp_path)
    service.refresh("2025-26")
    nba.shot_type_games["Pullups"] = 39
    nba.restricted_area_attempts = 50

    service.refresh("2025-26")
    result = service.get_for_players("2025-26", [2544])

    observations = {item.base: item for item in result.observations}
    assert (
        observations["shot_types"].status,
        observations["shot_types"].unavailable_reason,
    ) == ("unavailable", "provider_invalid_response")
    assert (
        observations["shot_zones"].status,
        observations["shot_zones"].unavailable_reason,
    ) == ("unavailable", "missing_games_played_evidence")
    assert next(
        fact.volume
        for fact in result.players[2544]
        if fact.base == "shot_zones" and fact.slice_key == "Restricted Area"
    ) == 40


def test_no_assist_location_observations_publish_missing_and_preserve_facts(tmp_path):
    service, nba, pbp = _service(tmp_path)
    service.refresh("2025-26")
    pbp.no_location_observations = True
    nba.shot_share = 0.3

    service.refresh("2025-26")
    result = service.get_for_players("2025-26", [2544])

    observation = {item.base: item for item in result.observations}[
        "assist_locations"
    ]
    assert (observation.status, observation.unavailable_reason) == (
        "missing",
        "provider_no_observation",
    )
    assert len(
        [fact for fact in result.players[2544] if fact.base == "assist_locations"]
    ) == 5
    assert next(
        fact.share
        for fact in result.players[2544]
        if fact.base == "shot_types" and fact.slice_key == "Catch and Shoot"
    ) == 0.3


def test_malformed_provider_schema_degrades_only_its_base_and_preserves_facts(
    tmp_path,
):
    service, nba, _ = _service(tmp_path)
    service.refresh("2025-26")
    nba.malformed_play_type = "Isolation"
    nba.shot_share = 0.3

    service.refresh("2025-26")
    result = service.get_for_players("2025-26", [2544])

    observation = {item.base: item for item in result.observations}["play_types"]
    assert (observation.status, observation.unavailable_reason) == (
        "unavailable",
        "provider_malformed_response",
    )
    assert len(
        [fact for fact in result.players[2544] if fact.base == "play_types"]
    ) == 11
    assert next(
        fact.share
        for fact in result.players[2544]
        if fact.base == "shot_types" and fact.slice_key == "Catch and Shoot"
    ) == 0.3


def test_missing_base_is_explicit_and_keeps_its_older_raw_facts(tmp_path):
    service, nba, _ = _service(tmp_path)
    service.refresh("2025-26")
    nba.missing_play_type = "Misc"

    service.refresh("2025-26")
    result = service.get_for_players("2025-26", [2544])

    observation = next(
        item for item in result.observations if item.base == "play_types"
    )
    assert (observation.status, observation.unavailable_reason) == (
        "unavailable",
        "provider_invalid_response",
    )
    assert len(
        [fact for fact in result.players[2544] if fact.base == "play_types"]
    ) == 11


def test_transport_failure_never_partially_publishes_other_collected_bases(tmp_path):
    service, nba, _ = _service(tmp_path)
    service.refresh("2025-26")
    nba.play_share = 0.1
    nba.fail_shot_zones = True

    try:
        service.refresh("2025-26")
    except RuntimeError as error:
        assert "transport" in str(error)
    else:
        raise AssertionError("provider transport failure must reach Nightly retry")

    result = service.get_for_players("2025-26", [2544])
    assert next(
        fact.share
        for fact in result.players[2544]
        if fact.base == "play_types" and fact.slice_key == "Isolation"
    ) == 0.2
    assert {item.status for item in result.observations} == {"available"}


def test_database_failure_rolls_back_fact_replacement_and_observations(tmp_path):
    service, nba, _ = _service(tmp_path)
    service.refresh("2025-26")
    nba.play_share = 0.1

    def fail_observation_insert(
        connection, cursor, statement, parameters, context, executemany
    ):
        if statement.startswith("INSERT INTO player_diet_surface_observations"):
            raise RuntimeError("simulated publication failure")

    event.listen(
        service.repository.engine,
        "before_cursor_execute",
        fail_observation_insert,
    )
    try:
        try:
            service.refresh("2025-26")
        except RuntimeError as error:
            assert "publication" in str(error)
        else:
            raise AssertionError("database publication failure must propagate")
    finally:
        event.remove(
            service.repository.engine,
            "before_cursor_execute",
            fail_observation_insert,
        )

    result = service.get_for_players("2025-26", [2544])
    assert next(
        fact.share
        for fact in result.players[2544]
        if fact.base == "play_types" and fact.slice_key == "Isolation"
    ) == 0.2
    assert {item.status for item in result.observations} == {"available"}


def test_bulk_reads_return_unthresholded_small_shares_and_no_synthetic_players(tmp_path):
    service, nba, _ = _service(tmp_path)
    nba.play_share = 0.01
    service.refresh("2025-26")

    result = service.get_for_players("2025-26", [2544, 201939])

    assert 201939 not in result.players
    assert next(
        fact.share
        for fact in result.players[2544]
        if fact.base == "play_types" and fact.slice_key == "Isolation"
    ) == 0.01


def test_refresh_refuses_a_stale_athlete_catalog_before_provider_collection(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'stale-catalog.sqlite3'}")
    run_migrations(engine)
    nba = RecordedNBAPlayerDiets()
    pbp = RecordedPBPPlayerDiets()
    service = PlayerDietService(
        engine,
        athlete_catalog=FreshAthleteCatalog(is_fresh=False),
        nba_stats_provider=nba,
        pbp_stats_provider=pbp,
        clock=lambda: NOW,
    )

    try:
        service.refresh("2025-26")
    except ValueError as error:
        assert "fresh Athlete Catalog" in str(error)
    else:
        raise AssertionError("stale canonical identity must fail closed")

    assert nba.calls == []
    assert pbp.calls == []
    assert service.get_for_players("2025-26", [2544]).players == {}


def test_unjoined_nba_identity_marks_each_affected_base_unavailable(tmp_path):
    service, nba, _ = _service(tmp_path)
    nba.player_id = 999999

    service.refresh("2025-26")
    result = service.get_for_players("2025-26", [2544, 999999])

    observations = {item.base: item for item in result.observations}
    for base in ("play_types", "shot_types", "shot_zones"):
        assert (
            observations[base].status,
            observations[base].unavailable_reason,
        ) == ("unavailable", "athlete_catalog_join_incomplete")
    assert result.players[2544][0].base == "assist_locations"
    assert 999999 not in result.players


# --- Diet Share league baseline (sigma_deviation) -----------------------

_BASELINE_SETTINGS = PlayerDietBaselineSettings()


def test_baseline_reports_a_deviation_above_one_sigma_for_a_fact_above_the_mean():
    shares = [0.5, 0.3, 0.2, 0.1]
    facts = [
        PlayerDietFact(
            player_id, "play_types", "Transition", share, 150.0, 25,
            "possessions", "nba_synergy",
        )
        for player_id, share in zip((1001, 1002, 1003, 1004), shares)
    ]

    baselines = compute_player_diet_baselines(facts, settings=_BASELINE_SETTINGS)

    baseline = baselines[("play_types", "Transition")]
    assert baseline.league_average_share == pytest.approx(fmean(shares))
    assert baseline.population_sigma == pytest.approx(pstdev(shares))
    assert baseline.sigma_deviation(0.5) > 1


def test_baseline_excludes_a_fact_below_the_population_floor_but_still_scores_it():
    population_shares = [0.5, 0.3, 0.2, 0.1]
    facts = [
        PlayerDietFact(
            player_id, "play_types", "Transition", share, 150.0, 25,
            "possessions", "nba_synergy",
        )
        for player_id, share in zip((1001, 1002, 1003, 1004), population_shares)
    ] + [
        # 10 possessions over 25 games is 0.4/game, below the 6.0/game floor,
        # so this player never joins the population even though a fact is
        # delivered for them.
        PlayerDietFact(
            1005, "play_types", "Transition", 0.9, 10.0, 25,
            "possessions", "nba_synergy",
        ),
    ]

    baselines = compute_player_diet_baselines(facts, settings=_BASELINE_SETTINGS)

    baseline = baselines[("play_types", "Transition")]
    expected_average = fmean(population_shares)
    expected_sigma = pstdev(population_shares)
    assert baseline.league_average_share == pytest.approx(expected_average)
    deviation = baseline.sigma_deviation(0.9)
    assert deviation is not None
    assert deviation == pytest.approx((0.9 - expected_average) / expected_sigma)


def test_baseline_reports_nulls_for_a_slice_with_fewer_than_two_population_players():
    facts = [
        PlayerDietFact(
            2001, "shot_zones", "Corner 3", 0.4, 140.0, 20,
            "field_goal_attempts", "nba_stats",
        ),
    ]

    baselines = compute_player_diet_baselines(facts, settings=_BASELINE_SETTINGS)

    baseline = baselines[("shot_zones", "Corner 3")]
    assert baseline.league_average_share is None
    assert baseline.sigma_deviation(0.4) is None


def test_baseline_reports_zero_deviation_when_the_population_sigma_is_zero():
    facts = [
        PlayerDietFact(
            player_id, "play_types", "Spotup", 0.3, 150.0, 25,
            "possessions", "nba_synergy",
        )
        for player_id in (3001, 3002)
    ]

    baselines = compute_player_diet_baselines(facts, settings=_BASELINE_SETTINGS)

    baseline = baselines[("play_types", "Spotup")]
    assert baseline.league_average_share == 0.3
    # Mirrors the team Defense Sheet convention: a zero population sigma
    # reports a 0.0 deviation rather than dividing by zero.
    assert baseline.sigma_deviation(0.3) == 0.0
    assert baseline.sigma_deviation(0.9) == 0.0


_SHARED_TRANSITION_FACTS = [
    PlayerDietFact(
        player_id, "play_types", "Transition", share, 150.0, 25,
        "possessions", "nba_synergy",
    )
    for player_id, share in zip((1001, 1002, 1003, 1004), (0.5, 0.3, 0.2, 0.1))
] + [
    PlayerDietFact(
        1005, "play_types", "Transition", 0.9, 10.0, 25,
        "possessions", "nba_synergy",
    ),
]


def _legacy_repository_with_shared_facts(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy-baseline.sqlite3'}")
    run_migrations(engine)
    repository = PlayerDietRepository(engine)
    repository.publish(
        "2025-26",
        _SHARED_TRANSITION_FACTS,
        (
            PlayerDietObservation("play_types", "available"),
            PlayerDietObservation(
                "shot_zones", "missing", "provider_no_observation"
            ),
            PlayerDietObservation(
                "shot_types", "missing", "provider_no_observation"
            ),
            PlayerDietObservation(
                "assist_locations", "missing", "provider_no_observation"
            ),
        ),
        retrieved_at=NOW,
    )
    return repository


class _RecordedPublicationReader:
    """Serves `play_types` from an activated payload; the rest report missing."""

    def __init__(self, decoded):
        self._decoded = decoded

    def read_many(self, stream_keys, *, season):
        reads = {}
        for stream_key in stream_keys:
            if stream_key == "synergy_play_types":
                reads[stream_key] = PublicationRead(
                    stream_key=stream_key,
                    publication_id="publication-1",
                    season=season,
                    cutoff=None,
                    version=1,
                    status="active",
                    freshness="fresh",
                    age_seconds=0,
                    payload={"rows": []},
                    decoded=self._decoded,
                    retrieved_at=NOW,
                )
            else:
                reads[stream_key] = PublicationRead(
                    stream_key=stream_key,
                    publication_id=None,
                    season=season,
                    cutoff=None,
                    version=None,
                    status="missing",
                    freshness="missing",
                    age_seconds=None,
                    payload=None,
                    retrieved_at=NOW,
                )
        return reads


def _publication_repository_with_shared_facts(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'publication-baseline.sqlite3'}")
    run_migrations(engine)
    return PlayerDietRepository(
        engine,
        publication_reader=_RecordedPublicationReader(_SHARED_TRANSITION_FACTS),
    )


def test_legacy_and_publication_paths_agree_on_the_same_baseline_fixture(tmp_path):
    requested = [1001, 1005]

    legacy_result = _legacy_repository_with_shared_facts(tmp_path).get_for_players(
        "2025-26", requested
    )
    publication_result = _publication_repository_with_shared_facts(
        tmp_path
    ).get_for_players("2025-26", requested)

    key = ("play_types", "Transition")
    legacy_baseline = legacy_result.baselines[key]
    publication_baseline = publication_result.baselines[key]
    assert legacy_baseline.league_average_share == pytest.approx(
        publication_baseline.league_average_share
    )
    assert legacy_baseline.population_sigma == pytest.approx(
        publication_baseline.population_sigma
    )
    assert legacy_baseline.sigma_deviation(0.9) == pytest.approx(
        publication_baseline.sigma_deviation(0.9)
    )
    assert {player_id for player_id in legacy_result.players} == set(requested)
    assert {player_id for player_id in publication_result.players} == set(requested)


def test_legacy_get_for_players_baselines_over_the_whole_stored_season_fact_set(
    tmp_path,
):
    repository = _legacy_repository_with_shared_facts(tmp_path)

    result = repository.get_for_players("2025-26", [1001])

    assert set(result.players) == {1001}
    baseline = result.baselines[("play_types", "Transition")]
    population_shares = [0.5, 0.3, 0.2, 0.1]
    assert baseline.league_average_share == pytest.approx(fmean(population_shares))
    assert baseline.population_sigma == pytest.approx(pstdev(population_shares))
