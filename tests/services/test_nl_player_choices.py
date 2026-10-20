"""Ambiguous player names become choices; names match without accents.

A seeded roster stands in for the current-season ``athlete_catalog``.  The
parser is driven through ``BaseQueryParser.parse`` and the route through
``POST /api/nl-query``, asserting literal values.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from app.config.settings import load_settings
from app.services.nl_query.parser import BaseQueryParser, QueryComponents

ROSTER = [
    ("LaMelo Ball", "CHA"),
    ("Lonzo Ball", "CLE"),
    ("Stephen Curry", "GSW"),
    ("Seth Curry", "CHA"),
    ("Anthony Davis", "DAL"),
    ("Davis Bertans", "OKC"),
    ("Jalen Williams", "OKC"),
    ("Grant Williams", "CHA"),
    ("LeBron James", "LAL"),
    ("Bronny James", "LAL"),
    ("Alperen Şengün", "HOU"),
    ("Kristaps Porziņģis", "ATL"),
    ("Bogdan Bogdanović", "ATL"),
    ("Bojan Bogdanović", "NYK"),
    ("Nikola Jokić", "DEN"),
    ("De'Aaron Fox", "SAS"),
    ("Karl-Anthony Towns", "NYK"),
    ("Jaren Jackson Jr.", "MEM"),
    ("Jalen Brunson", "NYK"),
    ("Terrance Shannon Jr.", "MIN"),
    ("Michael Porter Jr.", "BKN"),
    ("Ausar Thompson", "DET"),
    ("Amen Thompson", "HOU"),
    ("Klay Thompson", "DAL"),
    ("Marcus Morris", "LAC"),
    ("Marcus Morris", "DET"),
]
TEAMS = [("Los Angeles Lakers", "LAL"), ("Charlotte Hornets", "CHA"), ("New York Knicks", "NYK")]


def seed(engine, roster=ROSTER):
    season = load_settings().nba.current_season
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE athlete_catalog (season TEXT, display_name TEXT, team_abbreviation TEXT)")
        )
        connection.execute(text("CREATE TABLE team_info (full_name TEXT, abbreviation TEXT)"))
        for name, team in roster:
            connection.execute(
                text("INSERT INTO athlete_catalog VALUES (:season, :name, :team)"),
                {"season": season, "name": name, "team": team},
            )
        for full_name, abbreviation in TEAMS:
            connection.execute(
                text("INSERT INTO team_info VALUES (:name, :abbreviation)"),
                {"name": full_name, "abbreviation": abbreviation},
            )


def build_parser(roster):
    engine = create_engine("sqlite://")
    seed(engine, roster)
    return BaseQueryParser(engine, load_settings())


@pytest.fixture(scope="module")
def parser():
    return build_parser(ROSTER)


def choice(role, text_, *candidates):
    return {
        "role": role,
        "text": text_,
        "candidates": [{"name": name, "team": team} for name, team in candidates],
    }


# --- ambiguity is a choice, never a pick -----------------------------------


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (
            "Ball last 10 games",
            choice("main", "Ball", ("LaMelo Ball", "CHA"), ("Lonzo Ball", "CLE")),
        ),
        ("Curry last 10", choice("main", "Curry", ("Seth Curry", "CHA"), ("Stephen Curry", "GSW"))),
        (
            "Davis last 10",
            choice("main", "Davis", ("Anthony Davis", "DAL"), ("Davis Bertans", "OKC")),
        ),
        (
            "Williams last 10",
            choice("main", "Williams", ("Grant Williams", "CHA"), ("Jalen Williams", "OKC")),
        ),
        (
            "James last 10",
            choice("main", "James", ("Bronny James", "LAL"), ("LeBron James", "LAL")),
        ),
        (
            "Bogdanovic last 10",
            choice("main", "Bogdanovic", ("Bogdan Bogdanović", "ATL"), ("Bojan Bogdanović", "NYK")),
        ),
        (
            "Thompson last 5",
            choice(
                "main",
                "Thompson",
                ("Amen Thompson", "HOU"),
                ("Ausar Thompson", "DET"),
                ("Klay Thompson", "DAL"),
            ),
        ),
    ],
)
def test_an_ambiguous_main_player_is_a_choice(parser, query, expected):
    parsed = parser.parse(query)

    assert parsed.player_name is None
    assert parsed.player_choices == [expected]


def test_players_with_an_identical_display_name_are_distinguished_by_team(parser):
    parsed = parser.parse("Marcus Morris last 5")

    assert parsed.player_name is None
    assert parsed.player_choices == [
        choice("main", "Marcus Morris", ("Marcus Morris", "DET"), ("Marcus Morris", "LAC"))
    ]


def test_an_ambiguous_with_player_is_a_choice_and_not_in_the_list(parser):
    parsed = parser.parse("Jalen Brunson with Davis last 10")

    assert parsed.player_name == "Jalen Brunson"
    assert parsed.players_on == []
    assert parsed.player_choices == [
        choice("with", "Davis", ("Anthony Davis", "DAL"), ("Davis Bertans", "OKC"))
    ]


def test_an_ambiguous_without_player_is_a_choice_and_not_in_the_list(parser):
    parsed = parser.parse("LeBron James without Curry last 10")

    assert parsed.player_name == "LeBron James"
    assert parsed.players_off == []
    assert parsed.player_choices == [
        choice("without", "Curry", ("Seth Curry", "CHA"), ("Stephen Curry", "GSW"))
    ]


def test_a_with_phrase_that_starts_the_query_no_longer_crashes(parser):
    parsed = parser.parse("with Davis")

    assert parsed.player_name is None
    assert parsed.players_on == []
    assert parsed.player_choices == [
        choice("with", "Davis", ("Anthony Davis", "DAL"), ("Davis Bertans", "OKC"))
    ]


def test_an_ambiguous_name_is_not_sent_to_the_llm(parser):
    parsed = parser.parse("Ball last 10 games")

    assert [c["text"] for c in parsed.player_choices] == ["Ball"]
    assert parsed.confidence_breakdown.should_use_llm is False


# --- direct resolution ------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Stephen Curry last 10", "Stephen Curry"),
        ("Anthony Davis last 10", "Anthony Davis"),
        ("steph last 10", "Stephen Curry"),
        ("Brunson last 10", "Jalen Brunson"),
        ("Fox last 10", "De'Aaron Fox"),
        ("Jokic last 10", "Nikola Jokić"),
    ],
)
def test_full_names_aliases_and_unique_last_names_resolve_directly(parser, query, expected):
    parsed = parser.parse(query)

    assert parsed.player_name == expected
    assert parsed.player_choices == []


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("sengun last 10", "Alperen Şengün"),
        ("porzingis last 10", "Kristaps Porziņģis"),
        ("jokic last 10", "Nikola Jokić"),
        ("deaaron fox last 10", "De'Aaron Fox"),
        ("karl anthony towns last 10", "Karl-Anthony Towns"),
        ("jaren jackson jr last 10", "Jaren Jackson Jr."),
        ("sengun", "Alperen Şengün"),
        ("deaaron fox", "De'Aaron Fox"),
    ],
)
def test_names_match_without_accents_or_punctuation(parser, query, expected):
    parsed = parser.parse(query)

    assert parsed.player_name == expected
    assert parsed.player_choices == []


def test_a_unique_first_name_resolves_as_a_with_player(parser):
    parsed = parser.parse("Lonzo Ball with LaMelo")

    assert parsed.player_name == "Lonzo Ball"
    assert parsed.players_on == ["LaMelo Ball"]
    assert parsed.player_choices == []


@pytest.mark.parametrize("query", ["ausar", "Ausar points last 5", "ausar last 5"])
def test_a_unique_first_name_resolves(parser, query):
    parsed = parser.parse(query)

    assert parsed.player_name == "Ausar Thompson"
    assert parsed.player_choices == []


# --- generational suffixes are not surnames ---------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "Michael Porter Jr",
        "michael porter jr last 10 games",
        "Michael Porter Jr points",
        "michael porter jr. at home",
        "Porter Jr. last 5",
        "Michael Porter last 5",
        "porter last 5",
        "mpj last 10",
    ],
)
def test_michael_porter_jr_resolves_however_the_suffix_is_written(parser, query):
    assert parser.parse(query).player_name == "Michael Porter Jr."


@pytest.mark.parametrize(
    "name",
    ["Porter", "Porter Jr", "Porter Jr.", "Michael Porter", "michael porter jr", "Michael Porter Jr."],
)
def test_resolve_player_name_ignores_the_suffix(parser, name):
    assert parser.resolve_player_name(name) == "Michael Porter Jr."


def test_a_surname_nobody_has_does_not_resolve_to_another_suffix_holder():
    sparse = build_parser(
        [
            ("Terrance Shannon Jr.", "MIN"),
            ("Ausar Thompson", "DET"),
            ("Amen Thompson", "HOU"),
        ]
    )

    assert sparse.resolve_player_name("Porter Jr.") is None
    assert sparse.resolve_player_name("Michael Porter Jr.") is None
    assert sparse.resolve_player_name("Terrance Shannon Jr.") == "Terrance Shannon Jr."
    assert sparse.resolve_player_name("Shannon") == "Terrance Shannon Jr."
    assert sparse.parse("Porter Jr. last 5").player_name is None


def test_resolve_player_reference_returns_the_candidates(parser):
    reference = parser.resolve_player_reference("Ball")

    assert reference.name is None
    assert reference.candidates == [
        {"name": "LaMelo Ball", "team": "CHA"},
        {"name": "Lonzo Ball", "team": "CLE"},
    ]
    assert parser.resolve_player_name("Ball") is None


# --- teams are whole words ---------------------------------------------------


@pytest.mark.parametrize(
    ("query", "team"),
    [
        ("LeBron James last 10", None),
        ("Brunson in a world of chances", None),
        ("LeBron James against the Los Angeles Lakers", "LAL"),
        ("LeBron James against LAL", "LAL"),
        ("LeBron James vs CHA", "CHA"),
        ("LeBron James against New York Knicks", "NYK"),
    ],
)
def test_a_team_is_detected_only_from_a_whole_word(parser, query, team):
    assert parser.parse(query).team_name == team


# --- response and LLM path ---------------------------------------------------


@pytest.fixture
def nl_database(tmp_path):
    path = tmp_path / "choices.db"
    engine = create_engine(f"sqlite:///{path}")
    seed(engine)
    engine.dispose()
    return f"sqlite:///{path}"


def post(make_client, authenticate, database, query):
    client = make_client(database)
    response = client.post("/api/nl-query", headers=authenticate(), json={"query": query})
    return response.status_code, response.get_json()


def test_the_route_returns_choices_for_an_ambiguous_player(make_client, authenticate, nl_database):
    status, payload = post(make_client, authenticate, nl_database, "Ball last 10 games")

    assert status == 200
    assert payload["player_name"] is None
    assert payload["players_on"] == []
    assert payload["game_count"] == 10
    assert payload["parsed_by"] == "nlp"
    assert payload["player_choices"] == [
        choice("main", "Ball", ("LaMelo Ball", "CHA"), ("Lonzo Ball", "CLE"))
    ]


def test_the_route_returns_an_empty_list_when_every_name_resolved(make_client, authenticate, nl_database):
    status, payload = post(make_client, authenticate, nl_database, "Lonzo Ball with LaMelo last 10 games")

    assert status == 200
    assert payload["player_name"] == "Lonzo Ball"
    assert payload["players_on"] == ["LaMelo Ball"]
    assert payload["player_choices"] == []


def test_the_route_answers_with_davis_instead_of_failing(make_client, authenticate, nl_database):
    status, payload = post(make_client, authenticate, nl_database, "with Davis")

    assert status == 200
    assert payload["player_name"] is None
    assert payload["players_on"] == []
    assert payload["player_choices"] == [
        choice("with", "Davis", ("Anthony Davis", "DAL"), ("Davis Bertans", "OKC"))
    ]


def test_the_route_reports_no_team_for_last_and_chances(make_client, authenticate, nl_database):
    _, last = post(make_client, authenticate, nl_database, "Jalen Brunson last 10 games")
    _, chances = post(make_client, authenticate, nl_database, "Brunson in a world of chances")

    assert last["team_name"] is None
    assert chances["team_name"] is None


def test_the_route_resolves_accentless_names(make_client, authenticate, nl_database):
    _, payload = post(make_client, authenticate, nl_database, "sengun last 10 games")

    assert payload["player_name"] == "Alperen Şengün"
    assert payload["player_choices"] == []


class LlmWithRealRoster:
    """Parses as unsure, resolves names against the seeded roster."""

    def __init__(self, parser, player_name=None, player_choices=()):
        self.parser = parser
        self.player_name = player_name
        self.player_choices = list(player_choices)

    def parse(self, query):
        parsed = QueryComponents(
            raw_query=query, player_name=self.player_name, player_choices=self.player_choices
        )
        parsed.confidence_breakdown = SimpleNamespace(should_use_llm=True)
        return parsed

    def resolve_player_reference(self, name):
        return self.parser.resolve_player_reference(name)


def llm_service_for(parser, nlp=None, **fields):
    from tests.services.test_nl_service import StubLLM, llm_parse, make_service

    service = make_service(
        llm_service=StubLLM(llm_parse(**fields)), parser=LlmWithRealRoster(parser, **(nlp or {}))
    )
    return service


def test_an_ambiguous_llm_main_player_becomes_a_choice(parser):
    service = llm_service_for(parser, player_name="Ball")

    result = service.process_query("that ball guy last 10")

    assert result["parsed_by"] == "hybrid"
    assert result["player_name"] is None
    assert result["player_choices"] == [choice("main", "Ball", ("LaMelo Ball", "CHA"), ("Lonzo Ball", "CLE"))]


def test_ambiguous_llm_teammates_become_choices_and_resolved_ones_stay(parser):
    service = llm_service_for(
        parser,
        player_name="Jalen Brunson",
        players_on=["Davis", "Klay Thompson"],
        players_off=["Curry"],
    )

    result = service.process_query("brunson with davis and klay without curry")

    assert result["player_name"] == "Jalen Brunson"
    assert result["players_on"] == ["Klay Thompson"]
    assert result["players_off"] == []
    assert result["player_choices"] == [
        choice("with", "Davis", ("Anthony Davis", "DAL"), ("Davis Bertans", "OKC")),
        choice("without", "Curry", ("Seth Curry", "CHA"), ("Stephen Curry", "GSW")),
    ]


def test_a_fully_resolved_llm_result_has_no_choices(parser):
    service = llm_service_for(parser, player_name="Lonzo Ball", players_on=["LaMelo"])

    result = service.process_query("lonzo with lamelo")

    assert result["player_name"] == "Lonzo Ball"
    assert result["players_on"] == ["LaMelo Ball"]
    assert result["player_choices"] == []


BALL = choice("main", "Ball", ("LaMelo Ball", "CHA"), ("Lonzo Ball", "CLE"))
DAVIS = choice("with", "Davis", ("Anthony Davis", "DAL"), ("Davis Bertans", "OKC"))


def test_the_llm_cannot_settle_a_main_player_the_user_was_asked_about(parser):
    service = llm_service_for(parser, nlp={"player_choices": [BALL]}, player_name="LaMelo Ball")

    result = service.process_query("Ball points last 5")

    assert result["player_name"] is None
    assert result["player_choices"] == [BALL]


def test_the_llm_cannot_settle_a_teammate_the_user_was_asked_about(parser):
    service = llm_service_for(
        parser,
        nlp={"player_name": "Jalen Brunson", "player_choices": [DAVIS]},
        player_name="Jalen Brunson",
        players_on=["Anthony Davis", "Klay Thompson"],
    )

    result = service.process_query("Brunson with Davis and Klay points")

    assert result["player_name"] == "Jalen Brunson"
    assert result["players_on"] == ["Klay Thompson"]
    assert result["player_choices"] == [DAVIS]


def test_every_nlp_choice_survives_the_llm_fallback(parser):
    service = llm_service_for(
        parser, nlp={"player_choices": [BALL, DAVIS]}, player_name="Lonzo Ball", players_on=["Davis Bertans"]
    )

    result = service.process_query("Ball with Davis points")

    assert result["player_name"] is None
    assert result["players_on"] == []
    assert result["player_choices"] == [BALL, DAVIS]


def test_an_nlp_main_choice_survives_an_llm_that_names_nobody(parser):
    service = llm_service_for(parser, nlp={"player_choices": [BALL]}, player_name=None)

    result = service.process_query("Ball points last 5")

    assert result["player_name"] is None
    assert result["player_choices"] == [BALL]


# --- aliases answer to the catalog -------------------------------------------


def test_an_alias_for_a_name_shared_by_two_teams_is_a_team_choice():
    twin = build_parser([("Klay Thompson", "DAL"), ("Klay Thompson", "LAL"), ("Ausar Thompson", "DET")])

    parsed = twin.parse("Klay Thompson last 5")

    assert parsed.player_name is None
    assert parsed.player_choices == [
        choice("main", "Klay Thompson", ("Klay Thompson", "DAL"), ("Klay Thompson", "LAL"))
    ]


def test_an_alias_for_a_player_the_catalog_lacks_resolves_to_nothing():
    sparse = build_parser([("Terrance Shannon Jr.", "MIN"), ("Ausar Thompson", "DET")])

    assert sparse.resolve_player_name("mpj") is None
    parsed = sparse.parse("mpj last 10")
    assert parsed.player_name is None
    assert parsed.player_choices == []


# --- any case, and every mention ---------------------------------------------


@pytest.mark.parametrize("written", ["BALL", "ball", "Ball"])
def test_an_ambiguous_teammate_is_found_in_any_case(parser, written):
    parsed = parser.parse(f"Jalen Brunson with {written} points last 5")

    assert parsed.player_name == "Jalen Brunson"
    assert parsed.player_choices == [choice("with", written, ("LaMelo Ball", "CHA"), ("Lonzo Ball", "CLE"))]


@pytest.mark.parametrize("query", ["AUSAR points last 5", "ausar points last 5", "Ausar points last 5"])
def test_a_first_name_is_found_in_any_case(parser, query):
    assert parser.parse(query).player_name == "Ausar Thompson"


def test_the_same_ambiguous_name_in_two_roles_is_two_choices(parser):
    parsed = parser.parse("Jalen Brunson with Ball without Ball last 10")

    assert parsed.player_name == "Jalen Brunson"
    assert parsed.player_choices == [
        choice("with", "Ball", ("LaMelo Ball", "CHA"), ("Lonzo Ball", "CLE")),
        choice("without", "Ball", ("LaMelo Ball", "CHA"), ("Lonzo Ball", "CLE")),
    ]
