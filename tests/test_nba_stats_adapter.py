"""Concurrency and timeout tests for the bounded NBA Stats adapter (#10).

Every remaining ``stats.nba.com`` caller goes through :class:`NBAStatsAdapter`,
which runs as a synchronous Flask/threaded workload.  These tests prove that
the adapter enforces the configured ``NBA_STATS_MAX_CONCURRENCY`` bound across
separately constructed instances and reuses the configured timeout.  The
game-log route is no longer one of those callers (#198).
"""

import threading
import time
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import pytest
import requests
from nba_api.stats import endpoints
from nba_api.stats.endpoints.playergamelogs import PlayerGameLogs

from app.config.settings import (
    NBASeasonSettings,
    ProviderSettings,
    RuntimeSettings,
)
from app.errors import ProviderUnavailableError
from app.providers.nba_stats import (
    DERIVED_GAME_LOG_COLUMNS,
    NBAStatsAdapter as InjectedNBAStatsAdapter,
    REQUIRED_GAME_LOG_COLUMNS,
    normalize_archetype_game_logs,
    normalize_player_game_logs,
    normalize_season_player_game_logs,
)
from app.services.nba_stats_adapter import (
    GAME_LOG_REQUIRED_COLUMNS, NBAStatsAdapter,
    opponent_team_stats_request_descriptor,
    player_per36_request_descriptor,
    player_totals_request_descriptor,
)
from app.services.nba_stats_adapter import (
    parse_recorded_game_logs,
    parse_recorded_player_diet,
)
from app.utils.telemetry import ProviderResponseError

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "nba_stats_player_game_logs.json"
PLAYOFF_FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "nba_stats"
    / "player_game_logs.playoffs.json"
)
PLAYER_SHOT_ZONE_FIXTURE_PATH = (
    Path(__file__).parent / "fixtures" / "player_diets" / "shot_zones.json"
)


def _recorded_provider_frame() -> pd.DataFrame:
    payload = json.loads(FIXTURE_PATH.read_text())
    result_set = payload["resultSets"][0]
    return pd.DataFrame(result_set["rowSet"], columns=result_set["headers"])


def _recorded_provider_result_set() -> dict:
    payload = json.loads(FIXTURE_PATH.read_text())
    return payload["resultSets"][0]


def _recorded_playoff_provider_frame() -> pd.DataFrame:
    payload = json.loads(PLAYOFF_FIXTURE_PATH.read_text())
    return parse_recorded_game_logs(payload)


def _test_settings() -> RuntimeSettings:
    return RuntimeSettings(
        environment="testing",
        auth={"firebase_admin_disabled": True},
        cache={"enabled": False},
        providers={"nba_stats_timeout_seconds": 2.5},
        nba=NBASeasonSettings(current_season="2025-26"),
    )


class _ConcurrencyProbe:
    """Tracks how many fake provider calls are in flight at once."""

    def __init__(self, hold: float = 0.1):
        self._lock = threading.Lock()
        self._hold = hold
        self.calls = 0
        self.active = 0
        self.max_active = 0

    def run(self, fn):
        with self._lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self._hold)
            return fn()
        finally:
            with self._lock:
                self.active -= 1


def _probe_endpoint(probe):
    class _ProbePlayerGameLogs:
        def __init__(self, *args, **kwargs):
            pass

        def get_data_frames(self, *args, **kwargs):
            frame = probe.run(
                lambda: pd.DataFrame(
                    {column: [1] for column in GAME_LOG_REQUIRED_COLUMNS}
                )
            )
            return [frame]

    return _ProbePlayerGameLogs


def _settings(max_concurrency: int, timeout: float = 1.0) -> RuntimeSettings:
    return RuntimeSettings(
        environment="testing",
        providers=ProviderSettings(
            nba_stats_timeout_seconds=timeout,
            nba_stats_max_concurrency=max_concurrency,
        ),
    )


def test_adapter_exposes_configured_bound():
    adapter = NBAStatsAdapter(settings=_settings(max_concurrency=3))

    assert adapter.max_concurrency == 3
    assert adapter.timeout == 1.0


def test_adapter_enforces_concurrency_bound_with_concurrent_calls(monkeypatch):
    probe = _ConcurrencyProbe(hold=0.1)
    adapter = NBAStatsAdapter(settings=_settings(max_concurrency=2))

    monkeypatch.setattr(
        endpoints.playergamelogs,
        "PlayerGameLogs",
        _probe_endpoint(probe),
    )

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(adapter.fetch_player_game_logs, player_id, "2024-25")
            for player_id in range(8)
        ]
        frames = [future.result() for future in futures]

    assert probe.calls == 8
    assert probe.max_active <= 2
    assert len(frames) == 8
    assert all(isinstance(frame, pd.DataFrame) for frame in frames)
def test_separately_constructed_adapters_share_one_process_gate(monkeypatch):
    """Two adapters built independently still honour one process-wide bound."""

    probe = _ConcurrencyProbe(hold=0.05)
    settings = _settings(max_concurrency=2)
    monkeypatch.setattr(
        endpoints.playergamelogs,
        "PlayerGameLogs",
        _probe_endpoint(probe),
    )

    adapters = [
        InjectedNBAStatsAdapter(settings=settings),
        InjectedNBAStatsAdapter(settings=settings),
    ]

    with ThreadPoolExecutor(max_workers=8) as pool:
        frames = [
            future.result()
            for future in [
                pool.submit(
                    adapters[index % len(adapters)].fetch_player_game_logs,
                    index,
                    "2024-25",
                )
                for index in range(8)
            ]
        ]

    assert len(frames) == 8
    assert probe.calls == 8
    assert probe.max_active <= settings.providers.nba_stats_max_concurrency
    assert adapters[0]._bound is adapters[1]._bound


def test_adapter_passes_configured_timeout_to_provider(monkeypatch):
    captured = {}
    adapter = NBAStatsAdapter(settings=_settings(max_concurrency=1, timeout=7.5))

    def fake_endpoint(**kwargs):
        captured.update(kwargs)
        return FakeEndpoint()

    class FakeEndpoint:
        def get_data_frames(self):
            return [
                pd.DataFrame(
                    {column: [1] for column in GAME_LOG_REQUIRED_COLUMNS}
                )
            ]

    monkeypatch.setattr(
        endpoints.playergamelogs,
        "PlayerGameLogs",
        lambda *args, **kwargs: fake_endpoint(**kwargs),
    )

    adapter.fetch_player_game_logs("123", "2024-25")

    assert captured["timeout"] == 7.5
    assert captured["player_id_nullable"] == "123"
    assert captured["season_nullable"] == "2024-25"


def test_team_game_log_returns_independent_exact_membership():
    captured = {}

    class TeamGameLogFixture:
        def __init__(
            self,
            team_id,
            season="2025-26",
            season_type_all_star="Regular Season",
            date_from_nullable="",
            date_to_nullable="",
            league_id_nullable="00",
            proxy=None,
            headers=None,
            timeout=30,
            get_request=True,
        ):
            captured.update({
                "team_id": team_id,
                "season": season,
                "season_type_all_star": season_type_all_star,
                "date_from_nullable": date_from_nullable,
                "date_to_nullable": date_to_nullable,
                "league_id_nullable": league_id_nullable,
                "proxy": proxy,
                "headers": headers,
                "timeout": timeout,
                "get_request": get_request,
            })

        def get_data_frames(self):
            return [pd.DataFrame({"GAME_ID": ["g-2", "g-1"]})]

    adapter = NBAStatsAdapter(
        settings=_settings(max_concurrency=1, timeout=7.5),
        team_game_log_endpoint_factory=TeamGameLogFixture,
    )

    assert adapter.fetch_team_game_ids(
        1610612738,
        "2024-25",
        date_from="01/01/2025",
        date_to="01/31/2025",
    ) == ("g-1", "g-2")
    assert captured["team_id"] == 1610612738
    assert captured["season"] == "2024-25"
    assert captured["date_from_nullable"] == "01/01/2025"
    assert captured["date_to_nullable"] == "01/31/2025"
    assert captured["timeout"] == 7.5


def test_team_game_log_rejects_missing_membership_ids():
    class TeamGameLogFixture:
        def __init__(
            self,
            team_id,
            season="2025-26",
            season_type_all_star="Regular Season",
            date_from_nullable="",
            date_to_nullable="",
            league_id_nullable="00",
            proxy=None,
            headers=None,
            timeout=30,
            get_request=True,
        ):
            pass

        def get_data_frames(self):
            return [pd.DataFrame({"GP": [15]})]

    adapter = NBAStatsAdapter(
        settings=_settings(max_concurrency=1),
        team_game_log_endpoint_factory=TeamGameLogFixture,
    )
    with pytest.raises(ProviderResponseError):
        adapter.fetch_team_game_ids(1610612738, "2024-25")


def test_team_game_log_accepts_live_mixed_case_game_id_column():
    class TeamGameLogFixture:
        def __init__(self, **kwargs):
            pass

        def get_data_frames(self):
            return [pd.DataFrame({"Game_ID": ["g-2", "g-1"]})]

    adapter = NBAStatsAdapter(
        settings=_settings(max_concurrency=1),
        team_game_log_endpoint_factory=TeamGameLogFixture,
    )

    assert adapter.fetch_team_game_ids(
        1610612738, "2024-25"
    ) == ("g-1", "g-2")


def test_adapter_propagates_provider_timeout(monkeypatch):
    import requests

    def timed_out_endpoint(**kwargs):
        raise requests.exceptions.ReadTimeout("stats.nba.com timed out")

    adapter = NBAStatsAdapter(settings=_settings(max_concurrency=1))
    monkeypatch.setattr(
        endpoints.playergamelogs,
        "PlayerGameLogs",
        lambda *args, **kwargs: timed_out_endpoint(**kwargs),
    )

    with pytest.raises(requests.exceptions.ReadTimeout):
        adapter.fetch_player_game_logs("123", "2024-25")


class _SimpleNamespace:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _StatusResponse:
    """Fake endpoint that exposes ``nba_response.status_code`` only."""

    def __init__(self, status_code):
        self.nba_response = _SimpleNamespace(status_code=status_code)

    def get_data_frames(self):
        if self.nba_response.status_code >= 400:
            raise AssertionError("run_endpoint must raise before parsing")
        return [
            pd.DataFrame({column: [1] for column in GAME_LOG_REQUIRED_COLUMNS})
        ]


def test_adapter_records_upstream_status_on_provider_event(monkeypatch):
    adapter = NBAStatsAdapter(settings=_settings(max_concurrency=1))

    def fake_endpoint(**kwargs):
        return _StatusResponse(200)

    monkeypatch.setattr(
        endpoints.playergamelogs,
        "PlayerGameLogs",
        lambda *args, **kwargs: fake_endpoint(**kwargs),
    )

    from app.utils import telemetry

    telemetry.clear_recorded_provider_events()
    adapter.fetch_player_game_logs("123", "2024-25")
    events = telemetry.get_recorded_provider_events()
    assert events
    assert events[-1]["operation"] == "player_game_logs"
    assert events[-1]["status_code"] == 200
    assert events[-1]["outcome"] == telemetry.OUTCOME_SUCCESS


def test_non_cache_operation_defaults_to_disabled(monkeypatch):
    adapter = NBAStatsAdapter(settings=_settings(max_concurrency=1))

    monkeypatch.setattr(
        endpoints.playergamelogs,
        "PlayerGameLogs",
        lambda *args, **kwargs: _StatusResponse(200),
    )

    from app.utils import telemetry

    telemetry.clear_recorded_provider_events()
    adapter.fetch_player_game_logs("123", "2024-25")

    assert telemetry.get_recorded_provider_events()[-1]["cache_status"] == (
        telemetry.CACHE_DISABLED
    )


def test_cache_aware_calls_record_explicit_miss_and_hit(monkeypatch):
    adapter = NBAStatsAdapter(settings=_settings(max_concurrency=1))

    monkeypatch.setattr(
        endpoints.playergamelogs,
        "PlayerGameLogs",
        lambda *args, **kwargs: _StatusResponse(200),
    )

    from app.utils import telemetry

    telemetry.clear_recorded_provider_events()
    adapter.fetch_player_game_logs(
        "123", "2024-25", cache_status=telemetry.CACHE_MISS
    )
    adapter.record_cache_hit("player_game_logs")

    events = telemetry.get_recorded_provider_events()
    assert events[-2]["cache_status"] == telemetry.CACHE_MISS
    assert events[-1]["cache_status"] == telemetry.CACHE_HIT


def test_adapter_classifies_non_2xx_status_as_http_error(monkeypatch):
    import requests

    adapter = NBAStatsAdapter(settings=_settings(max_concurrency=1))

    def error_endpoint(**kwargs):
        return _StatusResponse(503)

    monkeypatch.setattr(
        endpoints.playergamelogs,
        "PlayerGameLogs",
        lambda *args, **kwargs: error_endpoint(**kwargs),
    )

    from app.utils import telemetry

    telemetry.clear_recorded_provider_events()
    with pytest.raises(requests.exceptions.HTTPError):
        adapter.fetch_player_game_logs("123", "2024-25")

    events = telemetry.get_recorded_provider_events()
    assert events[-1]["status_code"] == 503
    assert events[-1]["outcome"] == telemetry.OUTCOME_HTTP_ERROR


@pytest.mark.parametrize(
    "frame, required_columns, malformed",
    [
        (pd.DataFrame({"GAME_ID": [], "PTS": []}), ("GAME_ID", "PTS"), False),
        (pd.DataFrame({"GAME_ID": ["1"]}), ("GAME_ID", "PTS"), True),
    ],
)
def test_adapter_classifies_empty_or_invalid_schema_as_malformed(
    monkeypatch, frame, required_columns, malformed
):
    from app.utils import telemetry

    class Endpoint:
        def get_data_frames(self):
            return [frame]

    adapter = NBAStatsAdapter(settings=_settings(max_concurrency=1))
    telemetry.clear_recorded_provider_events()

    if malformed:
        with pytest.raises(telemetry.ProviderResponseError):
            adapter.run_endpoint(
                "health_probe",
                lambda timeout: Endpoint(),
                required_columns=required_columns,
            )
    else:
        result = adapter.run_endpoint(
            "health_probe",
            lambda timeout: Endpoint(),
            required_columns=required_columns,
        )
        assert result.empty

    event = telemetry.get_recorded_provider_events()[-1]
    assert event["outcome"] == (
        telemetry.OUTCOME_MALFORMED if malformed else telemetry.OUTCOME_SUCCESS
    )
def test_recorded_provider_fixture_is_normalized_across_schema_drift():
    raw_frame = _recorded_provider_frame()

    normalized = normalize_player_game_logs(raw_frame)

    assert list(normalized["GAME_DATE"]) == [
        "2018-03-20T00:00:00",
        "2018-03-18T00:00:00",
        "2018-03-17T00:00:00",
        "2018-03-15T00:00:00",
        "2018-03-13T00:00:00",
        "2018-03-11T00:00:00",
    ]
    assert "SEASON_YEAR" not in normalized.columns
    assert "TEAM_NAME" not in normalized.columns
    assert set(REQUIRED_GAME_LOG_COLUMNS).issubset(normalized.columns)
    assert set(DERIVED_GAME_LOG_COLUMNS).issubset(normalized.columns)
    assert normalized.loc[0, "MIN"] == 35
    assert normalized.loc[0, "PRA"] == 47
    assert normalized.loc[0, "FG2M"] == 13
    assert normalized.loc[1, "FG2A"] == 21
    assert list(raw_frame.columns) == _recorded_provider_result_set()["headers"]


def test_fixture_matches_current_player_game_logs_result_set_schema():
    result_set = _recorded_provider_result_set()

    payload = json.loads(FIXTURE_PATH.read_text())

    assert payload["resource"] == "gamelogs"
    assert payload["provenance"]["commit"] == (
        "03f6a064982edfc8c5d5905a6633a3af17569d54"
    )
    assert payload["provenance"]["repository"] == "eddiemay/NBAStats"
    assert result_set["name"] == "PlayerGameLogs"
    assert result_set["headers"] == PlayerGameLogs.expected_data["PlayerGameLogs"]


def test_archetype_normalization_preserves_player_ids_for_cluster_filtering():
    normalized = normalize_archetype_game_logs(_recorded_provider_frame())

    assert "PLAYER_ID" in normalized.columns
    assert list(normalized["PLAYER_ID"]) == [203076] * 6
    assert normalized.loc[0, "PRA"] == 47


def test_season_normalization_preserves_player_ids_and_exact_minutes():
    normalized = normalize_season_player_game_logs(_recorded_provider_frame())

    assert list(normalized["PLAYER_ID"]) == [203076] * 6
    assert normalized.loc[0, "MIN"] == 35.233333333333334


def test_missing_required_provider_column_is_centralized_provider_error():
    raw_frame = _recorded_provider_frame().drop(columns=["PTS"])

    with pytest.raises(ProviderUnavailableError, match="unsupported game-log schema"):
        normalize_player_game_logs(raw_frame)


def test_adapter_owns_timeout_and_translates_provider_timeout():
    calls: list[dict] = []

    class TimedOutEndpoint:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def get_data_frames(self):
            raise requests.exceptions.ReadTimeout("stats.nba.com timed out")

    adapter = NBAStatsAdapter(
        settings=_test_settings(), endpoint_factory=TimedOutEndpoint
    )

    with pytest.raises(ProviderUnavailableError) as error:
        adapter.get_player_game_logs(player_id=2544, season="2025-26")

    assert error.value.code == "provider_unavailable"
    assert calls == [
        {
            "player_id_nullable": 2544,
            "season_nullable": "2025-26",
            "season_type_nullable": "Regular Season",
            "timeout": 2.5,
        }
    ]


def test_adapter_normalizes_recorded_response_from_endpoint_factory():
    class RecordedEndpoint:
        def __init__(self, **kwargs):
            assert kwargs["player_id_nullable"] == 2544
            assert kwargs["season_nullable"] == "2025-26"
            assert kwargs["season_type_nullable"] == "Regular Season"
            assert kwargs["timeout"] == 2.5

        def get_data_frames(self):
            return [_recorded_provider_frame()]

    adapter = NBAStatsAdapter(
        settings=_test_settings(), endpoint_factory=RecordedEndpoint
    )

    normalized = adapter.get_player_game_logs(player_id=2544, season="2025-26")

    assert normalized.loc[0, "PRA"] == 47
    assert "TEAM_NAME" not in normalized.columns


def test_adapter_fetches_and_filters_archetype_logs_through_provider_seam():
    calls: list[dict] = []

    class RecordedEndpoint:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def get_data_frames(self):
            return [_recorded_provider_frame()]

    adapter = NBAStatsAdapter(
        settings=_test_settings(), endpoint_factory=RecordedEndpoint
    )

    normalized = adapter.get_archetype_game_logs(
        player_ids=[203076],
        opponent_team_id=1610612744,
        season="2025-26",
    )

    assert list(normalized["PLAYER_ID"]) == [203076] * 6
    assert normalized.loc[0, "PLAYER_NAME"] == "Anthony Davis"
    assert calls == [
        {
            "season_nullable": "2025-26",
            "season_type_nullable": "Regular Season",
            "opp_team_id_nullable": 1610612744,
            "timeout": 2.5,
        }
    ]


@pytest.mark.parametrize(
    ("season_type", "provider_frame", "game_id_prefix"),
    [
        ("Regular Season", _recorded_provider_frame, "002"),
        ("Playoffs", _recorded_playoff_provider_frame, "004"),
    ],
)
def test_adapter_fetches_each_complete_season_phase_in_one_provider_call(
    season_type, provider_frame, game_id_prefix,
):
    calls: list[dict] = []

    class RecordedEndpoint:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def get_data_frames(self):
            return [provider_frame()]

    adapter = InjectedNBAStatsAdapter(
        settings=_test_settings(), endpoint_factory=RecordedEndpoint
    )

    normalized = adapter.get_season_player_game_logs(
        season="2025-26", season_type=season_type
    )

    assert len(normalized) >= 2
    assert normalized.loc[0, "PLAYER_ID"] > 0
    assert normalized.loc[0, "MIN"] > 0
    assert normalized["GAME_ID"].str.startswith(game_id_prefix).all()
    assert calls == [
        {
            "season_nullable": "2025-26",
            "season_type_nullable": season_type,
            "timeout": 2.5,
        }
    ]


def test_team_matchup_nba_surface_uses_exact_last_n_and_as_of(monkeypatch):
    calls = []

    class Endpoint:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def get_data_frames(self):
            return [
                pd.DataFrame(
                    [{"TEAM_ID": 1610612738, "TEAM_NAME": "Boston Celtics"}]
                )
            ]

    monkeypatch.setattr(endpoints, "LeagueDashTeamStats", Endpoint)
    adapter = NBAStatsAdapter(settings=_settings(max_concurrency=1))

    adapter.fetch_opponent_team_stats(
        "03/01/2025",
        date_to="04/15/2025",
        season="2024-25",
        season_type="Regular Season",
        team_id=1610612738,
        last_n_games=15,
        per_mode_detailed="Totals",
    )

    assert calls == [
        {
            "measure_type_detailed_defense": "Opponent",
            "per_mode_detailed": "Totals",
            "date_from_nullable": "03/01/2025",
            "date_to_nullable": "04/15/2025",
            "season": "2024-25",
            "season_type_all_star": "Regular Season",
            "team_id_nullable": 1610612738,
            "last_n_games": 15,
            "league_id_nullable": "00",
            "timeout": adapter.timeout,
        }
    ]


def test_existing_opponent_stats_call_keeps_provider_scope_defaults(monkeypatch):
    calls = []

    class Endpoint:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def get_data_frames(self):
            return [
                pd.DataFrame(
                    [{"TEAM_ID": 1610612738, "TEAM_NAME": "Boston Celtics"}]
                )
            ]

    monkeypatch.setattr(endpoints, "LeagueDashTeamStats", Endpoint)
    adapter = NBAStatsAdapter(settings=_settings(max_concurrency=1))

    adapter.fetch_opponent_team_stats("03/01/2025")

    assert calls == [
        {
            "measure_type_detailed_defense": "Opponent",
            "per_mode_detailed": "Per48",
            "date_from_nullable": "03/01/2025",
            "league_id_nullable": "00",
            "timeout": adapter.timeout,
        }
    ]


def test_opponent_request_evidence_uses_endpoint_wire_parameters(monkeypatch):
    expected = opponent_team_stats_request_descriptor(
        date_from="03/01/2025", date_to="04/15/2025", season="2024-25",
        season_type="Regular Season", team_id=1610612738, last_n_games=15,
        per_mode_detailed="Totals", league_id="00",
    )

    class Endpoint:
        def __init__(self, **kwargs):
            self.parameters = dict(expected["parameters"])

        def get_data_frames(self):
            return [pd.DataFrame([{"TEAM_ID": 1610612738, "TEAM_NAME": "Boston"}])]

    monkeypatch.setattr(endpoints, "LeagueDashTeamStats", Endpoint)
    adapter = NBAStatsAdapter(settings=_settings(max_concurrency=1))
    adapter.fetch_opponent_team_stats(
        "03/01/2025", date_to="04/15/2025", season="2024-25",
        season_type="Regular Season", team_id=1610612738, last_n_games=15,
        per_mode_detailed="Totals",
    )
    recorded = adapter.transport_request_descriptor("league_opponent_team_stats")
    assert recorded == expected
    changed = json.loads(json.dumps(recorded))
    changed["parameters"]["Month"] = "1"
    assert changed != recorded


def test_opponent_shot_chart_uses_the_installed_endpoint_league_id_keyword(
    monkeypatch,
):
    calls = []

    class Endpoint:
        def __init__(
            self,
            *,
            general_range_nullable,
            date_from_nullable,
            per_mode_simple,
            league_id,
            date_to_nullable,
            season,
            season_type_all_star,
            team_id_nullable,
            last_n_games_nullable,
            timeout,
        ):
            calls.append(
                {
                    "general_range_nullable": general_range_nullable,
                    "date_from_nullable": date_from_nullable,
                    "per_mode_simple": per_mode_simple,
                    "league_id": league_id,
                    "date_to_nullable": date_to_nullable,
                    "season": season,
                    "season_type_all_star": season_type_all_star,
                    "team_id_nullable": team_id_nullable,
                    "last_n_games_nullable": last_n_games_nullable,
                    "timeout": timeout,
                }
            )

        def get_data_frames(self):
            return [
                pd.DataFrame(
                    [
                        {
                            "TEAM_ID": 1610612738,
                            "TEAM_NAME": "Boston Celtics",
                            "FG2M": 20,
                            "FG3M": 10,
                        }
                    ]
                )
            ]

    monkeypatch.setattr(endpoints, "LeagueDashOppPtShot", Endpoint)
    adapter = NBAStatsAdapter(settings=_settings(max_concurrency=1))

    adapter.fetch_opponent_shot_chart(
        "Catch and Shoot",
        "03/01/2025",
        date_to="04/15/2025",
        season="2024-25",
        season_type="Regular Season",
        team_id=1610612738,
        last_n_games=15,
        per_mode_simple="Totals",
    )

    assert calls == [
        {
            "general_range_nullable": "Catch and Shoot",
            "date_from_nullable": "03/01/2025",
            "per_mode_simple": "Totals",
            "league_id": "00",
            "date_to_nullable": "04/15/2025",
            "season": "2024-25",
            "season_type_all_star": "Regular Season",
            "team_id_nullable": 1610612738,
            "last_n_games_nullable": 15,
            "timeout": adapter.timeout,
        }
    ]


def test_synergy_play_types_uses_the_installed_endpoint_league_id_keyword(
    monkeypatch,
):
    calls = []

    class Endpoint:
        def __init__(
            self,
            *,
            play_type_nullable,
            player_or_team_abbreviation,
            type_grouping_nullable,
            league_id,
            season,
            per_mode_simple,
            timeout,
        ):
            calls.append(
                {
                    "play_type_nullable": play_type_nullable,
                    "player_or_team_abbreviation": player_or_team_abbreviation,
                    "type_grouping_nullable": type_grouping_nullable,
                    "league_id": league_id,
                    "season": season,
                    "per_mode_simple": per_mode_simple,
                    "timeout": timeout,
                }
            )

        def get_data_frames(self):
            return [
                pd.DataFrame(
                    [{"TEAM_NAME": "Boston Celtics", "GP": 82, "PTS": 1000}]
                )
            ]

    monkeypatch.setattr(endpoints, "SynergyPlayTypes", Endpoint)
    adapter = NBAStatsAdapter(settings=_settings(max_concurrency=1))

    adapter.fetch_synergy_play_types(
        "Transition",
        player_or_team_abbreviation="T",
        type_grouping="defensive",
        season="2024-25",
        per_mode_simple="Totals",
    )

    assert calls == [
        {
            "play_type_nullable": "Transition",
            "player_or_team_abbreviation": "T",
            "type_grouping_nullable": "defensive",
            "league_id": "00",
            "season": "2024-25",
            "per_mode_simple": "Totals",
            "timeout": adapter.timeout,
        }
    ]


def test_player_diet_synergy_uses_pinned_offensive_season_contract(monkeypatch):
    calls = []

    class Endpoint:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def get_data_frames(self):
            return [
                pd.DataFrame(
                    [{
                        "PLAYER_ID": 2544,
                        "PLAYER_NAME": "LeBron James",
                        "TEAM_ABBREVIATION": "LAL",
                        "PLAY_TYPE": "Isolation",
                        "TYPE_GROUPING": "Offensive",
                        "GP": 40,
                        "POSS_PCT": 0.2,
                        "POSS": 120,
                        "PTS": 100,
                    }]
                )
            ]

    monkeypatch.setattr(endpoints, "SynergyPlayTypes", Endpoint)
    adapter = NBAStatsAdapter(settings=_settings(max_concurrency=1))

    frame = adapter.fetch_synergy_play_types(
        "Isolation",
        player_or_team_abbreviation="P",
        type_grouping="Offensive",
        season="2025-26",
        season_type="Regular Season",
        per_mode_simple="Totals",
    )

    assert frame.loc[0, "PLAYER_ID"] == 2544
    assert calls == [
        {
            "play_type_nullable": "Isolation",
            "player_or_team_abbreviation": "P",
            "type_grouping_nullable": "Offensive",
            "league_id": "00",
            "season": "2025-26",
            "season_type_all_star": "Regular Season",
            "per_mode_simple": "Totals",
            "timeout": adapter.timeout,
        }
    ]


def test_player_per36_records_the_exact_endpoint_wire_request(monkeypatch):
    class Endpoint:
        def __init__(self, **kwargs):
            assert kwargs["season"] == "2025-26"
            assert kwargs["season_type_all_star"] == "Regular Season"
            self.parameters = player_per36_request_descriptor(
                season="2025-26"
            )["parameters"]

        def get_data_frames(self):
            return [pd.DataFrame([{"PLAYER_ID": 2544}])]

    monkeypatch.setattr(endpoints, "LeagueDashPlayerStats", Endpoint)
    adapter = NBAStatsAdapter(settings=_settings(max_concurrency=1))

    adapter.fetch_player_per36_stats(season="2025-26")

    assert adapter.transport_request_descriptor("player_per36_stats") == {
        "adapter": "nba_stats",
        "operation": "player_per36_stats",
        "endpoint": "LeagueDashPlayerStats",
        "parameters": player_per36_request_descriptor(
            season="2025-26"
        )["parameters"],
    }


def test_player_totals_records_independent_raw_count_request(monkeypatch):
    descriptor = player_totals_request_descriptor(season="2025-26")

    class Endpoint:
        def __init__(self, **kwargs):
            assert kwargs["season"] == "2025-26"
            assert kwargs["per_mode_detailed"] == "Totals"
            self.parameters = descriptor["parameters"]

        def get_data_frames(self):
            return [pd.DataFrame([{
                "PLAYER_ID": 2544, "TEAM_ID": 1610612747, "GP": 82,
                "MIN": 2800, "PTS": 2000, "REB": 600, "AST": 500,
                "FGM": 700, "FGA": 1400, "FG3M": 200, "FG3A": 500,
                "FTM": 400, "FTA": 500, "TOV": 200, "STL": 80,
                "BLK": 40, "PF": 100,
            }])]

    monkeypatch.setattr(endpoints, "LeagueDashPlayerStats", Endpoint)
    adapter = NBAStatsAdapter(settings=_settings(max_concurrency=1))

    adapter.fetch_player_totals_stats(season="2025-26")

    assert adapter.transport_request_descriptor("player_totals_stats") == descriptor


def test_player_diet_shot_type_uses_pinned_league_dash_player_contract(monkeypatch):
    calls = []

    class Endpoint:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def get_data_frames(self):
            return [
                pd.DataFrame(
                    [{
                        "PLAYER_ID": 2544,
                        "PLAYER_NAME": "LeBron James",
                        "GP": 40,
                        "FGA_FREQUENCY": 0.25,
                        "FGA": 100,
                    }]
                )
            ]

    monkeypatch.setattr(endpoints, "LeagueDashPlayerPtShot", Endpoint)
    adapter = NBAStatsAdapter(settings=_settings(max_concurrency=1))

    frame = adapter.fetch_player_shot_type(
        "Catch and Shoot",
        season="2025-26",
        season_type="Regular Season",
        per_mode_simple="Totals",
    )

    assert frame.loc[0, "PLAYER_ID"] == 2544
    assert calls == [
        {
            "general_range_nullable": "Catch and Shoot",
            "season": "2025-26",
            "season_type_all_star": "Regular Season",
            "per_mode_simple": "Totals",
            "league_id": "00",
            "timeout": adapter.timeout,
        }
    ]


def test_player_synergy_contract_requires_canonical_identity_share_volume_and_games(
    monkeypatch,
):
    from app.utils.telemetry import ProviderResponseError

    class Endpoint:
        def __init__(self, **kwargs):
            pass

        def get_data_frames(self):
            return [
                pd.DataFrame(
                    [{
                        "PLAYER_NAME": "LeBron James",
                        "TEAM_ABBREVIATION": "LAL",
                        "PTS": 100,
                    }]
                )
            ]

    monkeypatch.setattr(endpoints, "SynergyPlayTypes", Endpoint)
    adapter = NBAStatsAdapter(settings=_settings(max_concurrency=1))

    with pytest.raises(ProviderResponseError):
        adapter.fetch_synergy_play_types(
            "Isolation",
            player_or_team_abbreviation="P",
            type_grouping="Offensive",
            season="2025-26",
            per_mode_simple="Totals",
        )


def test_player_synergy_contract_keeps_legacy_team_and_points_requirements(
    monkeypatch,
):
    from app.utils.telemetry import ProviderResponseError

    class Endpoint:
        def __init__(self, **kwargs):
            pass

        def get_data_frames(self):
            return [
                pd.DataFrame(
                    [{
                        "PLAYER_ID": 2544,
                        "PLAYER_NAME": "LeBron James",
                        "PLAY_TYPE": "Isolation",
                        "TYPE_GROUPING": "Offensive",
                        "GP": 40,
                        "POSS_PCT": 0.2,
                        "POSS": 120,
                    }]
                )
            ]

    monkeypatch.setattr(endpoints, "SynergyPlayTypes", Endpoint)
    adapter = NBAStatsAdapter(settings=_settings(max_concurrency=1))

    with pytest.raises(ProviderResponseError):
        adapter.fetch_synergy_play_types(
            "Isolation",
            player_or_team_abbreviation="P",
            type_grouping="Offensive",
            season="2025-26",
            per_mode_simple="Totals",
        )


def test_player_diet_shot_zones_use_one_pinned_league_wide_season_call(monkeypatch):
    calls = []

    class Endpoint:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def get_data_frames(self):
            return [
                pd.DataFrame(
                    [{
                        "PLAYER_ID": 2544,
                        "PLAYER_NAME": "LeBron James",
                        "TEAM_ID": 1610612747,
                    }]
                )
            ]

    monkeypatch.setattr(endpoints, "LeagueDashPlayerShotLocations", Endpoint)
    adapter = NBAStatsAdapter(settings=_settings(max_concurrency=1))

    adapter.fetch_player_shooting_zone(
        season="2025-26",
        season_type="Regular Season",
        per_mode_detailed="Totals",
    )

    assert calls == [
        {
            "distance_range": "By Zone",
            "per_mode_detailed": "Totals",
            "date_from_nullable": None,
            "season": "2025-26",
            "season_type_all_star": "Regular Season",
            "timeout": adapter.timeout,
        }
    ]


def test_player_shot_zones_accept_the_recorded_multilevel_provider_schema(monkeypatch):
    payload = json.loads(PLAYER_SHOT_ZONE_FIXTURE_PATH.read_text(encoding="utf-8"))
    recorded_frame = parse_recorded_player_diet(payload, kind="shot_zones")

    class Endpoint:
        def __init__(self, **kwargs):
            pass

        def get_data_frames(self):
            return [recorded_frame]

    monkeypatch.setattr(endpoints, "LeagueDashPlayerShotLocations", Endpoint)
    adapter = NBAStatsAdapter(settings=_settings(max_concurrency=1))

    frame = adapter.fetch_player_shooting_zone(
        season="2025-26",
        season_type="Regular Season",
        per_mode_detailed="Totals",
    )

    assert frame.loc[0, ("", "PLAYER_ID")] == 1630639
    assert frame.loc[0, ("Corner 3", "FGA")] == 32
