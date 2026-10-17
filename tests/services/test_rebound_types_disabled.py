"""A disabled Rebound Types stream serves nothing, however complete the facts."""

from __future__ import annotations

from app.services.collection_control import PublicationService
from app.services.database_first_activation import DatabaseFirstPublicationReader
from app.services.team_matchup_query import TeamMatchupQueryService
from tests.services.test_ledger_matchup_materialization import (
    RETRIEVED_AT,
    _engine,
    _league_games,
    _materialize,
    _season_scope,
)


def test_composed_facts_are_not_served_while_the_streams_are_disabled(tmp_path):
    engine = _engine(tmp_path, "disabled.sqlite3")
    publications = PublicationService(engine, clock=lambda: RETRIEVED_AT)
    publications.register_default_streams()
    _service, repository, _result = _materialize(engine, _league_games())
    reader = DatabaseFirstPublicationReader(engine, clock=lambda: RETRIEVED_AT)

    # The facts exist and are complete, and the streams are genuinely disabled.
    stored = TeamMatchupQueryService(repository, clock=lambda: RETRIEVED_AT).get_window(_season_scope())
    assert len([m for m in stored.team_metrics[1] if m.base == "rebound_types"]) == 6
    assert publications.stream_enabled("rebound_types_season") is False

    window = TeamMatchupQueryService(
        repository, clock=lambda: RETRIEVED_AT, publication_reader=reader
    ).get_window(_season_scope())

    observation = next(o for o in window.observations if o.surface == "rebound_types")
    assert observation.status in {"missing", "unavailable"}
    assert [m for m in window.team_metrics[1] if m.base == "rebound_types"] == []
    assert [m for m in window.league_metrics if m.base == "rebound_types"] == []
    # The bases that do fall back to the ledger facts are still served.
    assert [m.slice_key for m in window.team_metrics[1] if m.base == "traditional"].count("OPP_REB") == 1
