"""Shadow sampling of confident NLP parses against the LLM.

The response must be the NLP result whether or not a query is sampled, the
comparison must run off the request path, and every sample must leave exactly
one structured ``nl_shadow`` log line that the summary script can read.
"""

import json
import logging

import pytest

from app.services import nl_shadow
from app.services.nl_query.parser import SelfFilter
from scripts.summarize_nl_shadow import parse_records, summarize
from tests.services.test_nl_service import (
    RosterParser,
    StubLLM,
    components,
    llm_parse,
    make_service,
)


class InlineExecutor:
    """Runs submitted work immediately, so tests observe it synchronously."""

    def submit(self, fn):
        fn()


class HeldExecutor:
    """Holds submitted work until released, to fill the in-flight slots."""

    def __init__(self):
        self.pending = []

    def submit(self, fn):
        self.pending.append(fn)

    def release_all(self):
        while self.pending:
            self.pending.pop(0)()


def sampler(rate=1.0, executor=None, rng=lambda: 0.0, max_in_flight=4):
    return nl_shadow.ShadowSampler(
        rate, executor=executor or InlineExecutor(), rng=rng, max_in_flight=max_in_flight
    )


def shadow_records(caplog):
    return list(parse_records(record.getMessage() for record in caplog.records))


# --- comparable views ------------------------------------------------------


def nl_result(**overrides):
    result = {
        "player_name": "Stephen Curry",
        "team_name": None,
        "game_count": 10,
        "location": "home",
        "players_on": [],
        "players_off": [],
        "teams_against": ["OPP_PTS"],
        "rank_filter": ["-5"],
        "minutes_filter": None,
        "self_filters": [],
        "date_filter": None,
        "season": "2025-26",
    }
    result.update(overrides)
    return result


def test_the_two_parsers_shapes_compare_equal_when_they_mean_the_same_filters():
    nlp = nl_result(
        location="Home",
        minutes_filter=(20, 35),
        self_filters=[SelfFilter(stat_column="PTS", operator="gte", value=30)],
        players_off=["Klay Thompson"],
    )
    llm = nl_result(
        location="home",
        minutes_filter=[20, 35],
        self_filters=[
            {"stat_column": "pts", "operator": "gte", "value": 30.0, "value2": None}
        ],
        players_off=["klay thompson"],
    )

    assert nl_shadow.differences(
        nl_shadow.comparable_view(nlp), nl_shadow.comparable_view(llm)
    ) == {}


def test_both_and_no_location_are_the_same_filter():
    assert nl_shadow.differences(
        nl_shadow.comparable_view(nl_result(location="both")),
        nl_shadow.comparable_view(nl_result(location=None)),
    ) == {}


def test_an_opposite_rank_sign_is_a_disagreement_on_the_opponent_filters():
    diff = nl_shadow.differences(
        nl_shadow.comparable_view(nl_result(rank_filter=["-5"])),
        nl_shadow.comparable_view(nl_result(rank_filter=["5"])),
    )

    assert diff == {
        "opponent_filters": {"nlp": [("OPP_PTS", "-5")], "llm": [("OPP_PTS", "5")]}
    }


def test_confidence_and_intent_are_not_compared():
    diff = nl_shadow.differences(
        nl_shadow.comparable_view(nl_result(confidence=0.95, intent="game_logs")),
        nl_shadow.comparable_view(nl_result(confidence=0.4, intent="player_profile")),
    )

    assert diff == {}


# --- the sampler -----------------------------------------------------------


def test_a_zero_rate_never_samples():
    calls = []

    assert sampler(rate=0.0).maybe_submit(lambda: calls.append(1)) is False
    assert calls == []


def test_a_query_above_the_rate_is_not_sampled():
    calls = []

    assert sampler(rate=0.1, rng=lambda: 0.5).maybe_submit(lambda: calls.append(1)) is False
    assert calls == []


def test_a_sampled_query_runs_its_comparison():
    calls = []

    assert sampler(rate=0.1, rng=lambda: 0.05).maybe_submit(lambda: calls.append(1)) is True
    assert calls == [1]


def test_samples_beyond_the_in_flight_cap_are_dropped_not_queued(caplog):
    held = HeldExecutor()
    shadow = sampler(executor=held, max_in_flight=2)
    caplog.set_level(logging.INFO, logger="app.services.nl_shadow")

    accepted = [shadow.maybe_submit(lambda: None) for _ in range(3)]

    assert accepted == [True, True, False]
    assert len(held.pending) == 2
    assert "nl_shadow dropped" in caplog.text

    # Finished comparisons free their slots for later samples.
    held.release_all()
    assert shadow.maybe_submit(lambda: None) is True


# --- through NLService -----------------------------------------------------


def confident_service(llm_response, shadow):
    parser = RosterParser(
        components(
            player_name="Stephen Curry",
            game_count=10,
            location="home",
            opponent_filters=[("OPP_PTS", -5)],
            confidence=0.97,
            should_use_llm=False,
        )
    )
    llm = StubLLM(llm_response)
    return make_service(llm_service=llm, parser=parser, shadow_sampler=shadow), llm


def test_a_sampled_confident_query_still_returns_the_nlp_result(caplog):
    caplog.set_level(logging.INFO, logger="app.services.nl_shadow")
    unsampled, _ = confident_service(llm_parse(), None)
    sampled, llm = confident_service(
        llm_parse(
            player_name="steph",
            game_count=10,
            location="home",
            opponent_filters=[{"filter_type": "OPP_PTS", "rank": 5}],
        ),
        sampler(),
    )

    result = sampled.process_query("steph vs top 5 defenses at home last 10")

    assert result == unsampled.process_query("steph vs top 5 defenses at home last 10")
    assert result["parsed_by"] == "nlp"
    assert len(llm.calls) == 1
    # The LLM is seeded with the same player context as the fallback path.
    assert llm.calls[0]["context"] == {"player_name": "Stephen Curry"}

    [record] = shadow_records(caplog)
    assert record["outcome"] == "disagree"
    assert record["query"] == "steph vs top 5 defenses at home last 10"
    assert record["nlp_confidence"] == 0.97
    assert set(record["differences"]) == {"opponent_filters"}


def test_an_agreeing_sample_logs_agree(caplog):
    caplog.set_level(logging.INFO, logger="app.services.nl_shadow")
    service, _ = confident_service(
        llm_parse(
            player_name="Stephen Curry",
            game_count=10,
            location="home",
            opponent_filters=[{"filter_type": "OPP_PTS", "rank": -5}],
        ),
        sampler(),
    )

    service.process_query("curry vs top 5 defenses at home last 10")

    [record] = shadow_records(caplog)
    assert record["outcome"] == "agree"
    assert "differences" not in record


def test_an_llm_failure_is_logged_as_an_error_and_never_reaches_the_user(caplog):
    from app.services.llm_service import LLMError

    caplog.set_level(logging.INFO, logger="app.services.nl_shadow")
    service, _ = confident_service(LLMError("rate limited"), sampler())

    result = service.process_query("curry last 10")

    assert result["parsed_by"] == "nlp"
    [record] = shadow_records(caplog)
    assert record["outcome"] == "error"
    assert "rate limited" in record["error"]


def test_a_low_confidence_query_is_not_also_shadowed(caplog):
    caplog.set_level(logging.INFO, logger="app.services.nl_shadow")
    parser = RosterParser(components(confidence=0.2, should_use_llm=True))
    llm = StubLLM(llm_parse(player_name="Stephen Curry"))
    service = make_service(llm_service=llm, parser=parser, shadow_sampler=sampler())

    service.process_query("curry games taking a lot of shots")

    # Exactly the one fallback call; no second, shadow call.
    assert len(llm.calls) == 1
    assert shadow_records(caplog) == []


# --- configuration ---------------------------------------------------------


@pytest.mark.parametrize(
    ("rate", "fallback", "expected"),
    [("0.1", "true", True), ("0", "true", False), ("0.1", "false", False)],
)
def test_sampling_is_on_only_with_a_rate_and_the_fallback_enabled(
    seeded_db_url, monkeypatch, rate, fallback, expected
):
    from sqlalchemy import create_engine

    from app.config.settings import load_settings
    from app.services import nl_service as nl_service_module
    from app.services.nl_service import NLService

    monkeypatch.setattr(nl_service_module, "LLMService", lambda **kwargs: object())
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("ENABLE_LLM_FALLBACK", fallback)
    monkeypatch.setenv("LLM_SHADOW_SAMPLE_RATE", rate)

    service = NLService(create_engine(seeded_db_url), settings=load_settings())

    assert (service.shadow_sampler is not None) is expected


def test_the_sample_rate_defaults_to_off(monkeypatch):
    from app.config.settings import load_settings

    monkeypatch.delenv("LLM_SHADOW_SAMPLE_RATE", raising=False)

    assert load_settings().llm.shadow_sample_rate == 0.0


@pytest.mark.parametrize("rate", ["-0.1", "1.5"])
def test_an_out_of_range_rate_is_a_configuration_error(monkeypatch, rate):
    from app.config.settings import load_settings

    monkeypatch.setenv("LLM_SHADOW_SAMPLE_RATE", rate)

    with pytest.raises(Exception, match="shadow_sample_rate"):
        load_settings()


# --- summary script --------------------------------------------------------


def test_the_summary_reads_prefixed_log_lines_and_ignores_the_rest():
    lines = [
        "2026-09-23 INFO app.services.nl_service Using NLP (confidence 0.970): x",
        "2026-09-23 INFO app.services.nl_shadow nl_shadow "
        + json.dumps({"outcome": "agree", "latency_ms": 400}),
        "[railway] nl_shadow "
        + json.dumps({
            "outcome": "disagree",
            "latency_ms": 900,
            "query": "q",
            "differences": {"opponent_filters": {}, "location": {}},
        }),
        "nl_shadow " + json.dumps({"outcome": "error", "latency_ms": 8000}),
        "nl_shadow dropped: too many comparisons in flight",
        "nl_shadow {not json",
    ]

    summary = summarize(parse_records(lines))

    assert summary["comparisons"] == 3
    assert summary["outcomes"] == {"agree": 1, "disagree": 1, "error": 1}
    # Errors say nothing about NLP accuracy, so they are outside the rate.
    assert summary["agreement_rate"] == 0.5
    assert summary["disagreements_by_field"] == {"opponent_filters": 1, "location": 1}
    assert summary["median_latency_ms"] == 900
    assert [example["query"] for example in summary["examples"]] == ["q"]
