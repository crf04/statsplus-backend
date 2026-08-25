"""PBP game-log provider and canonical ingestion normalization."""

from __future__ import annotations

import pandas as pd
import pytest

from app.errors import ProviderUnavailableError
from app.providers.pbp_game_logs import PBPGameLogAdapter, PBP_GAME_LOG_COLUMNS
from app.services.game_log_frame import derive_game_log_frame
from app.services.pbp_game_log_normalization import (
    normalize_pbp_game_logs,
    parse_pbp_minutes,
)
from app.utils import telemetry


def _events():
    return [
        {
            "nba_game_id": "0022500001",
            "season": "2025-26",
            "home_team_id": 1,
            "home_team_name": "AAA",
            "home_team_tricode": "AAA",
            "away_team_id": 2,
            "away_team_name": "BBB",
            "away_team_tricode": "BBB",
            "classification": "Regular Season",
            "status_code": 3,
            "status_text": "Final",
            "postponed_status": None,
            "scheduled_at": "2026-01-02T00:00:00+00:00",
        },
        {
            "nba_game_id": "0022500002",
            "season": "2025-26",
            "home_team_id": 3,
            "home_team_name": "CCC",
            "home_team_tricode": "CCC",
            "away_team_id": 4,
            "away_team_name": "DDD",
            "away_team_tricode": "DDD",
            "classification": "Regular Season",
            "status_code": 3,
            "status_text": "Final",
            "postponed_status": None,
            "scheduled_at": "2026-01-05T00:00:00+00:00",
        },
    ]


def _pbp_row(**overrides):
    # The live /get-game-logs rows carry no EntityId/Name/TeamId; the canonical
    # normalization attaches EntityId/Name from the request/envelope and
    # derives team identity from the Team tricode against the Event Catalog.
    row = {
        "EntityId": 101,
        "Name": "Player One",
        "GameId": "0022500001",
        "Date": "2026-01-02",
        "Team": "AAA",
        "Opponent": "BBB",
        "Minutes": "31:30",
        "FG2M": 6,
        "FG2A": 11,
        "FG3M": 3,
        "FG3A": 7,
        "FGM": 9,
        "FGA": 18,
        "FtPoints": 3,
        "FTA": 4,
        "OffRebounds": 2,
        "DefRebounds": 6,
        "Rebounds": 8,
        "Assists": 6,
        "Turnovers": 3,
        "Steals": 2,
        "Blocks": 1,
        "Fouls": 2,
        "Points": 24,
    }
    row.update(overrides)
    return row


def _pbp_frame(*rows):
    if not rows:
        return pd.DataFrame(columns=PBP_GAME_LOG_COLUMNS)
    return pd.DataFrame(
        [
            {column: row.get(column) for column in PBP_GAME_LOG_COLUMNS}
            for row in rows
        ]
    )


# ------------------------------------------------------------------ minutes


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("31:30", 31.5),
        ("48:10", pytest.approx(48.1667, abs=1e-4)),
        ("0:00", 0.0),
        ("53:00", 53.0),
    ],
)
def test_parse_pbp_minutes_decodes_mm_ss(value, expected):
    assert parse_pbp_minutes(value) == expected


@pytest.mark.parametrize("value", ["", "31", "31:60", "31:xx", "abc", "31:5", "-1:00", None])
def test_parse_pbp_minutes_rejects_invalid_evidence(value):
    with pytest.raises(ProviderUnavailableError):
        parse_pbp_minutes(value)


# ----------------------------------------------------------------- adapter


def test_adapter_parse_seams_validate_the_sparse_wire_contract():
    frame = PBPGameLogAdapter.parse_game_logs(
        {"multi_row_table_data": [_pbp_row(), _pbp_row(GameId="0022500002")]},
        entity_id="101",
        player_name="Player One",
    )
    assert list(frame.columns) == list(PBP_GAME_LOG_COLUMNS)
    assert len(frame) == 2
    assert frame.loc[0, "Minutes"] == "31:30"
    assert frame.loc[0, "EntityId"] == "101"
    assert frame.loc[0, "Name"] == "Player One"

    stats = PBPGameLogAdapter.parse_game_stats(
        {
            "stats": {
                "Home": {
                    "FullGame": [
                        {"EntityId": "0", "Name": "Team", "Minutes": "00:00"},
                        _pbp_row(),
                    ]
                },
                "Away": {"FullGame": []},
            },
            "home_team_abbreviation": "AAA",
            "away_team_abbreviation": "BBB",
            "date": "2026-01-02",
        },
        game_id="0022500001",
    )
    assert len(stats) == 1  # team-summary row excluded
    assert stats.loc[0, "EntityId"] == 101
    assert stats.loc[0, "GameId"] == "0022500001"
    assert stats.loc[0, "Date"] == "2026-01-02"
    assert stats.loc[0, "Team"] == "AAA"
    assert stats.loc[0, "Opponent"] == "BBB"


@pytest.mark.parametrize(
    "payload",
    [
        {"multi_row_table_data": "nope"},
        {"multi_row_table_data": [{"GameId": "1"}]},
        {"multi_row_table_data": [None]},
        None,
        {"multi_row_table_data": [_pbp_row(), "string"]},
    ],
)
def test_adapter_parse_seams_reject_malformed_payloads(payload):
    with pytest.raises(telemetry.ProviderResponseError):
        PBPGameLogAdapter.parse_game_logs(
            payload, entity_id="101", player_name="Player One"
        )
    with pytest.raises(telemetry.ProviderResponseError):
        PBPGameLogAdapter.parse_game_stats(payload, game_id="0022500001")


def test_adapter_empty_payload_carries_the_declared_schema():
    frame = PBPGameLogAdapter.parse_game_logs(
        {"multi_row_table_data": []},
        entity_id="101",
        player_name="Player One",
    )
    assert list(frame.columns) == list(PBP_GAME_LOG_COLUMNS)
    assert frame.empty


def test_adapter_fetch_game_player_logs_uses_game_params():
    from app.config.settings import ProviderSettings, RuntimeSettings

    class FakeResponse:
        status_code = 200
        payload = {
            "stats": {
                "Home": {
                    "FullGame": [
                        {"EntityId": "0", "Name": "Team", "Minutes": "00:00"},
                        _pbp_row(),
                    ]
                },
                "Away": {"FullGame": []},
            },
            "home_team_abbreviation": "AAA",
            "away_team_abbreviation": "BBB",
            "date": "2026-01-02",
        }

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class FakeSession:
        def __init__(self):
            self.calls = []

        def get(self, url, params, timeout):
            self.calls.append((url, params, timeout))
            return FakeResponse()

    session = FakeSession()
    adapter = PBPGameLogAdapter(
        RuntimeSettings(
            environment="testing",
            providers=ProviderSettings(
                pbp_connect_timeout_seconds=1.0,
                pbp_read_timeout_seconds=2.0,
            ),
        ),
        session=session,
    )
    frame = adapter.fetch_game_player_logs("0022500001", "2025-26")

    url, params, timeout = session.calls[0]
    assert url == adapter.game_stats_url
    assert params == {
        "GameId": "0022500001",
        "Type": "Player",
    }
    assert len(frame) == 1  # team-summary row excluded
    assert frame.loc[0, "GameId"] == "0022500001"
    event = telemetry.get_recorded_provider_events()[-1]
    assert event["operation"] == "game_player_stats"


def test_adapter_fetch_game_stats_returns_complete_raw_evidence():
    from app.config.settings import ProviderSettings, RuntimeSettings

    class FakeResponse:
        status_code = 200
        payload = {
            "stats": {
                "Home": {
                    "FullGame": [
                        {"EntityId": "0", "Name": "Team", "Minutes": "00:00"},
                        _pbp_row(),
                    ]
                },
                "Away": {"FullGame": []},
            },
            "home_team_abbreviation": "AAA",
            "away_team_abbreviation": "BBB",
            "date": "2026-01-02",
            "ProviderAddedField": "future-proof",
        }

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class FakeSession:
        def __init__(self):
            self.calls = []

        def get(self, url, params, timeout):
            self.calls.append((url, params, timeout))
            return FakeResponse()

    session = FakeSession()
    adapter = PBPGameLogAdapter(
        RuntimeSettings(
            environment="testing",
            providers=ProviderSettings(
                pbp_connect_timeout_seconds=1.0,
                pbp_read_timeout_seconds=2.0,
            ),
        ),
        session=session,
    )
    payload = adapter.fetch_game_stats("0022500001", "2025-26")

    url, params, timeout = session.calls[0]
    assert url == adapter.game_stats_url
    assert params == {
        "GameId": "0022500001",
        "Type": "Player",
    }
    assert payload["stats"]["Home"]["FullGame"][0]["EntityId"] == "0"
    assert payload["ProviderAddedField"] == "future-proof"
    event = telemetry.get_recorded_provider_events()[-1]
    assert event["operation"] == "game_player_stats"


# ------------------------------------------------------------- normalization


def test_normalize_pbp_game_logs_builds_the_canonical_frame():
    frame, counts = normalize_pbp_game_logs(_pbp_frame(_pbp_row()), _events())

    assert counts == telemetry_pbp_counts(source_row_count=1)
    assert len(frame) == 1
    row = frame.iloc[0]
    assert row["PLAYER_ID"] == 101
    assert row["PLAYER_NAME"] == "Player One"
    assert row["GAME_ID"] == "0022500001"
    assert row["GAME_DATE"] == "2026-01-02"
    assert row["MATCHUP"] == "AAA vs. BBB"
    assert row["TEAM_ID"] == 1
    assert row["TEAM_ABBREVIATION"] == "AAA"
    assert row["MIN"] == 32  # 31:30 rounds to a whole minute
    assert row["PTS"] == 24
    assert row["REB"] == 8
    assert row["AST"] == 6
    assert row["FGM"] == 9
    assert row["FGA"] == 18
    assert row["FG_PCT"] == pytest.approx(0.5)
    assert row["FG3M"] == 3
    assert row["FG3A"] == 7
    assert row["FTM"] == 3  # free-throw points == made free throws
    assert row["FTA"] == 4
    assert row["FT_PCT"] == pytest.approx(0.75)
    assert row["OREB"] == 2
    assert row["DREB"] == 6
    assert row["TOV"] == 3
    assert row["STL"] == 2
    assert row["BLK"] == 1
    assert row["PF"] == 2
    assert row["NBA_FANTASY_PTS"] == pytest.approx(48.6)
    assert row["FD_PTS"] == pytest.approx(48.6)
    assert row["FG2M"] == 6
    assert row["FG2A"] == 11
    assert row["PRA"] == 38
    assert row["STKS"] == 3


def test_normalize_pbp_game_logs_reconstructs_away_matchup():
    row = _pbp_row(Team="BBB", Date="2026-01-02", Minutes="22:00")
    frame, _ = normalize_pbp_game_logs(_pbp_frame(row), _events())

    assert frame.iloc[0]["MATCHUP"] == "BBB @ AAA"
    assert frame.iloc[0]["TEAM_ID"] == 2
    assert frame.iloc[0]["TEAM_ABBREVIATION"] == "BBB"
    assert frame.iloc[0]["MIN"] == 22


def test_normalize_pbp_game_logs_zero_fills_omitted_counting_fields():
    row = _pbp_row(Minutes="12:00")
    for field in (
        "FG2M",
        "FG2A",
        "FG3M",
        "FG3A",
        "FGM",
        "FGA",
        "FtPoints",
        "FTA",
        "OffRebounds",
        "DefRebounds",
        "Assists",
        "Turnovers",
        "Steals",
        "Blocks",
        "Fouls",
        "Points",
    ):
        del row[field]

    frame, _ = normalize_pbp_game_logs(_pbp_frame(row), _events())
    row_out = frame.iloc[0]
    assert row_out["PTS"] == 0
    assert row_out["REB"] == 0
    assert row_out["AST"] == 0
    assert row_out["FGM"] == 0
    assert row_out["FG_PCT"] == 0.0
    assert row_out["FT_PCT"] == 0.0
    assert row_out["TOV"] == 0
    assert row_out["NBA_FANTASY_PTS"] == 0.0


def test_normalize_pbp_game_logs_zero_fills_dataframe_nan_counting_values():
    observed = _pbp_row(EntityId=202, Name="Player Two")
    omitted = _pbp_row(EntityId=203, Name="Player Three")
    del omitted["FtPoints"]

    frame, _ = normalize_pbp_game_logs(
        _pbp_frame(observed, omitted),
        _events(),
    )

    assert frame.loc[frame["PLAYER_ID"] == 203, "FTM"].item() == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"GameId": "0022999999"},
        {"Team": "ZZZ"},
        {"GameId": "0042500002"},
    ],
)
def test_normalize_pbp_game_logs_fails_on_any_unjoined_or_contradictory_row(
    overrides,
):
    rows = [
        _pbp_row(),
        _pbp_row(EntityId=202, Minutes="10:00", **overrides),
    ]
    with pytest.raises(ProviderUnavailableError):
        normalize_pbp_game_logs(_pbp_frame(*rows), _events())


def test_normalize_pbp_game_logs_keeps_exact_minutes_without_rounding():
    frame, _ = normalize_pbp_game_logs(
        _pbp_frame(_pbp_row()),
        _events(),
        round_minutes=False,
    )

    assert frame.iloc[0]["MIN"] == pytest.approx(31.5)


@pytest.mark.parametrize(
    "overrides",
    [
        {"Minutes": "not-a-time"},
        {"Minutes": None},
        {"EntityId": 0},
        {"Name": None},
        {"FG3M": 99},
        {"FtPoints": 99},
        {"Points": 12.5},
    ],
)
def test_normalize_pbp_game_logs_rejects_malformed_rows(overrides):
    with pytest.raises(ProviderUnavailableError):
        normalize_pbp_game_logs(_pbp_frame(_pbp_row(**overrides)), _events())


def test_normalize_pbp_game_logs_empty_input_carries_full_schema():
    frame, counts = normalize_pbp_game_logs(_pbp_frame(), _events())

    assert counts.source_row_count == 0
    assert frame.empty
    assert "FG_PCT" in frame.columns
    assert "PRA" in frame.columns
    assert "FD_PTS" in frame.columns


# ------------------------------------------------------------------ derive


def test_derive_game_log_frame_computes_shared_values_and_rounds_minutes():
    frame = pd.DataFrame(
        [
            {
                "MIN": 31.5,
                "PTS": 24,
                "REB": 8,
                "AST": 6,
                "FGM": 9,
                "FGA": 18,
                "FG3M": 3,
                "FG3A": 7,
                "FTM": 3,
                "FTA": 4,
                "STL": 2,
                "BLK": 1,
                "TOV": 3,
            }
        ]
    )

    derived = derive_game_log_frame(frame)

    assert derived.loc[0, "MIN"] == 32
    assert derived.loc[0, "NBA_FANTASY_PTS"] == pytest.approx(48.6)
    assert derived.loc[0, "FG_PCT"] == pytest.approx(0.5)
    assert derived.loc[0, "FT_PCT"] == pytest.approx(0.75)
    assert derived.loc[0, "PRA"] == 38
    assert derived.loc[0, "FG2M"] == 6
    assert derived.loc[0, "FG2A"] == 11
    assert derived.loc[0, "FD_PTS"] == pytest.approx(48.6)


def test_derive_game_log_frame_handles_empty_denominators_and_exact_minutes():
    frame = pd.DataFrame(
        [
            {
                "MIN": 31.5,
                "PTS": 0,
                "REB": 0,
                "AST": 0,
                "FGM": 0,
                "FGA": 0,
                "FG3M": 0,
                "FG3A": 0,
                "FTM": 0,
                "FTA": 0,
                "STL": 0,
                "BLK": 0,
                "TOV": 0,
            }
        ]
    )

    derived = derive_game_log_frame(frame, round_minutes=False)

    assert derived.loc[0, "MIN"] == 31.5
    assert derived.loc[0, "FG_PCT"] == 0.0
    assert derived.loc[0, "FT_PCT"] == 0.0
    assert derived.loc[0, "NBA_FANTASY_PTS"] == 0.0


def telemetry_pbp_counts(**values):
    from app.services.pbp_game_log_normalization import PBPJoinCounts

    defaults = {
        "source_row_count": 0,
        "unjoined_event_count": 0,
        "team_mismatch_count": 0,
        "unsupported_phase_count": 0,
    }
    defaults.update(values)
    return PBPJoinCounts(**defaults)
