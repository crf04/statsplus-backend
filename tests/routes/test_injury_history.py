"""One player's stored injury statuses over a date range: ``/api/injuries?player=`` (crf04/statsplus#494).

Stored reports are real rows in a migrated SQLite database; only the Event
Catalog and the Athlete Catalog are replaced.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine

from app.config.settings import FeatureSettings, RuntimeSettings
from app.migrations import run_migrations
from app.services.injury_listing import StoredInjuryListing
from app.services.injury_snapshot_repository import InjurySnapshotRepository, InjurySnapshotScope
from tests.support.slate_catalog import NoNeighbourEvents

JACKSON, GOODWIN, OTHER_LAC = 1628402, 1630692, 1627000
WILLIAMS_OKC, WILLIAMS_PHX = 1631114, 1641000
SEASON = "2026-27"
#: 3pm Eastern on 2026-11-10.
NOW = datetime(2026, 11, 10, 20, 0, tzinfo=UTC)


def _event(game_id, away, home, tip, season=SEASON):
    return {
        "nba_game_id": game_id,
        "season": season,
        "scheduled_at": tip,
        "classification": "Regular Season",
        "away_team": {"id": None, "tricode": away, "name": None},
        "home_team": {"id": None, "tricode": home, "name": None},
    }


# Tip times are UTC: a West Coast evening tip is the next UTC day but the
# game's date is its US Eastern date.
GAMES = [
    _event("0022600101", "DEN", "LAC", "2026-11-03T03:30:00+00:00"),  # ET 11-02
    _event("0022600105", "SAC", "LAL", "2026-11-04T03:00:00+00:00"),  # ET 11-03
    _event("0022600115", "LAC", "PHX", "2026-11-05T02:00:00+00:00"),  # ET 11-04, no report stored
    _event("0022600130", "MIA", "LAC", "2026-11-07T03:30:00+00:00"),  # ET 11-06
    _event("0022600140", "UTA", "PHX", "2026-11-08T02:00:00+00:00"),  # ET 11-07
    _event("0022600150", "LAC", "DEN", "2026-11-09T02:00:00+00:00"),  # ET 11-08
    _event("0022600160", "PHX", "GSW", "2026-11-10T03:00:00+00:00"),  # ET 11-09
    _event("0022600170", "BOS", "LAC", "2026-11-11T03:30:00+00:00"),  # ET 11-10
    _event("0022500900", "LAC", "SAS", "2026-03-01T01:00:00+00:00", season="2025-26"),
]


class Events(NoNeighbourEvents):
    def count_events(self, season):
        return sum(event["season"] == season for event in GAMES)

    def get_events_between(self, season, starts_at, ends_at):
        return [
            event
            for event in GAMES
            if event["season"] == season
            and starts_at <= datetime.fromisoformat(event["scheduled_at"]) < ends_at
        ]


class AthleteCatalog:
    def get_catalog(self, season, *, active_only=False):
        return [
            {"player_id": JACKSON, "display_name": "Isaiah Jackson", "team_abbreviation": "LAC"},
            {"player_id": GOODWIN, "display_name": "Jordan Goodwin", "team_abbreviation": "PHX"},
            {"player_id": OTHER_LAC, "display_name": "Other Clipper", "team_abbreviation": "LAC"},
            {"player_id": WILLIAMS_OKC, "display_name": "Jalen Williams", "team_abbreviation": "OKC"},
            {"player_id": WILLIAMS_PHX, "display_name": "Jalen Williams", "team_abbreviation": "PHX"},
        ]


def _row(player_id, name, team, status, reason, game_date, matchup, report_time):
    return {
        "canonical_player_id": player_id,
        "source_player_name": name,
        "tricode": team,
        "canonical_status": status,
        "raw_status": status,
        "reason": reason,
        "game_date": game_date,
        "matchup": matchup,
        "report_time": report_time,
    }


def _store(repository, game_id, *rows, season=SEASON):
    repository.store_backfilled_report(
        source="nba_official",
        raw_payload=[],
        source_entries=[],
        retrieved_at=NOW,
        games=[(InjurySnapshotScope(season, game_id), list(rows), 0)],
    )


@pytest.fixture
def repository(tmp_path, dependencies):
    engine = create_engine(f"sqlite:///{tmp_path / 'history.sqlite3'}")
    run_migrations(engine)
    repository = InjurySnapshotRepository(engine)
    dependencies.injury_listing_service = StoredInjuryListing(
        event_catalog=Events(),
        snapshot_repository=repository,
        athlete_catalog=AthleteCatalog(),
        settings=RuntimeSettings(environment="testing", features=FeatureSettings(injury_report_enabled=True)),
        enabled=True,
        clock=lambda: NOW,
    )
    return repository


@pytest.fixture
def stored(repository):
    _store(
        repository,
        "0022600101",
        _row(
            JACKSON,
            "Isaiah Jackson",
            "LAC",
            "Questionable",
            "Right ankle sprain",
            "2026-11-02",
            "DEN@LAC",
            "2026-11-02T17:30:00-04:00",
        ),
    )
    _store(
        repository,
        "0022600105",
        _row(
            GOODWIN,
            "Jordan Goodwin",
            "LAL",
            "Out",
            "Not With Team",
            "2026-11-03",
            "SAC@LAL",
            "2026-11-03T17:30:00-05:00",
        ),
    )
    # LAC's 11-06 report lists another Clipper, and an unmatched name (null id) never stands for him.
    _store(
        repository,
        "0022600130",
        _row(
            OTHER_LAC,
            "Other Clipper",
            "LAC",
            "Out",
            "Rest",
            "2026-11-06",
            "MIA@LAC",
            "2026-11-06T17:30:00-05:00",
        ),
        _row(
            None,
            "Isaiah Jackson",
            "LAC",
            "Out",
            "Illness",
            "2026-11-06",
            "MIA@LAC",
            "2026-11-06T17:30:00-05:00",
        ),
    )
    _store(
        repository,
        "0022600140",
        _row(
            GOODWIN,
            "Jordan Goodwin",
            "PHX",
            "Out",
            "Left calf strain",
            "2026-11-07",
            "UTA@PHX",
            "2026-11-07T17:30:00-05:00",
        ),
    )
    # Two listings of him for the 11-08 game: the later report time wins.  The
    # row printed for the teams' next game (another date) does not describe this one.
    _store(
        repository,
        "0022600150",
        _row(
            JACKSON,
            "Isaiah Jackson",
            "LAC",
            "Questionable",
            "Right ankle sprain",
            "2026-11-08",
            "LAC@DEN",
            "2026-11-08T13:30:00-05:00",
        ),
        _row(
            JACKSON,
            "Isaiah Jackson",
            "LAC",
            "Out",
            "Right ankle sprain",
            "2026-11-08",
            "LAC@DEN",
            "2026-11-08T17:30:00-05:00",
        ),
        _row(
            JACKSON,
            "Isaiah Jackson",
            "LAC",
            "Doubtful",
            "Right ankle sprain",
            "2026-11-10",
            "BOS@LAC",
            "2026-11-08T17:30:00-05:00",
        ),
    )
    _store(
        repository,
        "0022600160",
        _row(
            WILLIAMS_PHX,
            "Jalen Williams",
            "PHX",
            "Probable",
            "Rest",
            "2026-11-09",
            "PHX@GSW",
            "2026-11-09T17:30:00-05:00",
        ),
    )
    # Evidence stored before game_date existed carries an empty one.
    _store(
        repository,
        "0022600170",
        _row(JACKSON, "Isaiah Jackson", "LAC", "Available", "", "", "", "2026-11-10T13:30:00-05:00"),
    )
    return repository


def _get(client, authenticate, query):
    return client.get(f"/api/injuries?{query}", headers=authenticate())


def test_a_players_stored_statuses_by_eastern_game_date_oldest_first(client, authenticate, stored):
    response = _get(client, authenticate, "player=Isaiah%20Jackson&from_date=2026-11-01&to_date=2026-11-09")

    assert response.status_code == 200
    assert response.get_json() == {
        "player": {"player_id": JACKSON, "name": "Isaiah Jackson", "team": "LAC"},
        "season": "2026-27",
        "from_date": "2026-11-01",
        "to_date": "2026-11-09",
        "report_coverage": [{"team": "LAC", "team_games": 4, "games_with_report": 3}],
        "statuses": [
            {
                "game_date": "2026-11-02",
                "game_id": "0022600101",
                "matchup": "LAC vs. DEN",
                "team": "LAC",
                "status": "Questionable",
                "reason": "Right ankle sprain",
                "report_time": "2026-11-02T17:30:00-04:00",
            },
            {
                "game_date": "2026-11-08",
                "game_id": "0022600150",
                "matchup": "LAC @ DEN",
                "team": "LAC",
                "status": "Out",
                "reason": "Right ankle sprain",
                "report_time": "2026-11-08T17:30:00-05:00",
            },
        ],
    }


def test_a_player_id_names_him_and_to_date_defaults_to_the_eastern_today(client, authenticate, stored):
    body = _get(client, authenticate, f"player={JACKSON}&from_date=2026-11-07").get_json()

    assert body["to_date"] == "2026-11-10"
    assert body["report_coverage"] == [{"team": "LAC", "team_games": 2, "games_with_report": 2}]
    assert [(row["game_date"], row["status"], row["reason"]) for row in body["statuses"]] == [
        ("2026-11-08", "Out", "Right ankle sprain"),
        ("2026-11-10", "Available", ""),
    ]


def test_a_traded_player_lists_both_teams_and_counts_each_teams_reports(client, authenticate, stored):
    body = _get(
        client, authenticate, "player=jordan%20goodwin&from_date=2026-11-01&to_date=2026-11-10"
    ).get_json()

    assert body["player"] == {"player_id": GOODWIN, "name": "Jordan Goodwin", "team": "PHX"}
    assert [(row["game_date"], row["team"], row["matchup"], row["reason"]) for row in body["statuses"]] == [
        ("2026-11-03", "LAL", "LAL vs. SAC", "Not With Team"),
        ("2026-11-07", "PHX", "PHX vs. UTA", "Left calf strain"),
    ]
    assert body["report_coverage"] == [
        {"team": "LAL", "team_games": 1, "games_with_report": 1},
        {"team": "PHX", "team_games": 3, "games_with_report": 2},
    ]


def test_a_player_on_no_report_in_the_range_lists_nothing_but_counts_his_teams_reports(
    client, authenticate, stored
):
    body = _get(
        client, authenticate, "player=Other%20Clipper&from_date=2026-11-07&to_date=2026-11-09"
    ).get_json()

    assert body["statuses"] == []
    assert body["report_coverage"] == [{"team": "LAC", "team_games": 1, "games_with_report": 1}]


def test_an_ambiguous_name_is_refused_with_candidates(client, authenticate, stored):
    response = _get(client, authenticate, "player=Jalen%20Williams&from_date=2026-11-01")

    assert response.status_code == 400
    assert response.get_json()["error"]["details"] == {
        "parameter": "player",
        "parameters": ["player"],
        "candidates": [
            {"player_id": WILLIAMS_OKC, "name": "Jalen Williams", "team": "OKC"},
            {"player_id": WILLIAMS_PHX, "name": "Jalen Williams", "team": "PHX"},
        ],
    }
    by_id = _get(client, authenticate, f"player={WILLIAMS_PHX}&from_date=2026-11-01").get_json()
    assert [(row["game_date"], row["status"]) for row in by_id["statuses"]] == [("2026-11-09", "Probable")]


@pytest.mark.parametrize(
    ("query", "parameters"),
    [
        ("player=Nobody%20Known&from_date=2026-11-01", ["player"]),
        ("player=99999999&from_date=2026-11-01", ["player"]),
        ("player=Isaiah%20Jackson", ["from_date"]),
        ("player=Isaiah%20Jackson&from_date=11-01-2026", ["from_date"]),
        ("player=Isaiah%20Jackson&from_date=2026-11-01&to_date=2026-11-1", ["to_date"]),
        ("player=Isaiah%20Jackson&from_date=2026-11-05&to_date=2026-11-01", ["to_date"]),
        ("player=Isaiah%20Jackson&from_date=2026-03-01&to_date=2026-11-01", ["to_date"]),
        ("player=Isaiah%20Jackson&from_date=2025-11-01&to_date=2026-11-10", ["to_date"]),
        ("player=Isaiah%20Jackson&from_date=2026-11-01&date=2026-11-02", ["date"]),
        ("player=Isaiah%20Jackson&from_date=2026-11-01&team=LAC", ["team"]),
        ("from_date=2026-11-01", ["from_date"]),
        ("date=2026-11-02&to_date=2026-11-03", ["to_date"]),
    ],
    ids=[
        "unknown-name",
        "unknown-id",
        "no-from-date",
        "bad-from-date",
        "bad-to-date",
        "ends-before-it-starts",
        "crosses-seasons",
        "longer-than-a-season",
        "with-date",
        "with-team",
        "range-without-player",
        "to-date-with-date",
    ],
)
def test_a_bad_history_request_is_a_400_naming_the_parameter(client, authenticate, stored, query, parameters):
    response = _get(client, authenticate, query)

    assert response.status_code == 400
    assert response.get_json()["error"]["details"]["parameters"] == parameters


def test_a_past_season_range_without_to_date_ends_with_that_season(client, authenticate, repository):
    _store(
        repository,
        "0022500900",
        _row(
            JACKSON,
            "Isaiah Jackson",
            "LAC",
            "Out",
            "Left knee soreness",
            "2026-02-28",
            "LAC@SAS",
            "2026-02-28T17:30:00-05:00",
        ),
        season="2025-26",
    )

    body = _get(client, authenticate, "player=Isaiah%20Jackson&from_date=2026-02-01").get_json()

    assert (body["season"], body["to_date"]) == ("2025-26", "2026-09-30")
    assert [(row["game_date"], row["game_id"], row["status"]) for row in body["statuses"]] == [
        ("2026-02-28", "0022500900", "Out")
    ]
    assert body["report_coverage"] == [{"team": "LAC", "team_games": 1, "games_with_report": 1}]
