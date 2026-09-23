"""Deterministic tests for the structured-output LLM service."""

from types import SimpleNamespace

import pytest

from app.services.llm_service import (
    LLMConfig,
    LLMError,
    LLMParsedQuery,
    LLMService,
)


def parsed_query(**overrides) -> LLMParsedQuery:
    fields = {
        "player_name": None,
        "team_name": None,
        "game_count": None,
        "date_range": None,
        "location": None,
        "minutes_filter": None,
        "self_filters": [],
        "opponent_filters": [],
        "players_on": [],
        "players_off": [],
        "season": None,
        "intent": "game_logs",
        "time_period": None,
        "confidence": 0.9,
    }
    fields.update(overrides)
    return LLMParsedQuery(**fields)


class FakeCompletions:
    """Stand-in for ``client.chat.completions`` with the structured parse API.

    Each queued outcome is a message (parsed/refusal) or an exception.
    """

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(choices=[SimpleNamespace(message=outcome)])


def message(parsed=None, refusal=None):
    return SimpleNamespace(parsed=parsed, refusal=refusal)


def make_service(*outcomes, model="gpt-4o-mini", max_retries=1) -> LLMService:
    service = LLMService.__new__(LLMService)
    service.config = SimpleNamespace(
        model=model,
        temperature=0,
        max_tokens=512,
        timeout=10.0,
        max_retries=max_retries,
    )
    service.client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions(*outcomes))
    )
    service.system_prompt = "Parse NBA queries."
    return service


def test_config_validation_requires_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    config = LLMConfig()

    assert config.validate() is False


def test_load_system_prompt_falls_back_for_missing_file():
    service = LLMService.__new__(LLMService)

    prompt = service._load_system_prompt_from_file("missing-prompt-file.txt")

    assert "NBA statistics queries" in prompt


def test_the_service_loads_the_production_prompt(monkeypatch):
    from app.config.settings import load_settings

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    client = SimpleNamespace()

    service = LLMService(settings=load_settings(), client=client)

    assert service.client is client
    assert "opponent_filters" in service.system_prompt


def test_parse_query_requests_the_strict_schema_and_returns_the_parse():
    expected = parsed_query(player_name="LeBron James", game_count=10)
    service = make_service(message(parsed=expected))

    result = service.parse_query("LeBron last 10 games")
    request = service.client.chat.completions.calls[0]

    assert result is expected
    assert request["response_format"] is LLMParsedQuery
    assert request["messages"][-1] == {
        "role": "user", "content": "LeBron last 10 games"
    }
    assert request["max_tokens"] == 512
    assert request["temperature"] == 0
    assert "max_completion_tokens" not in request


def test_resolved_players_are_added_to_the_system_prompt():
    service = make_service(message(parsed=parsed_query()))

    service.parse_query(
        "steph without klay",
        {"player_name": "Stephen Curry", "players_off": ["Klay Thompson"]},
    )
    system = service.client.chat.completions.calls[0]["messages"][0]["content"]

    assert system.startswith("Parse NBA queries.")
    assert "RESOLVED PLAYERS" in system
    assert "Main player: Stephen Curry" in system
    assert "Players off court: Klay Thompson" in system


def test_no_player_context_leaves_the_prompt_unchanged():
    service = make_service(message(parsed=parsed_query()))

    service.parse_query("top 5 defenses", {})
    system = service.client.chat.completions.calls[0]["messages"][0]["content"]

    assert system == "Parse NBA queries."


def test_gpt5_models_use_the_completion_token_parameter():
    service = make_service(message(parsed=parsed_query()), model="gpt-5-nano")

    service.parse_query("Giannis against stingy perimeter defenses")
    request = service.client.chat.completions.calls[0]

    assert request["max_completion_tokens"] == 512
    assert request["reasoning_effort"] == "minimal"
    assert "max_tokens" not in request
    assert "temperature" not in request


def test_a_refusal_is_an_error():
    service = make_service(message(refusal="I can't help with that."))

    with pytest.raises(LLMError, match="refused"):
        service.parse_query("anything")


def test_a_missing_parse_is_an_error():
    service = make_service(message(parsed=None))

    with pytest.raises(LLMError, match="no structured output"):
        service.parse_query("anything")


def test_a_transport_failure_is_retried_within_the_budget(monkeypatch):
    monkeypatch.setattr("app.services.llm_service.time.sleep", lambda _: None)
    expected = parsed_query(player_name="Stephen Curry")
    service = make_service(
        RuntimeError("timeout"), message(parsed=expected), max_retries=2
    )

    assert service.parse_query("curry") is expected
    assert len(service.client.chat.completions.calls) == 2


def test_exhausted_attempts_raise_with_the_last_error():
    service = make_service(RuntimeError("openai unreachable"))

    with pytest.raises(LLMError, match="openai unreachable"):
        service.parse_query("curry")


def test_the_schema_rejects_an_unknown_opponent_filter():
    with pytest.raises(ValueError):
        parsed_query(opponent_filters=[{"filter_type": "Vibes", "rank": 5}])
