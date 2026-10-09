"""Application logs are one JSON object per line on stdout."""

from __future__ import annotations

import json
import logging

from app import create_app


def test_error_with_exception_is_one_json_line_with_request_id(client, capsys):
    @client.application.route("/__boom")
    def boom():
        try:
            raise ValueError("kaboom detail")
        except ValueError:
            logging.getLogger("tests.boom").error("it broke", exc_info=True)
        return "ok"

    client.get("/__boom", headers={"X-Request-ID": "req-log-1"})

    lines = [
        line for line in capsys.readouterr().out.splitlines() if "it broke" in line
    ]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["level"] == "error"
    assert record["logger"] == "tests.boom"
    assert record["message"] == "it broke"
    assert record["request_id"] == "req-log-1"
    assert "Traceback" in record["exception"]
    assert "ValueError: kaboom detail" in record["exception"]


def test_record_outside_request_has_no_request_id_or_exception(capsys):
    logging.getLogger("tests.plain").warning("plain %s", "note")

    lines = [
        entry for entry in capsys.readouterr().out.splitlines() if "plain note" in entry
    ]
    assert [json.loads(entry) for entry in lines] == [
        {"level": "warning", "logger": "tests.plain", "message": "plain note"}
    ]


def test_repeated_create_app_does_not_duplicate_log_lines(capsys):
    create_app({"ENVIRONMENT": "testing"})
    create_app({"ENVIRONMENT": "testing"})
    capsys.readouterr()

    logging.getLogger("tests.dup").warning("once only")

    lines = [
        entry for entry in capsys.readouterr().out.splitlines() if "once only" in entry
    ]
    assert len(lines) == 1
