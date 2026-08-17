"""Natural-language date parsing performance boundaries."""

from __future__ import annotations

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
