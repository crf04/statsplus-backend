"""A cancelled statement on the game-log and archetype reads is a 503, not "absent".

These reads used to turn any database error into ``False``, ``None`` or ``[]``,
which the route served as "no publication" or an empty list with a 200.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError

from app.errors import QueryTimeoutError
from app.services.player_game_log_repository import PlayerGameLogRepository
from app.services.player_service import PlayerService


class _QueryCanceled(Exception):
    pgcode = "57014"


def _timeout() -> OperationalError:
    return OperationalError("SELECT 1", {}, _QueryCanceled("statement timeout"))


def _unavailable() -> OperationalError:
    return OperationalError("SELECT 1", {}, Exception("connection refused"))


class _Reader:
    def __init__(self, error):
        self.error = error

    def read(self, *args, **kwargs):
        raise self.error

    def snapshot_player_game_logs(self, **kwargs):
        raise self.error


def _repository(error) -> PlayerGameLogRepository:
    repository = object.__new__(PlayerGameLogRepository)
    repository._publication_reader = _Reader(error)
    return repository


@pytest.mark.parametrize(
    "call",
    [
        lambda repository: repository.has_complete_publication("2025-26"),
        lambda repository: repository.read_publication_snapshot("2025-26"),
    ],
)
def test_game_log_publication_reads_raise_a_statement_timeout(call):
    with pytest.raises(QueryTimeoutError):
        call(_repository(_timeout()))


def test_game_log_publication_reads_still_degrade_on_other_database_errors():
    repository = _repository(_unavailable())

    assert repository.read_publication_snapshot("2025-26") is None


def _player_service(error) -> PlayerService:
    service = object.__new__(PlayerService)

    def fail(table):
        raise error

    service._fetch_data_from_table = fail
    return service


def test_archetype_reads_raise_a_statement_timeout():
    with pytest.raises(QueryTimeoutError):
        _player_service(_timeout())._get_archetype_players_from_player("Anyone")
    with pytest.raises(QueryTimeoutError):
        _player_service(_timeout())._get_archetype_gamelogs("Anyone", "BOS")


def test_archetype_reads_still_return_empty_on_other_errors():
    service = _player_service(_unavailable())

    assert service._get_archetype_players_from_player("Anyone") == []
