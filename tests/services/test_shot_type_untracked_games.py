"""Shot-type publications over games NBA.com tracking never covered.

DEN @ IND on 2026-11-07 (``0022600189``) is declared untracked.  Tracking
short by exactly that game publishes without it; any other shortfall is
ordinary lag and blocks.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import Integer, create_engine, select
from sqlalchemy.orm import Session

from app.collector import untracked_games as untracked_games_module
from app.collector.normalizers import (
    normalize_grouped_shot_response,
    normalize_opponent_grouped_shot_response,
)
from app.domain.slate_time import slate_date_for_instant
from app.migrations import run_migrations
from app.models.canonical_game_ledger import (
    CanonicalGameLedgerGame,
    CanonicalGameLedgerPlayerFact,
    CanonicalGameLedgerTeamFact,
)
from app.models.collection_control import PublicationPointer
from app.services.collection_control import (
    NBA_TEAM_IDS,
    CollectionControlService,
    CollectionOperationsService,
    CollectorTokenService,
    ControlPlaneError,
    ObservationIngestionService,
    PublicationService,
)
from app.services.database_first_activation import decode_team_window
from app.services.ledger_runtime import ActiveManifestLedgerGovernanceReader
from app.services.team_matchup_publications import (
    PublicationValidationError,
    publication_stream,
    validate_publication_rows,
)
from tests.test_residential_collector import _grouped_shot_type_control_plane

SEASON = "2026-27"
DEN = 1610612743
IND = 1610612754
LAL = 1610612747
DEN_AT_IND = "0022600189"
GAME_DAY = date(2026, 11, 7)
SHOT_TYPES = ("Catch and Shoot", "Pullups", "Less Than 10 ft")
OPPONENT_SEASON = publication_stream("shot_types", "season")
OPPONENT_L15 = publication_stream("shot_types", "l15")

JOKIC = 203999
MURRAY = 1627750
HALIBURTON = 1630169
SIAKAM = 1627783
#: An IND player who did not play on 2026-11-07.
WALKER = 1641767
LEBRON = 2544


def _rounds(*, before: int, after: int) -> list[tuple[date, list[tuple[str, int, int]]]]:
    """A round robin of all thirty teams, one round a day, DEN @ IND only on 2026-11-07."""

    others = sorted(int(team_id) for team_id in NBA_TEAM_IDS if int(team_id) not in {DEN, IND})
    circle = [IND, *others, DEN]
    schedule = []
    for offset in range(-before, after + 1):
        turn = offset % 29
        rotated = [circle[0], *(circle[1:][-turn:] + circle[1:][:-turn])] if turn else circle
        games = []
        for index in range(15):
            home, away = rotated[index], rotated[29 - index]
            game_id = (
                DEN_AT_IND if offset == 0 and {home, away} == {DEN, IND} else f"g{offset:+03d}-{index:02d}"
            )
            games.append((game_id, home, away))
        schedule.append((GAME_DAY + timedelta(days=offset), games))
    return schedule


def _events(schedule):
    return [
        {
            "nba_game_id": game_id,
            "home_team_id": home,
            "away_team_id": away,
            "scheduled_at": datetime(day.year, day.month, day.day, 23, 0, tzinfo=UTC).isoformat(),
        }
        for day, games in schedule
        for game_id, home, away in games
    ]


def _cutoff(schedule) -> datetime:
    last_day = schedule[-1][0] + timedelta(days=1)
    return datetime(last_day.year, last_day.month, last_day.day, 9, 45, tzinfo=UTC)


def _zeroed(model, **values):
    defaults = {
        column.name: 0
        for column in model.__table__.columns
        if not column.nullable and isinstance(column.type, Integer) and column.name not in values
    }
    return model(**{**defaults, **values})


def _store_ledger_game(engine, *, team_minutes=48.0, players=None):
    """The governed ledger's DEN @ IND, its team minutes in game minutes as the ledger writes them."""

    if players is None:
        players = {JOKIC: (DEN, 36.0), MURRAY: (DEN, 34.0), HALIBURTON: (IND, 35.0), SIAKAM: (IND, 33.0)}
    now = datetime(2026, 11, 8, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        session.add(
            CanonicalGameLedgerGame(
                game_id=DEN_AT_IND,
                season=SEASON,
                season_type="Regular Season",
                game_date=GAME_DAY,
                home_team_id=IND,
                home_team_tricode="IND",
                away_team_id=DEN,
                away_team_tricode="DEN",
                status="final",
                source_observation_id="ledger-0022600189",
                checksum="0" * 64,
                retrieved_at=now,
                updated_at=now,
            )
        )
        for team_id, tricode, opponent_id, opponent in ((IND, "IND", DEN, "DEN"), (DEN, "DEN", IND, "IND")):
            session.add(
                _zeroed(
                    CanonicalGameLedgerTeamFact,
                    game_id=DEN_AT_IND,
                    team_id=team_id,
                    team_tricode=tricode,
                    opponent_team_id=opponent_id,
                    opponent_team_tricode=opponent,
                    is_home=team_id == IND,
                    team_minutes=team_minutes,
                )
            )
        for player_id, (team_id, minutes) in players.items():
            session.add(
                _zeroed(
                    CanonicalGameLedgerPlayerFact,
                    game_id=DEN_AT_IND,
                    player_id=player_id,
                    team_id=team_id,
                    player_name=str(player_id),
                    team_tricode="DEN" if team_id == DEN else "IND",
                    minutes=minutes,
                )
            )


@pytest.fixture
def declared(monkeypatch):
    """Only DEN @ IND is declared; ``declared(False)`` declares nothing."""

    entry = next(
        game for game in untracked_games_module.UNTRACKED_GAMES[SEASON] if game.game_id == DEN_AT_IND
    )

    def declare(on=True):
        monkeypatch.setitem(untracked_games_module.UNTRACKED_GAMES, SEASON, (entry,) if on else ())

    declare()
    return declare


# Player shot-type Diets.

DIET_SCHEDULE = _rounds(before=0, after=1)
DIET_CUTOFF = _cutoff(DIET_SCHEDULE)
#: Every shooter played on 11/07 and 11/08, except WALKER (11/08 only).
DIET_BOX_GP = {JOKIC: 2, MURRAY: 2, HALIBURTON: 2, SIAKAM: 2, WALKER: 1, LEBRON: 2}


def _diet_world(tmp_path, tracking_gp, *, overrides=None):
    """``overrides`` maps a shot type to the (Overall, category) games its observation carries instead."""

    engine, manifest, publications, deliver = _grouped_shot_type_control_plane(
        tmp_path,
        "diet.sqlite3",
        season=SEASON,
        now=DIET_CUTOFF,
        events=_events(DIET_SCHEDULE),
    )
    team_games = [{"TEAM_ID": int(team_id), "GP": 2} for team_id in sorted(NBA_TEAM_IDS)]
    overrides = overrides or {}
    observations = [
        normalize_grouped_shot_response(
            [
                {
                    "PLAYER_ID": player_id,
                    "SHOT_TYPE": category,
                    "GP": category_gp[player_id],
                    "FGA_FREQUENCY": 0.25,
                    "FGM": 10,
                    "FGA": 25,
                    "FG2M": 6,
                    "FG2A": 15,
                    "FG2A_FREQUENCY": 0.15,
                    "FG3M": 4,
                    "FG3A": 10,
                    "FG3A_FREQUENCY": 0.1,
                }
                for player_id in DIET_BOX_GP
                if category_gp[player_id] > 0
            ],
            season=SEASON,
            cutoff=DIET_CUTOFF,
            scope={
                "window": "season",
                "subject": "player",
                "phase": "Regular Season",
                "category": category,
                "endpoint_window": {
                    "date_from": None,
                    "date_to": slate_date_for_instant(DIET_CUTOFF).strftime("%m/%d/%Y"),
                },
            },
            games_response=[
                {"PLAYER_ID": player_id, "GP": games, "FGA": 100} for player_id, games in DIET_BOX_GP.items()
            ],
            team_games_response=team_games,
            tracking_response=[
                {"PLAYER_ID": player_id, "GP": overall_gp[player_id]}
                for player_id in DIET_BOX_GP
                if overall_gp[player_id] > 0
            ],
        )
        for category in SHOT_TYPES
        for overall_gp, category_gp in [overrides.get(category, (tracking_gp, tracking_gp))]
    ]
    deliver(None, observations=observations)
    _store_ledger_game(engine, players={**_DIET_PARTICIPANTS})
    return publications, manifest


_DIET_PARTICIPANTS = {
    JOKIC: (DEN, 36.0),
    MURRAY: (DEN, 34.0),
    HALIBURTON: (IND, 35.0),
    SIAKAM: (IND, 33.0),
    # Listed in the box score with no minutes: he did not play.
    WALKER: (IND, 0.0),
}


def _compose_diet(publications, manifest):
    version = publications.compose_from_observations(
        "grouped_shot_types", season=SEASON, cutoff=DIET_CUTOFF, manifest_id=manifest.manifest_id
    )
    return {
        (row["player_id"], row["slice_key"]): (row["share"], row["games_played"])
        for row in json.loads(version.payload)["rows"]
    }


def _diet(games_by_player):
    return {
        (player_id, slice_key): (0.25, games)
        for player_id, games in games_by_player.items()
        for slice_key in ("catch_and_shoot", "less_than_10_ft", "pullups")
    }


def _coverage_lines(caplog):
    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("shot_type_coverage_blocked")
    ]


def test_player_diet_publishes_without_the_declared_game(tmp_path, declared):
    publications, manifest = _diet_world(
        tmp_path, {JOKIC: 1, MURRAY: 1, HALIBURTON: 1, SIAKAM: 1, WALKER: 1, LEBRON: 2}
    )

    assert _compose_diet(publications, manifest) == _diet(
        {JOKIC: 1, MURRAY: 1, HALIBURTON: 1, SIAKAM: 1, WALKER: 1, LEBRON: 2}
    )


def test_player_diet_short_by_an_undeclared_game_is_lag_and_names_the_candidate(tmp_path, declared, caplog):
    declared(False)
    publications, manifest = _diet_world(
        tmp_path, {JOKIC: 1, MURRAY: 1, HALIBURTON: 1, SIAKAM: 1, WALKER: 1, LEBRON: 2}
    )

    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(ControlPlaneError, match="^provider_window_lagging$"),
    ):
        _compose_diet(publications, manifest)

    (line,) = _coverage_lines(caplog)
    assert line == (
        "shot_type_coverage_blocked stream=grouped_shot_types season=2026-27 cutoff=2026-11-09T09:45:00+00:00 "
        "short_teams=DEN,IND unexplained=203999:2/1,1627750:2/1,1627783:2/1,1630169:2/1 "
        "declared_absent= candidates=0022600189"
    )


@pytest.mark.parametrize(
    "tracking_gp",
    [
        # Last night's DEN game is not tracked yet either: Jokic is short by two.
        {JOKIC: 0, MURRAY: 1, HALIBURTON: 1, SIAKAM: 1, WALKER: 1, LEBRON: 2},
        # Partial: Siakam is tracked in DEN @ IND while the other three are not.
        {JOKIC: 1, MURRAY: 1, HALIBURTON: 1, SIAKAM: 2, WALKER: 1, LEBRON: 2},
        # WALKER did not play on 11/07, so his one missing game is lag.
        {JOKIC: 1, MURRAY: 1, HALIBURTON: 1, SIAKAM: 1, WALKER: 0, LEBRON: 2},
    ],
    ids=["declared game plus lag", "partial declared game", "non-participant short"],
)
def test_player_diet_shortfall_the_declared_game_does_not_explain_blocks(tmp_path, declared, tracking_gp):
    publications, manifest = _diet_world(tmp_path, tracking_gp)

    with pytest.raises(ControlPlaneError, match="^provider_window_lagging$"):
        _compose_diet(publications, manifest)


def test_player_diet_with_the_declared_game_restored_counts_it(tmp_path, declared, caplog):
    publications, manifest = _diet_world(
        tmp_path, {JOKIC: 2, MURRAY: 2, HALIBURTON: 2, SIAKAM: 2, WALKER: 1, LEBRON: 2}
    )

    with caplog.at_level(logging.WARNING):
        published = _compose_diet(publications, manifest)

    assert published == _diet({JOKIC: 2, MURRAY: 2, HALIBURTON: 2, SIAKAM: 2, WALKER: 1, LEBRON: 2})
    assert _coverage_lines(caplog) == []


DECLARED_ABSENT = {JOKIC: 1, MURRAY: 1, HALIBURTON: 1, SIAKAM: 1, WALKER: 1, LEBRON: 2}
RESTORED = {JOKIC: 2, MURRAY: 2, HALIBURTON: 2, SIAKAM: 2, WALKER: 1, LEBRON: 2}


def test_player_diet_categories_disagreeing_on_the_declared_game_block(tmp_path, declared, caplog):
    # Pullups was collected after NBA.com restored DEN @ IND; the other two
    # shot types were collected before.
    publications, manifest = _diet_world(
        tmp_path, DECLARED_ABSENT, overrides={"Pullups": (RESTORED, RESTORED)}
    )

    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(ControlPlaneError, match="^provider_window_lagging$"),
    ):
        _compose_diet(publications, manifest)

    (line,) = _coverage_lines(caplog)
    assert "unexplained=203999:2/1,1627750:2/1,1627783:2/1,1630169:2/1 declared_absent= " in line


@pytest.mark.parametrize(
    ("tracking_gp", "pullups"),
    [
        # The declared game is restored; Jokic's Pullups trail his Overall.
        (RESTORED, {**RESTORED, JOKIC: 1}),
        # The declared game is absent; LeBron, who did not play in it, has
        # Pullups one game short of his Overall.
        (DECLARED_ABSENT, {**DECLARED_ABSENT, LEBRON: 1}),
    ],
    ids=["declared game restored", "declared game absent"],
)
def test_player_diet_category_lag_behind_overall_blocks(tmp_path, declared, tracking_gp, pullups):
    publications, manifest = _diet_world(tmp_path, tracking_gp, overrides={"Pullups": (tracking_gp, pullups)})

    with pytest.raises(ControlPlaneError, match="^provider_window_lagging$"):
        _compose_diet(publications, manifest)


# Team shot-type defense.


def _opponent_world(tmp_path, schedule, *, tracking_gp, minutes, ledger_minutes=48.0, ledger=True):
    """Ingest Catch and Shoot, Pullups and Less Than 10 ft for all thirty teams.

    Every team allows 40 two-point catch-and-shoot makes in every window.
    ``tracking_gp`` is keyed by (window, team) or (window, team, shot type).
    """

    cutoff = _cutoff(schedule)
    tmp_path.mkdir(exist_ok=True)
    engine = create_engine(f"sqlite:///{tmp_path / 'opponent.sqlite3'}")
    run_migrations(engine)
    control = CollectionControlService(engine, clock=lambda: cutoff)
    control.activate_season(SEASON, actor="operator")
    events = _events(schedule)
    request = control.create_bootstrap_request(SEASON, "event", cutoff=cutoff)
    control.publish_catalog(
        request.request_id,
        {
            "complete_snapshot": True,
            "events": [{**event, "phase": "Regular Season", "status": "Final"} for event in events],
        },
        version="event-v1",
    )
    request = control.create_bootstrap_request(SEASON, "athlete", cutoff=cutoff)
    control.publish_catalog(
        request.request_id,
        {
            "complete_snapshot": True,
            "identities": [
                {
                    "player_id": str(JOKIC),
                    "team_id": DEN,
                    "status": "active",
                    "event_ids": [event["nba_game_id"] for event in events],
                }
            ],
        },
        version="athlete-v1",
    )
    manifest = control.create_manifest(
        SEASON,
        cutoff=cutoff,
        scopes={OPPONENT_SEASON, OPPONENT_L15, "canonical_game_ledger"},
        collect_before=cutoff + timedelta(hours=1),
    )
    publications = PublicationService(
        engine, clock=lambda: cutoff, l15_expectation_resolver=ActiveManifestLedgerGovernanceReader(engine)
    )
    publications.register_default_streams()
    operations = CollectionOperationsService(engine, publication_service=publications, clock=lambda: cutoff)
    for stream_key in (OPPONENT_SEASON, OPPONENT_L15):
        operations.activate_stream(stream_key, actor="operator", reason="enable")
    tokens = CollectorTokenService(engine, environment="testing", signing_secret="test", clock=lambda: cutoff)
    identity = tokens.create_identity(
        "collector",
        scopes=["ingest"],
        owner="residential_collector",
        providers=["nba"],
        surfaces=["shot_types_opponent"],
    )
    claims = tokens.validate(
        tokens.issue_for_secret(identity["identity_id"], identity["secret"], scopes=["ingest"])
    )
    ingestion = ObservationIngestionService(engine, publication_service=publications, clock=lambda: cutoff)
    date_to = slate_date_for_instant(cutoff).strftime("%m/%d/%Y")
    windows = {"season": None}
    if len(schedule) >= 15:
        windows["l15"] = schedule[-15][0].strftime("%m/%d/%Y")
    for window, date_from in windows.items():
        for team_id in sorted(int(value) for value in NBA_TEAM_IDS):
            games = min(len(schedule), 15) if window == "l15" else len(schedule)
            for category in SHOT_TYPES:
                observation = normalize_opponent_grouped_shot_response(
                    [
                        {
                            "TEAM_ID": team_id,
                            "GP": games,
                            "TRACKING_GP": tracking_gp.get(
                                (window, team_id, category), tracking_gp.get((window, team_id), games)
                            ),
                            "MIN": minutes.get((window, team_id), 48.0 * games),
                            "FG2M": 40,
                            "FG2A": 80,
                            "FG3M": 20,
                            "FG3A": 60,
                        }
                    ],
                    season=SEASON,
                    cutoff=cutoff,
                    team_id=team_id,
                    window=window,
                    category=category,
                    endpoint_window={
                        "last_n_games": 15 if window == "l15" else 0,
                        "date_from": date_from,
                        "date_to": date_to,
                    },
                )
                raw = json.dumps(observation.payload, sort_keys=True, separators=(",", ":")).encode()
                ingestion.ingest(
                    claims,
                    {
                        "client_observation_id": f"opp-{window}-{team_id}-{category}",
                        "observation_type": "shot_types_opponent",
                        "provider": "nba",
                        "season": SEASON,
                        "cutoff": cutoff.isoformat(),
                        "schema_version": 2,
                        "retrieved_at": cutoff.isoformat(),
                        "manifest_id": manifest.manifest_id,
                        "scope": observation.scope,
                        "environment": "testing",
                        "checksum": hashlib.sha256(raw).hexdigest(),
                    },
                    gzip.compress(raw),
                    compressed=True,
                )
    if ledger:
        _store_ledger_game(engine, team_minutes=ledger_minutes)

    def compose(stream_key, *, decoded=False):
        version = publications.compose_from_observations(
            stream_key, season=SEASON, cutoff=cutoff, manifest_id=manifest.manifest_id
        )
        with Session(engine) as session:
            active = session.scalar(
                select(PublicationPointer.active_publication_id).where(
                    PublicationPointer.stream_key == stream_key
                )
            )
        assert active == version.publication_id
        if decoded:
            return decode_team_window(json.loads(version.payload), stream_key=stream_key)
        return {row["team_id"]: row for row in json.loads(version.payload)["rows"]}

    return compose


#: DEN through 2026-11-08 on stats.nba.com LeagueDashTeamStats Totals:
#: GP 8, MIN 389 (one overtime game; DEN @ IND itself went 48 minutes).
SEASON_SCHEDULE = _rounds(before=6, after=1)


@pytest.mark.parametrize(
    ("den_minutes", "ind_minutes", "ledger_minutes", "den_per48", "ind_per48"),
    [
        # 40 makes x 48 / (389 - 48) and 40 x 48 / (384 - 48).
        (389.0, 384.0, 48.0, 5.630499, 5.714286),
        # Had DEN's one overtime game been DEN @ IND, both teams' box scores
        # hold its 53 minutes: 40 x 48 / (389 - 53) and 40 x 48 / (389 - 53).
        (389.0, 389.0, 53.0, 5.714286, 5.714286),
    ],
    ids=["regulation", "overtime"],
)
def test_opponent_shot_types_publish_without_the_declared_game(
    tmp_path, declared, den_minutes, ind_minutes, ledger_minutes, den_per48, ind_per48
):
    compose = _opponent_world(
        tmp_path,
        SEASON_SCHEDULE,
        tracking_gp={("season", DEN): 7, ("season", IND): 7},
        minutes={("season", DEN): den_minutes, ("season", IND): ind_minutes},
        ledger_minutes=ledger_minutes,
    )

    rows = compose(OPPONENT_SEASON)

    den = rows[DEN]
    assert (len(den["game_ids"]), den["game_count"], DEN_AT_IND in den["game_ids"]) == (7, 7, False)
    assert (len(rows[IND]["game_ids"]), DEN_AT_IND in rows[IND]["game_ids"]) == (7, False)
    assert round(den["per48"]["catch_and_shoot_FG2M"], 6) == den_per48
    assert round(rows[IND]["per48"]["catch_and_shoot_FG2M"], 6) == ind_per48
    assert rows[LAL]["game_count"] == 8
    assert rows[LAL]["per48"]["catch_and_shoot_FG2M"] == pytest.approx(5.0)


def test_opponent_shot_types_with_the_declared_game_restored_count_it(tmp_path, declared):
    compose = _opponent_world(tmp_path, SEASON_SCHEDULE, tracking_gp={}, minutes={("season", DEN): 389.0})

    den = compose(OPPONENT_SEASON)[DEN]

    assert (den["game_count"], DEN_AT_IND in den["game_ids"]) == (8, True)
    assert den["per48"]["catch_and_shoot_FG2M"] == pytest.approx(4.935732647814910)


@pytest.mark.parametrize(
    "tracking_gp",
    [
        {("season", DEN): 6, ("season", IND): 7},
        {("season", DEN): 7, ("season", IND): 8},
        # Pullups was collected after NBA.com restored DEN @ IND.
        {
            ("season", DEN): 7,
            ("season", IND): 7,
            ("season", DEN, "Pullups"): 8,
            ("season", IND, "Pullups"): 8,
        },
        # Ordinary lag in one shot type, with the declared game restored.
        {("season", LAL, "Pullups"): 7},
    ],
    ids=["declared game plus lag", "one side only", "shot types disagree", "one shot type lags"],
)
def test_opponent_shot_types_shortfall_the_declared_game_does_not_explain_blocks(
    tmp_path, declared, tracking_gp
):
    compose = _opponent_world(tmp_path, SEASON_SCHEDULE, tracking_gp=tracking_gp, minutes={})

    with pytest.raises(ControlPlaneError, match="^provider_window_lagging$"):
        compose(OPPONENT_SEASON)


def test_a_declared_game_missing_from_the_ledger_blocks(tmp_path, declared, caplog):
    compose = _opponent_world(
        tmp_path,
        SEASON_SCHEDULE,
        tracking_gp={("season", DEN): 7, ("season", IND): 7},
        minutes={},
        ledger=False,
    )

    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(ControlPlaneError, match="^publication_candidate_invalid$"),
    ):
        compose(OPPONENT_SEASON)

    (line,) = _coverage_lines(caplog)
    assert line.endswith(" ledger_missing=0022600189")


@pytest.mark.parametrize(
    ("schedule", "l15_game_count"),
    [
        # DEN @ IND is the 14th most recent game.
        (_rounds(before=15, after=13), 14),
        # Fifteen later games push it out of the window.
        (_rounds(before=15, after=15), 15),
    ],
    ids=["window holds the declared game", "later window"],
)
def test_last_15_excludes_the_declared_game_only_while_its_window_holds_it(
    tmp_path, declared, schedule, l15_game_count
):
    season_games = len(schedule)
    in_window = l15_game_count == 14
    compose = _opponent_world(
        tmp_path,
        schedule,
        tracking_gp={
            ("season", DEN): season_games - 1,
            ("season", IND): season_games - 1,
            **({("l15", DEN): 14, ("l15", IND): 14} if in_window else {}),
        },
        minutes={},
    )

    den = compose(OPPONENT_L15)[DEN]

    assert (den["game_count"], DEN_AT_IND in den["game_ids"]) == (l15_game_count, False)


def _governed_season(rows):
    """The governed season games behind ``rows``: DEN @ IND belongs to both teams."""

    return {
        row.team_id: (*row.game_ids, DEN_AT_IND) if row.team_id in {DEN, IND} else row.game_ids
        for row in rows
    }


def test_a_publication_without_the_declared_game_stays_valid_once_tracking_restores_it(tmp_path, declared):
    absent = _opponent_world(
        tmp_path / "absent",
        SEASON_SCHEDULE,
        tracking_gp={("season", DEN): 7, ("season", IND): 7},
        minutes={},
    )(OPPONENT_SEASON, decoded=True)
    restored = _opponent_world(tmp_path / "restored", SEASON_SCHEDULE, tracking_gp={}, minutes={})(
        OPPONENT_SEASON
    )

    assert validate_publication_rows(
        "shot_types", absent, expected_game_ids_by_team=_governed_season(absent), window="season"
    )
    assert (restored[DEN]["game_count"], DEN_AT_IND in restored[DEN]["game_ids"]) == (8, True)


def test_a_declared_game_omitted_for_only_one_of_its_teams_is_rejected(tmp_path, declared):
    rows = _opponent_world(
        tmp_path,
        SEASON_SCHEDULE,
        tracking_gp={("season", DEN): 7, ("season", IND): 7},
        minutes={},
    )(OPPONENT_SEASON, decoded=True)
    governed = _governed_season(rows)
    asymmetric = tuple(
        replace(row, game_ids=governed[IND], game_count=8) if row.team_id == IND else row for row in rows
    )

    with pytest.raises(PublicationValidationError, match="^publication_game_set_mismatch$"):
        validate_publication_rows(
            "shot_types", asymmetric, expected_game_ids_by_team=governed, window="season"
        )
