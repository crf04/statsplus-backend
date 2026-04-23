"""Deterministic tests for LLM service helpers."""

from types import SimpleNamespace

from app.services.llm_service import LLMConfig, LLMService


class FakeCompletions:
    """Minimal stand-in for OpenAI chat completions."""

    def __init__(self, content: str):
        self.content = content
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=self.content),
                )
            ],
            usage=None,
            model=kwargs["model"],
        )


def make_service(content: str) -> LLMService:
    service = LLMService.__new__(LLMService)
    service.config = SimpleNamespace(
        model="gpt-4o-mini",
        temperature=0,
        max_tokens=512,
        timeout=10.0,
        max_retries=1,
    )
    service.client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions(content))
    )
    service.player_aliases = {}
    service.team_aliases = {}
    return service


def test_config_validation_requires_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    config = LLMConfig()

    assert config.validate() is False


def test_load_system_prompt_falls_back_for_missing_file():
    service = LLMService.__new__(LLMService)

    prompt = service._load_system_prompt_from_file("missing-prompt-file.txt")

    assert "NBA statistics queries" in prompt


def test_query_llm_parses_valid_json_response():
    service = make_service('{"player_name": "LeBron James", "confidence": 0.9}')

    result = service.query_llm("LeBron last 10 games", system_prompt="Return JSON")

    assert result["success"] is True
    assert result["content"]["player_name"] == "LeBron James"
    assert result["content"]["confidence"] == 0.9
    assert result["attempt"] == 1


def test_query_llm_marks_invalid_json_response():
    service = make_service("not-json")

    result = service.query_llm("LeBron last 10 games", system_prompt="Return JSON")

    assert result["success"] is True
    assert result["content"]["parsing_error"] is True
    assert result["content"]["raw_response"] == "not-json"
