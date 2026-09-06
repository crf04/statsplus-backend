from unittest.mock import Mock

from app.domain.player_diet_taxonomy import PLAYER_DIET_QUALIFIER_SLICES


def test_baselines_requires_auth(client, authenticate):
    authenticate()
    response = client.get('/api/diet/baselines')
    assert response.status_code == 401
    assert response.json['error']['code'] == 'authentication_required'


def test_baselines_http_shape(client, authenticate, dependencies):
    dependencies.diet_baselines_service = Mock()
    payload = {'season': '2025-26', 'captured_at': None, 'shares': {
        base: dict.fromkeys(slices, 0.2)
        for base, slices in PLAYER_DIET_QUALIFIER_SLICES.items()
    }}
    dependencies.diet_baselines_service.get.return_value = payload
    response = client.get('/api/diet/baselines', headers=authenticate())
    assert response.status_code == 200
    assert response.json == payload
    dependencies.diet_baselines_service.get.assert_called_once_with()


def test_baselines_match_the_matchup_diet_read_for_every_slice(tmp_path):
    from datetime import datetime, timezone
    from sqlalchemy import create_engine
    from app.migrations import run_migrations
    from app.services.player_diet import PlayerDietRepository, PlayerDietFact, PlayerDietObservation
    from app.services.diet_baselines import DietBaselinesService
    from app.config.settings import RuntimeSettings, NBASeasonSettings

    engine = create_engine(f'sqlite:///{tmp_path / "baselines.db"}')
    run_migrations(engine)
    diets = PlayerDietRepository(engine)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    facts = [PlayerDietFact(
        player_id=player, base=base, slice_key=slice_key, share=share,
        volume=500, games_played=30,
        volume_unit={'play_types': 'possessions', 'assist_locations': 'assists'}.get(base, 'field_goal_attempts'),
        provider={'play_types': 'nba_synergy', 'assist_locations': 'pbp_stats'}.get(base, 'nba_stats'),
    ) for base, slices in PLAYER_DIET_QUALIFIER_SLICES.items()
        for index, slice_key in enumerate(slices)
        for player, share in [(1, 0.1 + index * 0.01), (2, 0.3 + index * 0.01)]]
    diets.publish('2025-26', facts, [
        PlayerDietObservation(base=base, status='available', unavailable_reason=None)
        for base in PLAYER_DIET_QUALIFIER_SLICES
    ], retrieved_at=now)
    service = DietBaselinesService(player_diets=diets, settings=RuntimeSettings(nba=NBASeasonSettings(current_season="2025-26")))
    payload = service.get()
    matchup_read = diets.get_for_players('2025-26', [1])
    assert payload['season'] == '2025-26'
    assert payload['captured_at'] == now.isoformat()
    assert set(payload['shares']) == set(PLAYER_DIET_QUALIFIER_SLICES)
    for base, slices in PLAYER_DIET_QUALIFIER_SLICES.items():
        assert set(payload['shares'][base]) == set(slices)
        for index, slice_key in enumerate(slices):
            expected = [0.2, 0.21, 0.22, 0.23, 0.24, 0.25, 0.26, 0.27, 0.28, 0.29][index]
            assert payload['shares'][base][slice_key] == expected
            assert round(matchup_read.baselines[(base, slice_key)].league_average_share, 6) == expected
    engine.dispose()


def test_baselines_capture_one_snapshot_and_preserve_missing_shares():
    from types import SimpleNamespace
    from app.services.diet_baselines import DietBaselinesService
    from app.services.player_diet import PlayerDietResult
    from app.config.settings import RuntimeSettings, NBASeasonSettings

    snapshot = object()
    reader = Mock(snapshot=Mock(return_value=snapshot))
    calls = []

    def read(season, player_ids, *, publication_snapshot):
        calls.append(publication_snapshot)
        return PlayerDietResult(season, {}, ())

    result = DietBaselinesService(
        player_diets=SimpleNamespace(get_for_players=read),
        publication_reader=reader, settings=RuntimeSettings(nba=NBASeasonSettings(current_season="2025-26")),
    ).get()
    assert calls == [snapshot]
    reader.snapshot.assert_called_once()
    assert result['captured_at'] is None
    assert result['shares']['shot_zones']['Corner 3'] is None
