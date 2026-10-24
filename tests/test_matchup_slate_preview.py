"""The Matchup and Slate link preview images (crf04/statsplus#320, contract
crf04/statsplus ``docs/contracts/matchup-slate-preview.md``).

The routes read real :class:`SlateEdgesService` compositions over fake Slate
and Matchup seams; the tests assert the facts each card draws (spied from the
route) and the response each contract case gets.
"""

from __future__ import annotations

import io
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw
from sqlalchemy.exc import OperationalError

from app.config.settings import RuntimeSettings
from app.errors import ProviderUnavailableError, QueryTimeoutError, ResourceNotFoundError
from app.services import matchup_slate_preview as preview
from app.services.matchup_slate_preview import (
    GameTile,
    MatchupCard,
    MatchupSlatePreviewService,
    ScoreRow,
    SlateCard,
    TeamColumn,
    draw_matchup,
    draw_slate,
    whole_percent,
)
from app.services.preview_card import DIM, HIT, LINE, MISS, TEXT, draw_fallback
from app.services.slate_edges import SlateEdgesService
from app.services.slate_service import SlateService
from tests.support.slate_catalog import NoNeighbourEvents
from tests.test_game_log_preview import DownRedis, FakeRedis
from tests.test_slate_edges import Publications, SnapshotComposer, _blend_player, _matchup

BKN, CLE = 1610612751, 1610612739
GAME_ID = "0022600105"
OTHER_GAME_ID = "0022600199"
DAY = "2026-10-24"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
CARD = "public, max-age=3600"
FALLBACK = "public, max-age=60"


def _game(game_id=GAME_ID, *, away=(BKN, "BKN"), home=(CLE, "CLE"), tip="2026-10-24T23:30:00+00:00", **extra):
    return {
        "game_id": game_id,
        "away_team": {"team_id": away[0], "tricode": away[1], "name": None},
        "home_team": {"team_id": home[0], "tricode": home[1], "name": None},
        "scheduled_at": tip,
        "status": {"state": "scheduled", "label": "Scheduled"},
        "injury_counts": {"Out": 4, "Questionable": 1},
        **extra,
    }


def _away(cid, name, value, **kwargs):
    return _blend_player(cid, name, value, team=BKN, tricode="BKN", **kwargs)


def _home(cid, name, value, **kwargs):
    return _blend_player(cid, name, value, team=CLE, tricode="CLE", **kwargs)


class Slates:
    """The Slate service seam: Slates by date, and each game's date."""

    def __init__(self, days, *, today=DAY, error=None, game_dates=None):
        self.days = days
        self.today = today
        self.error = error
        # Where the event catalog puts a game, when that is not its Slate's day.
        self.game_dates = game_dates or {}
        self.seasons = []

    def get_slate(self, requested_date=None, season=None):
        self.seasons.append(season)
        if self.error is not None:
            raise self.error
        if requested_date is not None:
            date.fromisoformat(requested_date)  # malformed dates are the test's own mistake
        day = requested_date or self.today
        later = sorted(other for other in self.days if other > day and self.days[other])
        return {
            "slate_date": day,
            "games": self.days.get(day, []),
            "next_game_date": later[0] if later else None,
            "previous_game_date": None,
        }

    def game_slate_date(self, game_id):
        if self.error is not None:
            raise self.error
        if game_id in self.game_dates:
            return self.game_dates[game_id]
        for day, games in self.days.items():
            if any(game["game_id"] == game_id for game in games):
                return day
        raise ResourceNotFoundError("The requested game was not found.")


class Matchups:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def get_matchup(self, *, game_id):
        self.calls.append(game_id)
        payload = self.payloads.get(game_id) or _matchup([])
        if isinstance(payload, Exception):
            raise payload
        return payload


@pytest.fixture
def board(dependencies):
    """Install a Slate of ``days`` and the Matchups of ``payloads`` behind the routes."""

    def install(days, payloads, *, today=DAY, error=None, game_dates=None):
        slates = Slates(days, today=today, error=error, game_dates=game_dates)
        matchups = Matchups(payloads)
        dependencies.slate_service = slates
        dependencies.slate_edges_service = SlateEdgesService(slates=slates, matchups=matchups)
        return SimpleNamespace(slates=slates, matchups=matchups)

    return install


@pytest.fixture
def drawn(monkeypatch):
    """The facts each card the routes draw was given, in order."""

    cards = []
    for name in ("draw_matchup", "draw_slate"):
        real = getattr(preview, name)

        def spy(card, real=real):
            cards.append(card)
            return real(card)

        monkeypatch.setattr(preview, name, spy)
    return cards


def _is_png(response, cache_control):
    return (
        response.status_code,
        response.mimetype,
        response.headers["Cache-Control"],
        response.data[:8],
        Image.open(io.BytesIO(response.data)).size,
    ) == (200, "image/png", cache_control, PNG_MAGIC, (1200, 630))


# -- percents -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "percent"),
    [(0.0617, 6), (-0.0551, -6), (0.025, 3), (-0.025, -3), (0.0049, 0), (-0.0049, 0), (0.005, 1)],
)
def test_a_score_is_a_whole_percent_rounded_half_up_exactly(value, percent):
    # round(2.5) would be 2: 0.025 must draw +3%.
    assert whole_percent(value) == percent


# -- the Matchup card -------------------------------------------------------------------


def test_the_matchup_card_draws_every_drawable_score_of_each_team_in_order(client, board, drawn):
    board(
        {DAY: [_game()]},
        {
            GAME_ID: _matchup(
                [
                    _away(1, "Day'Ron Sharpe", -0.03784),
                    _away(2, "Egor Dëmin", 0.025),
                    _away(3, "Julius Randle", 0.002502),
                    _away(4, "Thin Score", 0.09, thin=True),
                    _home(5, "James Harden", -0.05512, scoring=23.6),
                    _home(6, "Jarrett Allen", 0.061701),
                    # Tied scores: more Season scoring first, then the lower id.
                    _home(9, "Evan Mobley", 0.03, scoring=18.0),
                    _home(8, "Jaylon Tyson", 0.03, scoring=9.0),
                    _home(7, "Sam Merrill", 0.03, scoring=9.0),
                    _home(10, "Peyton Watson", -0.0049),
                ]
            )
        },
    )

    response = client.get(f"/api/games/matchup/preview.png?game_id={GAME_ID}&v=2026-10-24")

    assert _is_png(response, CARD)
    assert drawn == [
        MatchupCard(
            date="Saturday, Oct 24",
            away=TeamColumn(
                "BKN",
                "Nets · away",
                (
                    ScoreRow("Egor Dëmin", "BKN", 3),
                    ScoreRow("Julius Randle", "BKN", 0),
                    ScoreRow("Day'Ron Sharpe", "BKN", -4),
                ),
                0,
            ),
            home=TeamColumn(
                "CLE",
                "Cavaliers · home",
                (
                    ScoreRow("Jarrett Allen", "CLE", 6),
                    ScoreRow("Evan Mobley", "CLE", 3),
                    ScoreRow("Sam Merrill", "CLE", 3),
                    ScoreRow("Jaylon Tyson", "CLE", 3),
                    ScoreRow("Peyton Watson", "CLE", 0),
                    ScoreRow("James Harden", "CLE", -6),
                ),
                0,
            ),
            scored=9,
        )
    ]


def test_a_team_of_eleven_shows_its_five_highest_five_lowest_and_one_more(client, board, drawn):
    away = [_away(index, f"Player {index}", (6 - index) / 100) for index in range(1, 12)]
    board({DAY: [_game()]}, {GAME_ID: _matchup([*away, _home(50, "Jarrett Allen", 0.06)])})

    client.get(f"/api/games/matchup/preview.png?game_id={GAME_ID}")

    (card,) = drawn
    assert [(row.name, row.percent) for row in card.away.rows] == [
        ("Player 1", 5),
        ("Player 2", 4),
        ("Player 3", 3),
        ("Player 4", 2),
        ("Player 5", 1),
        ("Player 7", -1),
        ("Player 8", -2),
        ("Player 9", -3),
        ("Player 10", -4),
        ("Player 11", -5),
    ]
    assert (card.away.more, card.home.more, card.scored) == (1, 0, 12)


def test_a_game_without_drawable_scores_is_the_no_scores_card(client, board, drawn):
    board({DAY: [_game()]}, {GAME_ID: _matchup([_away(1, "Thin Score", 0.08, thin=True)])})

    response = client.get(f"/api/games/matchup/preview.png?game_id={GAME_ID}")

    assert _is_png(response, CARD)
    assert drawn == [
        MatchupCard(
            "Saturday, Oct 24",
            TeamColumn("BKN", "Nets · away", (), 0),
            TeamColumn("CLE", "Cavaliers · home", (), 0),
            0,
        )
    ]


def test_an_unknown_game_is_404_and_not_cached(client, board):
    board({DAY: [_game()]}, {})

    response = client.get("/api/games/matchup/preview.png?game_id=0022600999")

    assert (
        response.status_code,
        response.headers["Cache-Control"],
        response.get_json()["error"]["code"],
    ) == (
        404,
        "no-store",
        "resource_not_found",
    )


class EventCatalog:
    def __init__(self, events):
        self.events = events
        self.reads = []

    def get_event(self, season, game_id):
        self.reads.append((season, game_id))
        return self.events.get(game_id)


def test_a_game_reads_its_et_slate_date_from_the_season_its_id_names():
    last_season = "0022500105"
    catalog = EventCatalog({last_season: {"scheduled_at": "2025-10-25T02:00:00+00:00"}})
    settings = RuntimeSettings(environment="testing")
    assert settings.nba.current_season != "2025-26"
    slates = SlateService(catalog, settings=settings)

    # 22:00 ET on the 24th, a UTC date later; read from 2025-26, not the current season.
    assert slates.game_slate_date(last_season) == "2025-10-24"
    assert catalog.reads == [("2025-26", last_season)]
    with pytest.raises(ResourceNotFoundError):
        slates.game_slate_date("0022600999")


# -- the Slate card ---------------------------------------------------------------------


def _slate_game(index, **extra):
    return _game(f"00226001{index:02d}", away=(BKN, "BKN"), home=(CLE, "CLE"), **extra)


def test_a_tile_names_a_softest_or_toughest_score_only_at_two_percent_after_rounding(client, board, drawn):
    games = [_slate_game(1), _slate_game(2), _slate_game(3)]
    board(
        {DAY: games},
        {
            # 0.015 rounds to +2% and -0.015 to -2%: both are drawn.
            games[0]["game_id"]: _matchup(
                [
                    _away(1, "Keon Ellis", 0.015),
                    _home(2, "Sam Merrill", -0.015),
                    _home(3, "Thin", 0.2, thin=True),
                ]
            ),
            # 0.0149 and -0.0149 round to 1%: neither is.
            games[1]["game_id"]: _matchup(
                [_away(4, "Egor Dëmin", 0.0149), _home(5, "James Harden", -0.0149)]
            ),
        },
    )

    response = client.get(f"/api/games/slate/preview.png?date={DAY}&v=2026-10-24")

    assert _is_png(response, CARD)
    assert drawn == [
        SlateCard(
            "Saturday, Oct 24",
            (
                GameTile(
                    "BKN",
                    "CLE",
                    "7:30",
                    True,
                    ScoreRow("Keon Ellis", "BKN", 2),
                    ScoreRow("Sam Merrill", "CLE", -2),
                    4,
                ),
                GameTile("BKN", "CLE", "7:30", True, None, None, 4),
                GameTile("BKN", "CLE", "7:30", False, None, None, 4),
            ),
        )
    ]


@pytest.mark.parametrize(
    ("status", "counts", "tip", "out"),
    [
        ({"state": "scheduled", "label": "Scheduled"}, {"Out": 0}, "10:00", 0),
        ({"state": "scheduled", "label": "Q3 5:21"}, {"Out": 2}, "Live", 2),
        ({"state": "scheduled", "label": "7:30 pm ET"}, {"Out": 0}, "10:00", 0),
        ({"state": "scheduled", "label": "Halftime"}, {"Out": 0}, "Live", 0),
        ({"state": "final", "label": "Final"}, None, "Final", None),
        ({"state": "postponed", "label": "Postponed"}, {"Out": 1}, "Postponed", 1),
    ],
)
def test_a_tile_shows_the_tip_time_or_the_status_and_the_out_count(
    client, board, drawn, status, counts, tip, out
):
    game = _slate_game(1, tip="2026-10-25T02:00:00+00:00", status=status, injury_counts=counts)
    board({DAY: [game]}, {})

    client.get(f"/api/games/slate/preview.png?date={DAY}")

    assert [(tile.tip, tile.out) for tile in drawn[0].tiles] == [(tip, out)]


@pytest.mark.parametrize(("games", "out"), [(12, 4), (13, None), (15, None)])
def test_nights_of_thirteen_or_more_games_leave_the_out_line_off(client, board, drawn, games, out):
    board({DAY: [_slate_game(index) for index in range(games)]}, {})

    client.get(f"/api/games/slate/preview.png?date={DAY}")

    assert len(drawn[0].tiles) == games
    assert {tile.out for tile in drawn[0].tiles} == {out}


def test_a_date_without_games_is_the_no_games_card(client, board, drawn):
    board({}, {})

    response = client.get("/api/games/slate/preview.png?date=2026-10-24")

    assert _is_png(response, CARD)
    assert drawn == [SlateCard("Saturday, Oct 24", ())]


def test_without_a_date_the_card_is_the_day_the_slate_page_shows(client, board, drawn):
    # Today has no games: like the Slate page, the next game day.
    board({"2026-10-27": [_slate_game(1)]}, {}, today="2026-10-25")

    client.get("/api/games/slate/preview.png")

    assert (drawn[0].date, len(drawn[0].tiles)) == ("Tuesday, Oct 27", 1)


# -- no text overlaps --------------------------------------------------------------------


@pytest.fixture
def layout(monkeypatch):
    """Every text box, and every score bar, the cards draw."""

    texts, bars, axes = [], [], []
    real_text, real_rounded = ImageDraw.ImageDraw.text, ImageDraw.ImageDraw.rounded_rectangle
    real_rectangle = ImageDraw.ImageDraw.rectangle

    def text(self, xy, content, fill=None, font=None, anchor=None, **kwargs):
        texts.append((content, self.textbbox(xy, content, font=font, anchor=anchor), fill))
        return real_text(self, xy, content, fill, font, anchor, **kwargs)

    def rounded(self, xy, *args, **kwargs):
        if kwargs.get("fill") in (HIT, MISS):
            bars.append((tuple(xy), kwargs["fill"]))
        return real_rounded(self, xy, *args, **kwargs)

    def rectangle(self, xy, *args, **kwargs):
        # A score row's centre axis: a short, narrow rule.
        left, top, right, bottom = xy
        if kwargs.get("fill") == LINE and right - left <= 3 and bottom - top < 80:
            axes.append(tuple(xy))
        return real_rectangle(self, xy, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", text)
    monkeypatch.setattr(ImageDraw.ImageDraw, "rounded_rectangle", rounded)
    monkeypatch.setattr(ImageDraw.ImageDraw, "rectangle", rectangle)
    return SimpleNamespace(texts=texts, bars=bars, axes=axes)


def _overlaps(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _collisions(layout):
    boxes = [(text, box) for text, box, _ in layout.texts if text.strip()]
    found = [
        (first[0], second[0])
        for index, first in enumerate(boxes)
        for second in boxes[index + 1 :]
        if _overlaps(first[1], second[1])
    ]
    found += [(text, "bar") for text, box in boxes for bar, _ in layout.bars if _overlaps(box, bar)]
    outside = [text for text, box in boxes if box[0] < 0 or box[1] < 0 or box[2] > 1200 or box[3] > 630]
    return found, outside


LONG_NAMES = ("Nickeil Alexander-Walker", "Shai Gilgeous-Alexander", "Giannis Antetokounmpo")


def test_a_fifteen_game_slate_draws_no_text_over_other_text(layout):
    tiles = tuple(
        GameTile(
            "BKN",
            "CLE",
            ["7:30", "10:00", "Final", "Live", "Postponed"][index % 5],
            index % 7 != 3,
            ScoreRow(LONG_NAMES[index % 3], "BKN", 12),
            ScoreRow(LONG_NAMES[(index + 1) % 3], "CLE", -11) if index % 2 else None,
            None,
        )
        for index in range(15)
    )

    png = draw_slate(SlateCard("Wednesday, Dec 25", tiles))

    assert Image.open(io.BytesIO(png)).size == (1200, 630)
    assert _collisions(layout) == ([], [])


@pytest.mark.parametrize("out", [0, 4])
@pytest.mark.parametrize("count", [8, 12])
def test_an_eight_and_a_twelve_game_slate_draw_no_text_over_other_text(layout, count, out):
    tiles = tuple(
        GameTile(
            "BKN",
            "CLE",
            "10:00",
            True,
            ScoreRow(LONG_NAMES[0], "BKN", 9),
            ScoreRow(LONG_NAMES[1], "CLE", -9),
            out,
        )
        for _ in range(count)
    )

    draw_slate(SlateCard("Saturday, Oct 24", tiles))

    assert _collisions(layout) == ([], [])
    # Long names are cut short rather than run into the tricode or percent.
    names = {text for text, _, _ in layout.texts if text.split()[0] in ("Nickeil", "Shai")}
    assert len(names) == 2
    assert all(
        text.endswith("…") and any(name.startswith(text[:-1]) for name in LONG_NAMES) for text in names
    )


def test_a_twelve_per_team_matchup_draws_no_name_over_a_bar_or_other_text(layout):
    rows = tuple(ScoreRow(LONG_NAMES[index % 3], "BKN", 11 - 2 * index) for index in range(10))
    column = TeamColumn("BKN", "Trail Blazers · away", rows, 2)

    png = draw_matchup(MatchupCard("Wednesday, Dec 25", column, column, 24))

    assert Image.open(io.BytesIO(png)).size == (1200, 630)
    assert len(layout.bars) == 20
    assert _collisions(layout) == ([], [])


# -- parameters -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/api/games/slate/preview.png?date=2026-10-24&fallback=next_game_day",
        "/api/games/slate/preview.png?utm_source=x",
        "/api/games/slate/preview.png?date=2026-10-24&date=2026-10-25",
        "/api/games/slate/preview.png?date=2026-13-01",
        "/api/games/slate/preview.png?date=10/24/2026",
        "/api/games/matchup/preview.png",
        "/api/games/matchup/preview.png?game_id=22600105",
        "/api/games/matchup/preview.png?game_id=0022600105x",
        "/api/games/matchup/preview.png?game_id=0022600105&player=7",
    ],
)
def test_a_malformed_or_unknown_parameter_is_400_and_not_cached(client, dependencies, path):
    slates = SlateService(EventCatalog({}), settings=dependencies.settings)
    dependencies.slate_service = slates
    dependencies.slate_edges_service = SlateEdgesService(slates=slates, matchups=Matchups({}))

    response = client.get(path)

    assert (
        response.status_code,
        response.headers["Cache-Control"],
        response.get_json()["error"]["code"],
    ) == (
        400,
        "no-store",
        "invalid_input",
    )


def test_the_61st_request_from_one_ip_in_a_minute_is_429(client, board):
    board({DAY: [_game()]}, {})
    proxied = {"X-Forwarded-For": "203.0.113.9"}

    statuses = [
        client.get(f"/api/games/slate/preview.png?date={DAY}", headers=proxied).status_code for _ in range(60)
    ]
    limited = client.get(f"/api/games/matchup/preview.png?game_id={GAME_ID}", headers=proxied)

    assert statuses == [200] * 60
    assert (limited.status_code, limited.headers["Cache-Control"]) == (429, "no-store")
    assert 1 <= int(limited.headers["Retry-After"]) <= 60


# -- failures and the render budget -----------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        ProviderUnavailableError("The NBA schedule is not available."),
        OperationalError("SELECT events", {}, Exception("database is down")),
    ],
)
@pytest.mark.parametrize(
    "path", [f"/api/games/slate/preview.png?date={DAY}", f"/api/games/matchup/preview.png?game_id={GAME_ID}"]
)
def test_a_provider_or_database_failure_is_the_short_lived_fallback(client, board, failure, path):
    board({DAY: [_game()]}, {}, error=failure)

    response = client.get(path)

    assert _is_png(response, FALLBACK)
    assert response.data == draw_fallback()


@pytest.mark.parametrize(
    "failure",
    [
        ProviderUnavailableError("The matchup schedule is currently unavailable."),
        QueryTimeoutError(),
        OperationalError("SELECT matchup", {}, Exception("database is down")),
    ],
    ids=["provider", "query timeout", "database"],
)
def test_one_games_failed_matchup_read_is_the_fallback_not_a_card_without_it(client, board, failure):
    broken, fine = _slate_game(1), _slate_game(2)
    board(
        {DAY: [broken, fine]},
        {broken["game_id"]: failure, fine["game_id"]: _matchup([_away(1, "Keon Ellis", 0.03)])},
    )

    slate = client.get(f"/api/games/slate/preview.png?date={DAY}")
    broken_game = client.get(f"/api/games/matchup/preview.png?game_id={broken['game_id']}")
    fine_game = client.get(f"/api/games/matchup/preview.png?game_id={fine['game_id']}")
    edges = client.get(f"/api/games/slate/edges?date={DAY}").get_json()

    assert _is_png(slate, FALLBACK)
    assert slate.data == draw_fallback()
    assert _is_png(broken_game, FALLBACK)
    assert broken_game.data == draw_fallback()
    assert _is_png(fine_game, CARD)
    # The JSON still degrades per game.
    assert edges["games"][0] == {"game_id": broken["game_id"], "effects": [], "scores": []}


def test_a_game_outside_the_matchup_window_is_a_game_without_scores(client, board, drawn):
    game = _slate_game(1)
    board({DAY: [game]}, {game["game_id"]: ResourceNotFoundError("outside the Regular Season window")})

    slate = client.get(f"/api/games/slate/preview.png?date={DAY}")
    matchup = client.get(f"/api/games/matchup/preview.png?game_id={game['game_id']}")

    assert (_is_png(slate, CARD), _is_png(matchup, CARD)) == (True, True)
    assert (drawn[0].tiles[0].scored, drawn[1].scored) == (False, 0)


class FailingComposer(SnapshotComposer):
    def get_matchup_from_snapshot(self, *, game_id, **kwargs):
        self.calls.append(game_id)
        raise ProviderUnavailableError("The matchup schedule is currently unavailable.")


def test_a_cached_edges_response_with_a_failed_game_is_still_the_fallback(app, client, dependencies):
    days, _composer = _budgeted(app, dependencies, FakeRedis())
    composer = FailingComposer({})
    dependencies.slate_edges_service.matchups = composer

    first = _render(client, days[0], 1)
    again = _render(client, days[0], 2)

    assert (_is_png(first, FALLBACK), _is_png(again, FALLBACK)) == (True, True)
    # The second answer came from the edges cache, which kept the failure.
    assert composer.calls == [GAME_ID]


def _budgeted(app, dependencies, redis_client):
    """Edges cached in ``redis_client``, and a preview service on one fixed minute."""

    days = {(date(2026, 11, 1) + timedelta(days=n)).isoformat(): [_game()] for n in range(70)}
    # The last two days: one game no other day has, and no games.
    *_, other_day, empty_day = sorted(days)
    days[other_day], days[empty_day] = [_game(OTHER_GAME_ID)], []
    slates = Slates(days)
    dependencies.slate_service = slates
    composer = SnapshotComposer(
        {
            GAME_ID: _matchup([_away(1, "Keon Ellis", 0.03)]),
            OTHER_GAME_ID: _matchup([_away(2, "Egor Dëmin", 0.03)]),
        }
    )
    dependencies.slate_edges_service = SlateEdgesService(
        slates=slates,
        matchups=composer,
        publication_reader=Publications(),
        injuries=object(),
        settings=RuntimeSettings(environment="testing", nba={"current_season": "2025-26"}),
        redis_client=redis_client,
    )
    app.extensions["matchup_slate_previews"] = MatchupSlatePreviewService(
        edges=dependencies.slate_edges_service,
        slates=slates,
        redis_client=redis_client,
        clock=lambda: 1_800_000_000.0,
    )
    return sorted(days), composer


def _render(client, day, caller):
    # Each caller its own IP, so only the global budget can refuse.
    return client.get(
        f"/api/games/slate/preview.png?date={day}", headers={"X-Forwarded-For": f"198.51.100.{caller}"}
    )


def test_past_sixty_uncached_renders_a_minute_the_answer_is_the_fallback(app, client, dependencies):
    days, composer = _budgeted(app, dependencies, FakeRedis())

    statuses = [_render(client, day, n).headers["Cache-Control"] for n, day in enumerate(days[:60])]
    over = _render(client, days[60], 61)
    # A day the edges cache already holds is no uncached render.
    cached = _render(client, days[0], 62)
    matchup = client.get(
        f"/api/games/matchup/preview.png?game_id={GAME_ID}", headers={"X-Forwarded-For": "198.51.100.63"}
    )

    # The Matchup route shares the spent budget: its game's day is not cached.
    uncached_matchup = client.get(
        f"/api/games/matchup/preview.png?game_id={OTHER_GAME_ID}",
        headers={"X-Forwarded-For": "198.51.100.64"},
    )
    # A day without games composes nothing, so it is a card even now.
    empty = _render(client, days[-1], 65)

    assert statuses == [CARD] * 60
    assert _is_png(over, FALLBACK)
    assert over.data == draw_fallback()
    assert _is_png(cached, CARD)
    # The Matchup card reads the first day's cached edges too.
    assert _is_png(matchup, CARD)
    assert _is_png(uncached_matchup, FALLBACK)
    assert _is_png(empty, CARD)
    assert len(composer.calls) == 60


def test_the_matchup_route_spends_the_budget_the_slate_route_then_cannot_use(app, client, dependencies):
    days, _composer = _budgeted(app, dependencies, None)

    statuses = [
        client.get(
            f"/api/games/matchup/preview.png?game_id={GAME_ID}",
            headers={"X-Forwarded-For": f"198.51.100.{n}"},
        ).headers["Cache-Control"]
        for n in range(15)
    ]
    slate = _render(client, days[1], 16)
    empty = _render(client, days[-1], 17)

    assert statuses == [CARD] * 15
    assert _is_png(slate, FALLBACK)
    assert _is_png(empty, CARD)


@pytest.mark.parametrize("unavailable", [None, DownRedis()], ids=["not configured", "down"])
def test_without_redis_each_process_renders_15_a_minute(app, client, dependencies, unavailable):
    days, _composer = _budgeted(app, dependencies, unavailable)

    statuses = [_render(client, days[0], n).headers["Cache-Control"] for n in range(15)]
    over = _render(client, days[0], 16)

    assert statuses == [CARD] * 15
    assert _is_png(over, FALLBACK)


# -- what each card draws ----------------------------------------------------------------------


def _drawn_text(layout):
    return [(text, fill) for text, _, fill in layout.texts]


def _bar_sides(layout):
    """Each bar as (the name on its row, the side of its axis it grows to, its colour)."""

    names = {text: box for text, box, fill in layout.texts if fill == TEXT}
    sides = []
    for (left, top, right, bottom), fill in layout.bars:
        middle = (top + bottom) / 2
        axis = min(
            (axis for axis in layout.axes if axis[1] <= middle <= axis[3]),
            key=lambda axis: min(abs(axis[0] - right), abs(axis[2] - left)),
        )
        side = "right" if left >= axis[2] else "left" if right <= axis[0] else "across"
        # The row's name: on the bar's line, the nearest to its left.
        name = max(
            (text for text, box in names.items() if box[1] <= middle <= box[3] and box[2] <= left),
            key=lambda text: names[text][2],
        )
        sides.append((name, side, fill))
    return sides


def test_the_matchup_card_draws_signed_percents_in_their_colours_and_bars_on_their_side(layout):
    away = TeamColumn(
        "BKN",
        "Nets · away",
        (
            ScoreRow("Egor Dëmin", "BKN", 3),
            ScoreRow("Julius Randle", "BKN", 0),
            ScoreRow("Day'Ron Sharpe", "BKN", -4),
        ),
        0,
    )
    home = TeamColumn("CLE", "Cavaliers · home", (ScoreRow("Jarrett Allen", "CLE", 6),), 1)

    draw_matchup(MatchupCard("Saturday, Oct 24", away, home, 5))

    assert _drawn_text(layout) == [
        ("CourtAI", TEXT),
        ("Saturday, Oct 24 · Matchup", DIM),
        ("BKN", TEXT),
        ("Nets · away", DIM),
        ("Egor Dëmin", TEXT),
        ("+3%", HIT),
        ("Julius Randle", TEXT),
        ("0%", DIM),
        ("Day'Ron Sharpe", TEXT),
        ("-4%", MISS),
        ("CLE", TEXT),
        ("Cavaliers · home", DIM),
        ("Jarrett Allen", TEXT),
        ("+6%", HIT),
        ("+1 more players", DIM),
        ("Points Matchup Score vs league · all 5 scored players", DIM),
        ("tougher", MISS),
        ("·", DIM),
        ("softer", HIT),
    ]
    # 0% draws no bar; the card's largest percent fills its half.
    assert _bar_sides(layout) == [
        ("Egor Dëmin", "right", HIT),
        ("Day'Ron Sharpe", "left", MISS),
        ("Jarrett Allen", "right", HIT),
    ]
    (allen, _), (sharpe, _) = layout.bars[2], layout.bars[1]
    assert round(allen[2] - allen[0]) == round(1.5 * (sharpe[2] - sharpe[0]))


def test_the_no_scores_card_says_so(layout):
    draw_matchup(
        MatchupCard(
            "Saturday, Oct 24",
            TeamColumn("BKN", "Nets · away", (), 0),
            TeamColumn("CLE", "Cavaliers · home", (), 0),
            0,
        )
    )

    assert _drawn_text(layout) == [
        ("CourtAI", TEXT),
        ("Saturday, Oct 24 · Matchup", DIM),
        ("BKN", TEXT),
        ("Nets · away", DIM),
        ("CLE", TEXT),
        ("Cavaliers · home", DIM),
        ("No Matchup Scores yet", DIM),
        ("Points Matchup Score vs league", DIM),
    ]
    assert layout.bars == []


def test_the_no_games_card_names_the_date(layout):
    draw_slate(SlateCard("Saturday, Oct 24", ()))

    assert _drawn_text(layout) == [
        ("CourtAI", TEXT),
        ("Saturday, Oct 24", DIM),
        ("No games on Saturday, Oct 24", DIM),
    ]


def test_the_slate_card_draws_each_tile_in_order_with_its_edges_and_out_line(layout):
    draw_slate(
        SlateCard(
            "Saturday, Oct 24",
            (
                GameTile(
                    "MIA",
                    "CHA",
                    "6:00",
                    True,
                    ScoreRow("Naz Reid", "CHA", 4),
                    ScoreRow("Bam Adebayo", "MIA", -3),
                    0,
                ),
                GameTile("BKN", "CLE", "Final", True, None, ScoreRow("James Harden", "CLE", -6), 4),
                GameTile("SAS", "DAL", "8:00", False, None, None, None),
            ),
        )
    )

    assert _drawn_text(layout) == [
        ("CourtAI", TEXT),
        ("Saturday, Oct 24 · 3 games · ET", DIM),
        ("MIA", TEXT),
        ("@", DIM),
        ("CHA", TEXT),
        ("6:00", DIM),
        ("+4%", HIT),
        ("Naz Reid", TEXT),
        ("CHA", DIM),
        ("-3%", MISS),
        ("Bam Adebayo", TEXT),
        ("MIA", DIM),
        ("No one Out", DIM),
        ("BKN", TEXT),
        ("@", DIM),
        ("CLE", TEXT),
        ("Final", DIM),
        ("nothing above +2%", DIM),
        ("-6%", MISS),
        ("James Harden", TEXT),
        ("CLE", DIM),
        ("4 listed Out", MISS),
        ("SAS", TEXT),
        ("@", DIM),
        ("DAL", TEXT),
        ("8:00", DIM),
        ("No scores yet", DIM),
        ("Softest and toughest points Matchup Score per game, vs league", DIM),
    ]


def test_slate_tiles_fill_four_columns_left_to_right_then_the_next_row(layout):
    away = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    tiles = tuple(GameTile(tricode, "ZZZ", "7:30", False, None, None, None) for tricode in away)

    draw_slate(SlateCard("Saturday, Oct 24", tiles))

    boxes = {text: box for text, box, _ in layout.texts if text in away}
    lefts = [boxes[tricode][0] for tricode in away[:4]]
    assert lefts == sorted(set(lefts))
    assert {boxes[tricode][1] for tricode in away[:4]} == {boxes["AAA"][1]}
    assert (boxes["EEE"][0], boxes["EEE"][1] > boxes["AAA"][3]) == (boxes["AAA"][0], True)


# -- round-2 review ---------------------------------------------------------------------------


def test_scores_are_drawn_in_raw_score_order_not_rounded_percent_order(client, board, layout):
    # Both draw +3%; the higher raw score comes first despite fewer points per game.
    board(
        {DAY: [_game()]},
        {
            GAME_ID: _matchup(
                [_away(2, "High Scorer", 0.029, scoring=30.0), _away(1, "Low Scorer", 0.031, scoring=10.0)]
            )
        },
    )

    client.get(f"/api/games/matchup/preview.png?game_id={GAME_ID}")

    rows = [(text, fill) for text, _, fill in layout.texts if text in ("High Scorer", "Low Scorer", "+3%")]
    assert rows == [("Low Scorer", TEXT), ("+3%", HIT), ("High Scorer", TEXT), ("+3%", HIT)]


def test_a_catalog_game_missing_from_its_slate_is_404(client, board):
    board({DAY: [_game()]}, {}, game_dates={OTHER_GAME_ID: DAY})

    response = client.get(f"/api/games/matchup/preview.png?game_id={OTHER_GAME_ID}")

    assert (
        response.status_code,
        response.headers["Cache-Control"],
        response.get_json()["error"]["code"],
    ) == (
        404,
        "no-store",
        "resource_not_found",
    )


def test_a_catalog_game_missing_from_its_slate_is_404_even_past_the_budget(app, client, board, dependencies):
    board({DAY: [_game()]}, {}, game_dates={OTHER_GAME_ID: DAY})
    app.extensions["matchup_slate_previews"] = MatchupSlatePreviewService(
        edges=dependencies.slate_edges_service,
        slates=dependencies.slate_service,
        redis_client=None,
        clock=lambda: 1_800_000_000.0,
    )
    for caller in range(15):
        _render(client, DAY, caller)
    spent = _render(client, DAY, 15)

    response = client.get(
        f"/api/games/matchup/preview.png?game_id={OTHER_GAME_ID}",
        headers={"X-Forwarded-For": "198.51.100.16"},
    )

    assert _is_png(spent, FALLBACK)
    assert (response.status_code, response.headers["Cache-Control"]) == (404, "no-store")


class SeasonCatalog(NoNeighbourEvents):
    """An Event Catalog holding the given events of each season."""

    def __init__(self, events):
        self.events = events
        self.reads = []

    def get_event(self, season, game_id):
        self.reads.append(("event", season))
        return next((event for event in self.events.get(season, []) if event["nba_game_id"] == game_id), None)

    def count_events(self, season):
        return len(self.events.get(season, []))

    def get_freshness(self, season, *, now):
        return {"last_success_at": "2025-10-24T10:00:00+00:00"}

    def get_events_between(self, season, starts_at, ends_at):
        self.reads.append(("slate", season))
        return [
            event
            for event in self.events.get(season, [])
            if starts_at <= datetime.fromisoformat(event["scheduled_at"]) < ends_at
        ]


def _event(game_id, scheduled_at):
    return {
        "nba_game_id": game_id,
        "scheduled_at": scheduled_at,
        "status_text": "Final",
        "status_code": 3,
        "classification": "Regular Season",
        "away_team": {"id": BKN, "name": "Brooklyn Nets", "tricode": "BKN"},
        "home_team": {"id": CLE, "name": "Cleveland Cavaliers", "tricode": "CLE"},
    }


def test_a_game_of_another_season_draws_while_the_current_season_is_pinned(client, dependencies, drawn):
    last_season = "0022500105"
    catalog = SeasonCatalog(
        {
            "2025-26": [_event(last_season, "2025-10-24T23:30:00+00:00")],
            "2026-27": [_event("0022600105", "2026-10-24T23:30:00+00:00")],
        }
    )
    pinned = RuntimeSettings(environment="testing", nba={"current_season": "2026-27"})
    slates = SlateService(catalog, settings=pinned)
    dependencies.slate_service = slates
    dependencies.slate_edges_service = SlateEdgesService(
        slates=slates, matchups=Matchups({last_season: _matchup([_away(1, "Keon Ellis", 0.03)])})
    )

    response = client.get(f"/api/games/matchup/preview.png?game_id={last_season}")

    assert _is_png(response, CARD)
    assert (drawn[0].date, drawn[0].away.rows) == ("Friday, Oct 24", (ScoreRow("Keon Ellis", "BKN", 3),))
    assert catalog.reads == [("event", "2025-26"), ("slate", "2025-26")]
