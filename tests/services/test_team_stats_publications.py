"""Opponent Team Profile categories, served from the Season publications."""

from datetime import datetime, timezone

import pytest

from app.config.settings import RuntimeSettings
from app.domain.nba_teams import NBA_TEAM_ID_TO_TRICODE
from app.domain.team_matchup_taxonomy import (
    NBA_PUBLICATION_TAXONOMY,
    PLAY_TYPES,
)
from app.errors import InvalidInputError
from app.services.database_first_activation import (
    PublicationRead,
    PublicationTeamWindowRow,
)
from app.services.ledger_derivations import (
    ASSIST_DERIVED_METRICS,
    TEAM_METRICS,
)
from app.services.team_filter_rankings import TeamFilterRankingService
from app.services.team_service import TeamService

SEASON = "2025-26"
RETRIEVED_AT = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)
LAKERS = "Los Angeles Lakers"


def _row(team_id, tricode, per48):
    return PublicationTeamWindowRow(
        team_id=team_id,
        team_tricode=tricode,
        game_ids=("0022500001",),
        game_count=1,
        per48=per48,
        league_average={},
        population_sigma={},
        competition_rank={},
    )


def _league(per48_for):
    """Build the canonical thirty rows from one per-team metric builder."""

    return tuple(
        _row(team_id, tricode, per48_for(tricode))
        for team_id, tricode in NBA_TEAM_ID_TO_TRICODE.items()
    )


def _read(stream_key, rows, *, freshness="fresh", status="active"):
    return PublicationRead(
        stream_key=stream_key,
        publication_id="publication-1",
        season=SEASON,
        cutoff=RETRIEVED_AT.isoformat(),
        version=1,
        status=status,
        freshness=freshness,
        age_seconds=0,
        payload={"rows": []},
        retrieved_at=RETRIEVED_AT,
        decoded=tuple(rows),
    )


class _StubReader:
    """One publication generation, recorded so the read seam stays visible."""

    def __init__(self, reads):
        self._reads = reads
        self.calls = []

    def read_many(self, stream_keys, *, season=None):
        keys = tuple(stream_keys)
        self.calls.extend((stream_key, season) for stream_key in keys)
        return {
            stream_key: self._reads.get(
                stream_key,
                PublicationRead(
                    stream_key=stream_key,
                    publication_id=None,
                    season=season,
                    cutoff=None,
                    version=None,
                    status="missing",
                    freshness="missing",
                    age_seconds=None,
                    payload=None,
                ),
            )
            for stream_key in keys
        }


class _StubGovernance:
    """The governed per-team game set an NBA publication must match."""

    def resolve_team_game_ids(self, season, cutoff, *, window, **kwargs):
        return {
            team_id: frozenset({"0022500001"})
            for team_id in NBA_TEAM_ID_TO_TRICODE
        }


def _service(reads):
    """A Team Profile service that can reach nothing but the publications."""

    return TeamService(
        None,
        settings=RuntimeSettings(
            environment="testing", nba={"current_season": SEASON}
        ),
        season_publications=TeamFilterRankingService(
            _StubReader(reads), governance_resolver=_StubGovernance()
        ),
    )


# --- Traditional -----------------------------------------------------------


def _traditional_reads(values=None):
    """LAL is distinct; the other twenty-nine share one baseline row."""

    distinct = values or {
        "points": 120.0,
        "field_goals_made": 45.0,
        "field_goals_attempted": 90.0,
        "three_pointers_made": 15.0,
        "three_pointers_attempted": 40.0,
        "free_throws_attempted": 20.0,
        "rebounds": 44.0,
        "assists": 26.0,
        "turnovers": 14.0,
        "steals": 9.0,
        "blocks": 6.0,
    }

    def per48(tricode):
        baseline = {metric: 2.0 for metric in TEAM_METRICS}
        return {**baseline, **distinct} if tricode == "LAL" else baseline

    return {
        "traditional_opponent_season": _read(
            "traditional_opponent_season", _league(per48)
        )
    }


def test_traditional_serves_the_published_per48_values():
    stats = _service(_traditional_reads()).get_team_stats("Traditional", LAKERS)

    assert stats["OPP_PTS"] == 120.0
    assert stats["OPP_FGM"] == 45.0
    assert stats["OPP_FGA"] == 90.0
    assert stats["OPP_FG3M"] == 15.0
    assert stats["OPP_FG3A"] == 40.0
    assert stats["OPP_FTA"] == 20.0
    assert stats["OPP_REB"] == 44.0
    assert stats["OPP_AST"] == 26.0
    assert stats["OPP_TOV"] == 14.0
    assert stats["OPP_STL"] == 9.0
    assert stats["OPP_BLK"] == 6.0


def test_traditional_derives_the_panel_columns_the_ledger_does_not_publish():
    stats = _service(_traditional_reads()).get_team_stats("Traditional", LAKERS)

    assert stats["OPP_STL+BLK"] == 15.0
    assert stats["OPP_FG_PCT"] == pytest.approx(0.5)
    assert stats["OPP_FG3_PCT"] == pytest.approx(0.375)


def test_traditional_ranks_the_fewest_allowed_first():
    """Rank 1 allows the fewest; the twenty-nine tied teams share a rank."""

    stats = _service(_traditional_reads()).get_team_stats("Traditional", LAKERS)

    # LAL allows the most of everything above, so it ranks last.
    assert stats["OPP_PTS_RANK"] == 30
    assert stats["OPP_STL+BLK_RANK"] == 30
    # LAL allows 45 of 90, a lower rate than the baseline 2 of 2, so the
    # derived percentage ranks first while its counting columns rank last.
    assert stats["OPP_FG_PCT_RANK"] == 1

    baseline = _service(_traditional_reads()).get_team_stats(
        "Traditional", "Boston Celtics"
    )

    assert baseline["OPP_PTS"] == 2.0
    assert baseline["OPP_PTS_RANK"] == 1


def test_traditional_compares_each_column_with_the_league_average():
    stats = _service(_traditional_reads()).get_team_stats("Traditional", LAKERS)

    # Twenty-nine teams at 2.0 and one at 120.0 average 5.933...
    average = (29 * 2.0 + 120.0) / 30
    assert stats["OPP_PTS_vs_avg_pct"] == pytest.approx(
        (120.0 / average - 1) * 100
    )
    assert stats["OPP_FG_PCT_vs_avg_pct"] == pytest.approx(
        (0.5 / ((29 * 1.0 + 0.5) / 30) - 1) * 100
    )


def test_traditional_omits_the_rebound_split_the_publication_does_not_carry():
    stats = _service(_traditional_reads()).get_team_stats("Traditional", LAKERS)

    assert "OPP_OREB" not in stats
    assert "OPP_DREB" not in stats


def test_the_clippers_display_name_the_panel_sends_resolves_to_its_publication():
    def per48(tricode):
        return {
            metric: (99.0 if tricode == "LAC" else 2.0) for metric in TEAM_METRICS
        }

    reads = {
        "traditional_opponent_season": _read(
            "traditional_opponent_season", _league(per48)
        )
    }

    for display_name in ("Los Angeles Clippers", "LA Clippers"):
        stats = _service(reads).get_team_stats("Traditional", display_name)

        assert stats["OPP_PTS"] == 99.0


# --- Playtypes -------------------------------------------------------------


def _play_type_reads(values):
    """Build play-type rows from ``{tricode: (points, possessions)}``."""

    def per48(tricode):
        points, possessions = values.get(tricode, (10.0, 20.0))
        metrics = {key: 1.0 for key in NBA_PUBLICATION_TAXONOMY["play_types"]}
        metrics["Transition_PTS"] = points
        metrics["Transition_POSS"] = possessions
        return metrics

    return {
        "synergy_play_types_opponent_season": _read(
            "synergy_play_types_opponent_season", _league(per48)
        )
    }


def test_playtypes_serve_points_per_possession_against_the_league_average():
    """The panel's chart is centred on 1.0, so the value is a ratio."""

    stats = _service(
        _play_type_reads({"LAL": (22.0, 20.0)})
    ).get_team_stats("Playtypes", LAKERS)

    # LAL allows 1.1 per possession; the other twenty-nine allow 0.5.
    league_average = (1.1 + 29 * 0.5) / 30
    assert stats["Transition"] == pytest.approx(1.1 / league_average)
    assert set(stats) == {
        *PLAY_TYPES, *(f"{play_type}_RANK" for play_type in PLAY_TYPES)
    }


def test_playtypes_rank_the_rate_rather_than_the_raw_points():
    """GSW allows more transition points; LAL allows more per possession."""

    reads = _play_type_reads({"LAL": (22.0, 20.0), "GSW": (30.0, 30.0)})

    lakers = _service(reads).get_team_stats("Playtypes", LAKERS)
    warriors = _service(reads).get_team_stats("Playtypes", "Golden State Warriors")

    assert lakers["Transition_RANK"] == 30
    assert warriors["Transition_RANK"] == 29


def test_playtypes_carry_no_versus_average_percentage():
    stats = _service(_play_type_reads({})).get_team_stats("Playtypes", LAKERS)

    assert not [key for key in stats if key.endswith("_vs_avg_pct")]


# --- Assists ---------------------------------------------------------------


def _assist_reads(values):
    def per48(tricode):
        team_values = values.get(tricode, {})
        return {
            metric: team_values.get(metric, 1.0)
            for metric in ASSIST_DERIVED_METRICS
        }

    return {
        "assist_locations_season": _read("assist_locations_season", _league(per48))
    }


def test_assists_serve_every_location_as_a_ratio_to_the_league_average():
    reads = _assist_reads({
        "LAL": {"assists": 30.0, "two_point_assists": 18.0, "corner3_assists": 4.0}
    })

    stats = _service(reads).get_team_stats("Assists", LAKERS)

    assert stats["Assists"] == pytest.approx(30.0 / ((30.0 + 29 * 1.0) / 30))
    assert stats["TwoPtAssists"] == pytest.approx(
        18.0 / ((18.0 + 29 * 1.0) / 30)
    )
    assert stats["Corner3Assists"] == pytest.approx(
        4.0 / ((4.0 + 29 * 1.0) / 30)
    )
    # A team at the league average sits exactly on the chart's centre.
    assert _service(reads).get_team_stats("Assists", "Boston Celtics")[
        "Corner3Assists"
    ] == pytest.approx(1.0 / ((4.0 + 29 * 1.0) / 30))


def test_assist_points_are_derived_from_the_two_and_three_point_locations():
    reads = _assist_reads({
        "LAL": {"two_point_assists": 10.0, "three_point_assists": 8.0},
        "GSW": {"two_point_assists": 20.0, "three_point_assists": 1.0},
    })

    lakers = _service(reads).get_team_stats("Assists", LAKERS)
    warriors = _service(reads).get_team_stats("Assists", "Golden State Warriors")

    # LAL 2*10 + 3*8 = 44 beats GSW 2*20 + 3*1 = 43 on assisted points while
    # GSW allows more two-point assists, so the column is not a relabelled one.
    assert lakers["AssistPoints_RANK"] == 30
    assert warriors["AssistPoints_RANK"] == 29
    assert lakers["AssistPoints"] == pytest.approx(
        44.0 / ((44.0 + 43.0 + 28 * 5.0) / 30)
    )


def test_assists_expose_only_the_panel_fields_and_their_ranks():
    fields = (
        "Assists",
        "TwoPtAssists",
        "ThreePtAssists",
        "Arc3Assists",
        "Corner3Assists",
        "AtRimAssists",
        "ShortMidRangeAssists",
        "LongMidRangeAssists",
        "AssistPoints",
    )

    stats = _service(_assist_reads({})).get_team_stats("Assists", LAKERS)

    assert set(stats) == {*fields, *(f"{field}_RANK" for field in fields)}


# --- Zone Shooting ---------------------------------------------------------


def _shot_zone_reads(values):
    def per48(tricode):
        metrics = {key: 2.0 for key in NBA_PUBLICATION_TAXONOMY["shot_zones"]}
        metrics.update(values.get(tricode, {}))
        return metrics

    return {
        "exact_shot_zones_opponent_season": _read(
            "exact_shot_zones_opponent_season", _league(per48)
        )
    }


def test_zone_shooting_renames_each_published_zone_to_its_opponent_column():
    reads = _shot_zone_reads({
        "LAL": {"Restricted Area_FGM": 12.0, "Restricted Area_FGA": 20.0}
    })

    stats = _service(reads).get_team_stats("Zone Shooting", LAKERS)

    assert stats["Restricted Area_OPP_FGM"] == 12.0
    assert stats["Restricted Area_OPP_FGA"] == 20.0
    assert stats["Restricted Area_OPP_FGM_RANK"] == 30
    assert stats["Corner 3_OPP_FGA"] == 2.0
    assert stats["Restricted Area_OPP_FGM_vs_avg_pct"] == pytest.approx(
        (12.0 / ((12.0 + 29 * 2.0) / 30) - 1) * 100
    )
    assert "Restricted Area_FGM" not in stats


# --- Shooting Type ---------------------------------------------------------


def _shot_type_reads(values):
    def per48(tricode):
        metrics = {key: 1.0 for key in NBA_PUBLICATION_TAXONOMY["shot_types"]}
        metrics.update(values.get(tricode, {}))
        return metrics

    return {
        "grouped_shot_types_opponent_season": _read(
            "grouped_shot_types_opponent_season", _league(per48)
        )
    }


def test_shooting_type_returns_one_labelled_row_per_published_slice():
    reads = _shot_type_reads({
        "LAL": {"catch_and_shoot_FG2M": 4.0, "catch_and_shoot_FG3M": 9.0}
    })

    profile = _service(reads).get_team_stats("Shooting Type", LAKERS)

    assert [row["ShootingType"] for row in profile] == [
        "Catch and Shoot", "Pullups", "Less Than 10 ft"
    ]
    catch_and_shoot = profile[0]
    assert catch_and_shoot["FG2M"] == 4.0
    assert catch_and_shoot["FG3M"] == 9.0
    assert catch_and_shoot["FG2A"] == 1.0
    assert catch_and_shoot["FG3A"] == 1.0
    # 2 * 4 + 3 * 9 = 35, against 2 * 1 + 3 * 1 = 5 everywhere else.
    assert catch_and_shoot["PTS"] == 35.0
    assert catch_and_shoot["PTS_RANK"] == 30
    assert catch_and_shoot["PTS_vs_avg_pct"] == pytest.approx(
        (35.0 / ((35.0 + 29 * 5.0) / 30) - 1) * 100
    )
    assert set(catch_and_shoot) == {
        "ShootingType",
        *(
            f"{stat}{suffix}"
            for stat in ("PTS", "FG2M", "FG2A", "FG3M", "FG3A")
            for suffix in ("", "_RANK", "_vs_avg_pct")
        ),
    }


# --- Contract-wide behaviour -----------------------------------------------


def test_a_date_is_accepted_and_ignored():
    """Rankings are always whole-season; no provider can be reached."""

    reads = _traditional_reads()

    assert _service(reads).get_team_stats(
        "Traditional", LAKERS, "2026-01-05"
    ) == _service(reads).get_team_stats("Traditional", LAKERS)


def test_an_unknown_category_is_rejected_as_invalid_input():
    with pytest.raises(InvalidInputError):
        _service(_traditional_reads()).get_team_stats("Rebounding", LAKERS)


def test_a_stale_publication_still_serves_its_last_good_values():
    reads = {
        "traditional_opponent_season": _read(
            "traditional_opponent_season",
            _traditional_reads()["traditional_opponent_season"].decoded,
            freshness="stale",
            status="stale",
        )
    }

    assert _service(reads).get_team_stats("Traditional", LAKERS)["OPP_PTS"] == 120.0


def test_no_publication_serves_nothing_rather_than_a_partial_answer():
    empty = _service({})

    assert empty.get_team_stats("Traditional", LAKERS) == {}
    assert empty.get_team_stats("Playtypes", LAKERS) == {}
    assert empty.get_team_stats("Assists", LAKERS) == {}
    assert empty.get_team_stats("Zone Shooting", LAKERS) == {}
    assert empty.get_team_stats("Shooting Type", LAKERS) == []


def test_a_deployment_without_publications_serves_nothing():
    """The demo database carries no publication tables (the #198 precedent)."""

    demo = TeamService(
        None,
        settings=RuntimeSettings(
            environment="testing", nba={"current_season": SEASON}
        ),
        season_publications=None,
    )

    assert demo.get_team_stats("Traditional", LAKERS) == {}
    assert demo.get_team_stats("Shooting Type", LAKERS) == []


def test_a_team_outside_the_catalog_serves_nothing():
    assert _service(_traditional_reads()).get_team_stats(
        "Traditional", "Seattle SuperSonics"
    ) == {}


def test_each_category_reads_only_its_own_season_stream():
    service = _service({})
    reader = service.publications.publication_reader

    for category in (
        "Traditional", "Playtypes", "Assists", "Zone Shooting", "Shooting Type"
    ):
        service.get_team_stats(category, LAKERS)

    assert {season for _stream, season in reader.calls} == {SEASON}
    assert {stream for stream, _season in reader.calls} == {
        "traditional_opponent_season",
        "synergy_play_types_opponent_season",
        "assist_locations_season",
        "exact_shot_zones_opponent_season",
        "grouped_shot_types_opponent_season",
    }


def test_the_service_holds_no_provider_client():
    assert not hasattr(_service({}), "nba_stats")
