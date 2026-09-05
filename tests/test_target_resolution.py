"""Day-scoped Target resolution, at the service and HTTP seams (#245).

Resolution composes the Slate the slate route serves and the Matchup document
the matchup route serves, so the service tests drive fake Slate and Matchup
seams around a real migrated SQLite database holding the caller's Targets.
The route tests stay at the HTTP seam with a stub service, matching
``test_targets``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

from app.errors import InvalidInputError
from app.migrations import run_migrations
from app.models.user import User
from app.services.target_resolution import TargetResolutionService
from app.services.user_service import UserService


OWNER = "owner-uid"
GAME_ID = "0022500584"
LAL = 1610612747
OKC = 1610612760
SLATE_DATE = "2026-01-16"
TIP_OFF = "2026-01-17T00:30:00+00:00"

CORNER_THREE = {
    "base": "shot_zones",
    "slice_key": "Corner 3",
    "comparator": "at_or_above",
    "threshold": 0.4,
}
LOW_RIM = {
    "base": "shot_zones",
    "slice_key": "Restricted Area",
    "comparator": "at_or_below",
    "threshold": 0.25,
}
TRANSITION = {
    "base": "play_types",
    "slice_key": "Transition",
    "comparator": "at_or_above",
    "threshold": 0.2,
}

SHOT_ZONES = (
    "Restricted Area",
    "In The Paint (Non-RA)",
    "Mid-Range",
    "Corner 3",
    "Above the Break 3",
)


# --- fake seams ------------------------------------------------------------


class FakeSlate:
    """The date -> slate-events path both the slate and matchup reads use."""

    def __init__(self, games=None, *, error=None):
        self.games = [_game()] if games is None else games
        self.error = error
        self.calls = []

    def get_slate(self, requested_date=None):
        self.calls.append(requested_date)
        if self.error is not None:
            raise self.error
        return {
            "slate_date": requested_date or SLATE_DATE,
            "freshness": {"schedule": {"status": "fresh"}, "pool": {}},
            "games": list(self.games),
        }


class FakeMatchups:
    def __init__(self, payloads=None):
        self.payloads = payloads or {GAME_ID: _matchup()}
        self.calls = []

    def get_matchup(self, *, game_id):
        self.calls.append(game_id)
        return self.payloads[game_id]


def _game(
    *,
    game_id=GAME_ID,
    away=(LAL, "LAL", "Los Angeles Lakers"),
    home=(OKC, "OKC", "Oklahoma City Thunder"),
):
    return {
        "game_id": game_id,
        "away_team": {
            "team_id": away[0],
            "tricode": away[1],
            "name": away[2],
            "targetable_player_count": 3,
        },
        "home_team": {
            "team_id": home[0],
            "tricode": home[1],
            "name": home[2],
            "targetable_player_count": 4,
        },
        "scheduled_at": TIP_OFF,
        "status": {"state": "scheduled", "label": "Scheduled"},
        "classification": None,
        "preseason": False,
    }


def _diet_fact(key, share, *, volume=100.0, games_played=20, average=0.2):
    return {
        "key": key,
        "season": {
            "share": share,
            "volume": volume,
            "games_played": games_played,
            "volume_unit": "field_goal_attempts",
            "league_average_share": average,
            "sigma_deviation": 1.0,
        },
    }


def _player(
    canonical_id,
    name,
    *,
    team_id=LAL,
    tricode="LAL",
    shot_zones=(),
    play_types=(),
    season_scoring=20.0,
    posted_markets=("PTS",),
    injury_badge_ref=None,
    player_source="player_pool",
):
    return {
        "canonical_id": canonical_id,
        "name": name,
        "team_id": team_id,
        "tricode": tricode,
        "player_source": player_source,
        "stat_categories": list(posted_markets),
        "focal_game_line": None,
        "posted_markets": list(posted_markets),
        "provenance": {},
        "season_scoring": season_scoring,
        "last_10_minutes": [],
        "diet_shares": {
            "play_types": list(play_types),
            "shot_zones": list(shot_zones),
            "shot_types": [],
            "assist_locations": [],
        },
        "scores": {},
        "injury_badge_ref": injury_badge_ref,
    }


def _zone_diet(corner_three, restricted_area, **kwargs):
    """A complete five-slice shot-zone diet with the two shares under test."""

    remainder = round((1.0 - corner_three - restricted_area) / 3, 6)
    shares = {
        "Corner 3": corner_three,
        "Restricted Area": restricted_area,
        "In The Paint (Non-RA)": remainder,
        "Mid-Range": remainder,
        "Above the Break 3": remainder,
    }
    return tuple(
        _diet_fact(slice_key, shares[slice_key], **kwargs) for slice_key in SHOT_ZONES
    )


def _team_sheet(offset):
    return {
        "shot_zones": [
            row
            for slice_key in SHOT_ZONES
            for row in (
                {
                    "key": f"{slice_key}:FGA",
                    "label": f"{slice_key} FGA",
                    "markets": ["FGA", "FG3A"],
                    "season": {
                        "allowed_per_48": 20.0 + offset,
                        "percent_vs_league_average": offset * 5,
                        "sigma_deviation": offset / 2,
                        "rank": 4,
                    },
                    "last_15": {
                        "allowed_per_48": 22.0 + offset,
                        "percent_vs_league_average": offset * 6,
                        "sigma_deviation": offset / 3,
                        "rank": 2,
                    },
                },
                {
                    "key": f"{slice_key}:FGM",
                    "label": f"{slice_key} FGM",
                    "markets": ["PTS", "3PM"],
                    "season": {
                        "allowed_per_48": 9.0 + offset,
                        "percent_vs_league_average": offset * 3,
                        "sigma_deviation": offset / 4,
                        "rank": 7,
                    },
                    "last_15": {
                        "allowed_per_48": 10.0 + offset,
                        "percent_vs_league_average": offset * 4,
                        "sigma_deviation": offset / 5,
                        "rank": 9,
                    },
                },
            )
        ],
        "play_types": [
            {
                "key": "Transition:PTS",
                "label": "Transition PTS",
                "markets": ["PTS", "PA", "PR", "PRA"],
                "season": {
                    "allowed_per_48": 16.0 + offset,
                    "percent_vs_league_average": offset * 2,
                    "sigma_deviation": offset,
                    "rank": 11,
                },
                "last_15": None,
            }
        ],
        "shot_types": [],
        "assist_locations": [],
        "traditional": [],
    }


def _league_sheet():
    return {
        "shot_zones": [
            row
            for slice_key in SHOT_ZONES
            for row in (
                {
                    "key": f"{slice_key}:FGA",
                    "season": {"average_allowed_per_48": 20.0, "sigma": 2.0},
                    "last_15": {"average_allowed_per_48": 22.0, "sigma": 2.5},
                },
                {
                    "key": f"{slice_key}:FGM",
                    "season": {"average_allowed_per_48": 9.0, "sigma": 1.0},
                    "last_15": {"average_allowed_per_48": 10.0, "sigma": 1.5},
                },
            )
        ],
        "play_types": [
            {
                "key": "Transition:PTS",
                "season": {"average_allowed_per_48": 16.0, "sigma": 2.0},
                "last_15": None,
            }
        ],
        "shot_types": [],
        "assist_locations": [],
        "traditional": [],
    }


def _availability():
    available = {"status": "available", "unavailable_reason": None}
    return {
        "shot_zones": {"season": dict(available), "last_15": dict(available)},
        "shot_types": {"season": dict(available), "last_15": dict(available)},
        "assist_locations": {"season": dict(available), "last_15": dict(available)},
        "traditional": {"season": dict(available), "last_15": dict(available)},
        "play_types": {
            "season": dict(available),
            "last_15": {
                "status": "unavailable",
                "unavailable_reason": "provider_window_unsupported",
            },
        },
    }


def _historical_availability():
    """A completed game: no Last-15 snapshot was ever captured for it."""

    states = _availability()
    for base in states:
        states[base]["last_15"] = {
            "status": "missing",
            "unavailable_reason": "not_stored",
        }
    return states


def _historical_sheet(offset):
    """The same sheet with every Last-15 window nulled by its availability."""

    sheet = _team_sheet(offset)
    for rows in sheet.values():
        for row in rows:
            row["last_15"] = None
    return sheet


def _matchup(
    *,
    players=None,
    participants=None,
    game=None,
    surface_availability=None,
    historical=False,
    team_sheet=None,
):
    if players is None:
        players = [
            _player(1, "Corner Sniper", shot_zones=_zone_diet(0.4, 0.2)),
            _player(2, "Rim Runner", shot_zones=_zone_diet(0.1, 0.5), season_scoring=9.0),
        ]
    resolved_game = game or _game()
    sheet = team_sheet or _team_sheet
    return {
        "game": resolved_game,
        "experience": {
            "mode": "historical" if historical else "current",
            "player_source": "game_logs" if historical else "player_pool",
            "sections": {
                "participants": participants
                or (
                    {
                        "status": "available",
                        "source": "player_game_logs",
                        "context": "completed_season",
                        "unavailable_reason": None,
                    }
                    if historical
                    else {
                        "status": "available",
                        "source": "player_pool",
                        "context": "posted_markets",
                        "unavailable_reason": None,
                    }
                )
            },
        },
        "league": {
            "surface_availability": surface_availability or _availability(),
            "defense_sheet": _league_sheet(),
            "defensive_columns": {},
        },
        "teams": [
            {
                "team_id": resolved_game["away_team"]["team_id"],
                "tricode": resolved_game["away_team"]["tricode"],
                "name": resolved_game["away_team"]["name"],
                "defense_sheet": sheet(1.0),
                "defensive_columns": {},
            },
            {
                "team_id": resolved_game["home_team"]["team_id"],
                "tricode": resolved_game["home_team"]["tricode"],
                "name": resolved_game["home_team"]["name"],
                "defense_sheet": sheet(-1.0),
                "defensive_columns": {},
            },
        ],
        "players": list(players),
        "injuries": {},
        "freshness": {},
    }


# --- service ---------------------------------------------------------------


@pytest.fixture
def target_engine(tmp_path):
    """A migrated application database holding the resolving account."""

    engine = create_engine(f"sqlite:///{tmp_path / 'resolve.sqlite3'}")
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(
            User.__table__.insert(),
            {
                "firebase_uid": OWNER,
                "email": f"{OWNER}@example.com",
                "display_name": OWNER,
                "photo_url": None,
                "created_at": datetime(2026, 8, 1, tzinfo=timezone.utc),
                "last_login": datetime(2026, 8, 1, tzinfo=timezone.utc),
                "is_active": True,
            },
        )
    yield engine
    engine.dispose()


@pytest.fixture
def targets(target_engine, runtime_settings):
    return UserService(target_engine, settings=runtime_settings)


@pytest.fixture
def resolve(targets, runtime_settings):
    """Build the resolver over the caller's real Targets and fake seams."""

    def _resolve(*, slate=None, matchups=None, date=SLATE_DATE):
        service = TargetResolutionService(
            targets=targets,
            slates=slate or FakeSlate(),
            matchups=matchups or FakeMatchups(),
            settings=runtime_settings,
        )
        return service.resolve(OWNER, requested_date=date)

    return _resolve


def _create(targets, *, opponent="OKC", qualifiers=(CORNER_THREE,), note=None):
    return targets.create_target(
        OWNER, opponent=opponent, qualifiers=list(qualifiers), note=note
    )


def test_a_live_target_lists_the_opposing_pool_members_meeting_the_qualifier(
    targets, resolve
):
    created = _create(targets)

    payload = resolve()

    assert payload["slate_date"] == SLATE_DATE
    assert len(payload["targets"]) == 1
    resolved = payload["targets"][0]
    assert resolved["target"] == created
    assert resolved["game"] == {
        "game_id": GAME_ID,
        "scheduled_at": TIP_OFF,
        "status": {"state": "scheduled", "label": "Scheduled"},
        "opponent": {
            "team_id": OKC,
            "tricode": "OKC",
            "name": "Oklahoma City Thunder",
        },
        "opposing_team": {
            "team_id": LAL,
            "tricode": "LAL",
            "name": "Los Angeles Lakers",
        },
    }
    assert [player["name"] for player in resolved["players"]] == ["Corner Sniper"]
    assert resolved["players"][0] == {
        "canonical_id": 1,
        "name": "Corner Sniper",
        "team_id": LAL,
        "tricode": "LAL",
        "posted_markets": ["PTS"],
        "injury_badge_ref": None,
        "season_scoring": 20.0,
        "thin": False,
        "shares": [
            {
                "base": "shot_zones",
                "slice_key": "Corner 3",
                "share": 0.4,
                "league_average_share": 0.2,
            }
        ],
    }


def test_the_opponents_own_players_are_never_listed(targets, resolve):
    _create(targets)
    matchups = FakeMatchups(
        {
            GAME_ID: _matchup(
                players=[
                    _player(
                        3,
                        "Thunder Sniper",
                        team_id=OKC,
                        tricode="OKC",
                        shot_zones=_zone_diet(0.45, 0.2),
                    ),
                    _player(1, "Corner Sniper", shot_zones=_zone_diet(0.4, 0.2)),
                ]
            )
        }
    )

    payload = resolve(matchups=matchups)

    assert [player["name"] for player in payload["targets"][0]["players"]] == [
        "Corner Sniper"
    ]


@pytest.mark.parametrize(
    ("comparator", "threshold", "share", "expected"),
    [
        ("at_or_above", 0.4, 0.4, True),
        ("at_or_above", 0.4, 0.39, False),
        ("at_or_above", 0.4, 0.41, True),
        ("at_or_below", 0.25, 0.25, True),
        ("at_or_below", 0.25, 0.26, False),
        ("at_or_below", 0.25, 0.24, True),
    ],
)
def test_both_comparators_are_inclusive(
    targets, resolve, comparator, threshold, share, expected
):
    _create(
        targets,
        qualifiers=[
            {
                "base": "shot_zones",
                "slice_key": "Corner 3",
                "comparator": comparator,
                "threshold": threshold,
            }
        ],
    )
    matchups = FakeMatchups(
        {GAME_ID: _matchup(players=[_player(1, "Edge", shot_zones=_zone_diet(share, 0.2))])}
    )

    payload = resolve(matchups=matchups)

    named = [player["name"] for player in payload["targets"][0]["players"]]
    assert named == (["Edge"] if expected else [])


def test_two_qualifiers_return_only_players_meeting_both(targets, resolve):
    _create(targets, qualifiers=[CORNER_THREE, LOW_RIM])
    matchups = FakeMatchups(
        {
            GAME_ID: _matchup(
                players=[
                    _player(1, "Both", shot_zones=_zone_diet(0.42, 0.2)),
                    _player(2, "Corner only", shot_zones=_zone_diet(0.42, 0.4)),
                    _player(3, "Rim only", shot_zones=_zone_diet(0.1, 0.2)),
                ]
            )
        }
    )

    payload = resolve(matchups=matchups)

    resolved = payload["targets"][0]
    assert [player["name"] for player in resolved["players"]] == ["Both"]
    assert [share["slice_key"] for share in resolved["players"][0]["shares"]] == [
        "Corner 3",
        "Restricted Area",
    ]


def test_a_qualifier_whose_slice_the_player_has_no_fact_for_never_matches(
    targets, resolve
):
    _create(targets, qualifiers=[TRANSITION])
    matchups = FakeMatchups(
        {GAME_ID: _matchup(players=[_player(1, "No play types", shot_zones=_zone_diet(0.4, 0.2))])}
    )

    payload = resolve(matchups=matchups)

    assert payload["targets"][0]["players"] == []


def test_players_keep_the_matchups_own_ordering(targets, resolve):
    _create(targets, qualifiers=[LOW_RIM])
    matchups = FakeMatchups(
        {
            GAME_ID: _matchup(
                players=[
                    _player(1, "Top", shot_zones=_zone_diet(0.3, 0.2), season_scoring=28.0),
                    _player(2, "Middle", shot_zones=_zone_diet(0.3, 0.2), season_scoring=17.0),
                    _player(3, "Bottom", shot_zones=_zone_diet(0.3, 0.2), season_scoring=4.0),
                ]
            )
        }
    )

    payload = resolve(matchups=matchups)

    assert [player["name"] for player in payload["targets"][0]["players"]] == [
        "Top",
        "Middle",
        "Bottom",
    ]


def test_posted_markets_and_the_injury_badge_come_from_the_matchup(targets, resolve):
    _create(targets)
    matchups = FakeMatchups(
        {
            GAME_ID: _matchup(
                players=[
                    _player(
                        1,
                        "Corner Sniper",
                        shot_zones=_zone_diet(0.4, 0.2),
                        posted_markets=("PTS", "3PM"),
                        injury_badge_ref="rotowire:6504",
                    )
                ]
            )
        }
    )

    player = resolve(matchups=matchups)["targets"][0]["players"][0]

    assert player["posted_markets"] == ["PTS", "3PM"]
    assert player["injury_badge_ref"] == "rotowire:6504"


def test_a_thin_diet_is_flagged_rather_than_excluded(targets, resolve, runtime_settings):
    _create(targets)
    floors = runtime_settings.matchup_scores
    matchups = FakeMatchups(
        {
            GAME_ID: _matchup(
                players=[
                    _player(
                        1,
                        "Few games",
                        shot_zones=_zone_diet(
                            0.4, 0.2, games_played=floors.min_games - 1, volume=60.0
                        ),
                    ),
                    _player(
                        2,
                        "Low volume",
                        shot_zones=_zone_diet(0.4, 0.2, games_played=40, volume=4.0),
                        season_scoring=8.0,
                    ),
                    _player(
                        3,
                        "Solid",
                        shot_zones=_zone_diet(0.4, 0.2, games_played=40, volume=200.0),
                        season_scoring=6.0,
                    ),
                ]
            )
        }
    )

    players = resolve(matchups=matchups)["targets"][0]["players"]

    assert [(player["name"], player["thin"]) for player in players] == [
        ("Few games", True),
        ("Low volume", True),
        ("Solid", False),
    ]


def test_the_context_repeats_the_matchup_defense_sheet_for_that_slice(
    targets, resolve
):
    _create(targets, qualifiers=[CORNER_THREE, TRANSITION])
    matchups = FakeMatchups()

    resolved = resolve(matchups=matchups)["targets"][0]

    matchup = matchups.payloads[GAME_ID]
    opponent_sheet = next(
        team for team in matchup["teams"] if team["team_id"] == OKC
    )["defense_sheet"]
    league_sheet = matchup["league"]["defense_sheet"]
    corner, transition = resolved["context"]

    assert corner["base"] == "shot_zones"
    assert corner["slice_key"] == "Corner 3"
    assert corner["label"] == "Corner 3"
    assert corner["availability"] == {
        "season": {"status": "available", "unavailable_reason": None},
        "last_15": {"status": "available", "unavailable_reason": None},
    }
    assert [metric["key"] for metric in corner["metrics"]] == [
        "Corner 3:FGA",
        "Corner 3:FGM",
    ]
    expected_row = next(
        row for row in opponent_sheet["shot_zones"] if row["key"] == "Corner 3:FGA"
    )
    expected_league = next(
        row for row in league_sheet["shot_zones"] if row["key"] == "Corner 3:FGA"
    )
    assert corner["metrics"][0] == {
        "key": "Corner 3:FGA",
        "label": expected_row["label"],
        "markets": expected_row["markets"],
        "opponent": {
            "season": expected_row["season"],
            "last_15": expected_row["last_15"],
        },
        "league": {
            "season": expected_league["season"],
            "last_15": expected_league["last_15"],
        },
    }
    assert transition["label"] == "Transition"
    assert transition["availability"]["last_15"] == {
        "status": "unavailable",
        "unavailable_reason": "provider_window_unsupported",
    }
    assert transition["metrics"][0]["opponent"]["last_15"] is None


def test_an_idle_opponent_has_no_game_and_no_players(targets, resolve):
    created = _create(targets, opponent="MIA")

    resolved = resolve()["targets"][0]

    assert resolved == {
        "target": created,
        "game": None,
        "context": [],
        "availability": {
            "status": "unavailable",
            "source": None,
            "context": None,
            "unavailable_reason": "opponent_idle",
        },
        "players": [],
    }


def test_live_targets_come_before_idle_targets(targets, resolve):
    idle = _create(targets, opponent="MIA")
    live = _create(targets, opponent="OKC")
    also_idle = _create(targets, opponent="DEN")

    payload = resolve()

    assert [item["target"]["id"] for item in payload["targets"]] == [
        live["id"],
        also_idle["id"],
        idle["id"],
    ]
    assert payload["targets"][0]["game"] is not None
    assert [item["game"] for item in payload["targets"][1:]] == [None, None]


def test_an_unavailable_pool_says_so_and_lists_no_players(targets, resolve):
    _create(targets)
    matchups = FakeMatchups(
        {
            GAME_ID: _matchup(
                players=[],
                participants={
                    "status": "unavailable",
                    "source": "player_pool",
                    "context": None,
                    "unavailable_reason": "player_pool_unavailable",
                },
            )
        }
    )

    resolved = resolve(matchups=matchups)["targets"][0]

    assert resolved["availability"] == {
        "status": "unavailable",
        "source": "player_pool",
        "context": None,
        "unavailable_reason": "player_pool_unavailable",
    }
    assert resolved["players"] == []
    assert resolved["game"] is not None


def test_an_available_pool_with_no_qualifying_player_is_not_an_unavailable_pool(
    targets, resolve
):
    _create(targets)
    matchups = FakeMatchups(
        {GAME_ID: _matchup(players=[_player(2, "Rim Runner", shot_zones=_zone_diet(0.1, 0.5))])}
    )

    resolved = resolve(matchups=matchups)["targets"][0]

    assert resolved["availability"]["status"] == "available"
    assert resolved["players"] == []


def test_a_completed_game_resolves_against_its_game_log_participants(
    targets, resolve
):
    """The Matchup page lists participants for a completed game, so this does.

    A Target and the Matchup detail page must agree about the same game on the
    same date, whichever evidence named that game's players.
    """

    _create(targets)
    matchups = FakeMatchups(
        {
            GAME_ID: _matchup(
                historical=True,
                players=[
                    _player(
                        1,
                        "Played that night",
                        shot_zones=_zone_diet(0.42, 0.2),
                        player_source="game_logs",
                        posted_markets=(),
                    ),
                    _player(
                        2,
                        "Missed the cut",
                        shot_zones=_zone_diet(0.1, 0.5),
                        player_source="game_logs",
                        posted_markets=(),
                        season_scoring=8.0,
                    ),
                ],
            )
        }
    )

    resolved = resolve(matchups=matchups)["targets"][0]

    assert resolved["availability"] == {
        "status": "available",
        "source": "game_logs",
        "context": "completed_season",
        "unavailable_reason": None,
    }
    assert [player["name"] for player in resolved["players"]] == [
        "Played that night"
    ]
    assert resolved["players"][0]["posted_markets"] == []
    assert resolved["players"][0]["shares"] == [
        {
            "base": "shot_zones",
            "slice_key": "Corner 3",
            "share": 0.42,
            "league_average_share": 0.2,
        }
    ]


def test_a_completed_games_context_reads_its_own_defense_windows(targets, resolve):
    """A completed game has no Last-15 snapshot; the Season window still reads."""

    _create(targets)
    matchups = FakeMatchups(
        {
            GAME_ID: _matchup(
                historical=True,
                surface_availability=_historical_availability(),
                team_sheet=_historical_sheet,
            )
        }
    )

    corner = resolve(matchups=matchups)["targets"][0]["context"][0]

    assert corner["availability"] == {
        "season": {"status": "available", "unavailable_reason": None},
        "last_15": {"status": "missing", "unavailable_reason": "not_stored"},
    }
    assert corner["metrics"][0]["opponent"]["season"] == {
        "allowed_per_48": 19.0,
        "percent_vs_league_average": -5.0,
        "sigma_deviation": -0.5,
        "rank": 4,
    }
    assert corner["metrics"][0]["opponent"]["last_15"] is None


def test_incomplete_game_logs_leave_a_completed_game_without_participants(
    targets, resolve
):
    _create(targets)
    matchups = FakeMatchups(
        {
            GAME_ID: _matchup(
                historical=True,
                players=[],
                participants={
                    "status": "unavailable",
                    "source": "player_game_logs",
                    "context": None,
                    "unavailable_reason": "game_logs_incomplete",
                },
            )
        }
    )

    resolved = resolve(matchups=matchups)["targets"][0]

    assert resolved["players"] == []
    assert resolved["availability"] == {
        "status": "unavailable",
        "source": "game_logs",
        "context": None,
        "unavailable_reason": "game_logs_incomplete",
    }


def test_one_game_is_read_once_however_many_targets_name_that_opponent(
    targets, resolve
):
    _create(targets, qualifiers=[CORNER_THREE])
    _create(targets, qualifiers=[LOW_RIM])
    matchups = FakeMatchups()

    payload = resolve(matchups=matchups)

    assert len(payload["targets"]) == 2
    assert matchups.calls == [GAME_ID]


def test_a_game_no_target_names_is_never_read(targets, resolve):
    _create(targets, opponent="MIA")
    matchups = FakeMatchups()

    resolve(matchups=matchups)

    assert matchups.calls == []


def test_an_account_without_targets_resolves_to_an_empty_list(resolve):
    matchups = FakeMatchups()

    payload = resolve(matchups=matchups)

    assert payload["targets"] == []
    assert matchups.calls == []


def test_the_requested_date_is_the_slates_own_date(targets, resolve):
    _create(targets)
    slate = FakeSlate()

    payload = resolve(slate=slate, date="2026-02-01")

    assert slate.calls == ["2026-02-01"]
    assert payload["slate_date"] == "2026-02-01"


def test_a_malformed_date_is_refused_by_the_slates_own_rule(targets, resolve):
    _create(targets)
    slate = FakeSlate(error=InvalidInputError("The slate date must use YYYY-MM-DD."))

    with pytest.raises(InvalidInputError):
        resolve(slate=slate, date="16-01-2026")


def test_a_postponed_game_stays_live_and_keeps_its_slate_status(targets, resolve):
    _create(targets)
    game = _game()
    game["status"] = {"state": "postponed", "label": "PPD"}
    slate = FakeSlate(games=[game])
    matchups = FakeMatchups({GAME_ID: _matchup(game=game)})

    resolved = resolve(slate=slate, matchups=matchups)["targets"][0]

    assert resolved["game"]["status"] == {"state": "postponed", "label": "PPD"}


def test_an_opponent_playing_away_resolves_against_the_home_pool(targets, resolve):
    _create(targets, opponent="LAL")

    resolved = resolve()["targets"][0]

    assert resolved["game"]["opponent"]["tricode"] == "LAL"
    assert resolved["game"]["opposing_team"]["tricode"] == "OKC"
    assert resolved["players"] == []


# --- routes ----------------------------------------------------------------


RESOLVED = {
    "slate_date": SLATE_DATE,
    "targets": [
        {
            "target": {
                "id": 7,
                "opponent": "OKC",
                "title": "OKC vs Corner 3 ≥ 40%",
                "note": None,
                "qualifiers": [CORNER_THREE],
                "created_at": "2026-09-05T12:00:00+00:00",
                "updated_at": "2026-09-05T12:00:00+00:00",
            },
            "game": None,
            "context": [],
            "availability": {
                "status": "unavailable",
                "source": None,
                "context": None,
                "unavailable_reason": "opponent_idle",
            },
            "players": [],
        }
    ],
}


@pytest.fixture
def resolution_service(monkeypatch):
    """Swap the route module's resolution handle for a stub."""

    from app.routes import user_routes

    stub = SimpleNamespace()
    monkeypatch.setattr(user_routes, "target_resolution_service", stub)
    return stub


def test_the_resolve_route_returns_the_resolution_for_the_requested_date(
    client, authenticate, resolution_service
):
    headers = authenticate()
    asked = []

    def capture(firebase_uid, *, requested_date):
        asked.append((firebase_uid, requested_date))
        return RESOLVED

    resolution_service.resolve = capture

    response = client.get(
        f"/api/user/targets/resolve?date={SLATE_DATE}", headers=headers
    )

    assert response.status_code == 200
    assert response.get_json() == {"success": True, **RESOLVED}
    assert asked == [("test-uid", SLATE_DATE)]


def test_the_resolve_route_forwards_an_absent_date_unchanged(
    client, authenticate, resolution_service
):
    headers = authenticate()
    asked = []

    def capture(firebase_uid, *, requested_date):
        asked.append(requested_date)
        return RESOLVED

    resolution_service.resolve = capture

    response = client.get("/api/user/targets/resolve", headers=headers)

    assert response.status_code == 200
    assert asked == [None]


def test_the_resolve_route_reports_a_malformed_date_as_invalid_input(
    client, authenticate, resolution_service
):
    headers = authenticate()

    def refuse(firebase_uid, *, requested_date):
        raise InvalidInputError("The slate date must use YYYY-MM-DD.")

    resolution_service.resolve = refuse

    response = client.get(
        "/api/user/targets/resolve?date=16-01-2026", headers=headers
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == {
        "code": "invalid_input",
        "message": "The slate date must use YYYY-MM-DD.",
    }


def test_the_resolve_route_reports_an_unexpected_failure_safely(
    client, authenticate, resolution_service
):
    headers = authenticate()

    def explode(firebase_uid, *, requested_date):
        raise RuntimeError("stored facts are wrong")

    resolution_service.resolve = explode

    response = client.get("/api/user/targets/resolve", headers=headers)

    assert response.status_code == 500
    assert response.get_json()["error"] == {
        "code": "operation_failed",
        "message": "Failed to resolve targets.",
    }


def test_the_resolve_route_refuses_an_unauthenticated_caller(
    client, authenticate, resolution_service
):
    authenticate()
    resolution_service.resolve = lambda *args, **kwargs: RESOLVED

    response = client.get("/api/user/targets/resolve")

    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "authentication_required"
