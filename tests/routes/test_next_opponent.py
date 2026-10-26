"""Next-opponent HTTP reads and the filter tier applied to real game logs."""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.config.settings import RuntimeSettings
from app.domain.nba_teams import NBA_TEAM_ID_TO_TRICODE
from app.services.database_first_activation import PublicationReadSnapshot
from app.services.game_service import GameService
from app.services.next_opponent import NextOpponentService
from app.services.team_filter_rankings import TeamFilterRankingService
from tests.routes.test_team_stats import _seeded_reads
from tests.support.publication_stubs import StubGovernance, StubReader
from tests.test_game_logs import _game_logs_frame


class Reader(StubReader):
    def snapshot(self, keys, *, season, **kwargs):
        return PublicationReadSnapshot(season, self.read_many(keys, season=season), ())


class Catalog:
    def get_catalog(self, season, *, active_only=False):
        return [
            dict(
                player_id=1,
                display_name="LeBron James",
                is_active_for_season=True,
                team_id=1610612738,
            )
        ]


class Logs:
    def get_player_logs(self, player_id, season, **kwargs):
        return _game_logs_frame().head(1).copy()


@pytest.fixture
def setup(dependencies, client, mock_db_engine):
    reads = _seeded_reads()
    reader = Reader(reads)
    rankings = TeamFilterRankingService(reader, governance_resolver=StubGovernance())
    settings = RuntimeSettings(
        environment="testing",
        nba={"current_season": "2025-26"},
        cache={"enabled": False},
    )
    game = GameService(
        mock_db_engine,
        redis_client=SimpleNamespace(),
        settings=settings,
        athlete_catalog=Catalog(),
        game_logs_source=Logs(),
        team_filter_rankings=rankings,
        publication_reader=reader,
    )
    event = dict(
        nba_game_id="0022500500",
        scheduled_at="2026-01-20T00:30:00+00:00",
        status_code=1,
        home_team=dict(id=1610612747, tricode="LAL", name="Los Angeles Lakers"),
        away_team=dict(id=1610612738, tricode="BOS", name="Boston Celtics"),
    )
    events = [event]
    service = NextOpponentService(
        game,
        SimpleNamespace(get_events=lambda season: events),
        rankings,
        clock=lambda: datetime(2026, 1, 19, tzinfo=UTC),
    )
    dependencies.next_opponent_service = service
    dependencies.game_service = game
    return client, reads, events


def get(client):
    return client.get("/api/players/next-opponent?player_name=LeBron+James")


def distinct_metrics(reads):
    """Scale each metric by its own factor so no two columns repeat each other."""
    for key, stream in reads.items():
        metrics = sorted(stream.decoded[0].per48)
        reads[key] = replace(
            stream,
            decoded=tuple(
                replace(
                    row,
                    per48={
                        metric: value * (1 + (metrics.index(metric) + 1) / 100)
                        for metric, value in row.per48.items()
                    },
                )
                for row in stream.decoded
            ),
        )


def test_next_game_and_all_categories(setup):
    client, reads, _ = setup
    distinct_metrics(reads)
    response = get(client)
    assert response.status_code == 200
    body = response.get_json()
    assert body["next_game"] == dict(
        game_id="0022500500",
        date="2026-01-19",
        opponent="LAL",
        opponent_name="Los Angeles Lakers",
        home=False,
    )
    assert {row["group"] for row in body["opponent_ranks"]} == {
        "General",
        "Play type",
        "Assists",
        "Zones",
        "Shot type",
    }
    points = next(row for row in body["opponent_ranks"] if row["team_filter"] == "OPP_PTS")
    assert points["value"] == pytest.approx(184.8)
    assert points["most_rank"] == 1
    assert points["ranked_teams"] == 30
    assert {row["team_filter"] for row in body["opponent_ranks"] if row["team_filter"] is not None} == {
        "OPP_PTS",
        "OPP_REB",
        "OPP_AST",
        "OPP_STOCKS",
        "OPP_FTA",
        "OPP_TOV",
        "OPP_BLK",
        "OPP_STL",
        "OPP_FG3M",
        "OPP_FG3A",
        "C&S PTS",
        "PU PTS",
        "Less Than 10 ft",
        "sheet:play_types:PRBallHandler:PTS",
        "sheet:play_types:PRRollMan:PTS",
        "sheet:play_types:Transition:PTS",
        "sheet:play_types:Isolation:PTS",
        "sheet:play_types:Spotup:PTS",
        "sheet:play_types:Cut:PTS",
        "sheet:play_types:Handoff:PTS",
        "sheet:play_types:OffScreen:PTS",
        "sheet:play_types:Postup:PTS",
        "sheet:play_types:OffRebound:PTS",
        "sheet:play_types:Misc:PTS",
        "TwoPtAssists",
        "ThreePtAssists",
        "Arc3Assists",
        "Corner3Assists",
        "AtRimAssists",
        "ShortMidRangeAssists",
        "LongMidRangeAssists",
    }
    labels = {
        row["team_filter"]: row["label"] for row in body["opponent_ranks"] if row["team_filter"] is not None
    }
    assert labels["C&S PTS"] == "C&S points"
    assert labels["PU PTS"] == "Pull-up points"
    assert labels["sheet:play_types:PRBallHandler:PTS"] == "P&R ball-handler points"
    assert labels["sheet:play_types:OffRebound:PTS"] == "Putback points"
    assert labels["Corner3Assists"] == "Corner-3 assists"


@pytest.mark.parametrize("direction", ["most", "fewest", "tie"])
def test_returned_tiers_include_opponent_through_game_log(setup, direction):
    client, reads, _ = setup
    distinct_metrics(reads)
    if direction in ("fewest", "tie"):
        key = "traditional_opponent_season"
        codes = sorted(NBA_TEAM_ID_TO_TRICODE.values())
        rows = []
        for row in reads[key].decoded:
            value = 1 if direction == "fewest" and row.team_tricode == "LAL" else 112
            if direction == "tie":
                value = (
                    200
                    if row.team_tricode in [code for code in codes if code != "LAL"][:7]
                    else (50 if row.team_tricode < "LAL" else 100)
                )
            rows.append(replace(row, per48={**row.per48, "points": value}))
        reads[key] = replace(reads[key], decoded=tuple(rows))
    body = get(client).get_json()
    points = next(row for row in body["opponent_ranks"] if row["team_filter"] == "OPP_PTS")
    if direction == "fewest":
        assert points["most_rank"] == 30
    if direction == "tie":
        assert points["most_rank"] == 8
    for row in body["opponent_ranks"]:
        if not row["team_filter"] or not (
            row["most_rank"] <= 8 or row["most_rank"] > row["ranked_teams"] - 8
        ):
            continue
        low, high = (1, 8) if row["most_rank"] <= 8 else (row["ranked_teams"] - 7, row["ranked_teams"])
        response = client.get(
            "/api/games/game_logs",
            query_string={
                "player_name": "LeBron James",
                "season_filter": "2025-26",
                "teams_against[]": row["team_filter"],
                "rank_filter[]": f"{low},{high}",
            },
        )
        assert response.status_code == 200, response.get_json()
        assert [game["MATCHUP"] for game in response.get_json()["game_logs"]] == ["BOS vs. LAL"]
        assert response.get_json()["next_game"] is None


def test_no_next_game(setup):
    client, _, events = setup
    events.clear()
    assert get(client).get_json() == {"next_game": None, "opponent_ranks": []}


def test_unknown_player(setup):
    client, _, _ = setup
    response = client.get("/api/players/next-opponent?player_name=Nobody")
    assert response.status_code == 404
    assert response.get_json()["error"]["message"] == "The requested player was not found."


@pytest.mark.parametrize(
    "query",
    ["", "?player_name=", "?player_name=A&player_name=B", "?player_name=A&extra=x"],
)
def test_invalid_request(setup, query):
    response = setup[0].get("/api/players/next-opponent" + query)
    assert response.status_code == 400


def test_requires_authentication(setup, monkeypatch):
    monkeypatch.setattr("app.utils.auth.get_firebase_app", lambda: object())
    response = get(setup[0])
    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "authentication_required"


def test_next_game_ignores_past_finished_and_postponed_events(setup):
    client, _, events = setup
    original = events[0]
    events.extend(
        [
            {
                **original,
                "nba_game_id": "0022500400",
                "scheduled_at": "2026-01-18T00:30:00+00:00",
            },
            {**original, "nba_game_id": "0022500401", "status_code": 3},
            {
                **original,
                "nba_game_id": "0022500402",
                "postponement_evidence": {"source": "NBA"},
            },
            {
                **original,
                "nba_game_id": "0032500403",
                "scheduled_at": "2026-01-19T12:00:00+00:00",
            },
            {
                **original,
                "nba_game_id": "0022500600",
                "scheduled_at": "2026-01-21T00:30:00+00:00",
            },
        ]
    )
    assert get(client).get_json()["next_game"]["game_id"] == "0022500500"


def test_home_game(setup):
    client, _, events = setup
    events[0]["away_team"], events[0]["home_team"] = (
        events[0]["home_team"],
        events[0]["away_team"],
    )
    assert get(client).get_json()["next_game"]["home"] is True


def test_exhibition_opponent_preserves_next_game_without_nba_ranks(setup):
    client, _, events = setup
    events[0]["nba_game_id"] = "0012500500"
    events[0]["home_team"] = dict(id=1610619999, tricode="INT", name="International Select")
    response = get(client)
    assert response.status_code == 200
    assert response.get_json() == {
        "next_game": dict(
            game_id="0012500500",
            date="2026-01-19",
            opponent="INT",
            opponent_name="International Select",
            home=False,
        ),
        "opponent_ranks": [],
    }


def test_profile_ratios_percentages_and_units(setup):
    client, reads, _ = setup
    distinct_metrics(reads)
    key = "synergy_play_types_opponent_season"
    reads[key] = replace(
        reads[key],
        decoded=tuple(
            replace(row, per48={**row.per48, "Transition_PTS": 9.0 if row.team_tricode == "LAL" else 3.0})
            for row in reads[key].decoded
        ),
    )
    key = "traditional_opponent_season"
    reads[key] = replace(
        reads[key],
        decoded=tuple(
            replace(row, per48={**row.per48, "field_goals_made": 66.0, "field_goals_attempted": 132.0})
            if row.team_tricode == "LAL"
            else row
            for row in reads[key].decoded
        ),
    )
    response = get(client)
    assert response.status_code == 200
    rows = {row["team_filter"]: row for row in response.get_json()["opponent_ranks"] if row["team_filter"]}
    transition = rows["sheet:play_types:Transition:PTS"]
    assert transition["label"] == "Transition points"
    assert transition["value"] == 9.0
    assert transition["vs_league_pct"] == pytest.approx(181.25)
    assert transition["unit"] == "count"
    assert rows["AtRimAssists"]["value"] == pytest.approx(1.4754098360655739)
    assert rows["AtRimAssists"]["vs_league_pct"] == pytest.approx(47.54098360655738)
    assert rows["AtRimAssists"]["unit"] == "league_ratio"
    assert rows["OPP_PTS"]["unit"] == "count"
    fg_pct = next(row for row in response.get_json()["opponent_ranks"] if row["label"] == "FG%")
    assert fg_pct["value"] == 0.5
    assert fg_pct["unit"] == "percent"


@pytest.mark.parametrize("tied", [0.0, 4.0])
def test_a_column_every_team_ties_on_is_not_ranked(setup, tied):
    client, reads, _ = setup
    distinct_metrics(reads)
    key = "grouped_shot_types_opponent_season"
    reads[key] = replace(
        reads[key],
        decoded=tuple(
            replace(row, per48={**row.per48, "less_than_10_ft_FG3M": tied, "less_than_10_ft_FG3A": tied})
            for row in reads[key].decoded
        ),
    )
    labels = {row["label"] for row in get(client).get_json()["opponent_ranks"]}
    assert "Inside 10 ft 3s" not in labels
    assert "Inside 10 ft 3PA" not in labels
    assert "Inside 10 ft 2s" in labels


MARKETS = {"PTS", "REB", "AST", "FG3M", "FTA", "FTM", "STL", "BLK", "TOV"}


def test_every_metric_has_a_family_and_markets(setup):
    client, reads, _ = setup
    distinct_metrics(reads)
    rows = get(client).get_json()["opponent_ranks"]
    assert len(rows) == 58
    for row in rows:
        assert row["family"], row["label"]
        assert row["markets"] and set(row["markets"]) <= MARKETS, row["label"]


def test_rows_name_their_family_and_markets(setup):
    client, reads, _ = setup
    distinct_metrics(reads)
    rows = {row["label"]: (row["family"], row["markets"]) for row in get(client).get_json()["opponent_ranks"]}
    assert {
        label: rows[label]
        for label in (
            "Points",
            "Off. rebounds",
            "Putback points",
            "Assist points",
            "Short mid assists",
            "Corner-3 assists",
            "Steals",
            "Blocks",
            "Steals + blocks",
            "Turnovers forced",
            "Free throw attempts",
            "FG%",
            "3PT attempts",
            "Restricted Area makes",
            "In The Paint (Non-RA) attempts",
            "Inside 10 ft attempts",
            "Inside 10 ft 3PA",
            "Mid-Range attempts",
            "Corner 3 makes",
            "C&S attempts",
            "Pull-up 3PA",
            "Pull-up points",
            "Isolation points",
        )
    } == {
        "Points": ("points", ["PTS"]),
        "Off. rebounds": ("rebounds", ["REB"]),
        "Putback points": ("rebounds", ["PTS", "REB"]),
        "Assist points": ("assists", ["AST"]),
        "Short mid assists": ("2pt-assists", ["AST"]),
        "Corner-3 assists": ("3pt-assists", ["AST"]),
        "Steals": ("steals-blocks", ["STL"]),
        "Blocks": ("steals-blocks", ["BLK"]),
        "Steals + blocks": ("steals-blocks", ["STL", "BLK"]),
        "Turnovers forced": ("turnovers", ["TOV"]),
        "Free throw attempts": ("free-throws", ["FTA", "FTM", "PTS"]),
        "FG%": ("field-goals", ["PTS"]),
        "3PT attempts": ("threes", ["FG3M", "PTS"]),
        "Restricted Area makes": ("inside", ["PTS"]),
        "In The Paint (Non-RA) attempts": ("inside", ["PTS"]),
        "Inside 10 ft attempts": ("inside", ["PTS"]),
        "Inside 10 ft 3PA": ("inside", ["PTS"]),
        "Mid-Range attempts": ("zone:Mid-Range", ["PTS"]),
        "Corner 3 makes": ("zone:Corner 3", ["PTS", "FG3M"]),
        "C&S attempts": ("shot-type:catch_and_shoot", ["PTS"]),
        "Pull-up 3PA": ("shot-type:pullups", ["PTS", "FG3M"]),
        "Pull-up points": ("shot-type:pullups", ["PTS"]),
        "Isolation points": ("play-type:Isolation", ["PTS"]),
    }


def test_a_column_repeating_an_emitted_one_is_dropped(setup):
    client, reads, _ = setup
    distinct_metrics(reads)
    key = "grouped_shot_types_opponent_season"
    reads[key] = replace(
        reads[key],
        decoded=tuple(
            replace(row, per48={**row.per48, "less_than_10_ft_FG3M": 0.0, "less_than_10_ft_FG3A": 0.0})
            for row in reads[key].decoded
        ),
    )
    labels = [
        row["label"] for row in get(client).get_json()["opponent_ranks"] if row["label"].startswith("Inside")
    ]
    assert labels == ["Inside 10 ft 2s", "Inside 10 ft 2PA", "Inside 10 ft points"]


def test_of_two_copies_the_filterable_one_is_kept(setup):
    client, reads, _ = setup
    distinct_metrics(reads)
    zones, shots = "exact_shot_zones_opponent_season", "grouped_shot_types_opponent_season"
    copy = {row.team_tricode: row.per48["less_than_10_ft_FG2M"] for row in reads[shots].decoded}
    reads[zones] = replace(
        reads[zones],
        decoded=tuple(
            replace(row, per48={**row.per48, "Restricted Area_FGM": copy[row.team_tricode]})
            for row in reads[zones].decoded
        ),
    )
    labels = {row["label"]: row["team_filter"] for row in get(client).get_json()["opponent_ranks"]}
    assert "Restricted Area makes" not in labels
    assert labels["Inside 10 ft 2s"] == "Less Than 10 ft"


def test_only_the_chosen_catch_and_shoot_and_pull_up_rows_are_listed(setup):
    client, reads, _ = setup
    distinct_metrics(reads)
    labels = sorted(
        row["label"]
        for row in get(client).get_json()["opponent_ranks"]
        if row["label"].startswith(("C&S", "Pull-up"))
    )
    assert labels == [
        "C&S attempts",
        "C&S points",
        "Pull-up 2PA",
        "Pull-up 3PA",
        "Pull-up attempts",
        "Pull-up points",
    ]


def test_a_hidden_row_never_displaces_a_listed_copy(setup):
    client, reads, _ = setup
    distinct_metrics(reads)
    key = "grouped_shot_types_opponent_season"
    reads[key] = replace(
        reads[key],
        decoded=tuple(
            replace(row, per48={**row.per48, "catch_and_shoot_FG2A": 0.0}) for row in reads[key].decoded
        ),
    )
    labels = {row["label"] for row in get(client).get_json()["opponent_ranks"]}
    assert "C&S attempts" in labels
    assert "C&S 3PA" not in labels
