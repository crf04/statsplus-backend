"""Natural-language date parsing performance boundaries."""

from __future__ import annotations

import pytest

from app.utils.date_parser import NBADateParser


def test_general_date_fallback_only_considers_english(monkeypatch, runtime_settings):
    calls = []

    def parse(phrase, **kwargs):
        calls.append((phrase, kwargs))
        return None

    monkeypatch.setattr("app.utils.date_parser.dateparser.parse", parse)
    parser = NBADateParser(settings=runtime_settings)

    assert parser.parse_date_from_query("Donovan Mitchell this year") is None
    assert calls
    assert all(kwargs.get("languages") == ["en"] for _, kwargs in calls)


def test_structured_date_parses_only_consider_english(monkeypatch, runtime_settings):
    calls = []

    class ParsedDate:
        def strftime(self, _format):
            return "2026-01-01"

    def parse(phrase, **kwargs):
        calls.append((phrase, kwargs))
        return ParsedDate()

    monkeypatch.setattr("app.utils.date_parser.dateparser.parse", parse)
    parser = NBADateParser(settings=runtime_settings)

    assert parser._parse_relative_dates("since january") == "2026-01-01"
    assert parser._parse_explicit_dates("January 1, 2026") == "2026-01-01"
    assert calls
    assert all(kwargs.get("languages") == ["en"] for _, kwargs in calls)


# --- start and end dates ----------------------------------------------------


def _parser_on_2026_10_03(monkeypatch, runtime_settings):
    from datetime import datetime

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 3, 12, 0)

    monkeypatch.setattr("app.utils.date_parser.datetime", FrozenDatetime)
    return NBADateParser(settings=runtime_settings)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        # Explicit years are honoured, including for "since" (which lost the
        # year on master).
        ("games since March 1, 2024", "2024-03-01"),
        ("games until March 1, 2024", "2024-03-01"),
        ("games before March 1, 2024", "2024-02-29"),
        ("games before February 29, 2024", "2024-02-28"),
        ("games before 2026-03-01", "2026-02-28"),
        ("games before 03/01/2026", "2026-02-28"),
        ("games until Mar 1, 2024", "2024-03-01"),
        ("games until March 2025", "2025-03-01"),
        ("games before March 1st, 2024", "2024-02-29"),
        ("games since Mar 5, 2024", "2024-03-05"),
        ("games since March 2nd 2024", "2024-03-02"),
        # Month shortcuts, full and abbreviated.
        ("games since March 5", "2026-03-05"),
        ("games until March 15", "2026-03-15"),
        ("games before March 15", "2026-03-14"),
        ("games before mar 1", "2026-02-28"),
        ("games before February 1", "2026-01-31"),
        # Relative dates: "before" excludes the resolved day.
        ("games since this month", "2026-10-01"),
        ("games until this month", "2026-10-01"),
        ("games before this month", "2026-09-30"),
        ("games before last month", "2026-08-31"),
        ("games since last month", "2026-09-01"),
        # NBA-named dates subtract exactly one day for "before".
        ("games since christmas", "2024-12-25"),
        ("games until christmas", "2024-12-25"),
        ("games before christmas", "2024-12-24"),
    ],
)
def test_dates_resolve_to_literal_start_and_end_days(
    monkeypatch, runtime_settings, query, expected
):
    parser = _parser_on_2026_10_03(monkeypatch, runtime_settings)

    assert parser.parse_date_from_query(query) == expected


@pytest.mark.parametrize(
    "query", ["games since Marchetti returned", "games until Mayday", "games before Aprilia"]
)
def test_month_names_only_match_whole_words(monkeypatch, runtime_settings, query):
    parser = _parser_on_2026_10_03(monkeypatch, runtime_settings)

    assert parser.parse_date_from_query(query) is None
