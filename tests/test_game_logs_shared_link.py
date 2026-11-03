"""A shared Log Workspace link on ``/api/games/game_logs`` (crf04/statsplus#381).

Anyone can read the game logs; only a signed-in, admitted caller sees the
posted line's provider and sportsbook prices.  Anonymous callers are limited
to 30 reads a minute per IP.  The route runs over the real ``GameService`` and
``PostedLinesReader``, with the pool and sportsbook quotes stubbed.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine

import app.utils.auth as auth
from app.migrations import run_migrations
from app.services.user_service import UserService
from tests.test_game_logs_posted_line import LEBRON, TONIGHT, URL, Catalog, Pools, _serve

BOOKS = {
    "PTS": {
        "as_of": "2026-10-21T21:04:11Z",
        "closing": False,
        "books": [
            {
                "book": "draftkings",
                "line": 27.5,
                "over": -115,
                "under": -105,
                "updated_at": "2026-10-21T21:02:40Z",
                "best_over": False,
                "best_under": True,
            },
            {
                "book": "fanduel",
                "line": 27.5,
                "over": -110,
                "under": -110,
                "updated_at": "2026-10-21T21:03:02Z",
                "best_over": True,
                "best_under": False,
            },
        ],
    }
}

UNPRICED = {
    "game_id": "0022600012",
    "game_date": "2026-10-21",
    "matchup": "LAL vs GSW",
    "lines": {"PTS": {"value": 27.5, "provider": "prizepicks"}},
    "provider_lines": {"prizepicks": {"PTS": 27.5}, "underdog": {"PTS": 27.5}},
    "book_lines": {
        "PTS": {
            "as_of": "2026-10-21T21:04:11Z",
            "closing": False,
            "books": [
                {"book": "draftkings", "line": 27.5, "updated_at": "2026-10-21T21:02:40Z"},
                {"book": "fanduel", "line": 27.5, "updated_at": "2026-10-21T21:03:02Z"},
            ],
        }
    },
}


class Sportsbooks:
    def book_lines(self, game_id):
        return {LEBRON: BOOKS} if game_id == "0022600012" else {}


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'logs.db'}")
    run_migrations(engine)
    return engine


@pytest.fixture
def served(dependencies, engine):
    pools = Pools(
        {"0022600012": {"prizepicks": {"PTS": 27.5}, "underdog": {"PTS": 27.5}}},
        prices={"0022600012": {"underdog": {"PTS": {"over": -112, "under": -108}}}},
    )
    _serve(dependencies, engine, catalog=Catalog(TONIGHT), pools=pools, sportsbook=Sportsbooks())


@pytest.fixture(autouse=True)
def member(authenticate):
    """Firebase is configured, so no token means no user rather than the local bypass."""
    return authenticate()


@pytest.fixture
def waitlisted(authenticate, make_db_user):
    return authenticate(db_user=make_db_user(access_status="waitlisted"))


def test_an_anonymous_link_shows_the_logs_and_lines_without_prices(client, served):
    response = client.get(URL)

    assert response.status_code == 200
    body = response.get_json()
    assert [game["PTS"] for game in body["game_logs"]] == [27, 22, 31]
    assert body["posted_line"] == UNPRICED


def test_a_signed_in_caller_sees_every_price(client, served, member):
    response = client.get(URL, headers=member)

    assert response.status_code == 200
    assert response.get_json()["posted_line"] == {
        **UNPRICED,
        "line_prices": {"underdog": {"PTS": {"over": -112, "under": -108}}},
        "book_lines": BOOKS,
    }


def test_a_waitlisted_caller_sees_the_logs_without_prices(client, served, waitlisted):
    response = client.get(URL, headers=waitlisted)

    assert response.status_code == 200
    body = response.get_json()
    assert [game["PTS"] for game in body["game_logs"]] == [27, 22, 31]
    assert body["posted_line"] == UNPRICED


class UnreachableAccessStatus(UserService):
    """A signed-in caller with no synced row whose waitlist lookup fails."""

    def __init__(self):
        pass

    def create_or_update_user(self, user_data):
        return None

    def resolve_access_status(self, uid):
        raise RuntimeError("waitlist database unavailable")


def test_a_caller_whose_waitlist_status_cannot_be_read_sees_no_prices(client, served, member, monkeypatch):
    monkeypatch.setattr(
        auth, "get_dependencies", lambda: SimpleNamespace(user_service=UnreachableAccessStatus())
    )

    response = client.get(URL, headers=member)

    assert response.status_code == 200
    assert response.get_json()["posted_line"] == UNPRICED


def test_a_rejected_token_reads_the_shared_link_view(client, served, monkeypatch):
    def reject(token):
        raise ValueError("invalid signature")

    monkeypatch.setattr(auth, "verify_firebase_token", reject)

    response = client.get(URL, headers={"Authorization": "Bearer forged"})

    assert response.status_code == 200
    assert response.get_json()["posted_line"] == UNPRICED


def test_the_31st_anonymous_read_in_a_minute_is_refused_but_a_signed_in_caller_is_not(client, served, member):
    ip = {"X-Forwarded-For": "198.51.100.30"}

    anonymous = [client.get(URL, headers=ip).status_code for _ in range(31)]
    signed_in = [client.get(URL, headers={**ip, **member}).status_code for _ in range(40)]

    assert anonymous == [200] * 30 + [429]
    assert signed_in == [200] * 40
    assert client.get(URL, headers={"X-Forwarded-For": "198.51.100.31"}).status_code == 200
