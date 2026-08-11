"""Public refresh/query behavior for durable Season player Diet facts."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
from sqlalchemy import create_engine, event

from app.migrations import run_migrations
from app.models.catalogs import PLAY_TYPES, SHOOTING_TYPES
from app.services.player_diet import PlayerDietService


NOW = datetime(2026, 1, 6, 10, 0, tzinfo=timezone.utc)


class FreshAthleteCatalog:
    def __init__(self, *, is_fresh=True):
        self.is_fresh = is_fresh

    def get_freshness(self, season, *, now=None):
        return {"is_fresh": self.is_fresh, "row_count": 1}

    def get_catalog(self, season, *, active_only=False):
        return [{"player_id": 2544, "display_name": "LeBron James"}]


class RecordedNBAPlayerDiets:
    def __init__(self):
        self.calls = []
        self.player_id = 2544
        self.play_share = 0.2
        self.shot_share = 0.25
        self.shot_type_player_id = None
        self.restricted_area_attempts = 40
        self.missing_play_type = None
        self.fail_shot_zones = False
        self.duplicate_play_rows = False

    def fetch_synergy_play_types(self, play_type, **kwargs):
        self.calls.append(("play_type", play_type, kwargs))
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
        return pd.DataFrame([row, row] if self.duplicate_play_rows else [row])

    def fetch_player_shot_type(self, general_range, **kwargs):
        self.calls.append(("shot_type", general_range, kwargs))
        return pd.DataFrame(
            [{
                "PLAYER_ID": self.shot_type_player_id or self.player_id,
                "PLAYER_NAME": "LeBron James",
                "GP": 40,
                "FGA_FREQUENCY": self.shot_share,
                "FGA": 100,
            }]
        )

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
        return pd.DataFrame(
            [[
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
            ]],
            columns=columns,
        )


class RecordedPBPPlayerDiets:
    def __init__(self):
        self.calls = []
        self.arc_assists = 20

    def fetch_player_diet_totals(self, *, season):
        self.calls.append(season)
        return pd.DataFrame(
            [{
                "EntityId": "2544",
                "Name": "LeBron James",
                "GamesPlayed": 40,
                "Assists": 100,
                "Arc3Assists": self.arc_assists,
                "Corner3Assists": 10,
                "AtRimAssists": 30,
                "ShortMidRangeAssists": 25,
                "LongMidRangeAssists": 15,
            }]
        )


def _service(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'player-diets.sqlite3'}")
    run_migrations(engine)
    nba = RecordedNBAPlayerDiets()
    pbp = RecordedPBPPlayerDiets()
    return (
        PlayerDietService(
            engine,
            athlete_catalog=FreshAthleteCatalog(),
            nba_stats_provider=nba,
            pbp_stats_provider=pbp,
            clock=lambda: NOW,
        ),
        nba,
        pbp,
    )


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
    nba.duplicate_play_rows = True
    nba.shot_share = 0.3

    service.refresh("2025-26")

    result = service.get_for_players("2025-26", [2544])
    isolation = next(
        fact
        for fact in result.players[2544]
        if fact.base == "play_types" and fact.slice_key == "Isolation"
    )
    assert isolation.share == 0.2
    observation = {item.base: item for item in result.observations}["play_types"]
    assert (observation.status, observation.unavailable_reason) == (
        "unavailable",
        "provider_invalid_response",
    )
    assert next(
        fact.share
        for fact in result.players[2544]
        if fact.base == "shot_types" and fact.slice_key == "Catch and Shoot"
    ) == 0.3


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
