"""JSON-lines application logging for stdout.

Railway tags stderr as ``error``, so records go to stdout, one JSON object per
line.  ``json.dumps`` escapes newlines, so a traceback stays on the same line.
"""

from __future__ import annotations

import json
import logging
import sys

from flask import g, has_request_context


class JsonFormatter(logging.Formatter):
    """Format a record as one JSON object with correlation context."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if has_request_context():
            request_id = getattr(g, "request_id", None)
            if request_id:
                payload["request_id"] = request_id
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


class _StdoutHandler(logging.StreamHandler):
    """Stream handler that resolves ``sys.stdout`` at emit time."""

    def __init__(self) -> None:
        super().__init__(sys.stdout)

    @property
    def stream(self):
        return sys.stdout

    @stream.setter
    def stream(self, value) -> None:
        pass


def configure_json_logging(level: int | str) -> None:
    """Route root logging to stdout as JSON lines.

    Idempotent: ``create_app`` runs many times per process, so a previously
    installed handler is replaced rather than stacked.
    """

    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, _StdoutHandler):
            root.removeHandler(handler)
    handler = _StdoutHandler()
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)
