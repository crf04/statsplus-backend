"""The Draft Target preview at the HTTP seam (#253).

A preview composes three services the route already has handles for: the user
service validates the draft, the backtest reads its season to date, and the
resolver asks whether it fires today.  The route tests stub the two reads and
let the real draft validator run, so the ``400`` messages the Lab shows are
the create route's own rather than a stub's restatement of them.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from app.errors import InvalidInputError
from app.services.user_service import UserService
from tests.test_target_backtest import BACKTESTED, CORNER_THREE
from tests.test_target_resolution import RESOLVED


DRAFT = {
    "opponent": "OKC",
    "title": "OKC vs Corner 3 ≥ 40%",
    "note": None,
    "qualifiers": [CORNER_THREE],
}
PREVIEWED = {**BACKTESTED, "target": DRAFT}
TODAY = {"game": RESOLVED["targets"][0]["game"], "fit_count": 1}


@pytest.fixture
def preview_services(dependencies):
    """Stub the two reads and run the real validator, as ARCHITECTURE.md asks."""

    dependencies.user_service.validate_target_draft = Mock(
        side_effect=UserService.validate_target_draft
    )
    dependencies.target_backtest_service = Mock(name="target_backtest_service")
    dependencies.target_backtest_service.backtest_target.return_value = PREVIEWED
    dependencies.target_resolution_service = Mock(name="target_resolution_service")
    dependencies.target_resolution_service.today.return_value = TODAY
    return dependencies


def _preview(client, headers, body):
    return client.post("/api/user/targets/preview", headers=headers, json=body)


def test_the_preview_route_returns_the_drafts_backtest_and_today(
    client, authenticate, preview_services
):
    headers = authenticate()

    response = _preview(
        client, headers, {"opponent": "okc", "qualifiers": [CORNER_THREE]}
    )

    assert response.status_code == 200
    assert response.get_json() == {"success": True, **PREVIEWED, "today": TODAY}
    # The same validated draft reaches both reads, canonical tricode included.
    preview_services.target_backtest_service.backtest_target.assert_called_once_with(
        DRAFT
    )
    preview_services.target_resolution_service.today.assert_called_once_with(DRAFT)


def test_the_preview_route_reports_an_idle_opponent_as_a_null_today(
    client, authenticate, preview_services
):
    headers = authenticate()
    preview_services.target_resolution_service.today.return_value = None

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
    assert response.get_json()["error"] == {
        "code": "invalid_input",
        "message": message,
    }
    preview_services.target_backtest_service.backtest_target.assert_not_called()
    preview_services.target_resolution_service.today.assert_not_called()


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
    preview_services.target_backtest_service.backtest_target.side_effect = (
        RuntimeError("stored rows are wrong")
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
    preview_services.target_backtest_service.backtest_target.assert_not_called()
    preview_services.user_service.validate_target_draft.assert_not_called()


def test_a_malformed_slate_refusal_from_today_is_reported_as_invalid_input(
    client, authenticate, preview_services
):
    headers = authenticate()
    preview_services.target_resolution_service.today.side_effect = (
        InvalidInputError("The slate date must use YYYY-MM-DD.")
    )

    response = _preview(
        client, headers, {"opponent": "OKC", "qualifiers": [CORNER_THREE]}
    )

    assert response.status_code == 400
