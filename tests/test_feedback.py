"""``POST /api/user/feedback`` (crf04/statsplus-backend#486, crf04/statsplus#258).

The route is driven through the Flask test client.  Resend is stubbed where the
request leaves the process (``requests``' transport adapter), so the assertions
read the exact HTTP request the provider would have received.
"""

from __future__ import annotations

import base64
import json
import logging
from datetime import UTC, datetime

import pytest
import requests

from app.config.settings import (
    AuthenticationSettings,
    CacheSettings,
    FeedbackSettings,
    RuntimeSettings,
    load_settings,
)
from app.services import feedback_service

MESSAGE = "The rebounds rank on DET @ HOU looks flipped"
SCREENSHOT_BYTES = b"\xff\xd8\xff\xe0 pretend jpeg bytes"
SCREENSHOT_B64 = base64.b64encode(SCREENSHOT_BYTES).decode()
FULL_BODY = {
    "message": MESSAGE,
    "reply_ok": True,
    "x_handle": "@samrivera_bets",
    "page": "/matchups/0022600012?market=REB",
    "app_version": "c703b57",
    "error_ref": "3f9a12bc",
    "viewport": "390x844",
    "screenshot": f"data:image/jpeg;base64,{SCREENSHOT_B64}",
}
FROZEN_NOW = datetime(2026, 10, 19, 14, 30, 5, tzinfo=UTC)


@pytest.fixture
def feedback_settings():
    return FeedbackSettings(resend_api_key="re_test_key")


@pytest.fixture
def runtime_settings(feedback_settings):
    return RuntimeSettings(
        environment="staging",
        auth=AuthenticationSettings(firebase_admin_disabled=True),
        cache=CacheSettings(enabled=False),
        feedback=feedback_settings,
    )


@pytest.fixture(autouse=True)
def signed_in(monkeypatch):
    import app.utils.auth as auth

    monkeypatch.setattr(auth, "get_firebase_app", lambda: object())
    monkeypatch.setattr(
        auth,
        "verify_firebase_token",
        lambda token: {
            "uid": token,
            "email": f"{token}@example.com",
            "name": "Sam Rivera",
            "email_verified": True,
        },
    )
    monkeypatch.setattr(feedback_service, "_utcnow", lambda: FROZEN_NOW)


class Resend:
    """Stands in for api.resend.com beneath ``requests``."""

    def __init__(self):
        self.requests: list[requests.PreparedRequest] = []
        self.timeouts: list[object] = []
        self.status = 200
        self.error: Exception | None = None

    def send(self, adapter, request, **kwargs):
        self.requests.append(request)
        self.timeouts.append(kwargs.get("timeout"))
        if self.error:
            raise self.error
        response = requests.Response()
        response.status_code = self.status
        response._content = b'{"id": "email_1"}'
        response.request = request
        return response

    @property
    def payload(self):
        assert len(self.requests) == 1
        return json.loads(self.requests[0].body)


@pytest.fixture
def resend(monkeypatch):
    stub = Resend()
    monkeypatch.setattr(
        requests.adapters.HTTPAdapter,
        "send",
        lambda adapter, request, **kw: stub.send(adapter, request, **kw),
    )
    return stub


def _post(client, body, token="user-a"):
    return client.post("/api/user/feedback", json=body, headers={"Authorization": f"Bearer {token}"})


# --- delivery ---------------------------------------------------------------


def test_full_message_sends_the_exact_resend_request(client, resend):
    response = _post(client, FULL_BODY)

    assert response.status_code == 201
    assert response.get_json() == {"sent": True}
    request = resend.requests[0]
    assert request.url == "https://api.resend.com/emails"
    assert request.method == "POST"
    assert request.headers["Authorization"] == "Bearer re_test_key"
    assert resend.timeouts == [10]
    assert resend.payload == {
        "from": "CourtAI Feedback <feedback@courtai.app>",
        "to": ["support@courtai.app"],
        "subject": "[CourtAI feedback] /matchups/0022600012 · ref 3f9a12bc",
        "text": (
            "The rebounds rank on DET @ HOU looks flipped\n"
            "\n"
            "---\n"
            "From: Sam Rivera <user-a@example.com>\n"
            "UID: user-a\n"
            "X: https://x.com/samrivera_bets\n"
            "Page: /matchups/0022600012?market=REB\n"
            "App version: c703b57\n"
            "Error ref: 3f9a12bc\n"
            "Viewport: 390x844\n"
            "Time (UTC): 2026-10-19T14:30:05Z\n"
            "Reply allowed: yes\n"
            "Environment: staging"
        ),
        "reply_to": "user-a@example.com",
        "attachments": [{"filename": "screenshot.jpg", "content": SCREENSHOT_B64}],
    }


def test_minimal_message_has_no_reply_to_attachment_or_optional_lines(client, resend):
    response = _post(client, {"message": "  Love it.  "})

    assert response.status_code == 201
    assert resend.payload == {
        "from": "CourtAI Feedback <feedback@courtai.app>",
        "to": ["support@courtai.app"],
        "subject": "[CourtAI feedback] unknown page",
        "text": (
            "Love it.\n"
            "\n"
            "---\n"
            "From: Sam Rivera <user-a@example.com>\n"
            "UID: user-a\n"
            "Time (UTC): 2026-10-19T14:30:05Z\n"
            "Reply allowed: no\n"
            "Environment: staging"
        ),
    }


def test_reply_to_is_the_token_email_never_a_body_field(client, resend):
    body = {**FULL_BODY, "email": "attacker@example.com", "reply_to": "attacker@example.com"}

    _post(client, body, token="user-b")

    assert resend.payload["reply_to"] == "user-b@example.com"
    assert "attacker" not in json.dumps(resend.payload)


def test_unticked_reply_sends_no_reply_to_even_with_everything_else(client, resend):
    _post(client, {**FULL_BODY, "reply_ok": False})

    assert "reply_to" not in resend.payload
    assert "Reply allowed: no" in resend.payload["text"]


def test_png_screenshot_is_attached_as_png(client, resend):
    _post(client, {"message": MESSAGE, "screenshot": f"data:image/png;base64,{SCREENSHOT_B64}"})

    assert resend.payload["attachments"] == [{"filename": "screenshot.png", "content": SCREENSHOT_B64}]


def test_subject_drops_the_query_strips_newlines_and_caps_the_path(client, resend):
    _post(client, {"message": MESSAGE, "page": "/" + "a" * 300 + "?x=1"})

    assert resend.payload["subject"] == "[CourtAI feedback] /" + "a" * 149


def test_subject_strips_cr_and_lf_from_a_hostile_path(client, resend):
    # A control character in the path is refused, so the subject cannot be split.
    response = _post(client, {"message": MESSAGE, "page": "/x\r\nBcc: a@b.c"})

    assert response.status_code == 400
    assert resend.requests == []


def test_x_handle_with_a_leading_at_is_stored_without_it(client, resend):
    _post(client, {"message": MESSAGE, "x_handle": "@abc_123"})

    assert "X: https://x.com/abc_123\n" in resend.payload["text"]


def test_waitlisted_account_can_send(client, resend, monkeypatch):
    import app.utils.auth as auth

    monkeypatch.setattr(
        auth, "_refuse_waitlisted", lambda *a, **k: (_ for _ in ()).throw(AssertionError("checked"))
    )

    assert _post(client, {"message": MESSAGE}).status_code == 201


def test_unauthenticated_request_is_refused_and_sends_nothing(client, resend):
    response = client.post("/api/user/feedback", json={"message": MESSAGE})

    assert response.status_code == 401
    assert resend.requests == []


# --- validation -------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({}, id="no-message"),
        pytest.param({"message": "   "}, id="blank-message"),
        pytest.param({"message": 5}, id="non-string-message"),
        pytest.param({"message": "x" * 4001}, id="message-too-long"),
        pytest.param({"message": MESSAGE, "reply_ok": "yes"}, id="reply-ok-not-boolean"),
        pytest.param({"message": MESSAGE, "x_handle": "has space"}, id="x-handle-space"),
        pytest.param({"message": MESSAGE, "x_handle": "a" * 16}, id="x-handle-too-long"),
        pytest.param({"message": MESSAGE, "x_handle": "@@double"}, id="x-handle-double-at"),
        pytest.param({"message": MESSAGE, "x_handle": "@"}, id="x-handle-only-at"),
        pytest.param({"message": MESSAGE, "page": "https://evil.example/x"}, id="page-absolute-url"),
        pytest.param({"message": MESSAGE, "page": "//evil.example/x"}, id="page-protocol-relative"),
        pytest.param({"message": MESSAGE, "page": "matchups"}, id="page-not-a-path"),
        pytest.param({"message": MESSAGE, "page": "/" + "a" * 500}, id="page-too-long"),
        pytest.param({"message": MESSAGE, "app_version": "v" * 65}, id="app-version-too-long"),
        pytest.param({"message": MESSAGE, "error_ref": "3F9A12BC"}, id="error-ref-uppercase"),
        pytest.param({"message": MESSAGE, "error_ref": "3f9a12b"}, id="error-ref-short"),
        pytest.param({"message": MESSAGE, "viewport": "390 x 844"}, id="viewport-space"),
        pytest.param({"message": MESSAGE, "viewport": "5x844"}, id="viewport-too-small"),
        pytest.param(
            {"message": MESSAGE, "screenshot": f"data:image/gif;base64,{SCREENSHOT_B64}"}, id="shot-gif"
        ),
        pytest.param({"message": MESSAGE, "screenshot": SCREENSHOT_B64}, id="shot-not-a-data-url"),
        pytest.param({"message": MESSAGE, "screenshot": "data:image/png;base64,@@@@"}, id="shot-bad-base64"),
        pytest.param({"message": MESSAGE, "screenshot": "data:image/png;base64,abc"}, id="shot-bad-padding"),
    ],
)
def test_invalid_input_is_a_400_with_the_standard_envelope_and_sends_nothing(client, resend, body):
    response = _post(client, body)

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_input"
    assert resend.requests == []


def test_non_object_body_is_a_400(client, resend):
    response = client.post(
        "/api/user/feedback", json=["not", "an", "object"], headers={"Authorization": "Bearer user-a"}
    )

    assert response.status_code == 400


def test_boundary_values_are_accepted(client, resend):
    body = {
        "message": "x" * 4000,
        "x_handle": "a" * 15,
        "page": "/" + "a" * 499,
        "app_version": "v" * 64,
        "viewport": "10x99999",
    }

    assert _post(client, body).status_code == 201


def test_screenshot_of_exactly_2_mb_is_accepted_and_one_byte_more_is_a_400(client, resend):
    limit = 2 * 1024 * 1024
    at_limit = base64.b64encode(b"\x00" * limit).decode()
    over_limit = base64.b64encode(b"\x00" * (limit + 1)).decode()

    assert (
        _post(client, {"message": MESSAGE, "screenshot": f"data:image/png;base64,{at_limit}"}).status_code
        == 201
    )
    assert len(resend.requests) == 1

    refused = _post(client, {"message": MESSAGE, "screenshot": f"data:image/png;base64,{over_limit}"})

    assert refused.status_code == 400
    assert refused.get_json()["error"]["code"] == "invalid_input"
    assert len(resend.requests) == 1


# --- unavailable ------------------------------------------------------------


@pytest.mark.parametrize("feedback_settings", [FeedbackSettings(resend_api_key=None)])
def test_missing_key_is_a_503_and_never_reports_success(client, resend):
    response = _post(client, FULL_BODY)

    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "feedback_unavailable"
    assert response.headers["X-Request-ID"]
    assert resend.requests == []


@pytest.mark.parametrize("status", [400, 401, 422, 429, 500, 502])
def test_provider_error_is_a_503(client, resend, status):
    resend.status = status

    response = _post(client, FULL_BODY)

    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "feedback_unavailable"
    assert response.headers["X-Request-ID"]


@pytest.mark.parametrize(
    "error", [requests.ConnectTimeout("t"), requests.ReadTimeout("t"), requests.ConnectionError("c")]
)
def test_provider_timeout_or_connection_failure_is_a_503(client, resend, error):
    resend.error = error

    response = _post(client, FULL_BODY)

    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "feedback_unavailable"


# --- rate limit -------------------------------------------------------------


def test_eleventh_message_in_an_hour_is_429_and_another_user_is_unaffected(client, resend):
    statuses = [_post(client, {"message": MESSAGE}).status_code for _ in range(11)]

    assert statuses == [201] * 10 + [429]
    assert len(resend.requests) == 10
    assert _post(client, {"message": MESSAGE}, token="user-b").status_code == 201


# --- delivery=log -----------------------------------------------------------


@pytest.mark.parametrize("feedback_settings", [FeedbackSettings(delivery="log")])
def test_log_delivery_logs_the_composed_email_and_sends_nothing(client, resend, caplog):
    with caplog.at_level(logging.INFO):
        response = _post(client, FULL_BODY)

    assert response.status_code == 201
    assert response.get_json() == {"sent": True}
    assert resend.requests == []
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Feedback ")]
    assert len(lines) == 1
    assert "outcome=logged" in lines[0]
    assert "uid=user-a" in lines[0]
    assert "[CourtAI feedback] /matchups/0022600012 · ref 3f9a12bc" in lines[0]
    assert MESSAGE in lines[0]
    assert "('screenshot.jpg'," in lines[0]
    assert SCREENSHOT_B64 not in "\n".join(r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("feedback_settings", [FeedbackSettings(delivery="log")])
def test_log_delivery_needs_no_key_and_still_validates(client, resend):
    assert _post(client, {"message": ""}).status_code == 400


# --- logging ----------------------------------------------------------------


def _all_log_text(caplog):
    return "\n".join(
        record.getMessage() + " " + str(record.__dict__.get("exc_text")) + str(record.args)
        for record in caplog.records
    )


def test_sent_message_logs_one_line_without_the_text_or_screenshot(client, resend, caplog):
    with caplog.at_level(logging.DEBUG):
        _post(client, FULL_BODY)

    feedback_lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Feedback ")]
    assert len(feedback_lines) == 1
    assert feedback_lines[0].startswith("Feedback id=")
    assert "uid=user-a outcome=sent" in feedback_lines[0]
    text = _all_log_text(caplog)
    assert MESSAGE not in text
    assert SCREENSHOT_B64 not in text
    assert "re_test_key" not in text


def test_failed_delivery_logs_neither_text_nor_screenshot(client, resend, caplog):
    resend.status = 500
    with caplog.at_level(logging.DEBUG):
        _post(client, FULL_BODY)
    resend.status = 200
    resend.error = requests.ReadTimeout("read timed out")
    with caplog.at_level(logging.DEBUG):
        _post(client, FULL_BODY)

    outcomes = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Feedback ")]
    assert [o.split("outcome=")[1] for o in outcomes] == [
        "failed resend_status=500",
        "failed reason=ReadTimeout",
    ]
    text = _all_log_text(caplog)
    assert MESSAGE not in text
    assert SCREENSHOT_B64 not in text
    assert "re_test_key" not in text


def test_invalid_input_logs_neither_text_nor_screenshot(client, resend, caplog):
    with caplog.at_level(logging.DEBUG):
        _post(client, {**FULL_BODY, "error_ref": "nothex!!"})

    text = _all_log_text(caplog)
    assert MESSAGE not in text
    assert SCREENSHOT_B64 not in text


# --- settings ---------------------------------------------------------------


def test_settings_defaults_and_overrides():
    defaults = load_settings(environ={"FLASK_ENV": "development"}).feedback
    assert (defaults.resend_api_key, defaults.to, defaults.sender, defaults.delivery) == (
        None,
        "support@courtai.app",
        "CourtAI Feedback <feedback@courtai.app>",
        "resend",
    )

    configured = load_settings(
        environ={
            "FLASK_ENV": "development",
            "RESEND_API_KEY": "re_x",
            "FEEDBACK_TO": "me@example.com",
            "FEEDBACK_FROM": "Me <me@example.com>",
            "FEEDBACK_DELIVERY": "LOG",
        }
    ).feedback
    assert (configured.resend_api_key, configured.to, configured.sender, configured.delivery) == (
        "re_x",
        "me@example.com",
        "Me <me@example.com>",
        "log",
    )
    assert "re_x" not in repr(configured)


def test_unknown_delivery_mode_fails_startup():
    from app.config.settings import ConfigurationError

    with pytest.raises(ConfigurationError, match="FEEDBACK_DELIVERY"):
        load_settings(environ={"FLASK_ENV": "development", "FEEDBACK_DELIVERY": "smtp"})
