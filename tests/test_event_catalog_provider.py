"""Recorded/offline contract for the whole-season NBA schedule seam."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from app.config.settings import RuntimeSettings
from app.errors import ProviderUnavailableError
from app.providers.nba_stats import NBAStatsAdapter
from app.services.nba_stats_adapter import CANONICAL_SCHEDULE_COLUMNS
from app.services.nba_stats_adapter import parse_recorded_schedule
from app.utils import telemetry


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "nba_stats" / "schedule.valid.json"
CLASSIFICATION_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "nba_stats" / "schedule.classifications.json"
)


def _frame() -> pd.DataFrame:
    payload = json.loads(FIXTURE_PATH.read_text())
    result_set = payload["resultSets"][0]
    return pd.DataFrame(result_set["rowSet"], columns=result_set["headers"])


class RecordedScheduleEndpoint:
    def __init__(self, **kwargs):
        assert kwargs["season"] == "2025-26"
        assert kwargs["timeout"] == 10.0

    def get_data_frames(self):
        return [_frame()]


def test_whole_season_schedule_uses_instrumented_closed_provider_operation():
    telemetry.clear_recorded_provider_events()
    adapter = NBAStatsAdapter(
        settings=RuntimeSettings(environment="testing"),
        endpoint_factory=RecordedScheduleEndpoint,
    )

    result = adapter.fetch_whole_season_schedule(season="2025-26")

    assert list(result.columns) == list(CANONICAL_SCHEDULE_COLUMNS)
    assert result.loc[0, "nba_game_id"] == "0022500001"
    assert result.loc[0, "scheduled_at"].isoformat() == "2025-10-23T00:00:00+00:00"
    events = telemetry.get_recorded_provider_events()
    assert events[-1]["provider"] == telemetry.PROVIDER_NBA_STATS
    assert events[-1]["operation"] == "schedule_whole_season"
    assert events[-1]["outcome"] == telemetry.OUTCOME_SUCCESS


@pytest.mark.parametrize("season", ["current", "2025", "2025-27"])
def test_whole_season_schedule_requires_explicit_canonical_season(season):
    adapter = NBAStatsAdapter(endpoint_factory=RecordedScheduleEndpoint)
    with pytest.raises(ValueError, match="canonical NBA season"):
        adapter.fetch_whole_season_schedule(season=season)


def test_whole_season_schedule_rejects_missing_explicit_teams():
    frame = _frame().drop(columns=["awayTeam_teamId"])

    class MalformedEndpoint:
        def get_data_frames(self):
            return [frame]

    telemetry.clear_recorded_provider_events()
    adapter = NBAStatsAdapter(endpoint_factory=lambda **_: MalformedEndpoint())
    with pytest.raises(ProviderUnavailableError):
        adapter.fetch_whole_season_schedule(season="2025-26")

    assert telemetry.get_recorded_provider_events()[-1]["outcome"] == (
        telemetry.OUTCOME_MALFORMED
    )


def test_recorded_schedule_fixture_uses_the_production_parser():
    payload = json.loads(FIXTURE_PATH.read_text())
    parsed = parse_recorded_schedule(payload, season="2025-26")
    assert parsed.loc[1, "postponed_status"] == "Postponed"


def test_recorded_schedule_preserves_provider_and_game_type_classification():
    payload = json.loads(CLASSIFICATION_FIXTURE_PATH.read_text())

    parsed = parse_recorded_schedule(payload, season="2025-26")

    assert parsed["classification"].tolist() == [
        "NBA Finals",
        "Emirates NBA Cup",
        "International Series",
        "Skills Challenge",
    ]


def test_schedule_sublabel_filters_generic_series_and_postponement_evidence():
    payload = json.loads(CLASSIFICATION_FIXTURE_PATH.read_text())
    result_set = payload["resultSets"][0]
    sublabel_index = result_set["headers"].index("gameSubLabel")
    result_set["rowSet"][0][sublabel_index] = "Series tied 1-1"
    result_set["rowSet"][1][sublabel_index] = "Postponed due to weather"

    parsed = parse_recorded_schedule(payload, season="2025-26")

    assert parsed["classification"].tolist()[:2] == [
        "Regular Season",
        "Regular Season",
    ]
