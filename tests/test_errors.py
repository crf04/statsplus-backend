"""Regression tests for the application's structured error contract."""

from __future__ import annotations

import logging

import pytest
from flask import Flask
from werkzeug.datastructures import MultiDict

from app.errors import (
    AppError,
    InvalidConfigurationError,
    InvalidInputError,
    OperationFailedError,
    ProviderUnavailableError,
    ResourceNotFoundError,
    register_error_handlers,
    route_error_boundary,
)


@pytest.fixture
def error_app() -> Flask:
    app = Flask(__name__)
    app.config["TESTING"] = True
    register_error_handlers(app)

    @app.get("/invalid-input")
    def invalid_input() -> None:
        raise InvalidInputError(
            "The minutes filter is invalid.",
            detail="minutes_filter=not-a-range",
        )

    @app.get("/missing-resource")
    def missing_resource() -> None:
        raise ResourceNotFoundError(
            "The requested player was not found.",
            detail="player_name=secret-player",
        )

    @app.get("/provider-unavailable")
    def provider_unavailable() -> None:
        raise ProviderUnavailableError(
            detail="stats.nba.com timed out with internal token=secret-token"
        )

    @app.get("/invalid-configuration")
    def invalid_configuration() -> None:
        raise InvalidConfigurationError(
            detail="DATABASE_URL=postgresql://user:password@example.invalid/stats"
        )

    @app.get("/unexpected")
    def unexpected() -> None:
        raise RuntimeError(
            "unexpected provider response: token=token-secret api_key=api-secret"
        )

    @app.get("/wrapped-unexpected")
    @route_error_boundary("The wrapped operation failed.")
    def wrapped_unexpected() -> None:
        raise RuntimeError(
            "provider request failed: "
            "DATABASE_URL=postgresql://db-user:db-password@example.invalid/stats "
            "token=token-secret api_key=api-secret password=password-secret "
            "private_key=private-key-secret Authorization: Bearer bearer-secret "
            "-----BEGIN PRIVATE KEY-----\nprivate-key-material\n"
            "-----END PRIVATE KEY-----"
        )

    return app


@pytest.mark.parametrize(
    ("path", "status", "code", "message"),
    [
        (
            "/invalid-input",
            400,
            "invalid_input",
            "The minutes filter is invalid.",
        ),
        (
            "/missing-resource",
            404,
            "resource_not_found",
            "The requested player was not found.",
        ),
        (
            "/provider-unavailable",
            503,
            "provider_unavailable",
            "An upstream provider is currently unavailable. Please try again later.",
        ),
        (
            "/invalid-configuration",
            500,
            "invalid_configuration",
            "The server configuration is invalid.",
        ),
    ],
)
def test_public_error_categories_have_stable_responses(
    error_app: Flask,
    path: str,
    status: int,
    code: str,
    message: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR)

    response = error_app.test_client().get(path)

    assert response.status_code == status
    assert response.get_json() == {"error": {"code": code, "message": message}}


def test_internal_exception_details_are_sanitized_and_logged_once(
    error_app: Flask, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR)

    response = error_app.test_client().get("/unexpected")

    assert response.status_code == 500
    assert response.get_json() == {
        "error": {
            "code": "internal_error",
            "message": "An unexpected server error occurred.",
        }
    }
    assert "unexpected provider response" in caplog.text
    assert "token-secret" not in caplog.text
    assert "api-secret" not in caplog.text
    assert len([record for record in caplog.records if record.name == "app.errors"]) == 1


def test_sensitive_diagnostic_details_are_redacted_without_losing_context(
    error_app: Flask, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR)

    response = error_app.test_client().get("/wrapped-unexpected")

    assert response.status_code == 500
    assert response.get_json() == {
        "error": {
            "code": "operation_failed",
            "message": "The wrapped operation failed.",
        }
    }
    assert "provider request failed" in caplog.text
    for secret in (
        "db-password",
        "token-secret",
        "api-secret",
        "password-secret",
        "private-key-secret",
        "bearer-secret",
        "private-key-material",
    ):
        assert secret not in caplog.text
    assert "[REDACTED]" in caplog.text
    assert "[REDACTED PEM]" in caplog.text
    assert len([record for record in caplog.records if record.name == "app.errors"]) == 1
    assert not any(
        record.exc_info
        for record in caplog.records
        if record.name == "app.errors"
    )


def test_app_error_defaults_to_safe_public_message() -> None:
    error = AppError(detail="private implementation detail")

    assert error.public_message == "An unexpected server error occurred."
    assert error.detail == "private implementation detail"


def test_route_error_boundary_preserves_expected_application_errors() -> None:
    expected = InvalidInputError("The request is invalid.")

    @route_error_boundary("The operation failed.")
    def handler() -> None:
        raise expected

    with pytest.raises(InvalidInputError) as raised:
        handler()

    assert raised.value is expected


def test_route_error_boundary_translates_unexpected_errors_without_logging(caplog) -> None:
    @route_error_boundary("The operation failed.")
    def handler() -> None:
        raise RuntimeError("private provider detail")

    with pytest.raises(OperationFailedError) as raised:
        handler()

    assert raised.value.public_message == "The operation failed."
    assert raised.value.detail == "private provider detail"
    assert not caplog.records


def test_game_logs_invalid_input_uses_central_handler(client) -> None:
    response = client.get(
        "/api/games/game_logs?player_name=LeBron%20James&minutes_filter=not-a-range"
    )

    assert response.status_code == 400
    error = response.get_json()["error"]
    assert error["code"] == "invalid_input"
    assert error["message"] == "One or more game log filters are invalid."


# Synthetic marker for the redaction test: never a real credential.
REDACTED_TEAM_VALUE = "token=[REDACTED]"


def _rejected_filters(details):
    """Map one details payload to parameter -> values for assertions."""

    return {
        entry["parameter"]: entry["values"]
        for entry in details["filters"]
    }


def test_game_logs_rejected_teams_against_names_parameter_and_values(client) -> None:
    response = client.get(
        "/api/games/game_logs"
        "?player_name=LeBron%20James"
        "&teams_against%5B%5D=Arc3Assists"
        "&teams_against%5B%5D=NotAFilter"
        "&rank_filter%5B%5D=5"
        "&rank_filter%5B%5D=6"
    )

    assert response.status_code == 400
    payload = response.get_json()
    assert payload["error"]["message"] == "One or more game log filters are invalid."
    # Only the unsupported entry names itself; the honoured Arc3Assists does
    # not appear as unusable.
    rejected = _rejected_filters(payload["error"]["details"])
    assert rejected == {"teams_against": ["NotAFilter"]}


def test_game_logs_teams_against_rejection_carries_the_canonical_vocabulary(
    client,
) -> None:
    from app.models.catalogs import TEAM_FILTER_ALIASES, SUPPORTED_TEAM_FILTERS

    response = client.get(
        "/api/games/game_logs"
        "?player_name=LeBron%20James"
        "&teams_against%5B%5D=NotAFilter"
        "&rank_filter%5B%5D=5"
    )

    assert response.status_code == 400
    details = response.get_json()["error"]["details"]["filters"][0]
    # The full authoritative vocabulary travels with the rejection, sourced
    # from the backend constant, so a caller never holds a drifting copy.
    assert details["supported_values"] == list(SUPPORTED_TEAM_FILTERS)
    assert "Arc3Assists" in details["supported_values"]
    assert details["supported_aliases"] == list(TEAM_FILTER_ALIASES)
    assert "<10 Ft" in details["supported_aliases"]


def test_game_logs_rejected_opponent_tricode_names_parameter_and_value(client) -> None:
    response = client.get(
        "/api/games/game_logs"
        "?player_name=LeBron%20James&opponent_tricode=XXX"
    )

    assert response.status_code == 400
    rejected = _rejected_filters(response.get_json()["error"]["details"])
    assert rejected == {"opponent_tricode": ["XXX"]}


def test_game_logs_rank_filter_rejection_reports_every_unusable_entry(
    client,
) -> None:
    response = client.get(
        "/api/games/game_logs"
        "?player_name=LeBron%20James"
        "&rank_filter%5B%5D=abc"
        "&rank_filter%5B%5D=5"
        "&rank_filter%5B%5D=xyz"
    )

    assert response.status_code == 400
    rejected = _rejected_filters(response.get_json()["error"]["details"])
    # All unusable entries, with the parseable 5 left out.
    assert rejected == {"rank_filter": ["abc", "xyz"]}


def test_game_logs_model_level_alignment_failure_names_submitted_values(
    client,
) -> None:
    response = client.get(
        "/api/games/game_logs"
        "?player_name=LeBron%20James"
        "&teams_against%5B%5D=OPP_PTS"
        "&rank_filter%5B%5D=5"
        "&rank_filter%5B%5D=9"
    )

    assert response.status_code == 400
    rejected = _rejected_filters(response.get_json()["error"]["details"])
    assert rejected == {"rank_filter": ["5", "9"]}


def test_game_logs_range_failures_name_their_submitted_values(client) -> None:
    response = client.get(
        "/api/games/game_logs"
        "?player_name=LeBron%20James&minutes_filter=40,20"
    )

    assert response.status_code == 400
    rejected = _rejected_filters(response.get_json()["error"]["details"])
    assert rejected == {"minutes_filter": ["40,20"]}


def test_game_logs_playstyle_failures_use_the_separate_query_names(
    client,
) -> None:
    response = client.get(
        "/api/games/game_logs"
        "?player_name=LeBron%20James&playstyle_RTG_min=abc"
    )

    assert response.status_code == 400
    rejected = _rejected_filters(response.get_json()["error"]["details"])
    assert rejected == {"playstyle_RTG_min": ["abc"]}

    response = client.get(
        "/api/games/game_logs"
        "?player_name=LeBron%20James&playstyle_RTG_max=inf"
    )

    assert response.status_code == 400
    rejected = _rejected_filters(response.get_json()["error"]["details"])
    assert rejected == {"playstyle_RTG_max": ["inf"]}


def test_game_logs_reversed_playstyle_range_names_submitted_bounds(
    client,
) -> None:
    # Only the minimum was submitted; the untouched 200 default must never
    # be blamed, and the submitted minimum must not disappear.
    response = client.get(
        "/api/games/game_logs"
        "?player_name=LeBron%20James&playstyle_RTG_min=201"
    )

    assert response.status_code == 400
    rejected = _rejected_filters(response.get_json()["error"]["details"])
    assert rejected == {"playstyle_RTG_min": ["201"]}

    # With both bounds submitted, each names itself with its own value.
    response = client.get(
        "/api/games/game_logs"
        "?player_name=LeBron%20James&playstyle_RTG_min=150&playstyle_RTG_max=50"
    )

    assert response.status_code == 400
    rejected = _rejected_filters(response.get_json()["error"]["details"])
    assert rejected == {"playstyle_RTG_min": ["150"], "playstyle_RTG_max": ["50"]}

    # A submitted maximum below the minimum default blames only itself.
    response = client.get(
        "/api/games/game_logs"
        "?player_name=LeBron%20James&playstyle_RTG_max=-5"
    )

    assert response.status_code == 400
    rejected = _rejected_filters(response.get_json()["error"]["details"])
    assert rejected == {"playstyle_RTG_max": ["-5"]}


def test_game_logs_rejected_self_filter_numeric_values_name_the_stat(
    client,
) -> None:
    response = client.get(
        "/api/games/game_logs"
        "?player_name=LeBron%20James"
        "&self_filters%5BBOGUS%5D=a,b"
    )

    assert response.status_code == 400
    rejected = _rejected_filters(response.get_json()["error"]["details"])
    # An unsupported stat never blinds the range facts: the numeric
    # failures still attribute to the stat the caller submitted.
    assert rejected == {"self_filters[BOGUS]": ["a", "b"]}


def test_game_logs_rejected_self_filter_zero_bound_survives(
    client,
) -> None:
    response = client.get(
        "/api/games/game_logs"
        "?player_name=LeBron%20James&self_filters%5BPTS%5D=2,0"
    )

    assert response.status_code == 400
    rejected = _rejected_filters(response.get_json()["error"]["details"])
    # Zero is a submitted value, not one to drop by truthiness.
    assert rejected == {"self_filters[PTS]": ["2", "0"]}


def test_game_logs_parameter_names_are_redacted_and_bounded_too(
    client,
) -> None:
    response = client.get(
        "/api/games/game_logs"
        "?player_name=LeBron%20James"
        "&self_filters%5Btoken%3Dsynthetic-secret%5D=bad-value"
    )

    assert response.status_code == 400
    entry = response.get_json()["error"]["details"]["filters"][0]
    assert entry["parameter"] == "self_filters[token=[REDACTED]]"
    assert "synthetic-secret" not in response.get_data(as_text=True)
    # The ordinary rejected value stays verbatim.
    assert entry["values"] == ["bad-value"]


def test_game_logs_published_values_are_bounded(client) -> None:
    response = client.get(
        "/api/games/game_logs"
        "?player_name=LeBron%20James"
        "&teams_against%5B%5D=" + "A" * 300 +
        "&rank_filter%5B%5D=1"
    )

    assert response.status_code == 400
    rejected = _rejected_filters(response.get_json()["error"]["details"])
    assert rejected == {"teams_against": ["A" * 200]}


def test_game_logs_details_carry_only_the_documented_facts(client) -> None:
    response = client.get(
        "/api/games/game_logs"
        "?player_name=LeBron%20James"
        "&date_filter=not-a-date"
        "&teams_against%5B%5D=NotAFilter"
        "&teams_against%5B%5D=token%3Dsecret-token"
        "&rank_filter%5B%5D=1"
    )

    assert response.status_code == 400
    error = response.get_json()["error"]
    assert error["code"] == "invalid_input"
    # Structure, not just substrings: every entry carries only the keys
    # the documented contract allows, and values are scalars.
    documented = {
        "supported_values",
        "supported_aliases",
        "values",
        "parameter",
    }
    for entry in error["details"]["filters"]:
        assert set(entry) <= documented
        for key in ("ctx", "input", "url", "loc", "type", "msg"):
            assert key not in entry
    body = response.get_data(as_text=True)
    assert "secret-token" not in body
    assert "ValueError" not in body
    assert "pydantic" not in body
    assert "errors.pydantic.dev" not in body
    assert "input_value" not in body


def test_game_logs_scalar_parse_failures_name_parameter_and_value(
    client,
) -> None:
    response = client.get(
        "/api/games/game_logs"
        "?player_name=LeBron%20James&date_filter=not-a-date"
        "&location_filter=home"
    )

    assert response.status_code == 400
    rejected = _rejected_filters(response.get_json()["error"]["details"])
    assert rejected == {
        "date_filter": ["not-a-date"],
        "location_filter": ["home"],
    }


def test_game_logs_known_failures_survive_other_unknown_failures(
    client,
) -> None:
    response = client.get(
        "/api/games/game_logs"
        "?player_name=LeBron%20James"
        "&date_filter=not-a-date"
        "&teams_against%5B%5D=NotAFilter"
        "&rank_filter%5B%5D=5"
    )

    assert response.status_code == 400
    rejected = _rejected_filters(response.get_json()["error"]["details"])
    # Each rejection translates separately: a date parse failure never
    # blanks the teams_against refusal.
    assert rejected == {
        "date_filter": ["not-a-date"],
        "teams_against": ["NotAFilter"],
    }


def test_game_log_validation_details_keep_known_and_skip_unknown() -> None:
    from pydantic import ValidationError

    from app.models.game_logs import GameLogQuery
    from app.routes.game_routes import _game_log_validation_details

    # One typed refusal plus one internal unknown shape: the known detail
    # survives, the unknown one is skipped rather than blanking the payload.
    try:
        GameLogQuery(
            season_filter="2024-25",
            teams_against=["NotAFilter"],
            self_filters="oops",
        )
    except ValidationError as error:
        details = _game_log_validation_details(error, filters={}, args=MultiDict())
        assert _rejected_filters(details) == {"teams_against": ["NotAFilter"]}
    else:
        pytest.fail("the malformed request must be rejected")


def test_game_logs_season_failure_names_parameter_and_value(client) -> None:
    response = client.get(
        "/api/games/game_logs"
        "?player_name=LeBron%20James&season_filter=potato"
    )

    assert response.status_code == 400
    rejected = _rejected_filters(response.get_json()["error"]["details"])
    assert rejected == {"season_filter": ["potato"]}


def test_game_logs_minutes_part_failures_name_the_offending_part(
    client,
) -> None:
    response = client.get(
        "/api/games/game_logs?player_name=LeBron%20James&minutes_filter=a,20"
    )

    assert response.status_code == 400
    rejected = _rejected_filters(response.get_json()["error"]["details"])
    assert rejected == {"minutes_filter": ["a"]}


def test_game_logs_self_filter_failures_name_parameter_and_value(
    client,
) -> None:
    response = client.get(
        "/api/games/game_logs"
        "?player_name=LeBron%20James"
        "&self_filters%5BBOGUS%5D=1,2"
    )

    assert response.status_code == 400
    rejected = _rejected_filters(response.get_json()["error"]["details"])
    assert rejected == {"self_filters[BOGUS]": ["BOGUS"]}


def test_game_logs_game_filter_failure_names_parameter_and_value(client) -> None:
    response = client.get(
        "/api/games/game_logs?player_name=LeBron%20James&game_filter=0"
    )

    assert response.status_code == 400
    rejected = _rejected_filters(response.get_json()["error"]["details"])
    assert rejected == {"game_filter": ["0"]}


def test_game_logs_credential_looking_values_are_redacted_not_echoed(
    client,
) -> None:
    response = client.get(
        "/api/games/game_logs"
        "?player_name=LeBron%20James"
        "&teams_against%5B%5D=token%3Dsecret-token"
        "&rank_filter%5B%5D=1"
    )

    assert response.status_code == 400
    body = response.get_data(as_text=True)
    rejected = _rejected_filters(response.get_json()["error"]["details"])
    assert rejected == {
        "teams_against": [
            REDACTED_TEAM_VALUE,
        ]
    }
    assert REDACTED_TEAM_VALUE in body
    assert "secret-token" not in body
    assert "token=secret-token" not in body
    # The vocabulary in the same payload is untouched by the redaction.
    details = response.get_json()["error"]["details"]["filters"][0]
    assert "OPP_PTS" in details["supported_values"]


def test_game_logs_rejected_values_stay_identifiable(client) -> None:
    response = client.get(
        "/api/games/game_logs"
        "?player_name=LeBron%20James"
        "&teams_against%5B%5D=NotAFilter"
        "&rank_filter%5B%5D=1"
    )

    assert response.status_code == 400
    rejected = _rejected_filters(response.get_json()["error"]["details"])
    # Ordinary rejected input is echoed verbatim; redaction only strips
    # credential-shaped text.
    assert rejected == {"teams_against": ["NotAFilter"]}


def test_game_logs_details_never_leak_pydantic_context_or_inputs(
    client,
) -> None:
    response = client.get(
        "/api/games/game_logs"
        "?player_name=LeBron%20James"
        "&date_filter=not-a-date"
        "&teams_against%5B%5D=NotAFilter"
        "&rank_filter%5B%5D=1"
    )

    assert response.status_code == 400
    body = response.get_data(as_text=True)
    # Only bounded facts appear: no Pydantic error text, no full-input dump,
    # no internal type names or error URLs in the published payload.
    assert "ValueError" not in body
    assert "GameLogFilterError" not in body
    assert "pydantic" not in body
    assert "errors.pydantic.dev" not in body
    assert "input_value" not in body
    assert "input_type" not in body


def test_game_log_validation_details_skip_unknown_internal_failures() -> None:
    from pydantic import ValidationError

    from app.models.game_logs import GameLogQuery
    from app.routes.game_routes import _game_log_validation_details

    # An internal error shape with no identifying facts (here, a self
    # Filters payload no route would send) yields no details at all, so the
    # generic message stays the full answer.
    try:
        GameLogQuery(season_filter="2024-25", self_filters="oops")
    except ValidationError as error:
        assert _game_log_validation_details(error, filters={}, args=MultiDict()) is None
    else:
        pytest.fail("the malformed self_filters payload must be rejected")



def test_player_profile_missing_resource_uses_central_handler(client, monkeypatch) -> None:
    from app.routes import player_routes

    with client.application.app_context():
        monkeypatch.setattr(
            player_routes.player_service,
            "get_player_profile",
            lambda *args, **kwargs: None,
        )

    response = client.get(
        "/api/players/profile?player_name=LeBron%20James&category=Playtypes"
    )

    assert response.status_code == 404
    assert response.get_json() == {
        "error": {
            "code": "resource_not_found",
            "message": "The requested player profile was not found.",
        }
    }


def test_player_profile_provider_failure_uses_central_handler(client, monkeypatch) -> None:
    import requests

    from app.routes import player_routes

    def unavailable(*args, **kwargs):
        raise requests.exceptions.ReadTimeout("profile-provider-secret")

    with client.application.app_context():
        monkeypatch.setattr(
            player_routes.player_service,
            "get_player_profile",
            unavailable,
        )

    response = client.get(
        "/api/players/profile?player_name=LeBron%20James&category=Playtypes"
    )

    assert response.status_code == 503
    assert response.get_json() == {
        "error": {
            "code": "provider_unavailable",
            "message": "An upstream provider is currently unavailable. Please try again later.",
        }
    }


def test_missing_auth_header_uses_nested_error_contract(client, monkeypatch) -> None:
    import app.utils.auth as auth

    monkeypatch.setattr(auth, "get_firebase_app", lambda: object())

    response = client.get("/api/games/game_logs")

    assert response.status_code == 401
    assert response.get_json() == {
        "error": {
            "code": "authentication_required",
            "message": "Please provide a valid Firebase token.",
        }
    }


def test_non_admin_auth_uses_nested_error_contract(client, monkeypatch) -> None:
    import app.utils.auth as auth

    monkeypatch.setattr(auth, "get_firebase_app", lambda: object())
    monkeypatch.setattr(
        auth,
        "verify_firebase_token",
        lambda token: {
            "uid": "viewer",
            "email": "viewer@example.com",
            "role": "viewer",
        },
    )
    monkeypatch.setattr(auth.UserService, "create_or_update_user", lambda *args: None)

    response = client.get(
        "/api/data/fetch_playtypes",
        headers={"Authorization": "Bearer viewer-token"},
    )

    assert response.status_code == 403
    assert response.get_json() == {
        "error": {
            "code": "forbidden",
            "message": "Administrator privileges are required.",
        }
    }


def test_invalid_token_details_are_not_exposed(client, monkeypatch) -> None:
    import app.utils.auth as auth

    monkeypatch.setattr(auth, "get_firebase_app", lambda: object())

    def reject_token(token):
        raise ValueError("firebase-token-secret")

    monkeypatch.setattr(auth, "verify_firebase_token", reject_token)

    response = client.get(
        "/api/games/game_logs",
        headers={"Authorization": "Bearer invalid-token"},
    )

    assert response.status_code == 401
    assert response.get_json() == {
        "error": {
            "code": "invalid_token",
            "message": "The provided Firebase token is invalid.",
        }
    }
    assert "firebase-token-secret" not in response.get_data(as_text=True)


def test_route_exception_details_are_not_exposed(client, monkeypatch) -> None:
    from app.routes import player_routes

    def fail_to_load_players():
        raise RuntimeError("player-provider-secret")

    with client.application.app_context():
        monkeypatch.setattr(
            player_routes.player_service,
            "get_all_players",
            fail_to_load_players,
        )

    response = client.get("/api/players")

    assert response.status_code == 500
    assert response.get_json() == {
        "error": {
            "code": "operation_failed",
            "message": "Failed to retrieve players.",
        }
    }
    assert "player-provider-secret" not in response.get_data(as_text=True)


def test_nl_query_missing_query_uses_nested_error_contract(client) -> None:
    response = client.post("/api/nl-query", json={})

    assert response.status_code == 400
    assert response.get_json() == {
        "error": {
            "code": "invalid_input",
            "message": "A query is required.",
        }
    }


def test_health_failure_uses_nested_error_contract(client, monkeypatch) -> None:
    from app.routes import health_routes

    with client.application.app_context():
        monkeypatch.setattr(
            health_routes.health_service,
            "check_database",
            lambda: {
                "status": "unhealthy",
                "error": "Database health check failed.",
            },
        )

    response = client.get("/api/health/db")

    assert response.status_code == 500
    assert response.get_json() == {
        "error": {
            "code": "internal_error",
            "message": "The database health check failed.",
        }
    }
    assert "database-password-secret" not in response.get_data(as_text=True)


def test_data_route_failure_uses_nested_error_contract(client, monkeypatch) -> None:
    from app.routes import data_update_routes

    def fail_to_fetch_playtypes():
        raise RuntimeError("playtypes-provider-secret")

    with client.application.app_context():
        monkeypatch.setattr(
            data_update_routes.data_service,
            "get_playtypes",
            fail_to_fetch_playtypes,
        )

    response = client.get("/api/data/fetch_playtypes")

    assert response.status_code == 500
    assert response.get_json() == {
        "error": {
            "code": "operation_failed",
            "message": "Failed to fetch play types.",
        }
    }
    assert "playtypes-provider-secret" not in response.get_data(as_text=True)


def test_team_missing_data_uses_nested_error_contract(client, monkeypatch) -> None:
    from app.routes import team_routes

    with client.application.app_context():
        monkeypatch.setattr(
            team_routes.team_service,
            "get_team_stats",
            lambda *args, **kwargs: None,
        )

    response = client.get("/api/teams/stats?team=Example&category=Traditional")

    assert response.status_code == 404
    assert response.get_json() == {
        "error": {
            "code": "resource_not_found",
            "message": "No data found for the specified team and category.",
        }
    }


def test_user_route_exception_details_are_not_exposed(client, monkeypatch) -> None:
    from app.routes import user_routes

    def fail_to_load_user(*args, **kwargs):
        raise RuntimeError("user-database-secret")

    with client.application.app_context():
        monkeypatch.setattr(
            user_routes.user_service,
            "get_user_by_firebase_uid",
            fail_to_load_user,
        )

    response = client.get("/api/user/profile")

    assert response.status_code == 500
    assert response.get_json() == {
        "error": {
            "code": "operation_failed",
            "message": "Failed to retrieve user profile.",
        }
    }
    assert "user-database-secret" not in response.get_data(as_text=True)


def test_error_details_are_omitted_unless_a_failure_states_them(error_app):
    client = error_app.test_client()

    payload = client.get("/invalid-input").get_json()

    assert payload["error"].keys() == {"code", "message"}


def test_error_details_carry_safe_structured_facts():
    class NarrowMeError(InvalidInputError):
        code = "narrow_me"

        @property
        def public_details(self):
            return {"observed_market_count": 12345, "supported_filters": ["providers"]}

    app = Flask(__name__)
    app.config["TESTING"] = True
    register_error_handlers(app)

    @app.get("/too-large")
    def too_large() -> None:
        raise NarrowMeError("Narrow it.", detail="secret=abc")

    response = app.test_client().get("/too-large")

    assert response.status_code == 400
    assert response.get_json() == {
        "error": {
            "code": "narrow_me",
            "message": "Narrow it.",
            "details": {
                "observed_market_count": 12345,
                "supported_filters": ["providers"],
            },
        }
    }
