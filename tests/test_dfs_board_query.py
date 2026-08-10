"""Typed parsing of the DFS Board query string."""

from __future__ import annotations

import pytest
from werkzeug.datastructures import MultiDict

from app.config.settings import RuntimeSettings
from app.errors import InvalidInputError
from app.providers.dfs import MarketStatus
from app.services.dfs_board_query import MAX_FILTER_VALUES, parse_board_request


@pytest.fixture
def settings() -> RuntimeSettings:
    return RuntimeSettings(environment="testing")


def parse(settings, **params):
    args = MultiDict()
    for name, values in params.items():
        for value in values if isinstance(values, list) else [values]:
            args.add(name, value)
    return parse_board_request(args, settings=settings)


def test_an_unfiltered_read_uses_the_configured_season(settings):
    request = parse(settings)

    assert request.query.season == settings.nba.current_season
    assert request.filters.is_empty


def test_every_supported_filter_is_parsed_into_typed_identities(settings):
    request = parse(
        settings,
        season="2024-25",
        providers="prizepicks,underdog",
        canonical_athlete_ids=["203999", "2544"],
        canonical_event_ids="0022400001",
        canonical_statistic_ids="points",
        market_statuses=["available", "suspended"],
    )

    assert request.query.season == "2024-25"
    assert request.filters.providers == ("prizepicks", "underdog")
    assert request.filters.canonical_athlete_ids == (2544, 203999)
    assert request.filters.canonical_event_ids == ("0022400001",)
    assert request.filters.canonical_statistic_ids == ("points",)
    assert request.filters.market_statuses == (
        MarketStatus.AVAILABLE,
        MarketStatus.SUSPENDED,
    )


def test_an_unknown_parameter_is_refused_rather_than_ignored(settings):
    with pytest.raises(InvalidInputError) as error:
        parse(settings, player_name="LeBron James")

    assert "player_name" in str(error.value)


def test_no_fuzzy_name_filter_exists(settings):
    for parameter in ("athlete_name", "player_name", "statistic_label", "search"):
        with pytest.raises(InvalidInputError):
            parse(settings, **{parameter: "jokic"})


def test_an_unsupported_provider_is_refused(settings):
    with pytest.raises(InvalidInputError) as error:
        parse(settings, providers="not-a-provider")

    assert "providers" in str(error.value)


def test_an_unsupported_market_status_is_refused(settings):
    with pytest.raises(InvalidInputError) as error:
        parse(settings, market_statuses="settled")

    assert "market_statuses" in str(error.value)


def test_a_non_integer_athlete_id_is_refused(settings):
    with pytest.raises(InvalidInputError) as error:
        parse(settings, canonical_athlete_ids="jokic")

    assert "canonical_athlete_ids" in str(error.value)


def test_a_non_canonical_season_is_refused(settings):
    with pytest.raises(InvalidInputError) as error:
        parse(settings, season="2024-99")

    assert "season" in str(error.value)


def test_a_repeated_season_is_refused(settings):
    with pytest.raises(InvalidInputError):
        parse(settings, season=["2024-25", "2023-24"])


@pytest.mark.parametrize(
    "parameter",
    [
        "providers",
        "market_statuses",
        "canonical_athlete_ids",
        "canonical_event_ids",
        "canonical_statistic_ids",
    ],
)
@pytest.mark.parametrize("value", ["", "   ", ",", ",,"])
def test_a_supplied_but_empty_filter_is_refused(settings, parameter, value):
    """An empty filter narrows nothing, so it is a 400 rather than a default.

    Silently reading ``providers=`` as "every provider" would answer a request
    the caller did not make with a whole unfiltered board.
    """

    with pytest.raises(InvalidInputError) as error:
        parse(settings, **{parameter: value})

    assert parameter in str(error.value)


@pytest.mark.parametrize(
    "value", ["dabble,", ",dabble", "dabble,,prizepicks", "dabble, ,prizepicks"]
)
def test_an_empty_csv_member_is_refused(settings, value):
    with pytest.raises(InvalidInputError) as error:
        parse(settings, providers=value)

    assert "providers" in str(error.value)


def test_an_empty_repeated_value_is_refused_beside_a_real_one(settings):
    with pytest.raises(InvalidInputError):
        parse(settings, providers=["dabble", ""])


@pytest.mark.parametrize("value", ["", "   "])
def test_a_supplied_but_empty_season_is_refused(settings, value):
    with pytest.raises(InvalidInputError) as error:
        parse(settings, season=value)

    assert "season" in str(error.value)


def test_a_repeated_identity_is_accepted_once(settings):
    """Duplicates are a redundant way to name one identity, not an error."""

    request = parse(
        settings,
        providers=["dabble,dabble", "dabble"],
        canonical_athlete_ids="203999,203999",
    )

    assert request.filters.providers == ("dabble",)
    assert request.filters.canonical_athlete_ids == (203999,)


def test_surrounding_whitespace_is_trimmed_from_a_real_value(settings):
    request = parse(settings, providers=" dabble , prizepicks ", season=" 2024-25 ")

    assert request.filters.providers == ("dabble", "prizepicks")
    assert request.query.season == "2024-25"


def test_an_unbounded_identity_list_is_refused(settings):
    values = [str(index) for index in range(MAX_FILTER_VALUES + 1)]

    with pytest.raises(InvalidInputError) as error:
        parse(settings, canonical_athlete_ids=values)

    assert "canonical_athlete_ids" in str(error.value)
