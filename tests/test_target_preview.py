"""The Draft Target preview, at the service and HTTP seams (#253).

A preview owes a Draft Target the numbers a saved Target gets, from one
Publication generation, without reaching a provider or writing a row.  The
service tests compose ``TargetPreviewService`` from the real backtest and
resolver over the fake seams ``test_target_backtest`` and
``test_target_resolution`` already drive, plus a real ``MatchupService`` where
the injury path is what is under test.  The route tests stub the preview and
let the real draft validator run, so the ``400`` messages the Lab shows are the
create route's own rather than a stub's restatement of them.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import Mock

import pytest

import tests.services.test_matchup_service as matchup_doubles
from app.config.settings import (
    MatchupScoreSettings,
    NBASeasonSettings,
    RuntimeSettings,
)
from app.errors import InvalidInputError, ProviderUnavailableError
from app.services.injury_snapshot_repository import StoredInjurySnapshot
from app.services.matchup import MATCHUP_PUBLICATION_STREAM_KEYS
from app.services.matchup_injuries import (
    MatchupInjuryService,
    StoredMatchupInjuryReader,
)
from app.services.statistic_catalog import StatisticCatalog
from app.services.target_backtest import (
    BACKTEST_PUBLICATION_STREAM_KEYS,
    TargetBacktestService,
)
from app.services.target_preview import TargetPreviewService
from app.services.target_resolution import TargetResolutionService
from app.services.user_service import UserService
from tests import test_target_backtest as bt
from tests import test_target_resolution as res
from tests.test_target_backtest import BACKTESTED, CORNER_THREE
from tests.test_target_resolution import RESOLVED


DRAFT = {
    "opponent": "OKC",
    "title": "OKC vs Corner 3 ≥ 40%",
    "note": None,
    "qualifiers": [CORNER_THREE],
}
SETTINGS = RuntimeSettings(
    environment="testing",
    nba=NBASeasonSettings(current_season=bt.SEASON),
    matchup_scores=MatchupScoreSettings(),
)


# --- service ---------------------------------------------------------------


class AdvancingPublicationReader:
    """Every capture is a new generation, so two captures can never agree."""

    def __init__(self):
        self.calls = []

    def snapshot(self, stream_keys, *, season, projection_only_keys=None):
        self.calls.append((tuple(stream_keys), season, projection_only_keys))
        return f"generation-{len(self.calls)}"


class SnapshotMatchups:
    """A Matchup composer recording the generation and injury reader given."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get_matchup_from_snapshot(self, *, game_id, publication_snapshot, injuries):
        self.calls.append((game_id, publication_snapshot, injuries))
        return self.payload

    def get_matchup(self, *, game_id):
        raise AssertionError("a preview composes its Matchup from its own snapshot")


def _preview_service(*, logs, diets, matchups, reader, slate=None, injuries=None):
    return TargetPreviewService(
        backtests=TargetBacktestService(
            # A draft is stored nowhere, so neither read has a Target reader.
            targets=object(),
            player_logs=logs,
            player_diets=diets,
            statistic_catalog=StatisticCatalog.load_default(),
            settings=SETTINGS,
            publication_reader=reader,
        ),
        resolutions=TargetResolutionService(
            targets=object(), slates=slate or res.FakeSlate(), matchups=matchups
        ),
        matchups=matchups,
        injuries=injuries,
        settings=SETTINGS,
        publication_reader=reader,
    )


def test_a_preview_resolves_one_generation_and_hands_it_to_every_seam():
    seams = bt._two_games()
    reader = AdvancingPublicationReader()
    matchups = SnapshotMatchups(res._matchup())
    stored_injuries = object()

    payload = _preview_service(
        logs=seams["logs"],
        diets=seams["diets"],
        matchups=matchups,
        reader=reader,
        injuries=stored_injuries,
    ).preview(DRAFT)

    # One capture for the request, covering both reads' streams, narrowed
    # only where both reads resolve through the projection.
    assert len(reader.calls) == 1
    stream_keys, season, projection_only = reader.calls[0]
    assert season == bt.SEASON
    assert set(stream_keys) == (
        set(BACKTEST_PUBLICATION_STREAM_KEYS) | set(MATCHUP_PUBLICATION_STREAM_KEYS)
    )
    assert len(stream_keys) == len(set(stream_keys))
    assert projection_only == frozenset({"player_game_logs"})
    # The season's evidence and tonight's Matchup come from that one
    # generation; the Matchup is read with the stored-only injury reader.
    assert seams["logs"].snapshots == ["generation-1", "generation-1"]
    assert seams["diets"].snapshots == ["generation-1"]
    assert matchups.calls == [(res.GAME_ID, "generation-1", stored_injuries)]
    assert payload["target"] == DRAFT
    assert payload["summary"]["games"] == 2
    assert payload["today"]["fit_count"] == 1


def test_an_idle_draft_still_reads_one_generation_and_no_matchup():
    seams = bt._two_games()
    reader = AdvancingPublicationReader()
    matchups = SnapshotMatchups(res._matchup())

    payload = _preview_service(
        logs=seams["logs"], diets=seams["diets"], matchups=matchups, reader=reader
    ).preview({**DRAFT, "opponent": "MIA", "title": "MIA vs Corner 3 ≥ 40%"})

    assert payload["today"] is None
    assert len(reader.calls) == 1
    assert matchups.calls == []


#: The only slice the matchup doubles store for LeBron, so a draft he fits.
RIM_LIGHT = {
    "opponent": "BOS",
    "title": "BOS vs Restricted area ≤ 30%",
    "note": None,
    "qualifiers": [
        {
            "base": "shot_zones",
            "slice_key": "Restricted Area",
            "comparator": "at_or_below",
            "threshold": 0.3,
        }
    ],
}


def _stored_out(retrieved_at):
    """A stored injury override listing LeBron as Out for the doubles' game."""

    return StoredInjurySnapshot(
        normalized_entries=(
            {
                "entry_id": "rotowire:6504",
                "source_player_id": "6504",
                "source_player_name": "LeBron James",
                "canonical_player_id": 2544,
                "team_id": matchup_doubles.LAL,
                "tricode": "LAL",
                "canonical_status": "Out",
                "raw_status": "Out",
                "reason": "Ankle",
                "source_url": "https://www.rotowire.com/basketball/injury-report.php",
            },
        ),
        retrieved_at=retrieved_at,
    )


@pytest.mark.parametrize(
    ("stored", "expected_fit_count", "live_path_refreshes"),
    [
        pytest.param(None, 1, True, id="nothing-stored"),
        pytest.param(
            _stored_out(matchup_doubles.NOW - timedelta(minutes=1)), 0, False,
            id="fresh-stored-out",
        ),
        pytest.param(
            _stored_out(matchup_doubles.NOW - timedelta(minutes=10)), 0, True,
            id="stale-stored-out",
        ),
    ],
)
def test_a_preview_never_reaches_the_injury_provider_or_writes_a_snapshot(
    stored, expected_fit_count, live_path_refreshes
):
    """Stored injuries count; stale or missing ones must not start collection.

    A stored Out entry removes its participant from tonight's fit count
    whether the override is fresh or stale, exactly as the Matchup would show
    it, while the provider and the snapshot table are never touched.
    """

    provider = Mock(name="rotowire")
    provider.get_snapshot.side_effect = ProviderUnavailableError("rotowire down")
    repository = Mock(name="injury_snapshots")
    repository.get.return_value = stored
    repository.get_latest_source.return_value = None
    injuries = MatchupInjuryService(
        provider=provider,
        snapshot_repository=repository,
        athlete_catalog=Mock(name="athletes"),
        enabled=True,
        permission_granted=True,
        clock=lambda: matchup_doubles.NOW,
    )
    matchups = matchup_doubles._service(injuries=injuries)
    slate = res.FakeSlate(
        games=[
            res._game(
                game_id=matchup_doubles.GAME_ID,
                away=(matchup_doubles.LAL, "LAL", "Los Angeles Lakers"),
                home=(matchup_doubles.BOS, "BOS", "Boston Celtics"),
            )
        ]
    )
    seams = bt._two_games()

    payload = _preview_service(
        logs=seams["logs"],
        diets=seams["diets"],
        matchups=matchups,
        reader=None,
        slate=slate,
        injuries=StoredMatchupInjuryReader(injuries),
    ).preview(RIM_LIGHT)

    assert payload["today"]["game"]["game_id"] == matchup_doubles.GAME_ID
    assert payload["today"]["fit_count"] == expected_fit_count
    provider.get_snapshot.assert_not_called()
    repository.publish.assert_not_called()
    repository.replace_from_source.assert_not_called()
    # The Matchup route's own read refreshes on the same evidence whenever the
    # override is not fresh, so the shared path is what this test would catch.
    matchups.get_matchup(game_id=matchup_doubles.GAME_ID)
    assert provider.get_snapshot.call_count == (1 if live_path_refreshes else 0)


# --- routes ----------------------------------------------------------------


PREVIEWED = {**BACKTESTED, "target": DRAFT}
TODAY = {"game": RESOLVED["targets"][0]["game"], "fit_count": 1}


@pytest.fixture
def preview_services(dependencies):
    """Stub the preview and run the real validator, as ARCHITECTURE.md asks."""

    dependencies.user_service.validate_target_draft = Mock(
        side_effect=UserService(db_engine=Mock(), settings=RuntimeSettings()).validate_target_draft
    )
    dependencies.target_preview_service = Mock(name="target_preview_service")
    # Echo the validated draft, so a dropped field is visible on the wire.
    dependencies.target_preview_service.preview.side_effect = lambda draft: {
        **PREVIEWED,
        "target": draft,
        "today": TODAY,
    }
    return dependencies


def _preview(client, headers, body):
    return client.post("/api/user/targets/preview", headers=headers, json=body)


def test_the_preview_route_returns_the_drafts_backtest_and_today(
    client, authenticate, preview_services
):
    headers = authenticate()

    response = _preview(
        client,
        headers,
        {"opponent": "okc", "qualifiers": [CORNER_THREE], "note": " Leaks corner threes "},
    )

    assert response.status_code == 200
    validated = {**DRAFT, "note": "Leaks corner threes", "conditions": None, "stat_preferences": None}
    assert response.get_json() == {
        "success": True,
        **PREVIEWED,
        "target": validated,
        "today": TODAY,
    }
    # The validated draft -- canonical tricode, trimmed note -- is what the
    # preview evaluates.
    preview_services.target_preview_service.preview.assert_called_once_with(validated)


def test_the_preview_route_reports_an_idle_opponent_as_a_null_today(
    client, authenticate, preview_services
):
    headers = authenticate()
    preview_services.target_preview_service.preview.side_effect = None
    preview_services.target_preview_service.preview.return_value = {
        **PREVIEWED,
        "today": None,
    }

    response = _preview(
        client, headers, {"opponent": "OKC", "qualifiers": [CORNER_THREE]}
    )

    assert response.status_code == 200
    assert response.get_json()["today"] is None


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            {"opponent": "XXX", "qualifiers": [CORNER_THREE]},
            "A target needs one NBA team as its opponent.",
        ),
        (
            {"opponent": "OKC", "qualifiers": []},
            "A target needs at least one qualifier.",
        ),
        (
            {"opponent": "OKC", "qualifiers": [{**CORNER_THREE, "threshold": 1.5}]},
            "A qualifier needs a known diet base, a slice of that base, a "
            "comparator of at_or_above or at_or_below, and a threshold share "
            "between 0 and 1.",
        ),
    ],
)
def test_the_preview_route_refuses_an_unusable_draft_before_reading_anything(
    client, authenticate, preview_services, body, message
):
    headers = authenticate()

    response = _preview(client, headers, body)

    assert response.status_code == 400
    assert response.get_json() == {
        "error": {"code": "invalid_input", "message": message}
    }
    preview_services.target_preview_service.preview.assert_not_called()


def test_the_preview_route_rejects_a_body_that_is_not_an_object(
    client, authenticate, preview_services
):
    headers = authenticate()

    response = client.post("/api/user/targets/preview", headers=headers)

    assert response.status_code == 400
    assert response.get_json()["error"] == {
        "code": "invalid_input",
        "message": "No target data was provided.",
    }


def test_the_preview_route_never_writes_a_target(
    client, authenticate, preview_services
):
    headers = authenticate()

    _preview(client, headers, {"opponent": "OKC", "qualifiers": [CORNER_THREE]})

    assert not preview_services.user_service.create_target.called
    assert not preview_services.user_service.update_target.called


def test_the_preview_route_reports_an_unexpected_failure_safely(
    client, authenticate, preview_services
):
    headers = authenticate()
    preview_services.target_preview_service.preview.side_effect = RuntimeError(
        "stored rows are wrong"
    )

    response = _preview(
        client, headers, {"opponent": "OKC", "qualifiers": [CORNER_THREE]}
    )

    assert response.status_code == 500
    assert response.get_json()["error"] == {
        "code": "operation_failed",
        "message": "Failed to preview the target.",
    }


def test_the_preview_route_refuses_an_unauthenticated_caller(
    client, authenticate, preview_services
):
    authenticate()

    response = client.post(
        "/api/user/targets/preview",
        json={"opponent": "OKC", "qualifiers": [CORNER_THREE]},
    )

    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "authentication_required"
    preview_services.target_preview_service.preview.assert_not_called()
    preview_services.user_service.validate_target_draft.assert_not_called()


def test_a_slate_refusal_from_the_preview_keeps_its_own_error_envelope(
    client, authenticate, preview_services
):
    headers = authenticate()
    preview_services.target_preview_service.preview.side_effect = InvalidInputError(
        "The slate date must use YYYY-MM-DD."
    )

    response = _preview(
        client, headers, {"opponent": "OKC", "qualifiers": [CORNER_THREE]}
    )

    assert response.status_code == 400
    # The whole body: the error envelope alone, never ``success`` beside it.
    assert response.get_json() == {
        "error": {
            "code": "invalid_input",
            "message": "The slate date must use YYYY-MM-DD.",
        }
    }
