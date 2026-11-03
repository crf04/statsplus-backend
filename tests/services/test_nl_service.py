"""Tests for the live natural-language path.

``/api/nl-query`` parses a question and returns structured filters for the
frontend to apply; it does not execute the query server-side. These tests cover
that contract, the LLM routing decision, and the NLP fallback behaviour.

Routing and formatting tests build NLService without ``__init__`` so they do not
pay for a spaCy load; the end-to-end tests drive the real parser.
"""

from types import SimpleNamespace
from typing import ClassVar

import pytest

from app.services.nl_query.parser import QueryComponents, SelfFilter


def make_service(llm_service=None, parser=None, shadow_sampler=None):
    """Build an NLService with its collaborators stubbed."""
    from app.config.settings import load_settings
    from app.services.nl_service import NLService

    service = NLService.__new__(NLService)
    service.settings = load_settings()
    service.publication_reader = None
    service.nl_parser = parser or SimpleNamespace()
    service.query_executor = SimpleNamespace()
    service.llm_service = llm_service
    service.shadow_sampler = shadow_sampler
    return service


def components(**overrides):
    """Build QueryComponents with a confidence breakdown attached."""
    should_use_llm = overrides.pop("should_use_llm", False)
    parsed = QueryComponents(**overrides)
    parsed.confidence_breakdown = SimpleNamespace(should_use_llm=should_use_llm)
    return parsed


class StubParser:
    def __init__(self, result):
        self.result = result
        self.seen = []

    def parse(self, query):
        self.seen.append(query)
        return self.result


# --- input validation ------------------------------------------------------


@pytest.mark.parametrize("query", ["", "   ", "\n\t"])
def test_blank_queries_are_rejected(query):
    service = make_service()

    with pytest.raises(ValueError, match="Empty query"):
        service.process_query(query)


def test_an_uninitialized_system_reports_a_runtime_error():
    service = make_service()
    service.nl_parser = None

    with pytest.raises(RuntimeError, match="not initialized"):
        service.process_query("LeBron last 10 games")


def test_the_query_is_stripped_before_parsing():
    parser = StubParser(components(player_name="LeBron James"))
    service = make_service(parser=parser)

    service.process_query("  LeBron last 10 games  ")

    assert parser.seen == ["LeBron last 10 games"]


# --- NLP fast path ---------------------------------------------------------


def test_confident_queries_stay_on_the_nlp_path():
    parser = StubParser(
        components(
            player_name="LeBron James",
            game_count=10,
            time_period="recent",
            location="home",
            confidence=0.91,
        )
    )
    service = make_service(llm_service=SimpleNamespace(), parser=parser)

    result = service.process_query("LeBron last 10 games at home")

    assert result["parsed_by"] == "nlp"
    assert result["player_name"] == "LeBron James"
    assert result["game_count"] == 10
    assert result["location"] == "home"
    assert result["original_query"] == "LeBron last 10 games at home"


def test_opponent_filters_split_into_teams_and_ranks():
    parser = StubParser(
        components(
            player_name="LeBron James",
            opponent_filters=[("OPP_PTS", 10), ("OPP_REB", -5)],
        )
    )
    service = make_service(parser=parser)

    result = service.process_query("LeBron against top 10 defenses")

    assert result["teams_against"] == ["OPP_PTS", "OPP_REB"]
    # Ranks are stringified for the frontend, negatives preserved.
    assert result["rank_filter"] == ["10", "-5"]


def test_self_filters_are_passed_through_unchanged():
    self_filter = SelfFilter(stat_column="PTS", operator="gte", value=25)
    parser = StubParser(components(player_name="LeBron James", self_filters=[self_filter]))
    service = make_service(parser=parser)

    result = service.process_query("LeBron games with 25+ points")

    assert result["self_filters"] == [self_filter]


def test_a_season_the_query_names_is_kept():
    parser = StubParser(components(player_name="LeBron James"))
    service = make_service(parser=parser)

    result = service.process_query("LeBron James in 2024-25")

    assert result["season"] == "2024-25"


def test_a_date_range_is_not_read_as_a_season():
    parser = StubParser(components(player_name="LeBron James"))
    service = make_service(parser=parser)

    result = service.process_query("LeBron James 2024-11")

    assert result["season"] is None


def test_a_full_date_is_not_read_as_a_season():
    parser = StubParser(components(player_name="LeBron James"))
    service = make_service(parser=parser)

    result = service.process_query("LeBron James after 2011-12-25")

    assert result["season"] is None


def test_a_query_naming_no_season_leaves_the_season_to_the_reader():
    parser = StubParser(components(player_name="LeBron James"))
    service = make_service(parser=parser)

    result = service.process_query("LeBron last 10 games")

    assert result["season"] is None


# --- LLM routing -----------------------------------------------------------


def llm_parse(**overrides):
    from app.services.llm_service import LLMParsedQuery

    fields = {
        "player_name": None,
        "team_name": None,
        "game_count": None,
        "date_range": None,
        "location": None,
        "minutes_filter": None,
        "self_filters": [],
        "opponent_filters": [],
        "opponent_team": None,
        "players_on": [],
        "players_off": [],
        "season": None,
        "intent": "game_logs",
        "time_period": None,
        "confidence": 0.88,
    }
    fields.update(overrides)
    return LLMParsedQuery(**fields)


class StubLLM:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def parse_query(self, query, context):
        self.calls.append({"query": query, "context": context})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class RosterParser(StubParser):
    """A stub parser that also resolves names against a small roster."""

    ROSTER: ClassVar = {
        "kobe": "Kobe Bryant",
        "kobe bryant": "Kobe Bryant",
        "steph": "Stephen Curry",
        "stephen curry": "Stephen Curry",
        "draymond green": "Draymond Green",
        "klay thompson": "Klay Thompson",
        "klay": "Klay Thompson",
    }

    def resolve_player_name(self, name):
        return self.ROSTER.get(name.lower())

    def resolve_player_reference(self, name):
        from app.services.nl_query.parser import PlayerReference

        return PlayerReference(name=self.resolve_player_name(name))


def test_low_confidence_queries_route_to_the_llm():
    llm = StubLLM(llm_parse(player_name="Kobe Bryant"))
    parser = RosterParser(components(confidence=0.2, should_use_llm=True))
    service = make_service(llm_service=llm, parser=parser)

    result = service.process_query("kobe games taking a lot of shots")

    assert result["parsed_by"] == "hybrid"
    assert result["player_name"] == "Kobe Bryant"
    assert result["confidence"] == 0.88
    assert len(llm.calls) == 1


def test_nlp_player_context_is_forwarded_to_the_llm():
    llm = StubLLM(llm_parse())
    parser = RosterParser(
        components(
            player_name="Stephen Curry",
            players_on=["Draymond Green"],
            players_off=["Klay Thompson"],
            should_use_llm=True,
        )
    )
    service = make_service(llm_service=llm, parser=parser)

    service.process_query("curry games taking a lot of shots")

    assert llm.calls[0]["context"] == {
        "player_name": "Stephen Curry",
        "players_on": ["Draymond Green"],
        "players_off": ["Klay Thompson"],
    }


def test_a_failed_llm_parse_falls_back_to_the_nlp_result():
    from app.services.llm_service import LLMError

    llm = StubLLM(LLMError("rate limited"))
    parser = RosterParser(components(player_name="LeBron James", confidence=0.3, should_use_llm=True))
    service = make_service(llm_service=llm, parser=parser)

    result = service.process_query("some ambiguous query")

    assert result["parsed_by"] == "nlp"
    assert result["player_name"] == "LeBron James"


def test_an_unexpected_llm_exception_falls_back_to_the_nlp_result():
    llm = StubLLM(RuntimeError("openai unreachable"))
    parser = RosterParser(components(player_name="LeBron James", confidence=0.3, should_use_llm=True))
    service = make_service(llm_service=llm, parser=parser)

    result = service.process_query("some ambiguous query")

    assert result["parsed_by"] == "nlp"


def test_low_confidence_without_an_llm_stays_on_nlp():
    parser = StubParser(components(player_name="LeBron James", confidence=0.1, should_use_llm=True))
    service = make_service(llm_service=None, parser=parser)

    result = service.process_query("some ambiguous query")

    assert result["parsed_by"] == "nlp"


# --- LLM player resolution -------------------------------------------------


def test_llm_player_names_are_resolved_to_the_roster():
    llm = StubLLM(llm_parse(player_name="steph", players_off=["klay", "Klay Thompson"]))
    parser = RosterParser(components(should_use_llm=True))
    service = make_service(llm_service=llm, parser=parser)

    result = service.process_query("steph without klay")

    assert result["player_name"] == "Stephen Curry"
    # Both spellings resolve to one player, listed once.
    assert result["players_off"] == ["Klay Thompson"]


def test_unresolved_llm_teammates_are_dropped():
    llm = StubLLM(llm_parse(player_name="Stephen Curry", players_on=["Draymond Green", "Made Up Player"]))
    parser = RosterParser(components(should_use_llm=True))
    service = make_service(llm_service=llm, parser=parser)

    result = service.process_query("steph with draymond and made up player")

    assert result["players_on"] == ["Draymond Green"]


def test_an_unresolved_llm_main_player_falls_back_to_the_nlp_name():
    llm = StubLLM(llm_parse(player_name="Stephen Curry III"))
    parser = RosterParser(components(player_name="Stephen Curry", should_use_llm=True))
    service = make_service(llm_service=llm, parser=parser)

    result = service.process_query("curry games taking a lot of shots")

    assert result["player_name"] == "Stephen Curry"


def test_no_llm_main_player_keeps_the_nlp_name():
    llm = StubLLM(llm_parse(player_name=None))
    parser = RosterParser(components(player_name="Stephen Curry", should_use_llm=True))
    service = make_service(llm_service=llm, parser=parser)

    assert service.process_query("curry vibes")["player_name"] == "Stephen Curry"


# --- LLM result shaping ----------------------------------------------------


def test_llm_opponent_filters_are_converted_for_the_frontend():
    llm = StubLLM(
        llm_parse(
            opponent_filters=[
                {"filter_type": "OPP_PTS", "rank": 10},
                {"filter_type": "OPP_REB", "rank": -5},
            ]
        )
    )
    service = make_service(llm_service=llm, parser=RosterParser(components(should_use_llm=True)))

    result = service.process_query("vs top 10 defenses and bottom 5 rebounding")

    assert result["teams_against"] == ["OPP_PTS", "OPP_REB"]
    assert result["rank_filter"] == ["10", "-5"]


def test_llm_self_filters_keep_the_nlp_shape():
    llm = StubLLM(
        llm_parse(
            self_filters=[
                {"stat_column": "PTS", "operator": "gte", "value": 25, "value2": None},
                {"stat_column": "FG3M", "operator": "between", "value": 2.0, "value2": 4.5},
            ]
        )
    )
    service = make_service(llm_service=llm, parser=RosterParser(components(should_use_llm=True)))

    result = service.process_query("25+ points and 2 to 4.5 threes")

    assert result["self_filters"] == [
        {"stat_column": "PTS", "operator": "gte", "value": 25, "value2": None},
        {"stat_column": "FG3M", "operator": "between", "value": 2, "value2": 4.5},
    ]


def test_llm_minutes_come_from_the_dedicated_field():
    llm = StubLLM(llm_parse(minutes_filter={"min": 20, "max": 35}))
    service = make_service(llm_service=llm, parser=RosterParser(components(should_use_llm=True)))

    assert service.process_query("20-35 minutes")["minutes_filter"] == [20, 35]


def test_llm_minutes_are_lifted_out_of_self_filters():
    llm = StubLLM(
        llm_parse(
            self_filters=[
                {"stat_column": "MIN", "operator": "between", "value": 20, "value2": 40},
                {"stat_column": "PTS", "operator": "gte", "value": 25, "value2": None},
            ]
        )
    )
    service = make_service(llm_service=llm, parser=RosterParser(components(should_use_llm=True)))

    result = service.process_query("20-40 minutes with 25+ points")

    assert result["minutes_filter"] == [20, 40]
    assert [f["stat_column"] for f in result["self_filters"]] == ["PTS"]


def test_llm_results_keep_the_response_contract():
    llm = StubLLM(llm_parse(player_name="Kobe Bryant", location="home"))
    service = make_service(llm_service=llm, parser=RosterParser(components(should_use_llm=True)))
    nlp_keys = set(make_service(parser=StubParser(components())).process_query("kobe").keys())

    result = service.process_query("kobe at home")

    assert set(result) == nlp_keys
    assert result["season"] is None
    assert result["location"] == "home"


def test_a_season_the_llm_reads_from_the_query_is_kept():
    llm = StubLLM(llm_parse(player_name="Kobe Bryant", season="2024-25"))
    service = make_service(llm_service=llm, parser=RosterParser(components(should_use_llm=True)))

    result = service.process_query("kobe in 2024-25")

    assert result["season"] == "2024-25"


# --- routing settings ------------------------------------------------------


def test_the_confidence_threshold_setting_controls_routing():
    from app.services.nl_query.parser import (
        MasterConfidenceCalculator,
        QueryCoverage,
    )

    query = "LeBron James last 10 games"
    parsed = QueryComponents(player_name="LeBron James", game_count=10)

    def routes_to_llm(threshold):
        calculator = MasterConfidenceCalculator(None, ["LeBron James"], {}, llm_threshold=threshold)
        breakdown = calculator.calculate_confidence(query, parsed, QueryCoverage(query))
        return breakdown.final_confidence, breakdown.should_use_llm

    confidence, _ = routes_to_llm(0.5)
    assert routes_to_llm(confidence - 0.01)[1] is False
    assert routes_to_llm(min(confidence + 0.01, 1.0))[1] is True


def test_the_parser_reads_the_threshold_from_settings(seeded_db_url, monkeypatch):
    from sqlalchemy import create_engine

    from app.config.settings import load_settings
    from app.services.nl_query.parser import BaseQueryParser

    monkeypatch.setenv("LLM_CONFIDENCE_THRESHOLD", "0.42")
    parser = BaseQueryParser(create_engine(seeded_db_url), settings=load_settings())

    assert parser.confidence_calculator.llm_threshold == 0.42


def test_the_default_threshold_matches_the_previous_hard_coded_value(monkeypatch):
    from app.config.settings import load_settings

    monkeypatch.delenv("LLM_CONFIDENCE_THRESHOLD", raising=False)

    assert load_settings().llm.confidence_threshold == 0.9


def test_the_parser_resolves_free_text_player_names(seeded_db_url):
    from sqlalchemy import create_engine

    from app.config.settings import load_settings
    from app.services.nl_query.parser import BaseQueryParser

    parser = BaseQueryParser(create_engine(seeded_db_url), settings=load_settings())

    assert parser.resolve_player_name("LeBron James") == "LeBron James"
    assert parser.resolve_player_name("lebron james") == "LeBron James"
    assert parser.resolve_player_name("Zzyzx Qwertyuiop") is None


# --- real initialization ---------------------------------------------------


def _real_service(seeded_db_url):
    from sqlalchemy import create_engine

    from app.config.settings import load_settings
    from app.services.nl_service import NLService

    return NLService(create_engine(seeded_db_url), settings=load_settings())


@pytest.mark.parametrize(("fallback", "constructed"), [("false", False), ("true", True)])
def test_the_llm_is_built_only_when_the_fallback_is_enabled(
    seeded_db_url, monkeypatch, fallback, constructed
):
    """A configured key alone must not turn on OpenAI calls."""
    from app.services import nl_service as nl_service_module

    built = []
    monkeypatch.setattr(nl_service_module, "LLMService", lambda **kwargs: built.append(kwargs) or "llm")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("ENABLE_LLM_FALLBACK", fallback)

    service = _real_service(seeded_db_url)

    assert bool(built) is constructed
    assert (service.llm_service == "llm") is constructed


def test_a_real_service_initializes_and_parses(seeded_db_url):
    """Exercises initialize_nl_system, which the stubbed tests bypass."""
    service = _real_service(seeded_db_url)

    assert service.nl_parser is not None

    result = service.process_query("LeBron last 10 games")

    assert result["player_name"] == "LeBron James"
    assert result["parsed_by"] in {"nlp", "llm", "hybrid"}


def test_a_failed_parser_build_leaves_the_service_unavailable(seeded_db_url, monkeypatch):
    """Initialization swallows failures, so process_query owns the guard."""
    from app.services import nl_service as nl_service_module

    def boom(*args, **kwargs):
        raise RuntimeError("spaCy model missing")

    monkeypatch.setattr(nl_service_module, "BaseQueryParser", boom)

    service = _real_service(seeded_db_url)

    assert service.nl_parser is None
    with pytest.raises(RuntimeError, match="not initialized"):
        service.process_query("LeBron last 10 games")


def test_initialization_does_not_build_the_game_or_player_services(seeded_db_url, monkeypatch):
    """Parsing must not depend on provider, Redis, or cache construction.

    The deleted QueryExecutor built GameService and PlayerService during
    NLService init, so an unrelated provider outage could disable natural
    language parsing entirely.
    """
    from app.services import game_service as game_service_module
    from app.services import player_service as player_service_module

    # Record rather than raise: initialize_nl_system swallows exceptions, so a
    # raising stub would be absorbed and the assertion would never be reached.
    constructed = []
    monkeypatch.setattr(game_service_module, "GameService", lambda *a, **k: constructed.append("game"))
    monkeypatch.setattr(
        player_service_module,
        "PlayerService",
        lambda *a, **k: constructed.append("player"),
    )

    service = _real_service(seeded_db_url)

    assert constructed == []
    assert service.process_query("LeBron last 10 games")["player_name"] == "LeBron James"


# --- end to end through the route -----------------------------------------


def test_nl_query_route_returns_structured_filters(make_client, seeded_db_url, authenticate):
    """Drive the real parser from HTTP request to structured response."""
    headers = authenticate()
    client = make_client(seeded_db_url)

    response = client.post("/api/nl-query", headers=headers, json={"query": "LeBron last 10 games at home"})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["player_name"] == "LeBron James"
    assert payload["game_count"] == 10
    assert payload["location"] == "home"
    assert payload["original_query"] == "LeBron last 10 games at home"
    assert payload["parsed_by"] in {"nlp", "llm", "hybrid"}


def test_nl_query_route_extracts_a_stat_threshold(make_client, seeded_db_url, authenticate):
    headers = authenticate()
    client = make_client(seeded_db_url)

    response = client.post("/api/nl-query", headers=headers, json={"query": "Curry games with 30+ points"})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["player_name"] == "Stephen Curry"
    assert payload["self_filters"], "expected a self filter for '30+ points'"


@pytest.mark.parametrize("body", [{}, {"query": ""}, {"query": "   "}, {"query": 5}])
def test_nl_query_route_rejects_malformed_bodies(make_client, seeded_db_url, authenticate, body):
    headers = authenticate()
    client = make_client(seeded_db_url)

    response = client.post("/api/nl-query", headers=headers, json=body)

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_input"


# --- end dates: "until"/"before" are date_to, "since"/"after" are date_filter --


@pytest.mark.parametrize(
    ("query", "date_filter", "date_to"),
    [
        ("LeBron games until March 1", None, "2026-03-01"),
        ("LeBron games until March 15", None, "2026-03-15"),
        ("LeBron games before March 15", None, "2026-03-14"),
        ("LeBron games before March 1", None, "2026-02-28"),
        ("LeBron games before March 1, 2026", None, "2026-02-28"),
        ("LeBron games until March 1, 2026", None, "2026-03-01"),
        ("LeBron games since March 1", "2026-03-01", None),
        ("LeBron games after March 1", "2026-03-01", None),
    ],
)
def test_nl_query_route_maps_start_and_end_dates(
    make_client, seeded_db_url, authenticate, query, date_filter, date_to
):
    headers = authenticate()
    client = make_client(seeded_db_url)
    # Pin the year the parser assumes for a month and day without one.
    client.application.extensions["dependencies"].nl_service.nl_parser.date_parser.current_year = 2026

    response = client.post("/api/nl-query", headers=headers, json={"query": query})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["date_filter"] == date_filter
    assert payload["date_to"] == date_to


def test_an_nlp_end_date_phrase_fills_only_date_to():
    parsed = components(player_name="LeBron James", date_to="2026-03-01")

    result = make_service(parser=StubParser(parsed)).process_query("LeBron until March 1")

    assert result["date_filter"] is None
    assert result["date_to"] == "2026-03-01"


def test_llm_start_dates_stay_on_date_filter_with_no_end_date():
    llm = StubLLM(llm_parse(date_range="2026-03-01"))
    parser = RosterParser(components(confidence=0.2, should_use_llm=True))

    result = make_service(llm_service=llm, parser=parser).process_query("q")

    assert result["date_filter"] == "2026-03-01"
    assert result["date_to"] is None


# --- one opponent team: "jokic against the blazers" -------------------------


def test_a_team_named_after_vs_or_against_is_the_opponent(seeded_db_url):
    service = _real_service(seeded_db_url)
    queries = {
        "jokic against the blazers": "POR",
        "Jokic vs the Portland Trail Blazers": "POR",
        "Jokic vs. Portland": "POR",
        "Jokic versus the trail blazers": "POR",
        "Tatum vs MIA": "MIA",
        "LeBron v the Sixers": "PHI",
        "Curry against the Cavs": "CLE",
        "Curry vs the Mavs": "DAL",
        "LeBron against the Wolves": "MIN",
        "Curry vs the Pels": "NOP",
        "LeBron vs LA": None,
        "LeBron against Los Angeles": None,
        "Jokic vs Gotham": None,
        "Jokic last 10 games": None,
    }

    assert {query: service.process_query(query)["opponent_tricode"] for query in queries} == queries


@pytest.mark.parametrize(
    "query",
    [
        "Curry versus the Lakers last 10 games",
        "Curry versus the Lakers last 10",
        "Curry versus the Lakers last 10?",
        "Curry last 10 against the Lakers",
        "Curry past 10 against the Lakers",
    ],
)
def test_an_opponent_team_keeps_the_rest_of_the_parse(seeded_db_url, query):
    service = _real_service(seeded_db_url)
    service.llm_service = llm = StubLLM(llm_parse())

    result = service.process_query(query)

    assert llm.calls == []
    assert result["parsed_by"] == "nlp"
    assert result["player_name"] == "Stephen Curry"
    assert result["opponent_tricode"] == "LAL"
    assert result["game_count"] == 10


@pytest.mark.parametrize(
    "query",
    ["Curry last 10 minutes", "Curry past 3 seasons", "Curry past 1.5 seasons", "Curry last 2.5 minutes"],
)
def test_a_bare_count_before_another_unit_is_not_a_game_count(seeded_db_url, query):
    assert _real_service(seeded_db_url).process_query(query)["game_count"] is None


@pytest.mark.parametrize("query", ["jokic against the blazers", "Jokic vs the Portland Trail Blazers"])
def test_a_named_opponent_team_needs_no_llm(seeded_db_url, query):
    service = _real_service(seeded_db_url)
    service.llm_service = llm = StubLLM(llm_parse())

    result = service.process_query(query)

    assert llm.calls == []
    assert result["parsed_by"] == "nlp"


def test_a_tricode_is_an_opponent_team_and_not_a_ranked_filter(seeded_db_url):
    from sqlalchemy import create_engine, text

    with create_engine(seeded_db_url).begin() as connection:
        connection.execute(text("CREATE TABLE team_info (full_name TEXT, abbreviation TEXT)"))
        connection.execute(text("INSERT INTO team_info VALUES ('Miami Heat', 'MIA')"))

    result = _real_service(seeded_db_url).process_query("LeBron vs MIA")

    assert result["opponent_tricode"] == "MIA"
    assert result["teams_against"] == []
    assert result["rank_filter"] == []


def test_a_ranked_opponent_group_is_not_an_opponent_team(seeded_db_url):
    result = _real_service(seeded_db_url).process_query("LeBron against top 10 defenses")

    assert result["opponent_tricode"] is None
    assert result["teams_against"] == ["OPP_PTS"]
    assert result["rank_filter"] == ["-10"]


def test_a_query_can_name_a_team_and_a_ranked_group(seeded_db_url):
    result = _real_service(seeded_db_url).process_query("Jokic vs the Blazers against top 10 defenses")

    assert result["opponent_tricode"] == "POR"
    assert result["teams_against"] == ["OPP_PTS"]
    assert result["rank_filter"] == ["-10"]


@pytest.mark.parametrize(
    ("opponent_team", "nlp_tricode", "expected"),
    [
        ("Trail Blazers", None, "POR"),
        ("the Portland Trail Blazers", None, "POR"),
        ("Gotham", None, None),
        ("Los Angeles", None, None),
        (None, "POR", "POR"),
        ("Miami Heat", "POR", "MIA"),
    ],
)
def test_the_llm_opponent_team_is_read_through_the_team_table(opponent_team, nlp_tricode, expected):
    llm = StubLLM(llm_parse(player_name="Kobe Bryant", opponent_team=opponent_team))
    parser = RosterParser(components(opponent_tricode=nlp_tricode, should_use_llm=True))

    result = make_service(llm_service=llm, parser=parser).process_query("kobe against them")

    assert result["parsed_by"] == "hybrid"
    assert result["opponent_tricode"] == expected


def test_nl_query_route_returns_the_opponent_tricode(make_client, seeded_db_url, authenticate):
    headers = authenticate()
    client = make_client(seeded_db_url)

    named = client.post("/api/nl-query", headers=headers, json={"query": "jokic against the blazers"})
    unnamed = client.post("/api/nl-query", headers=headers, json={"query": "LeBron last 10 games"})

    assert named.status_code == unnamed.status_code == 200
    assert named.get_json()["player_name"] == "Nikola Jokić"
    assert named.get_json()["opponent_tricode"] == "POR"
    assert "opponent_tricode" in unnamed.get_json()
    assert unnamed.get_json()["opponent_tricode"] is None
