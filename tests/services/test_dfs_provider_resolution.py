"""Dabble and Underdog lines resolve to games, players and statistics.

Recorded payloads (2026-10-15) are replayed through the real adapters and the
real resolvers.  Catalog rows have the shape production stores: nba_api's
``teamName`` (the nickname, "Pistons") and ``teamTricode`` ("DET").  A hand-made
full-name row hid the original defect, so none is used here.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.domain.statistics import MatchState
from app.providers.dabble import DabbleAdapter
from app.providers.dfs import (
    AthleteEvidence,
    NBAMarketQuery,
    RetrievalContext,
    TeamEvidence,
)
from app.providers.underdog import UnderdogAdapter
from app.services.athlete_resolver import AthleteResolver, MappingResolutionState
from app.services.event_resolver import EventResolutionState, EventResolver
from app.services.statistic_catalog import StatisticCatalog

FIXTURES = Path(__file__).parents[1] / "fixtures"
SEASON = "2026-27"

DET, BOS, NYK, PHI, TOR = 1610612765, 1610612738, 1610612752, 1610612755, 1610612761


def _athlete_row(player_id, name, team_id, nickname, tricode):
    return {
        "season": SEASON, "player_id": player_id, "display_name": name,
        "roster_status": "Active", "is_active": True, "team_id": team_id,
        "team_name": nickname, "team_abbreviation": tricode,
    }


def _event_row(game_id, when, home, away):
    (home_id, home_name, home_code), (away_id, away_name, away_code) = home, away
    return {
        "nba_game_id": game_id, "season": SEASON, "scheduled_at": when,
        "home_team_id": home_id, "home_team_name": home_name,
        "home_team_tricode": home_code,
        "away_team_id": away_id, "away_team_name": away_name,
        "away_team_tricode": away_code,
        "status_text": "Scheduled", "status_code": 1, "postponed_status": None,
        "postponement_evidence": None, "classification": "Regular Season",
        "is_postponed": False,
    }


class _AthleteCatalog:
    ROWS = [
        _athlete_row(1630595, "Cade Cunningham", DET, "Pistons", "DET"),
        _athlete_row(1628973, "Jalen Brunson", NYK, "Knicks", "NYK"),
        _athlete_row(1628369, "Jayson Tatum", BOS, "Celtics", "BOS"),
        _athlete_row(1629029, "Mikal Bridges", NYK, "Knicks", "NYK"),
        _athlete_row(1630202, "Josh Hart", NYK, "Knicks", "NYK"),
        _athlete_row(2544, "LeBron James", PHI, "76ers", "PHI"),
        _athlete_row(1630178, "Tyrese Maxey", PHI, "76ers", "PHI"),
        _athlete_row(1630567, "Scottie Barnes", TOR, "Raptors", "TOR"),
        _athlete_row(1629628, "RJ Barrett", TOR, "Raptors", "TOR"),
    ]

    def get_catalog(self, season, active_only=False):
        return [row for row in self.ROWS if row["season"] == season]


class _EventCatalog:
    ROWS = [
        _event_row(
            "0022600001", datetime(2026, 10, 15, 23, 30, tzinfo=timezone.utc),
            (NYK, "Knicks", "NYK"), (TOR, "Raptors", "TOR"),
        ),
        _event_row(
            "0022600077", datetime(2026, 10, 20, 23, 0, tzinfo=timezone.utc),
            (NYK, "Knicks", "NYK"), (PHI, "76ers", "PHI"),
        ),
        _event_row(
            "0022600078", datetime(2026, 10, 20, 19, 5, tzinfo=timezone.utc),
            (DET, "Pistons", "DET"), (BOS, "Celtics", "BOS"),
        ),
    ]

    def get_events(self, season):
        return [row for row in self.ROWS if row["season"] == season]

    def get_freshness(self, season):
        return {"season": season, "fresh": True, "last_success_at": "2026-10-15T12:00:00+00:00"}


class _Response:
    def __init__(self, payload):
        self.payload = payload
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Session:
    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.headers = {}

    def get(self, url, **kwargs):
        return _Response(self.payloads.pop(0))


def _json(*parts):
    return json.loads(FIXTURES.joinpath(*parts).read_text(encoding="utf-8"))


def _context():
    return RetrievalContext(deadline="2099-01-01T00:00:00Z", request_id="resolution-test")


def _underdog():
    session = _Session(_json("underdog", "recorded_2026-10-15.json"))
    return UnderdogAdapter(session=session).get_snapshot(NBAMarketQuery(), _context())


def _dabble():
    session = _Session(
        _json("dabble", "recorded_2026-10-15.competitions.json"),
        _json("dabble", "recorded_2026-10-15.fixtures.json"),
        _json("dabble", "recorded_2026-10-15.fixture_details.json"),
    )
    return DabbleAdapter(session=session).get_snapshot(NBAMarketQuery(), _context())


# -- D1: team evidence in the athlete resolver --------------------------------


def _resolve_cade(team: TeamEvidence | None):
    return AthleteResolver(_AthleteCatalog()).resolve(
        "dabble",
        AthleteEvidence(provider_id="p-cade", name="Cade Cunningham", team=team),
        SEASON,
    )


def test_agreeing_tricodes_win_over_a_full_name_that_differs_from_the_nickname():
    result = _resolve_cade(TeamEvidence(name="Detroit Pistons", abbreviation="DET"))

    assert result.state is MappingResolutionState.AUTO
    assert result.canonical_athlete.player_id == 1630595


def test_different_tricodes_conflict_even_when_the_names_agree():
    result = _resolve_cade(TeamEvidence(name="Pistons", abbreviation="BOS"))

    assert result.state is MappingResolutionState.TEAM_CONFLICT


def test_a_full_name_without_a_tricode_counts_as_its_catalog_tricode():
    assert _resolve_cade(TeamEvidence(name="Detroit Pistons")).state is (
        MappingResolutionState.AUTO
    )
    assert _resolve_cade(TeamEvidence(name="Boston Celtics")).state is (
        MappingResolutionState.TEAM_CONFLICT
    )


def test_an_unknown_name_without_a_tricode_is_compared_by_name_as_before():
    assert _resolve_cade(TeamEvidence(name="Pistons")).state is MappingResolutionState.AUTO
    assert _resolve_cade(TeamEvidence(name="Motor City")).state is (
        MappingResolutionState.TEAM_CONFLICT
    )


# -- D2/D3/D4: recorded Underdog and Dabble payloads ---------------------------


def test_underdog_lines_resolve_to_their_game_player_and_statistic():
    snapshot = _underdog()
    athletes = AthleteResolver(_AthleteCatalog())
    events = EventResolver(_EventCatalog())
    statistics = StatisticCatalog.load_default().resolver

    seen = {}
    for market in snapshot.markets:
        athlete = athletes.resolve_market(market, SEASON)
        event = events.resolve_market(market, SEASON)
        statistic = statistics.resolve_market(market)
        seen[market.market_id] = (
            athlete.state,
            athlete.canonical_athlete and athlete.canonical_athlete.player_id,
            event.state,
            event.canonical_event_id,
            statistic.state,
            statistic.canonical and statistic.canonical.id,
        )

    auto, matched, canonical = (
        MappingResolutionState.AUTO,
        EventResolutionState.AUTO,
        MatchState.CANONICAL,
    )
    assert seen == {
        # LeBron James and Tyrese Maxey, Points, PHI @ NYK 2026-10-20.
        "2fa5c85c-f7ea-4b06-aa69-dc3ef0d91adf": (auto, 2544, matched, "0022600077", canonical, "points"),
        "057a031d-b6e6-40b6-b417-e66ba2d12964": (auto, 1630178, matched, "0022600077", canonical, "points"),
        # Mikal Bridges Points and Josh Hart Assists, the home side of the same game.
        "d3a2954e-492b-440b-b0cb-fa42b80fbd15": (auto, 1629029, matched, "0022600077", canonical, "points"),
        "b8eed623-0a87-4ca6-bf51-1aa4fa9ae0e0": (auto, 1630202, matched, "0022600077", canonical, "assists"),
        # "Pts + Rebs + Asts", TOR @ NYK 2026-10-15.
        "61d50281-1530-4117-b386-8c3df0fa97bf": (auto, 1630567, matched, "0022600001", canonical, "pra"),
        "655696de-0883-47cc-8dd0-481d9c2c7392": (auto, 1629628, matched, "0022600001", canonical, "pra"),
    }


@pytest.mark.parametrize(
    ("label", "canonical_id"),
    [
        ("Pts + Rebs + Asts", "pra"),
        ("Pts + Asts", "pa"),
        ("Pts + Rebs", "pr"),
        ("Rebs + Asts", "ra"),
    ],
)
def test_underdog_spaced_combo_labels_map_to_their_statistic(label, canonical_id):
    from app.providers.dfs import StatisticEvidence

    match = StatisticCatalog.load_default().resolver.resolve(
        "underdog", StatisticEvidence(label=label), scoring_period="full_game"
    )

    assert match.state is MatchState.CANONICAL
    assert match.canonical.id == canonical_id


def test_underdog_event_teams_carry_tricodes_and_names_from_the_titles():
    market = next(m for m in _underdog().markets if m.market_id == "2fa5c85c-f7ea-4b06-aa69-dc3ef0d91adf")

    assert (market.event.away_team.abbreviation, market.event.away_team.name) == (
        "PHI", "Philadelphia 76ers",
    )
    assert (market.event.home_team.abbreviation, market.event.home_team.name) == (
        "NYK", "New York Knicks",
    )
    assert market.team.abbreviation == "PHI"
    assert market.opponent.abbreviation == "NYK"


def test_dabble_lines_resolve_with_standard_and_one_sided_variants_and_periods():
    snapshot = _dabble()
    athletes = AthleteResolver(_AthleteCatalog())
    statistics = StatisticCatalog.load_default().resolver

    rows = {}
    for market in snapshot.markets:
        athlete = athletes.resolve_market(market, SEASON)
        statistic = statistics.resolve_market(market)
        rows[(market.statistic.label, str(market.threshold.value))] = (
            athlete.state,
            athlete.canonical_athlete and athlete.canonical_athlete.player_id,
            market.scoring_period.value,
            market.variant.value,
            market.variant_label,
            statistic.canonical and statistic.canonical.id,
        )

    auto = MappingResolutionState.AUTO
    cade = 1630595
    assert rows == {
        ("assists", "9.5"): (auto, cade, "full_game", "standard", "standard", "assists"),
        ("threes-made", "2.5"): (auto, cade, "full_game", "standard", "standard", "three_pointers_made"),
        ("threes-made", "1.5"): (auto, cade, "full_game", "alternate", "one_sided", "three_pointers_made"),
        ("ft-made", "5.5"): (auto, cade, "full_game", "standard", "standard", None),
        ("fg-attempted", "18.5"): (auto, cade, "full_game", "standard", "standard", "field_goals_attempted"),
        ("first-quarter-points", "5.5"): (auto, cade, "first_quarter", "standard", "standard", None),
    }


# -- Dabble game resolution and period guards ----------------------------------


def _dabble_with(mutate):
    detail = _json("dabble", "recorded_2026-10-15.fixture_details.json")
    mutate(detail["sportFixtureDetail"])
    session = _Session(
        _json("dabble", "recorded_2026-10-15.competitions.json"),
        _json("dabble", "recorded_2026-10-15.fixtures.json"),
        detail,
    )
    return DabbleAdapter(session=session).get_snapshot(NBAMarketQuery(), _context())


def _periods(snapshot):
    return {
        (m.statistic.label, str(m.threshold.value)): m.scoring_period.value
        for m in snapshot.markets
    }


def test_dabble_lines_resolve_to_the_catalog_game():
    events = EventResolver(_EventCatalog())

    resolved = {
        (m.market_id[-6:], events.resolve_market(m, SEASON).state, events.resolve_market(m, SEASON).canonical_event_id)
        for m in _dabble().markets
    }

    assert {(state, game) for _, state, game in resolved} == {
        (EventResolutionState.AUTO, "0022600078")
    }
    assert len(resolved) == 6


def test_dabble_sides_come_from_the_scoreboard_display_order():
    market = _dabble().markets[0]

    assert (market.event.away_team.abbreviation, market.event.away_team.name) == (
        "BOS", "Boston Celtics",
    )
    assert (market.event.home_team.abbreviation, market.event.home_team.name) == (
        "DET", "Detroit Pistons",
    )


def test_dabble_gives_no_sides_when_the_title_disagrees_with_the_scoreboard():
    snapshot = _dabble_with(lambda d: d.update(name="Detroit Pistons @ Boston Celtics"))

    assert {(m.event.away_team, m.event.home_team) for m in snapshot.markets} == {(None, None)}


def test_dabble_period_stays_unknown_without_a_market_type():
    def strip(detail):
        detail["markets"] = []

    assert _periods(_dabble_with(strip)) == {
        ("assists", "9.5"): "full_game",  # a known whole-game stat needs no marker
        ("threes-made", "2.5"): "unknown",
        ("threes-made", "1.5"): "unknown",
        ("ft-made", "5.5"): "unknown",
        ("fg-attempted", "18.5"): "unknown",
        ("first-quarter-points", "5.5"): "first_quarter",
    }


def test_dabble_period_stays_unknown_for_a_mismatched_market_type():
    def mismatch(detail):
        for market in detail["markets"]:
            if market["resultingType"] == "swish_threes_made":
                market["resultingType"] = "swish_assists"
            elif market["resultingType"] == "swish_ft_made":
                market["resultingType"] = "swish_first_quarter_ft_made"
            elif market["resultingType"] == "swish_fg_attempted":
                market["resultingType"] = "swish_to_have_3_plus_threes_made"

    periods = _periods(_dabble_with(mismatch))

    assert periods[("threes-made", "2.5")] == "unknown"
    assert periods[("ft-made", "5.5")] == "unknown"
    assert periods[("fg-attempted", "18.5")] == "unknown"


def test_dabble_period_qualified_stat_is_never_full_game():
    def third_quarter(detail):
        for prop in detail["playerProps"]:
            if prop["stats"] == ["threes-made"] and prop["value"] == 2.5:
                prop["stats"] = ["third-quarter-points"]
        for market in detail["markets"]:
            if market["resultingType"] == "swish_threes_made":
                market["resultingType"] = "swish_third_quarter_points"

    assert _periods(_dabble_with(third_quarter))[("third-quarter-points", "2.5")] == "unknown"


def test_underdog_titles_without_known_nba_tricodes_give_no_tricodes():
    payload = _json("underdog", "recorded_2026-10-15.json")
    for game in payload["games"]:
        game["abbreviated_title"] = "XXX @ NYK"
    session = _Session(payload)

    snapshot = UnderdogAdapter(session=session).get_snapshot(NBAMarketQuery(), _context())

    assert {
        (m.event.away_team.abbreviation, m.event.home_team.abbreviation)
        for m in snapshot.markets
    } == {(None, None)}


def test_dabble_gives_no_sides_for_a_scoreboard_tricode_that_is_not_an_nba_team():
    def foreign(detail):
        for entry in detail["stateSummary"]["scoreboard"].values():
            if entry["abbreviation"] == "BOS":
                entry["abbreviation"] = "XXX"

    snapshot = _dabble_with(foreign)

    assert {(m.event.away_team, m.event.home_team) for m in snapshot.markets} == {(None, None)}


def test_dabble_gives_no_sides_when_both_competitors_share_a_display_order():
    def duplicate(detail):
        for entry in detail["stateSummary"]["scoreboard"].values():
            entry["competitorDisplayOrder"] = 1

    snapshot = _dabble_with(duplicate)

    assert {(m.event.away_team, m.event.home_team) for m in snapshot.markets} == {(None, None)}
