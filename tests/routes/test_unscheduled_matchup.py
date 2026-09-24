"""Route tests for the Unscheduled Matchup read (crf04/statsplus#95).

An Unscheduled Matchup scores a player, or a team's players, against one
opponent's latest Defense Sheet with no Slate game.  The persisted-fixture
tests below serve it and the game Matchup from one seeded database through
the Flask test client, so the equivalence test compares the two public
documents rather than any private scoring function.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine

from app import create_app
from app.config.settings import (
    AuthenticationSettings,
    CacheSettings,
    NBASeasonSettings,
    RuntimeSettings,
)
from app.errors import ResourceNotFoundError
from app.migrations import run_migrations
from app.services.matchup import MatchupService
from app.services.player_diet import PlayerDietFact, PlayerDietObservation
from app.services.publication_snapshot_calls import call_with_read_scope
from app.services.statistic_catalog import StatisticCatalog
from app.services.stats_freshness_repository import StatsFreshnessRepository
from tests.routes.test_matchup_fixtures import (
    BOS,
    GAME_ID,
    LAL,
    NOW,
    SEASON,
    _drop_last_15_snapshots,
    _event_catalog,
    _player_diets,
    _player_logs,
    _player_pool,
    _team_matchups,
)


UNSCHEDULED = "/api/matchups/unscheduled"
PER_GAME_PLAYER_KEYS = {"focal_game_line", "posted_markets", "injury_badge_ref"}


# --- HTTP contract --------------------------------------------------------


def test_unscheduled_matchup_passes_one_parsed_selector_to_the_service(
    client, dependencies
):
    dependencies.matchup_service = Mock()
    dependencies.matchup_service.get_unscheduled_matchup.return_value = {
        "experience": {"mode": "unscheduled"}
    }

    response = client.get(f"{UNSCHEDULED}?player_id=2544&opponent=bos")

    assert response.status_code == 200
    assert response.get_json() == {"experience": {"mode": "unscheduled"}}
    dependencies.matchup_service.get_unscheduled_matchup.assert_called_once_with(
        opponent="BOS",
        player_id=2544,
        player_name=None,
        player_team=None,
        team=None,
    )


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (
            "team=det&opponent=CHA",
            {"team": "DET", "player_name": None, "player_team": None},
        ),
        (
            "player_name=Cade%20Cunningham&opponent=CHA",
            {"team": None, "player_name": "Cade Cunningham", "player_team": None},
        ),
        (
            "player_name=Cade%20Cunningham&player_team=det&opponent=CHA",
            {"team": None, "player_name": "Cade Cunningham", "player_team": "DET"},
        ),
    ],
)
def test_unscheduled_matchup_accepts_team_and_player_name_selectors(
    client, dependencies, query, expected
):
    dependencies.matchup_service = Mock()
    dependencies.matchup_service.get_unscheduled_matchup.return_value = {}

    response = client.get(f"{UNSCHEDULED}?{query}")

    assert response.status_code == 200
    dependencies.matchup_service.get_unscheduled_matchup.assert_called_once_with(
        opponent="CHA", player_id=None, **expected
    )


@pytest.mark.parametrize(
    ("query", "parameters"),
    [
        # No selector at all, and more than one selector.
        ("opponent=CHA", ["player_id", "player_name", "team"]),
        ("player_id=2544&team=DET&opponent=CHA", ["player_id", "team"]),
        ("player_id=2544&player_name=LeBron&opponent=CHA", ["player_id", "player_name"]),
        # The opponent is required, well formed, and not repeated.
        ("player_id=2544", ["opponent"]),
        ("player_id=2544&opponent=", ["opponent"]),
        ("player_id=2544&opponent=CHAR", ["opponent"]),
        ("player_id=2544&opponent=C1A", ["opponent"]),
        ("player_id=2544&opponent=%20CHA", ["opponent"]),
        ("player_id=2544&opponent=CHA&opponent=DET", ["opponent"]),
        # Selector values.
        ("player_id=0&opponent=CHA", ["player_id"]),
        ("player_id=abc&opponent=CHA", ["player_id"]),
        ("player_id=%202544&opponent=CHA", ["player_id"]),
        ("player_id=99999999999999999999&opponent=CHA", ["player_id"]),
        ("player_id=2544&player_id=2545&opponent=CHA", ["player_id"]),
        ("player_name=%20%20&opponent=CHA", ["player_name"]),
        ("team=DE&opponent=CHA", ["team"]),
        # A team is never its own opponent.
        ("team=cha&opponent=CHA", ["opponent"]),
        # player_team only narrows a player_name, and is a tricode.
        ("player_id=2544&player_team=LAL&opponent=CHA", ["player_team"]),
        ("team=DET&player_team=LAL&opponent=CHA", ["player_team"]),
        ("player_team=LAL&opponent=CHA", ["player_id", "player_name", "team"]),
        ("player_name=LeBron&player_team=LA&opponent=CHA", ["player_team"]),
        (
            "player_name=LeBron&player_team=LAL&player_team=BOS&opponent=CHA",
            ["player_team"],
        ),
        # Unknown parameters are refused by name.
        ("player_id=2544&opponent=CHA&date=2026-04-10", ["date"]),
    ],
)
def test_unscheduled_matchup_names_the_offending_parameter(
    client, dependencies, query, parameters
):
    dependencies.matchup_service = Mock()

    response = client.get(f"{UNSCHEDULED}?{query}")

    assert response.status_code == 400
    error = response.get_json()["error"]
    assert error["code"] == "invalid_input"
    assert error["details"] == {"parameters": parameters}
    for parameter in parameters:
        assert parameter in error["message"]
    dependencies.matchup_service.get_unscheduled_matchup.assert_not_called()


def test_unscheduled_matchup_requires_authentication_before_calling_service(
    client, dependencies, monkeypatch
):
    dependencies.matchup_service = Mock()
    monkeypatch.setattr("app.utils.auth.get_firebase_app", lambda: object())

    response = client.get(f"{UNSCHEDULED}?player_id=2544&opponent=BOS")

    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "authentication_required"
    dependencies.matchup_service.get_unscheduled_matchup.assert_not_called()


def test_unscheduled_matchup_preserves_not_found_errors(client, dependencies):
    dependencies.matchup_service = Mock()
    dependencies.matchup_service.get_unscheduled_matchup.side_effect = (
        ResourceNotFoundError()
    )

    response = client.get(f"{UNSCHEDULED}?player_id=2544&opponent=BOS")

    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "resource_not_found"


# --- Persisted fixture ----------------------------------------------------


def _catalog_row(player_id, name, team_id, tricode):
    return {
        "player_id": player_id,
        "display_name": name,
        "roster_status": "Active",
        "is_active": True,
        "is_active_for_season": True,
        "season": SEASON,
        "team_id": team_id,
        "team_name": None,
        "team_abbreviation": tricode,
    }


#: LeBron has a stored Season Diet in the shared fixture; the other two do
#: not, so neither can be scored or listed.
CATALOG_ROWS = (
    _catalog_row(2544, "LeBron James", LAL, "LAL"),
    _catalog_row(1629029, "Luka Dončić", LAL, "LAL"),
    _catalog_row(1628369, "Jayson Tatum", BOS, "BOS"),
    # Namesakes: two catalog athletes who normalize to the same name.
    _catalog_row(1630001, "Jalen Williams", BOS, "BOS"),
    _catalog_row(1630002, "Jalen Williams", LAL, "LAL"),
)


class _RecordedAthleteCatalog:
    def __init__(self, rows):
        self.rows = tuple(rows)
        self.calls = []

    def get_catalog(self, season, *, active_only=False):
        self.calls.append((season, active_only))
        assert season == SEASON
        return [dict(row) for row in self.rows]


def _fixture_client(
    tmp_path, *, drop_last_15=False, with_service=False, diet_players=()
):
    engine = create_engine(f"sqlite:///{tmp_path / 'unscheduled.sqlite3'}")
    run_migrations(engine)
    settings = RuntimeSettings(
        environment="testing",
        auth=AuthenticationSettings(firebase_admin_disabled=True),
        cache=CacheSettings(enabled=False),
        nba=NBASeasonSettings(current_season=SEASON),
    )
    catalog = StatisticCatalog.load_default()
    stats_freshness = StatsFreshnessRepository(engine)
    stats_freshness.record_success(NOW)
    team_matchups = _team_matchups(engine)
    if drop_last_15:
        _drop_last_15_snapshots(engine)
    player_diets = _player_diets(engine)
    if diet_players:
        _copy_lebron_diet(player_diets, diet_players)
    service = MatchupService(
        event_catalog=_event_catalog(engine, settings),
        player_pool=_player_pool(engine),
        player_logs=_player_logs(engine, catalog),
        player_diets=player_diets,
        team_matchups=team_matchups,
        stats_freshness=stats_freshness,
        settings=settings,
        statistic_catalog=catalog,
        injuries=None,
        clock=lambda: NOW,
        athlete_catalog=_RecordedAthleteCatalog(CATALOG_ROWS),
        engine=engine,
    )
    dependencies = SimpleNamespace(
        settings=settings,
        matchup_service=service,
        user_service=SimpleNamespace(create_or_update_user=lambda _user: None),
    )
    app = create_app(
        {
            "TESTING": True,
            "RUNTIME_SETTINGS": settings,
            "DEPENDENCIES": dependencies,
            "SKIP_FIREBASE_INIT": True,
            "SKIP_TABLE_CREATE": True,
        }
    )
    client = app.test_client()
    return (client, service) if with_service else client


def _copy_lebron_diet(player_diets, player_ids):
    """Republish the season Diet with LeBron's facts also stored for others."""

    stored = player_diets.repository.get_for_players(SEASON, (2544,))
    facts = [
        PlayerDietFact(
            player_id,
            fact.base,
            fact.slice_key,
            fact.share,
            fact.volume,
            fact.games_played,
            fact.volume_unit,
            fact.provider,
        )
        for player_id in (2544, *player_ids)
        for fact in stored.players[2544]
    ]
    player_diets.repository.publish(
        SEASON,
        tuple(facts),
        tuple(
            PlayerDietObservation(observation.base, observation.status)
            for observation in stored.observations
        ),
        retrieved_at=NOW,
    )


class _Generation:
    """One captured Publication generation with no activated stream."""

    def metadata(self):
        return {
            "streams": {},
            "mixed_cutoff": False,
            "mixed_freshness": False,
            "coverage_cutoffs": [],
        }


class _SnapshotRecorder:
    """Records the snapshot each read receives, then reads the fixture."""

    def __init__(self, target, seen):
        self._target = target
        self._seen = seen

    def __getattr__(self, name):
        method = getattr(self._target, name)

        def read(*args, publication_snapshot=None, **kwargs):
            self._seen.append((name, publication_snapshot))
            return call_with_read_scope(method, *args, **kwargs)

        return read


def test_unscheduled_matchup_reads_one_publication_snapshot(tmp_path):
    client, service = _fixture_client(tmp_path, with_service=True)
    captured = []

    def snapshot(stream_keys, *, season):
        captured.append(_Generation())
        return captured[-1]

    service.publication_reader = SimpleNamespace(snapshot=snapshot)
    seen = []
    for name in ("player_diets", "player_logs", "team_matchups"):
        setattr(service, name, _SnapshotRecorder(getattr(service, name), seen))

    response = client.get(f"{UNSCHEDULED}?team=LAL&opponent=BOS")

    assert response.status_code == 200
    (generation,) = captured
    assert sorted(name for name, _snapshot in seen) == [
        "get_for_players",
        "get_latest_window",
        "get_latest_window",
        "get_player_summaries",
        "get_read_freshness",
        "publication_season_is_complete",
    ]
    assert all(snapshot is generation for _name, snapshot in seen)
    assert response.get_json()["provenance"] == {}


def test_unscheduled_scores_equal_the_game_matchup_scores(tmp_path):
    """Both routes score LeBron against Boston from the same latest windows.

    The fixture game tips after the fixture clock, so the game Matchup reads
    the latest Defense Sheet windows too: the two documents read the same
    evidence and must agree market by market and window by window.
    """

    client = _fixture_client(tmp_path)

    game = client.get(f"/api/games/matchup?game_id={GAME_ID}")
    unscheduled = client.get(f"{UNSCHEDULED}?player_id=2544&opponent=BOS")

    assert game.status_code == 200
    assert unscheduled.status_code == 200
    game_player = next(
        row for row in game.get_json()["players"] if row["canonical_id"] == 2544
    )
    (unscheduled_player,) = unscheduled.get_json()["players"]
    game_scores = game_player["scores"]
    unscheduled_scores = unscheduled_player["scores"]
    assert game_scores
    # With no Player Pool, every scoreable category is scored; the game
    # Matchup's posted markets are all among them.
    assert set(game_scores) <= set(unscheduled_scores)
    for market, windows in game_scores.items():
        for window_name in ("season", "last_15"):
            assert unscheduled_scores[market][window_name] == windows[window_name], (
                market,
                window_name,
            )
    # The windows really carry scores, so the comparison is not vacuous.
    assert game_scores["PTS"]["season"]["blend"] is not None
    assert game_scores["PTS"]["last_15"]["blend"] is not None
    for key in ("diet_shares", "diet_thin", "season_scoring", "last_10_minutes"):
        assert unscheduled_player[key] == game_player[key], key
    # The opponent's Defense Sheet is the same sheet the game shows for BOS.
    game_bos = next(team for team in game.get_json()["teams"] if team["team_id"] == BOS)
    (unscheduled_bos,) = unscheduled.get_json()["teams"]
    assert unscheduled_bos == game_bos
    assert unscheduled.get_json()["league"] == game.get_json()["league"]


def test_unscheduled_matchup_omits_every_per_game_part(tmp_path):
    client = _fixture_client(tmp_path)

    response = client.get(f"{UNSCHEDULED}?player_id=2544&opponent=bos")

    assert response.status_code == 200
    payload = response.get_json()
    assert set(payload) == {
        "experience",
        "as_of",
        "league",
        "teams",
        "players",
        "freshness",
        "provenance",
        "coverage",
    }
    experience = payload["experience"]
    assert experience["mode"] == "unscheduled"
    assert experience["player_source"] == "athlete_catalog"
    assert set(experience["sections"]) == {"season_defense", "last_15_defense"}
    assert experience["sections"]["season_defense"] == {
        "status": "available",
        "source": "team_matchup_publication",
        "context": "latest",
        "unavailable_reason": None,
    }
    assert "participants_basis" not in experience
    assert payload["as_of"] == {
        "season": SEASON,
        "season_defense": "2026-01-15",
        "last_15_defense": "2026-01-15",
        "player_diets": "2026-01-15",
    }
    assert set(payload["freshness"]) == {
        "stats",
        "team_matchups",
        "player_diets",
        "player_game_logs",
    }
    # Player mode shows only the opponent's Defense Sheet.
    assert [team["tricode"] for team in payload["teams"]] == ["BOS"]
    assert payload["teams"][0]["name"] == "Boston Celtics"
    (player,) = payload["players"]
    assert PER_GAME_PLAYER_KEYS.isdisjoint(player)
    assert player["canonical_id"] == 2544
    assert player["name"] == "LeBron James"
    assert player["team_id"] == LAL
    assert player["tricode"] == "LAL"
    assert player["player_source"] == "athlete_catalog"
    assert player["provenance"] == {}
    assert player["stat_categories"] == sorted(player["scores"])
    # Current-mode blend rules: a scored offensive window keeps its blend and
    # defensive markets carry none.
    assert player["scores"]["PTS"]["season"]["blend"] is not None
    assert "blend" not in player["scores"]["TOV"]["season"]


def test_unscheduled_matchup_resolves_a_player_name_like_the_profile(tmp_path):
    client = _fixture_client(tmp_path)

    by_id = client.get(f"{UNSCHEDULED}?player_id=2544&opponent=BOS")
    by_name = client.get(f"{UNSCHEDULED}?player_name=lebron%20JAMES.&opponent=BOS")

    assert by_name.status_code == 200
    assert by_name.get_json() == by_id.get_json()


def test_unscheduled_last_15_is_unavailable_with_a_reason(tmp_path):
    client = _fixture_client(tmp_path, drop_last_15=True)

    response = client.get(f"{UNSCHEDULED}?player_id=2544&opponent=BOS")

    assert response.status_code == 200
    payload = response.get_json()
    # The section repeats the first governed Base reason, as the game
    # Matchup's does; Synergy's permanent Last-15 reason comes first.
    assert payload["experience"]["sections"]["last_15_defense"] == {
        "status": "unavailable",
        "source": None,
        "context": None,
        "unavailable_reason": "provider_window_unsupported",
    }
    assert payload["league"]["surface_availability"]["shot_zones"]["last_15"] == {
        "status": "missing",
        "unavailable_reason": "not_stored",
    }
    assert payload["experience"]["sections"]["season_defense"]["status"] == (
        "available"
    )
    assert payload["as_of"]["last_15_defense"] is None
    assert payload["as_of"]["season_defense"] == "2026-01-15"
    scores = payload["players"][0]["scores"]
    assert scores["PTS"]["last_15"]["blend"] is None
    assert "team_defense:shot_zones" in scores["PTS"]["last_15"]["missing_inputs"]
    assert scores["PTS"]["season"]["blend"] is not None


def test_unscheduled_team_mode_scores_the_teams_players_with_diets(tmp_path):
    client = _fixture_client(tmp_path)

    response = client.get(f"{UNSCHEDULED}?team=LAL&opponent=BOS")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["experience"]["participants_basis"] == {
        "source": "athlete_catalog",
        "context": "season_roster_with_player_diet",
    }
    assert [team["tricode"] for team in payload["teams"]] == ["LAL", "BOS"]
    assert payload["teams"][0]["name"] == "Los Angeles Lakers"
    # Luka is on the roster but has no Diet; Tatum is the opponent's player.
    assert [player["canonical_id"] for player in payload["players"]] == [2544]
    player = payload["players"][0]
    assert PER_GAME_PLAYER_KEYS.isdisjoint(player)
    single = client.get(f"{UNSCHEDULED}?player_id=2544&opponent=BOS").get_json()
    assert player["scores"] == single["players"][0]["scores"]


@pytest.mark.parametrize(
    ("query", "reason", "message"),
    [
        # Not in the current-season Athlete Catalog.
        (
            "player_id=203999&opponent=BOS",
            "player_not_found",
            "The requested player was not found.",
        ),
        (
            "player_name=Nobody%20Here&opponent=BOS",
            "player_not_found",
            "The requested player was not found.",
        ),
        # A real name, but not on the named team.
        (
            "player_name=LeBron%20James&player_team=BOS&opponent=CHA",
            "player_not_found",
            "The requested player was not found.",
        ),
        # In the catalog, but no current-season Diet.
        (
            "player_id=1629029&opponent=BOS",
            "no_player_diet",
            "The requested player has no current-season Player Diet.",
        ),
        (
            "player_name=Luka%20Doncic&opponent=BOS",
            "no_player_diet",
            "The requested player has no current-season Player Diet.",
        ),
        # Well-formed tricodes that name no NBA team.
        (
            "player_id=2544&opponent=XYZ",
            "team_not_found",
            "The requested team was not found.",
        ),
        (
            "team=XYZ&opponent=BOS",
            "team_not_found",
            "The requested team was not found.",
        ),
        (
            "player_name=LeBron%20James&player_team=XYZ&opponent=BOS",
            "team_not_found",
            "The requested team was not found.",
        ),
    ],
)
def test_unscheduled_matchup_reports_unknown_resources(
    tmp_path, query, reason, message
):
    client = _fixture_client(tmp_path)

    response = client.get(f"{UNSCHEDULED}?{query}")

    assert response.status_code == 404
    assert response.get_json()["error"] == {
        "code": "resource_not_found",
        "message": message,
        "details": {"reason": reason},
    }


def test_unscheduled_matchup_refuses_an_ambiguous_player_name(tmp_path):
    client = _fixture_client(tmp_path, diet_players=(1630001, 1630002))

    response = client.get(f"{UNSCHEDULED}?player_name=jalen%20williams&opponent=CHA")

    assert response.status_code == 400
    error = response.get_json()["error"]
    assert error["code"] == "invalid_input"
    assert error["details"] == {
        "parameter": "player_name",
        "parameters": ["player_name"],
        "candidates": [
            {"name": "Jalen Williams", "team": "BOS"},
            {"name": "Jalen Williams", "team": "LAL"},
        ],
    }


def test_unscheduled_matchup_ignores_namesakes_without_a_diet(tmp_path):
    """Only a namesake the route could score makes a name ambiguous."""

    client = _fixture_client(tmp_path, diet_players=(1630002,))

    response = client.get(f"{UNSCHEDULED}?player_name=Jalen%20Williams&opponent=BOS")

    assert response.status_code == 200
    assert [row["canonical_id"] for row in response.get_json()["players"]] == [
        1630002
    ]


def test_unscheduled_player_team_narrows_a_namesake(tmp_path):
    client = _fixture_client(tmp_path, diet_players=(1630001, 1630002))

    response = client.get(
        f"{UNSCHEDULED}?player_name=Jalen%20Williams&player_team=bos&opponent=CHA"
    )

    assert response.status_code == 200
    (player,) = response.get_json()["players"]
    assert (player["canonical_id"], player["tricode"]) == (1630001, "BOS")


class _CompletedSeasonWindows:
    """The fixture's windows, with their publications' governance saying the
    configured season is over."""

    def __init__(self, target):
        self._target = target
        self.snapshots = []

    def __getattr__(self, name):
        return getattr(self._target, name)

    def publication_season_is_complete(self, season, *, publication_snapshot=None):
        assert season == SEASON
        self.snapshots.append(publication_snapshot)
        return True


def test_unscheduled_defense_sections_say_completed_season_in_the_offseason(
    tmp_path,
):
    client, service = _fixture_client(tmp_path, with_service=True)
    service.team_matchups = _CompletedSeasonWindows(service.team_matchups)

    payload = client.get(f"{UNSCHEDULED}?player_id=2544&opponent=BOS").get_json()

    sections = payload["experience"]["sections"]
    assert sections["season_defense"]["context"] == "completed_season"
    assert sections["last_15_defense"]["context"] == "completed_season"
    assert sections["season_defense"]["status"] == "available"
    assert service.team_matchups.snapshots


def test_unscheduled_offseason_keeps_an_unavailable_section_contextless(tmp_path):
    client, service = _fixture_client(tmp_path, with_service=True, drop_last_15=True)
    service.team_matchups = _CompletedSeasonWindows(service.team_matchups)

    sections = client.get(
        f"{UNSCHEDULED}?player_id=2544&opponent=BOS"
    ).get_json()["experience"]["sections"]

    assert sections["season_defense"]["context"] == "completed_season"
    assert sections["last_15_defense"]["context"] is None
