"""Stored player-archetype membership queries."""

import pandas as pd
from sqlalchemy import create_engine

from app.services.player_archetype_repository import PlayerArchetypeRepository


def test_archetype_query_returns_sorted_peers_without_the_selected_player(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'archetypes.sqlite3'}")
    pd.DataFrame(
        [
            {"PlayerName": "Selected", "ClusterID": 7, "PlayerID": 101},
            {"PlayerName": "Peer Two", "ClusterID": 7, "PlayerID": 303},
            {"PlayerName": "Peer One", "ClusterID": 7, "PlayerID": 202},
            {"PlayerName": "Other", "ClusterID": 8, "PlayerID": 404},
        ]
    ).to_sql("player_clusters", engine, index=False)

    assert PlayerArchetypeRepository(engine).list_peer_ids(101) == (202, 303)


def test_missing_archetype_table_is_an_honest_empty_sample(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'archetypes.sqlite3'}")

    assert PlayerArchetypeRepository(engine).list_peer_ids(101) == ()
