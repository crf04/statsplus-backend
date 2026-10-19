"""In-app feedback email (crf04/statsplus#258, crf04/statsplus-backend#486).

``parse_feedback`` validates the request body; ``deliver`` composes a
plain-text email and sends it through the Resend HTTP API (or logs it when
``FEEDBACK_DELIVERY=log``).  Nothing is stored: the email is the record.  The
account name and email come from the verified token, never the body.  In
``resend`` mode no log line carries the message text or the screenshot; in
``log`` mode the composed email (without the screenshot) is the point.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import requests

from app.config.settings import RuntimeSettings
from app.errors import FeedbackUnavailableError, InvalidInputError

logger = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"
RESEND_TIMEOUT_SECONDS = 10

MESSAGE_MAX_CHARS = 4000
PAGE_MAX_CHARS = 500
APP_VERSION_MAX_CHARS = 64
SUBJECT_PATH_MAX_CHARS = 150
SCREENSHOT_MAX_BYTES = 2 * 1024 * 1024
#: A 2 MB screenshot is about 2.8 MB as base64; the rest is headroom for the fields.
MAX_REQUEST_BYTES = 4 * 1024 * 1024

_X_HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}")
_ERROR_REF = re.compile(r"[0-9a-f]{8}")
_VIEWPORT = re.compile(r"\d{2,5}x\d{2,5}", re.ASCII)
_SCREENSHOT = re.compile(r"data:image/(jpeg|png);base64,([A-Za-z0-9+/]+={0,2})")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class Feedback:
    message: str
    reply_ok: bool
    x_handle: str | None
    page: str | None
    app_version: str | None
    error_ref: str | None
    viewport: str | None
    screenshot_extension: str | None
    screenshot_base64: str | None


def _optional_text(body: dict[str, Any], name: str) -> str | None:
    value = body.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidInputError(f"{name} must be a string.")
    value = value.strip()
    return value or None


def parse_feedback(body: Any) -> Feedback:
    """Validate a request body into a :class:`Feedback`, or raise a 400."""

    if not isinstance(body, dict):
        raise InvalidInputError("A JSON object is required.")

    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        raise InvalidInputError("message is required.")
    message = message.strip()
    if len(message) > MESSAGE_MAX_CHARS:
        raise InvalidInputError(f"message must be at most {MESSAGE_MAX_CHARS} characters.")

    reply_ok = body.get("reply_ok", False)
    if reply_ok is None:
        reply_ok = False
    if not isinstance(reply_ok, bool):
        raise InvalidInputError("reply_ok must be a boolean.")

    x_handle = _optional_text(body, "x_handle")
    if x_handle is not None:
        x_handle = x_handle.removeprefix("@")
        if not _X_HANDLE.fullmatch(x_handle):
            raise InvalidInputError("x_handle must be 1-15 letters, digits or underscores.")

    page = _optional_text(body, "page")
    if page is not None and (
        len(page) > PAGE_MAX_CHARS
        or not page.startswith("/")
        or page.startswith(("//", "/\\"))
        or _CONTROL.search(page)
    ):
        raise InvalidInputError(f"page must be a path of at most {PAGE_MAX_CHARS} characters.")

    app_version = _optional_text(body, "app_version")
    if app_version is not None and (len(app_version) > APP_VERSION_MAX_CHARS or _CONTROL.search(app_version)):
        raise InvalidInputError(f"app_version must be at most {APP_VERSION_MAX_CHARS} characters.")

    error_ref = _optional_text(body, "error_ref")
    if error_ref is not None and not _ERROR_REF.fullmatch(error_ref):
        raise InvalidInputError("error_ref must be 8 lowercase hexadecimal characters.")

    viewport = _optional_text(body, "viewport")
    if viewport is not None and not _VIEWPORT.fullmatch(viewport):
        raise InvalidInputError("viewport must look like 390x844.")

    extension = screenshot_base64 = None
    screenshot = _optional_text(body, "screenshot")
    if screenshot is not None:
        match = _SCREENSHOT.fullmatch(screenshot)
        if match is None:
            raise InvalidInputError("screenshot must be a base64 data:image/jpeg or data:image/png URL.")
        kind, screenshot_base64 = match.groups()
        extension = "jpg" if kind == "jpeg" else "png"
        # Reject before decoding: base64 is 4 characters per 3 bytes.
        if len(screenshot_base64) > (SCREENSHOT_MAX_BYTES + 2) // 3 * 4:
            raise InvalidInputError("screenshot must be at most 2 MB.")
        try:
            decoded_size = len(base64.b64decode(screenshot_base64, validate=True))
        except (binascii.Error, ValueError) as error:
            raise InvalidInputError("screenshot is not valid base64.") from error
        if decoded_size > SCREENSHOT_MAX_BYTES:
            raise InvalidInputError("screenshot must be at most 2 MB.")

    return Feedback(
        message=message,
        reply_ok=reply_ok,
        x_handle=x_handle,
        page=page,
        app_version=app_version,
        error_ref=error_ref,
        viewport=viewport,
        screenshot_extension=extension,
        screenshot_base64=screenshot_base64,
    )


def _utcnow() -> datetime:
    return datetime.now(UTC)


def compose_subject(feedback: Feedback) -> str:
    path = (feedback.page or "unknown page").split("?", 1)[0]
    path = path.replace("\r", "").replace("\n", "")[:SUBJECT_PATH_MAX_CHARS]
    subject = f"[CourtAI feedback] {path}"
    if feedback.error_ref:
        subject += f" · ref {feedback.error_ref}"
    return subject


def compose_body(feedback: Feedback, user: dict[str, Any], environment: str) -> str:
    lines = [
        feedback.message,
        "",
        "---",
        f"From: {user.get('name') or '(no name)'} <{user.get('email') or 'no email'}>",
        f"UID: {user['uid']}",
    ]
    optional = (
        ("X", f"https://x.com/{feedback.x_handle}" if feedback.x_handle else None),
        ("Page", feedback.page),
        ("App version", feedback.app_version),
        ("Error ref", feedback.error_ref),
        ("Viewport", feedback.viewport),
    )
    lines.extend(f"{label}: {value}" for label, value in optional if value)
    lines.extend(
        [
            f"Time (UTC): {_utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')}",
            f"Reply allowed: {'yes' if feedback.reply_ok else 'no'}",
            f"Environment: {environment}",
        ]
    )
    return "\n".join(lines)


def deliver(feedback: Feedback, user: dict[str, Any] | None, settings: RuntimeSettings) -> None:
    """Send (or log) the email; raise ``FeedbackUnavailableError`` if it cannot be sent."""

    if not user:
        raise FeedbackUnavailableError(detail="no authenticated user")
    config = settings.feedback
    feedback_id = uuid.uuid4().hex[:12]

    payload: dict[str, Any] = {
        "from": config.sender,
        "to": [config.to],
        "subject": compose_subject(feedback),
        "text": compose_body(feedback, user, settings.environment),
    }
    if feedback.reply_ok and user.get("email"):
        payload["reply_to"] = user["email"]
    if feedback.screenshot_base64:
        payload["attachments"] = [
            {
                "filename": f"screenshot.{feedback.screenshot_extension}",
                "content": feedback.screenshot_base64,
            }
        ]

    if config.delivery == "log":
        # Local and QA runs read the composed email here; the screenshot is
        # only named and sized, never written.
        logger.info(
            "Feedback id=%s uid=%s outcome=logged email=%s attachments=%s",
            feedback_id,
            user["uid"],
            {key: value for key, value in payload.items() if key != "attachments"},
            [(a["filename"], len(a["content"])) for a in payload.get("attachments", [])],
        )
        return

    if not config.resend_api_key:
        logger.error("Feedback id=%s uid=%s outcome=unconfigured", feedback_id, user["uid"])
        raise FeedbackUnavailableError(detail="RESEND_API_KEY is not set")
    try:
        response = requests.post(
            RESEND_URL,
            json=payload,
            headers={"Authorization": f"Bearer {config.resend_api_key}"},
            timeout=RESEND_TIMEOUT_SECONDS,
        )
    except requests.RequestException as error:
        logger.error(
            "Feedback id=%s uid=%s outcome=failed reason=%s", feedback_id, user["uid"], type(error).__name__
        )
        raise FeedbackUnavailableError(detail=type(error).__name__) from error
    if not response.ok:
        logger.error(
            "Feedback id=%s uid=%s outcome=failed resend_status=%s",
            feedback_id,
            user["uid"],
            response.status_code,
        )
        raise FeedbackUnavailableError(detail=f"Resend answered {response.status_code}")
    logger.info("Feedback id=%s uid=%s outcome=sent", feedback_id, user["uid"])
