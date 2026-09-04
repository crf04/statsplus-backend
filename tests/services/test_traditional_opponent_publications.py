"""The traditional-opponent publication family's explicit formats.

Every assertion here crosses the module's public interface: formats, format
recognition, and the normalized read model.  Nothing reaches through it to a
private helper, so the tests survive a restructuring of composition or
persistence.
"""

import pytest

from app.domain.nba_teams import NBA_TEAM_ID_TO_TRICODE
from app.services.database_first_activation import (
    PublicationPayloadError,
    decode_team_window,
)
from app.services.ledger_derivations import TEAM_METRICS
from app.services.traditional_opponent_publications import (
    REBOUND_SPLIT,
    REBOUND_SPLIT_METRICS,
    SUPPORTED_TRADITIONAL_OPPONENT_FORMATS,
    TRADITIONAL_OPPONENT_STREAM_KEYS,
    TRADITIONAL_OPPONENT_TARGET_FORMAT,
    TRADITIONAL_OPPONENT_V1,
    TRADITIONAL_OPPONENT_V2,
    TraditionalOpponentFormatError,
    normalize_traditional_opponent_window,
    recognize_traditional_opponent_format,
    traditional_opponent_format_by_name,
    traditional_opponent_window_kind,
)

SEASON_STREAM = "traditional_opponent_season"
L15_STREAM = "traditional_opponent_l15"

#: One team's counts.  The rebound split is exact: 44 = 11 + 33.
_COUNTS = {
    "points": 110.0,
    "rebounds": 44.0,
    "assists": 25.0,
    "field_goals_made": 40.0,
    "field_goals_attempted": 88.0,
    "three_pointers_made": 12.0,
    "three_pointers_attempted": 33.0,
    "free_throws_made": 18.0,
    "free_throws_attempted": 22.0,
    "turnovers": 14.0,
    "steals": 7.0,
    "blocks": 5.0,
    "personal_fouls": 19.0,
    "offensive_rebounds": 11.0,
    "defensive_rebounds": 33.0,
}


def _metrics(publication_format):
    return tuple(publication_format.metrics)


def _block(publication_format, *, scale=1.0):
    return {metric: _COUNTS[metric] * scale for metric in publication_format.metrics}


def _row(team_id, tricode, publication_format, *, scale=1.0, rank=1):
    counts = _block(publication_format)
    return {
        "team_id": team_id,
        "team_tricode": tricode,
        "game_ids": ["0022500001"],
        "game_count": 1,
        "counts": counts,
        "team_minutes": 48.0,
        "per48": _block(publication_format, scale=scale),
        "league_average": _block(publication_format),
        "population_sigma": {metric: 1.0 for metric in publication_format.metrics},
        "competition_rank": {metric: rank for metric in publication_format.metrics},
    }


def _payload(publication_format, *, mutate=None):
    """The canonical thirty rows in one exact publication format."""

    rows = [
        _row(team_id, tricode, publication_format)
        for team_id, tricode in sorted(NBA_TEAM_ID_TO_TRICODE.items())
    ]
    if mutate is not None:
        mutate(rows)
    return rows


# --- Format identities -----------------------------------------------------


def test_the_two_supported_formats_are_exactly_v1_and_v2():
    assert SUPPORTED_TRADITIONAL_OPPONENT_FORMATS == (
        TRADITIONAL_OPPONENT_V1,
        TRADITIONAL_OPPONENT_V2,
    )
    assert TRADITIONAL_OPPONENT_TARGET_FORMAT is TRADITIONAL_OPPONENT_V2


def test_v1_is_the_current_metric_set_and_v2_adds_only_the_rebound_split():
    assert TRADITIONAL_OPPONENT_V1.metrics == tuple(TEAM_METRICS)
    assert TRADITIONAL_OPPONENT_V1.capabilities == frozenset()
    assert set(TRADITIONAL_OPPONENT_V2.metrics) - set(TEAM_METRICS) == set(
        REBOUND_SPLIT_METRICS
    )
    assert TRADITIONAL_OPPONENT_V2.capabilities == frozenset({REBOUND_SPLIT})
    assert REBOUND_SPLIT_METRICS == ("offensive_rebounds", "defensive_rebounds")


def test_each_format_has_a_stable_name_and_a_deterministic_distinct_fingerprint():
    assert TRADITIONAL_OPPONENT_V1.name == "traditional_opponent.v1"
    assert TRADITIONAL_OPPONENT_V2.name == "traditional_opponent.v2"
    assert traditional_opponent_format_by_name(
        "traditional_opponent.v2"
    ) is TRADITIONAL_OPPONENT_V2
    # Deterministic across calls, and never equal between formats.
    assert (
        TRADITIONAL_OPPONENT_V1.fingerprint == TRADITIONAL_OPPONENT_V1.fingerprint
    )
    assert TRADITIONAL_OPPONENT_V1.fingerprint != TRADITIONAL_OPPONENT_V2.fingerprint
    assert len(TRADITIONAL_OPPONENT_V2.fingerprint) == 64


def test_an_unknown_format_name_is_refused():
    with pytest.raises(TraditionalOpponentFormatError):
        traditional_opponent_format_by_name("traditional_opponent.v3")


def test_the_family_owns_exactly_the_two_window_streams():
    assert TRADITIONAL_OPPONENT_STREAM_KEYS == (SEASON_STREAM, L15_STREAM)
    assert traditional_opponent_window_kind(SEASON_STREAM) == "season"
    assert traditional_opponent_window_kind(L15_STREAM) == "l15"
    with pytest.raises(TraditionalOpponentFormatError):
        traditional_opponent_window_kind("assist_locations_season")


# --- Format recognition ----------------------------------------------------


def test_recognition_accepts_the_two_exact_taxonomies():
    assert recognize_traditional_opponent_format(
        _metrics(TRADITIONAL_OPPONENT_V1)
    ) is TRADITIONAL_OPPONENT_V1
    assert recognize_traditional_opponent_format(
        _metrics(TRADITIONAL_OPPONENT_V2)
    ) is TRADITIONAL_OPPONENT_V2
    # Order is not part of the taxonomy; the exact key set is.
    assert recognize_traditional_opponent_format(
        reversed(_metrics(TRADITIONAL_OPPONENT_V2))
    ) is TRADITIONAL_OPPONENT_V2


@pytest.mark.parametrize(
    "metrics",
    [
        pytest.param((), id="empty"),
        pytest.param(TEAM_METRICS[:-1], id="subset"),
        pytest.param(TEAM_METRICS + ("pace",), id="superset"),
        pytest.param(
            TEAM_METRICS + ("offensive_rebounds",), id="half-the-rebound-split"
        ),
        pytest.param(
            TEAM_METRICS + REBOUND_SPLIT_METRICS + ("pace",), id="beyond-v2"
        ),
    ],
)
def test_recognition_refuses_arbitrary_subsets_and_supersets(metrics):
    with pytest.raises(TraditionalOpponentFormatError):
        recognize_traditional_opponent_format(metrics)


# --- Normalized reads ------------------------------------------------------


def _normalize(publication_format, *, stream_key=SEASON_STREAM, mutate=None):
    return normalize_traditional_opponent_window(
        decode_team_window(_payload(publication_format, mutate=mutate),
                           stream_key=stream_key),
        stream_key=stream_key,
    )


def test_an_exact_v1_publication_serves_its_metrics_and_no_rebound_split():
    window = _normalize(TRADITIONAL_OPPONENT_V1)

    assert window.format is TRADITIONAL_OPPONENT_V1
    assert window.capabilities == frozenset()
    assert not window.supports(REBOUND_SPLIT)
    assert window.metrics == tuple(TEAM_METRICS)
    assert len(window.teams) == 30
    # Every existing metric still serves.
    for metric in TEAM_METRICS:
        assert window.metric_values(metric).keys() == set(NBA_TEAM_ID_TO_TRICODE)
    # The split is absent, never fabricated as zero or null.
    for metric in REBOUND_SPLIT_METRICS:
        assert window.metric_values(metric) is None
        assert all(metric not in team.per48 for team in window.teams)


def test_an_exact_v2_publication_serves_the_rebound_split():
    window = _normalize(TRADITIONAL_OPPONENT_V2)

    assert window.format is TRADITIONAL_OPPONENT_V2
    assert window.supports(REBOUND_SPLIT)
    assert window.capabilities == frozenset({REBOUND_SPLIT})
    for metric in REBOUND_SPLIT_METRICS:
        assert window.metric_values(metric).keys() == set(NBA_TEAM_ID_TO_TRICODE)
    team = window.teams[0]
    assert team.per48["offensive_rebounds"] == 11.0
    assert team.per48["defensive_rebounds"] == 33.0
    assert team.per48["rebounds"] == 44.0


def test_both_windows_normalize_through_the_same_interface():
    for stream_key in TRADITIONAL_OPPONENT_STREAM_KEYS:
        window = _normalize(TRADITIONAL_OPPONENT_V2, stream_key=stream_key)
        assert window.stream_key == stream_key
        assert window.window == traditional_opponent_window_kind(stream_key)


def test_an_incomplete_league_fails_closed():
    with pytest.raises(TraditionalOpponentFormatError):
        _normalize(TRADITIONAL_OPPONENT_V2, mutate=lambda rows: rows.pop())


def test_a_mixed_format_publication_fails_closed():
    def downgrade_one(rows):
        rows[0] = _row(
            rows[0]["team_id"], rows[0]["team_tricode"], TRADITIONAL_OPPONENT_V1
        )

    with pytest.raises(PublicationPayloadError):
        _normalize(TRADITIONAL_OPPONENT_V2, mutate=downgrade_one)


def test_a_non_family_stream_cannot_be_normalized_as_traditional_opponent():
    rows = decode_team_window(
        _payload(TRADITIONAL_OPPONENT_V1), stream_key=SEASON_STREAM
    )
    with pytest.raises(TraditionalOpponentFormatError):
        normalize_traditional_opponent_window(
            rows, stream_key="assist_locations_season"
        )


# --- Whole-publication rejection at the decode boundary --------------------


@pytest.mark.parametrize("stream_key", TRADITIONAL_OPPONENT_STREAM_KEYS)
def test_the_decoder_accepts_exactly_the_two_supported_formats(stream_key):
    for publication_format in SUPPORTED_TRADITIONAL_OPPONENT_FORMATS:
        rows = decode_team_window(
            _payload(publication_format), stream_key=stream_key
        )
        assert len(rows) == 30


def _corrupt_taxonomy(block):
    def mutate(rows):
        rows[0] = {**rows[0], block: {**rows[0][block], "pace": 1.0}}

    return mutate


@pytest.mark.parametrize(
    "block",
    ["counts", "per48", "league_average", "population_sigma", "competition_rank"],
)
def test_a_wrong_taxonomy_in_any_metric_block_rejects_the_publication(block):
    with pytest.raises(PublicationPayloadError):
        decode_team_window(
            _payload(TRADITIONAL_OPPONENT_V2, mutate=_corrupt_taxonomy(block)),
            stream_key=SEASON_STREAM,
        )


def test_a_v2_publication_whose_counts_break_the_rebound_identity_is_rejected():
    def mutate(rows):
        counts = {**rows[3]["counts"], "offensive_rebounds": 12.0}
        rows[3] = {**rows[3], "counts": counts}

    with pytest.raises(PublicationPayloadError):
        decode_team_window(
            _payload(TRADITIONAL_OPPONENT_V2, mutate=mutate),
            stream_key=SEASON_STREAM,
        )


def test_a_v2_publication_whose_per48_breaks_the_rebound_identity_is_rejected():
    def mutate(rows):
        per48 = {**rows[7]["per48"], "defensive_rebounds": 30.0}
        rows[7] = {**rows[7], "per48": per48}

    with pytest.raises(PublicationPayloadError):
        decode_team_window(
            _payload(TRADITIONAL_OPPONENT_V2, mutate=mutate),
            stream_key=SEASON_STREAM,
        )


def test_a_v1_publication_carries_no_rebound_identity_obligation():
    """v1 never published a split, so it can neither satisfy nor violate it."""

    window = _normalize(TRADITIONAL_OPPONENT_V1)

    assert window.format.invariants == ()
    assert window.teams[0].per48["rebounds"] == 44.0
