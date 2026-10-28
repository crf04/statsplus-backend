"""CourtAI reports: publish, read, meta, preview, delete and takedown (crf04/statsplus#342),
and the Research feed: listing, handles, Featured and author pages (crf04/statsplus#372).

The routes run against the real :class:`ReportService` on a migrated SQLite
database, so ownership, soft deletes and the author join are exercised rather
than stubbed.
"""

from __future__ import annotations

import copy
import io
import json
from datetime import UTC, datetime, timedelta

import pytest
from PIL import Image
from sqlalchemy import create_engine, text

from app.migrations import run_migrations
from app.models.report import Report
from app.models.user import User
from app.services.reports import ReportService, describe

OWNER = "owner-uid"
OTHER = "other-uid"
ADMIN = "admin-uid"

NOT_FOUND = {"error": {"code": "resource_not_found", "message": "This report isn't available."}}
REMOVED_BY_AUTHOR = {"error": "removed", "by": "author", "message": "This report was removed by its author."}
REMOVED_BY_TAKEDOWN = {"error": "removed", "by": "takedown", "message": "This report was taken down."}


def _call(call_id, card="game-logs", tool="query_game_logs"):
    return {
        "id": call_id,
        "tool": tool,
        "args": {"player": "Jalen Brunson"},
        "structured": {"rows": [{"PTS": 31}]},
        "meta": {"data_as_of": "2026-10-24"},
        "card": card,
    }


def _body(**report_overrides):
    report = {
        "title": "Injury brief: Friday, Oct 24",
        "date": "2026-10-24",
        "opening": ["Three games carry a real absence."],
        "skill": {"name": "courtai-injury-brief", "url": "https://courtai.app/connect#injury-brief"},
        "footnote": "Facts about past games only.",
        "tabs": [
            {
                "title": "Games",
                "layout": "tiles",
                "items": [{"title": "NYK @ BOS", "tag": "strong", "cards": ["c1"]}],
            },
            {
                "title": "Spots",
                "layout": "rows",
                "items": [
                    {
                        "title": "Brunson points",
                        "tag": "lean",
                        "subtitle": "Plays",
                        "key": "beat 9 of 12",
                        "body": ["He scored more without Randle."],
                        "bullets": [["Sample", "12 games"]],
                        "watch": "Minutes cap",
                        "cards": ["c1", "c2"],
                        "sources": [["Official report", "https://official.nba.com/injury-report"]],
                    },
                    {"title": "Hart rebounds", "tag": "lean"},
                ],
            },
            {"title": "Set aside", "layout": "list", "items": [{"title": "Tatum", "tag": "pass"}]},
        ],
    }
    report.update(report_overrides)
    return {
        "report": report,
        "calls": [_call("c1"), _call("c2", card="matchup", tool="get_matchup")],
        "cards": {"game-logs": "<html>logs</html>", "matchup": "<html>matchup</html>"},
        "show_author": False,
    }


@pytest.fixture
def report_engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'reports.db'}")
    run_migrations(engine)
    with engine.begin() as connection:
        for uid in (OWNER, OTHER, ADMIN):
            connection.execute(
                User.__table__.insert(),
                {
                    "firebase_uid": uid,
                    "email": f"{uid}@example.com",
                    "display_name": f"Google name of {uid}",
                    "created_at": datetime(2026, 10, 1, tzinfo=UTC),
                    "last_login": datetime(2026, 10, 1, tzinfo=UTC),
                    "is_active": True,
                },
            )
    yield engine
    engine.dispose()


class Clock:
    """A settable clock that moves one second per reading, so times never tie."""

    def __init__(self):
        self.now = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)

    def __call__(self):
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def reports(report_engine, dependencies, clock):
    ids = (f"rep{number:07d}" for number in range(1, 10_000))
    service = ReportService(report_engine, clock=clock, new_id=lambda: next(ids))
    dependencies.report_service = service
    return service


@pytest.fixture
def signed_in(authenticate, reports):
    """Headers for a signed-in account: ``signed_in(uid, admin=False, waitlisted=False)``."""

    def _signed_in(uid=OWNER, *, admin=False, waitlisted=False):
        db_user = (
            User(firebase_uid=uid, email=f"{uid}@example.com", access_status="waitlisted")
            if waitlisted
            else None
        )
        return authenticate(claims={"uid": uid, **({"admin": True} if admin else {})}, db_user=db_user)

    return _signed_in


def _publish(client, headers, body=None):
    return client.post("/api/user/reports", json=body if body is not None else _body(), headers=headers)


def _published_id(client, headers, body=None):
    response = _publish(client, headers, body)
    assert response.status_code == 201, response.get_json()
    return response.get_json()["report"]["id"]


# --- publish -------------------------------------------------------------------


def test_publish_returns_the_id_url_and_creation_time(client, signed_in):
    response = _publish(client, signed_in())

    assert response.status_code == 201
    body = response.get_json()
    assert set(body) == {"success", "report"}
    assert body["success"] is True
    report = body["report"]
    assert set(report) == {"id", "url", "created_at"}
    assert len(report["id"]) == 10 and report["id"].isalnum() and report["id"].isascii()
    assert report["url"] == f"https://courtai.app/reports/{report['id']}"
    datetime.fromisoformat(report["created_at"])


def test_a_waitlisted_account_cannot_publish_but_can_read(client, signed_in):
    report_id = _published_id(client, signed_in(OWNER))
    waitlisted = signed_in(OTHER, waitlisted=True)

    refused = _publish(client, waitlisted)
    read = client.get(f"/api/reports/{report_id}", headers=waitlisted)

    assert refused.status_code == 403
    assert refused.get_json()["error"]["code"] == "waitlisted"
    assert read.status_code == 200
    assert read.get_json()["report"]["is_author"] is False


def _set(path):
    """A mutation that sets ``path`` (dotted keys and indexes) to a value."""

    def mutate(body, value):
        target = body
        *parents, last = path
        for key in parents:
            target = target[key]
        target[last] = value

    return mutate


_ITEM = ("report", "tabs", 1, "items", 0)


@pytest.mark.parametrize(
    ("path", "value", "field"),
    [
        (("report", "title"), "x" * 121, "report.title"),
        (("report", "title"), "  ", "report.title"),
        (("report", "date"), "2026-02-30", "report.date"),
        (("report", "date"), "Oct 24", "report.date"),
        (("report", "opening"), ["a", "b", "c", "d"], "report.opening"),
        (("report", "opening"), ["x" * 601], "report.opening[0]"),
        (
            ("report", "skill"),
            {"name": "brief", "url": "https://evil.example/courtai.app/"},
            "report.skill.url",
        ),
        (("report", "skill"), {"name": "x" * 61}, "report.skill.name"),
        (("report", "footnote"), "x" * 301, "report.footnote"),
        (("report", "tabs"), [], "report.tabs"),
        (("report", "tabs"), [{"title": "T", "layout": "list", "items": []}] * 6, "report.tabs"),
        (("report", "tabs", 0, "title"), "x" * 31, "report.tabs[0].title"),
        (("report", "tabs", 0, "layout"), "grid", "report.tabs[0].layout"),
        (("report", "tabs", 2, "items"), [{"title": "x"}] * 61, "report.tabs[2].items"),
        (("report", "tabs", 0, "extra"), 1, "report.tabs[0]"),
        ((*_ITEM, "title"), "x" * 121, "report.tabs[1].items[0].title"),
        ((*_ITEM, "tag"), "lock", "report.tabs[1].items[0].tag"),
        ((*_ITEM, "subtitle"), "x" * 241, "report.tabs[1].items[0].subtitle"),
        ((*_ITEM, "key"), "x" * 241, "report.tabs[1].items[0].key"),
        ((*_ITEM, "body"), ["p"] * 7, "report.tabs[1].items[0].body"),
        ((*_ITEM, "body"), ["x" * 1501], "report.tabs[1].items[0].body[0]"),
        ((*_ITEM, "body"), ["bell\x07"], "report.tabs[1].items[0].body[0]"),
        ((*_ITEM, "bullets"), [["only label"]], "report.tabs[1].items[0].bullets[0]"),
        ((*_ITEM, "bullets"), [["x" * 81, "t"]], "report.tabs[1].items[0].bullets[0][0]"),
        ((*_ITEM, "bullets"), [["l", "x" * 301]], "report.tabs[1].items[0].bullets[0][1]"),
        ((*_ITEM, "bullets"), [["l", "t"]] * 11, "report.tabs[1].items[0].bullets"),
        ((*_ITEM, "watch"), "x" * 501, "report.tabs[1].items[0].watch"),
        ((*_ITEM, "cards"), ["c1", "nope"], "report.tabs[1].items[0].cards[1]"),
        ((*_ITEM, "cards"), ["c1"] * 9, "report.tabs[1].items[0].cards"),
        ((*_ITEM, "sources"), [["fact", "javascript:alert(1)"]], "report.tabs[1].items[0].sources[0][1]"),
        ((*_ITEM, "sources"), [["fact", "ftp://example.com/x"]], "report.tabs[1].items[0].sources[0][1]"),
        ((*_ITEM, "sources"), [["x" * 201, "https://a.example"]], "report.tabs[1].items[0].sources[0][0]"),
        (
            (*_ITEM, "sources"),
            [["f", "https://a.example/" + "x" * 500]],
            "report.tabs[1].items[0].sources[0][1]",
        ),
        ((*_ITEM, "sources"), [["f", "https://a.example"]] * 13, "report.tabs[1].items[0].sources"),
        (("calls",), [_call(f"c{index}") for index in range(49)], "calls"),
        (("calls", 1, "id"), "c1", "calls[1].id"),
        (("calls", 1, "id"), "C2", "calls[1].id"),
        (("calls", 1, "id"), "x" * 41, "calls[1].id"),
        (("calls", 1, "tool"), "publish_report", "calls[1].tool"),
        # Dropped from the contract: it draws no card.
        (("calls", 1, "tool"), "get_injury_scenarios", "calls[1].tool"),
        (("calls", 1, "args"), "player=Brunson", "calls[1].args"),
        (("calls", 1, "structured"), None, "calls[1].structured"),
        (("calls", 1, "meta"), "stale", "calls[1].meta"),
        (("calls", 1, "card"), "rotation", "calls[1].card"),
        (("calls", 1, "card"), "../matchup", "calls[1].card"),
        (("cards", "matchup"), 42, "cards"),
        (("cards", "unused"), "<html></html>", "cards"),
        (("show_author",), "yes", "show_author"),
        (("extra",), True, "body"),
    ],
)
def test_an_invalid_body_is_refused_naming_the_field(client, signed_in, reports, path, value, field):
    body = _body()
    _set(path)(body, value)

    response = _publish(client, signed_in(), body)

    assert response.status_code == 400
    error = response.get_json()["error"]
    assert error["code"] == "invalid_input"
    assert error["details"] == {"field": field}
    assert error["message"].startswith(f"{field} ")
    with reports._engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM reports")).scalar() == 0


@pytest.mark.parametrize("card", ["../matchup", "Matchup", "match up", "x" * 61])
def test_a_malformed_card_name_is_refused_even_with_a_snapshot_under_that_name(
    client, signed_in, reports, card
):
    body = _body()
    body["calls"][1]["card"] = card
    body["cards"] = {"game-logs": "<html>logs</html>", card: "<html>matchup</html>"}

    response = _publish(client, signed_in(), body)

    assert response.status_code == 400
    assert response.get_json()["error"]["details"] == {"field": "calls[1].card"}
    assert response.get_json()["error"]["message"].startswith("calls[1].card must be a card name")


def test_html_in_the_text_is_stored_as_given(client, signed_in):
    script = "<script>alert('x')</script> & <b>bold</b>"
    body = _body(title=script)

    report_id = _published_id(client, signed_in(), body)

    document = client.get(f"/api/reports/{report_id}").get_json()["report"]["document"]
    assert document["title"] == script


def test_the_stored_document_fills_omitted_lists_and_drops_null_optionals(client, signed_in):
    body = _body(opening=None, footnote=None, skill=None, date=None)
    body["report"]["tabs"] = [{"title": "Spots", "layout": "rows", "items": [{"title": "Bare", "tag": None}]}]

    report_id = _published_id(client, signed_in(), body)

    assert client.get(f"/api/reports/{report_id}").get_json()["report"]["document"] == {
        "title": "Injury brief: Friday, Oct 24",
        "opening": [],
        "tabs": [
            {
                "title": "Spots",
                "layout": "rows",
                "items": [{"title": "Bare", "body": [], "bullets": [], "cards": [], "sources": []}],
            }
        ],
    }


def _realistic_body(cards=38):
    """A brief of ``cards`` card calls at realistic sizes: about 820 KB for 38."""

    kinds = ["game-logs", "matchup", "rotation", "on-off", "injuries"]
    tools = [
        "query_game_logs",
        "get_matchup",
        "get_rotation",
        "get_on_off",
        "get_injuries",
    ]
    calls = []
    for index in range(cards):
        kind = index % len(kinds)
        rows = [
            {"GAME_DATE": f"2026-03-{day:02d}", "MATCHUP": "NYK vs. BOS", "PTS": 20 + day, "MIN": "34:12"}
            for day in range(1, 29)
        ]
        calls.append(
            {
                "id": f"card-{index}",
                "tool": tools[kind],
                "args": {"player": f"Player {index}", "stat": "PTS"},
                "structured": {"rows": rows * 3, "summary": "x" * 7500},
                "meta": {"data_as_of": "2026-10-24", "limits": ["sample of 12"]},
                "card": kinds[kind],
            }
        )
    html = {kind: "<html><script>" + "render();" * 5500 + "</script></html>" for kind in kinds}
    items = [
        {"title": f"Spot {index}", "tag": "lean", "body": ["x" * 400], "cards": [f"card-{index}"]}
        for index in range(cards)
    ]
    body = _body()
    body["report"]["tabs"] = [{"title": "Spots", "layout": "rows", "items": items}]
    body["calls"] = calls
    body["cards"] = html
    return body


def test_a_brief_with_38_cards_of_about_820_kb_publishes_and_reads_back(client, signed_in):
    body = _realistic_body()
    size = len(json.dumps(body).encode())
    assert 780_000 < size < 880_000

    report_id = _published_id(client, signed_in(), body)
    read = client.get(f"/api/reports/{report_id}").get_json()["report"]

    assert len(read["calls"]) == 38
    assert read["calls"][37]["structured"] == body["calls"][37]["structured"]
    assert read["cards"] == body["cards"]


def test_a_body_over_the_app_wide_1_mb_cap_but_under_2_mb_is_accepted(client, signed_in):
    body = _body()
    body["cards"]["matchup"] = "<html>" + "m" * 1_500_000 + "</html>"

    response = _publish(client, signed_in(), body)

    assert response.status_code == 201


def test_a_body_over_2_mb_is_payload_too_large(client, signed_in, reports):
    body = _body()
    body["cards"]["matchup"] = "<html>" + "m" * (2 * 1024 * 1024) + "</html>"

    response = _publish(client, signed_in(), body)

    assert response.status_code == 413
    assert response.get_json()["error"]["code"] == "payload_too_large"


def test_publishing_is_limited_to_10_an_hour_per_account(client, signed_in):
    headers = signed_in()
    statuses = [_publish(client, headers).status_code for _ in range(11)]

    assert statuses == [201] * 10 + [429]
    assert _publish(client, signed_in(OTHER)).status_code == 201


def test_publishing_is_limited_to_30_a_day_per_account(client, signed_in, monkeypatch):
    import time

    clock = [time.time()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    headers = signed_in()
    statuses = []
    for _hour in range(3):
        statuses += [_publish(client, headers).status_code for _ in range(10)]
        clock[0] += 3601
    statuses.append(_publish(client, headers).status_code)

    assert statuses == [201] * 30 + [429]


def test_an_account_holding_1000_reports_is_refused(client, signed_in, report_engine):
    with report_engine.begin() as connection:
        connection.execute(
            Report.__table__.insert(),
            [
                {
                    "id": f"seed{index:06d}",
                    "firebase_uid": OWNER,
                    "created_at": datetime(2026, 10, 1, tzinfo=UTC),
                    "title": "Seeded",
                    "document": {},
                    "calls": [],
                    "cards": {},
                    "show_author": False,
                    # One deleted report frees its slot.
                    "deleted_at": datetime(2026, 10, 2, tzinfo=UTC) if index == 0 else None,
                }
                for index in range(1001)
            ],
        )

    response = _publish(client, signed_in())

    assert response.status_code == 409
    assert response.get_json()["error"] == {
        "code": "operation_conflict",
        "message": "An account may hold at most 1,000 reports.",
    }


# --- read ----------------------------------------------------------------------


def test_the_report_reads_anonymously_in_the_contract_shape(client, signed_in):
    body = _body()
    report_id = _published_id(client, signed_in(), body)

    response = client.get(f"/api/reports/{report_id}")

    assert response.status_code == 200
    report = response.get_json()["report"]
    assert set(response.get_json()) == {"report"}
    assert set(report) == {
        "id",
        "created_at",
        "author",
        "is_author",
        "listed",
        "featured",
        "document",
        "calls",
        "cards",
    }
    assert report["id"] == report_id
    assert report["author"] is None
    assert report["is_author"] is False
    assert report["listed"] is False
    assert report["featured"] is False
    assert report["calls"] == [
        {
            "id": "c1",
            "tool": "query_game_logs",
            "card": "game-logs",
            "structured": {"rows": [{"PTS": 31}]},
            "meta": {"data_as_of": "2026-10-24"},
        },
        {
            "id": "c2",
            "tool": "get_matchup",
            "card": "matchup",
            "structured": {"rows": [{"PTS": 31}]},
            "meta": {"data_as_of": "2026-10-24"},
        },
    ]
    assert report["cards"] == {"game-logs": "<html>logs</html>", "matchup": "<html>matchup</html>"}
    assert report["document"]["tabs"][1]["items"][0]["sources"] == [
        ["Official report", "https://official.nba.com/injury-report"]
    ]


def test_is_author_is_true_only_for_the_signed_in_author(client, signed_in):
    report_id = _published_id(client, signed_in(OWNER))

    def is_author(headers):
        return client.get(f"/api/reports/{report_id}", headers=headers).get_json()["report"]["is_author"]

    assert is_author(signed_in(OWNER)) is True
    assert is_author(signed_in(OTHER)) is False
    assert is_author({}) is False


@pytest.mark.parametrize(
    ("show_author", "handle", "author"),
    [
        (True, "bighatprops", {"label": "@bighatprops"}),
        (True, None, None),
        (False, "bighatprops", None),
    ],
)
def test_the_author_label_shows_only_when_opted_in_and_a_handle_is_set(
    client, signed_in, show_author, handle, author
):
    if handle is not None:
        set_handle = client.put(
            f"/api/admin/users/{OWNER}/public-handle",
            json={"public_handle": handle},
            headers=signed_in(ADMIN, admin=True),
        )
        assert set_handle.get_json() == {"uid": OWNER, "public_handle": "bighatprops"}
    body = _body()
    body["show_author"] = show_author
    report_id = _published_id(client, signed_in(OWNER), body)

    assert client.get(f"/api/reports/{report_id}").get_json()["report"]["author"] == author


def test_setting_a_public_handle_is_admin_only_and_validated(client, signed_in):
    path = f"/api/admin/users/{OWNER}/public-handle"

    assert client.put(path, json={"public_handle": "@me"}, headers=signed_in(OWNER)).status_code == 403
    admin = signed_in(ADMIN, admin=True)
    assert client.put(path, json={"public_handle": "x" * 41}, headers=admin).status_code == 400
    assert client.put(path, json={}, headers=admin).status_code == 400
    assert (
        client.put(
            "/api/admin/users/ghost/public-handle", json={"public_handle": "@ghosty"}, headers=admin
        ).status_code
        == 404
    )
    assert client.put(path, json={"public_handle": None}, headers=admin).get_json() == {
        "uid": OWNER,
        "public_handle": None,
    }


# --- meta ----------------------------------------------------------------------


def test_meta_has_the_title_date_description_and_tag_counts(client, signed_in):
    report_id = _published_id(client, signed_in())

    response = client.get(f"/api/reports/{report_id}/meta")

    assert response.status_code == 200
    assert response.get_json() == {
        "id": report_id,
        "title": "Injury brief: Friday, Oct 24",
        "date": "2026-10-24",
        "description": "Three games carry a real absence.",
        "tags": {"strong": 1, "lean": 2, "pass": 1},
    }


def test_the_description_cuts_the_first_opening_paragraph_at_a_word_boundary():
    words = "Brunson scored more in the nine games Randle missed than in any stretch of the season"
    paragraph = " ".join([words] * 4)

    description = describe({"opening": [paragraph, "second paragraph"]})

    assert description == (
        "Brunson scored more in the nine games Randle missed than in any stretch of the season "
        "Brunson scored more in the nine games Randle missed than in any stretch of the season "
        "Brunson scored more in the…"
    )
    assert len(description) <= 200
    assert describe({"opening": ["a" * 199 + " tail"]}) == "a" * 199 + "…"
    assert describe({"opening": ["a" * 300]}) == "a" * 199 + "…"
    assert describe({"opening": ["Short and\nsweet."]}) == "Short and sweet."
    assert describe({"opening": []}) is None


# --- preview -------------------------------------------------------------------


def test_the_preview_is_a_1200_by_630_png_revalidated_on_every_use(client, signed_in):
    report_id = _published_id(client, signed_in())

    response = client.get(f"/api/reports/{report_id}/preview.png")

    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert response.headers["Cache-Control"] == "no-cache"
    image = Image.open(io.BytesIO(response.data))
    assert image.format == "PNG"
    assert image.size == (1200, 630)


def test_the_preview_draws_a_long_title_and_no_tagged_items(client, signed_in):
    body = _body(title="A very long report title " * 4, date=None)
    for tab in body["report"]["tabs"]:
        for item in tab["items"]:
            item.pop("tag")

    report_id = _published_id(client, signed_in(), body)
    response = client.get(f"/api/reports/{report_id}/preview.png")

    assert response.status_code == 200
    assert Image.open(io.BytesIO(response.data)).size == (1200, 630)


def test_the_preview_draws_the_first_three_tagged_items():
    from app.services.report_preview import tagged_items

    document = copy.deepcopy(_body()["report"])

    assert tagged_items(document) == [
        ("strong", "NYK @ BOS"),
        ("lean", "Brunson points"),
        ("lean", "Hart rebounds"),
    ]


# --- delete, takedown and 404 sameness ------------------------------------------


def _reads(client, report_id):
    return [
        client.get(f"/api/reports/{report_id}"),
        client.get(f"/api/reports/{report_id}/meta"),
        client.get(f"/api/reports/{report_id}/preview.png"),
    ]


def test_only_the_author_can_delete_and_then_it_reads_as_removed_by_its_author(client, signed_in):
    report_id = _published_id(client, signed_in(OWNER))

    foreign = client.delete(f"/api/user/reports/{report_id}", headers=signed_in(OTHER))
    assert foreign.status_code == 404
    assert foreign.get_json() == NOT_FOUND
    assert client.get(f"/api/reports/{report_id}").status_code == 200

    deleted = client.delete(f"/api/user/reports/{report_id}", headers=signed_in(OWNER))
    assert deleted.status_code == 200
    assert deleted.get_json() == {"success": True, "message": "Report deleted"}

    for uid in (None, OWNER, OTHER):
        read = client.get(f"/api/reports/{report_id}", headers=signed_in(uid) if uid else {})
        assert read.status_code == 410
        assert read.get_json() == REMOVED_BY_AUTHOR
    assert client.delete(f"/api/user/reports/{report_id}", headers=signed_in(OWNER)).status_code == 404


def test_an_admin_takedown_hides_the_report_and_only_an_admin_can_do_it(client, signed_in):
    report_id = _published_id(client, signed_in(OWNER))
    path = f"/api/admin/reports/{report_id}/takedown"

    assert client.post(path, headers=signed_in(OWNER)).status_code == 403
    assert client.post(path).status_code == 401
    first = client.post(path, headers=signed_in(ADMIN, admin=True))
    again = client.post(path, headers=signed_in(ADMIN, admin=True))

    assert first.status_code == 200
    assert first.get_json()["report"]["id"] == report_id
    assert again.get_json() == first.get_json()
    read = client.get(f"/api/reports/{report_id}", headers=signed_in(OWNER))
    assert read.status_code == 410
    assert read.get_json() == REMOVED_BY_TAKEDOWN
    assert (
        client.post(
            "/api/admin/reports/missing0001/takedown", headers=signed_in(ADMIN, admin=True)
        ).status_code
        == 404
    )


def test_removed_reports_read_as_410_naming_who_and_their_meta_and_preview_as_missing(client, signed_in):
    deleted = _published_id(client, signed_in(OWNER))
    client.delete(f"/api/user/reports/{deleted}", headers=signed_in(OWNER))
    taken_down = _published_id(client, signed_in(OWNER))
    client.post(f"/api/admin/reports/{taken_down}/takedown", headers=signed_in(ADMIN, admin=True))
    both = _published_id(client, signed_in(OWNER))
    client.delete(f"/api/user/reports/{both}", headers=signed_in(OWNER))
    client.post(f"/api/admin/reports/{both}/takedown", headers=signed_in(ADMIN, admin=True))

    expected = {
        "NoSuchId01": (404, NOT_FOUND),
        "not-base62!": (404, NOT_FOUND),
        deleted: (410, REMOVED_BY_AUTHOR),
        taken_down: (410, REMOVED_BY_TAKEDOWN),
        both: (410, REMOVED_BY_TAKEDOWN),
    }
    for report_id, (status, body) in expected.items():
        read, meta, preview = _reads(client, report_id)
        assert (read.status_code, read.get_json()) == (status, body)
        for response in (meta, preview):
            assert response.status_code == 404
            assert response.get_json() == NOT_FOUND
        assert preview.headers["Cache-Control"] == "no-store"


def test_deleting_the_account_deletes_its_reports(signed_in, client, report_engine):
    report_id = _published_id(client, signed_in(OWNER))

    with report_engine.begin() as connection:
        connection.execute(text("DELETE FROM users WHERE firebase_uid = :uid"), {"uid": OWNER})

    with report_engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT count(*) FROM reports WHERE id = :id"), {"id": report_id}
            ).scalar()
            == 0
        )


# --- Research: handles (crf04/statsplus#372) ----------------------------------------


def _claim(client, headers, handle):
    return client.put("/api/user/public-handle", json={"handle": handle}, headers=headers)


def _admin_handle(client, signed_in, uid, handle):
    return client.put(
        f"/api/admin/users/{uid}/public-handle",
        json={"public_handle": handle},
        headers=signed_in(ADMIN, admin=True),
    )


INVALID_HANDLE = {"error": "invalid_handle", "message": "A handle is 3-20 characters of a-z, 0-9 and _."}
HANDLE_TAKEN = {"error": "handle_taken", "message": "That handle is taken."}
HANDLE_ALREADY_SET = {
    "error": "handle_already_set",
    "message": "Your handle is already set. Ask CourtAI support to change it.",
}
HANDLE_REQUIRED = {
    "error": "handle_required",
    "message": "Choose a public handle before listing a report in Research.",
}
LISTED_LIMIT = {
    "error": "listed_limit",
    "message": "An account may list at most 5 reports in 24 hours. Try again later.",
}


def test_a_user_sets_a_handle_once_stored_lowercase_without_the_at(client, signed_in):
    first = _claim(client, signed_in(OWNER), "@BigHat_Props")
    second = _claim(client, signed_in(OWNER), "another")

    assert (first.status_code, first.get_json()) == (200, {"handle": "bighat_props"})
    assert (second.status_code, second.get_json()) == (409, HANDLE_ALREADY_SET)
    _claim(client, signed_in(OTHER), "taken_one")
    occupied = _claim(client, signed_in(OWNER), "taken_one")
    assert (occupied.status_code, occupied.get_json()) == (409, HANDLE_ALREADY_SET)
    report_id = _published_id(client, signed_in(OWNER), {**_body(), "show_author": True})
    assert client.get(f"/api/reports/{report_id}").get_json()["report"]["author"] == {
        "label": "@bighat_props"
    }


@pytest.mark.parametrize(
    "handle",
    ["ab", "x" * 21, "has space", "dash-name", "émile", "", "@", "@@double", 42, None, ["bighat"]],
)
def test_a_handle_outside_3_to_20_of_a_z_0_9_underscore_is_invalid(client, signed_in, handle):
    refused = _claim(client, signed_in(OWNER), handle)

    assert (refused.status_code, refused.get_json()) == (400, INVALID_HANDLE)
    # Nothing was stored, so a valid handle can still be chosen.
    assert _claim(client, signed_in(OWNER), "abc").get_json() == {"handle": "abc"}


def test_a_handle_of_exactly_3_or_20_characters_is_accepted(client, signed_in):
    assert _claim(client, signed_in(OWNER), "a_1").get_json() == {"handle": "a_1"}
    assert _claim(client, signed_in(OTHER), "x" * 20).get_json() == {"handle": "x" * 20}


def test_a_handle_is_unique_case_insensitively(client, signed_in):
    assert _claim(client, signed_in(OTHER), "bighat").status_code == 200

    taken = _claim(client, signed_in(OWNER), "@BigHat")

    assert (taken.status_code, taken.get_json()) == (409, HANDLE_TAKEN)
    assert _claim(client, signed_in(OWNER), "bighat2").get_json() == {"handle": "bighat2"}


def test_a_waitlisted_account_cannot_set_a_handle(client, signed_in):
    refused = _claim(client, signed_in(OWNER, waitlisted=True), "bighat")

    assert refused.status_code == 403
    assert refused.get_json()["error"]["code"] == "waitlisted"
    assert _claim(client, signed_in(OWNER), "bighat").get_json() == {"handle": "bighat"}


def test_the_admin_endpoint_changes_a_handle_with_the_same_format_and_uniqueness(client, signed_in):
    assert _claim(client, signed_in(OWNER), "bighat").status_code == 200
    assert _claim(client, signed_in(OTHER), "other").status_code == 200

    changed = _admin_handle(client, signed_in, OWNER, "@NewHat")
    kept = _admin_handle(client, signed_in, OWNER, "newhat")
    taken = _admin_handle(client, signed_in, OWNER, "OTHER")
    invalid = _admin_handle(client, signed_in, OWNER, "no")

    assert (changed.status_code, changed.get_json()) == (200, {"uid": OWNER, "public_handle": "newhat"})
    assert (kept.status_code, kept.get_json()) == (200, {"uid": OWNER, "public_handle": "newhat"})
    assert (taken.status_code, taken.get_json()) == (409, HANDLE_TAKEN)
    assert (invalid.status_code, invalid.get_json()) == (400, INVALID_HANDLE)
    # The old handle is free again.
    assert _claim(client, signed_in(ADMIN), "bighat").get_json() == {"handle": "bighat"}


# --- Research: listing ---------------------------------------------------------------


def _list(client, headers, report_id, listed=True):
    return client.patch(f"/api/user/reports/{report_id}", json={"listed": listed}, headers=headers)


def _feed(client, cursor=None):
    response = client.get("/api/research", query_string={"cursor": cursor} if cursor else None)
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def _ids(entries):
    return [entry["id"] for entry in entries]


def _report_count(engine):
    with engine.connect() as connection:
        return connection.execute(text("SELECT count(*) FROM reports")).scalar()


def test_publishing_listed_without_a_handle_is_refused_and_stores_nothing(client, signed_in, report_engine):
    refused = _publish(client, signed_in(), {**_body(), "listed": True})

    assert (refused.status_code, refused.get_json()) == (409, HANDLE_REQUIRED)
    assert _report_count(report_engine) == 0


def test_publishing_listed_lists_it_with_its_author_even_without_show_author(client, signed_in):
    _claim(client, signed_in(OWNER), "bighat")
    report_id = _published_id(client, signed_in(OWNER), {**_body(), "listed": True, "show_author": False})

    report = client.get(f"/api/reports/{report_id}").get_json()["report"]

    assert report["listed"] is True
    assert report["featured"] is False
    assert report["author"] == {"label": "@bighat", "handle": "bighat"}
    assert _ids(_feed(client)["reports"]) == [report_id]


def test_publishing_unlisted_by_default_keeps_it_out_of_research(client, signed_in):
    _claim(client, signed_in(OWNER), "bighat")
    report_id = _published_id(client, signed_in(OWNER))

    assert client.get(f"/api/reports/{report_id}").get_json()["report"]["listed"] is False
    assert _feed(client) == {"featured": [], "reports": [], "next_cursor": None}


def test_the_author_lists_and_unlists_by_patch(client, signed_in):
    _claim(client, signed_in(OWNER), "bighat")
    report_id = _published_id(client, signed_in(OWNER))

    listed = _list(client, signed_in(OWNER), report_id)
    assert listed.status_code == 200
    assert listed.get_json() == {
        "success": True,
        "report": {"id": report_id, "listed": True, "listed_at": "2026-10-28T12:00:02+00:00"},
    }
    assert _ids(_feed(client)["reports"]) == [report_id]
    assert client.get(f"/api/reports/{report_id}").get_json()["report"]["author"] == {
        "label": "@bighat",
        "handle": "bighat",
    }

    unlisted = _list(client, signed_in(OWNER), report_id, listed=False)
    assert unlisted.get_json() == {
        "success": True,
        "report": {"id": report_id, "listed": False, "listed_at": None},
    }
    read = client.get(f"/api/reports/{report_id}").get_json()["report"]
    assert (read["listed"], read["author"]) == (False, None)
    assert _feed(client)["reports"] == []
    assert client.get("/api/research/authors/bighat").get_json()["reports"] == []


def test_listing_by_patch_needs_a_handle_and_only_the_author(client, signed_in):
    report_id = _published_id(client, signed_in(OWNER))
    _claim(client, signed_in(OTHER), "other")

    no_handle = _list(client, signed_in(OWNER), report_id)
    foreign = _list(client, signed_in(OTHER), report_id)
    foreign_unlist = _list(client, signed_in(OTHER), report_id, listed=False)

    assert (no_handle.status_code, no_handle.get_json()) == (409, HANDLE_REQUIRED)
    assert (foreign.status_code, foreign.get_json()) == (404, NOT_FOUND)
    assert (foreign_unlist.status_code, foreign_unlist.get_json()) == (404, NOT_FOUND)
    assert client.get(f"/api/reports/{report_id}").get_json()["report"]["listed"] is False


@pytest.mark.parametrize("body", [{}, {"listed": "yes"}, {"listed": True, "extra": 1}, [True]])
def test_a_patch_body_other_than_listed_true_or_false_is_refused(client, signed_in, body):
    _claim(client, signed_in(OWNER), "bighat")
    report_id = _published_id(client, signed_in(OWNER))

    response = client.patch(f"/api/user/reports/{report_id}", json=body, headers=signed_in(OWNER))

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_input"


def test_deleted_and_taken_down_reports_cannot_be_listed(client, signed_in):
    _claim(client, signed_in(OWNER), "bighat")
    deleted = _published_id(client, signed_in(OWNER))
    client.delete(f"/api/user/reports/{deleted}", headers=signed_in(OWNER))
    taken_down = _published_id(client, signed_in(OWNER))
    client.post(f"/api/admin/reports/{taken_down}/takedown", headers=signed_in(ADMIN, admin=True))

    for report_id in (deleted, taken_down, "NoSuchId01"):
        response = _list(client, signed_in(OWNER), report_id)
        assert (response.status_code, response.get_json()) == (404, NOT_FOUND)


def test_a_waitlisted_account_cannot_list(client, signed_in):
    _claim(client, signed_in(OWNER), "bighat")
    report_id = _published_id(client, signed_in(OWNER))

    by_patch = _list(client, signed_in(OWNER, waitlisted=True), report_id)
    by_publish = _publish(client, signed_in(OWNER, waitlisted=True), {**_body(), "listed": True})

    for refused in (by_patch, by_publish):
        assert refused.status_code == 403
        assert refused.get_json()["error"]["code"] == "waitlisted"
    assert _feed(client)["reports"] == []


def test_a_waitlisted_author_can_still_unlist(client, signed_in):
    _claim(client, signed_in(OWNER), "bighat")
    report_id = _published_id(client, signed_in(OWNER), {**_body(), "listed": True})

    unlisted = _list(client, signed_in(OWNER, waitlisted=True), report_id, listed=False)

    assert unlisted.status_code == 200
    assert unlisted.get_json()["report"]["listed"] is False
    assert _feed(client)["reports"] == []


def test_clearing_a_handle_unlists_and_unfeatures_the_authors_reports(client, signed_in):
    _claim(client, signed_in(OWNER), "bighat")
    _claim(client, signed_in(OTHER), "other")
    mine = _published_id(client, signed_in(OWNER), {**_body(), "listed": True})
    theirs = _published_id(client, signed_in(OTHER), {**_body(), "listed": True})
    _feature(client, signed_in, mine)

    cleared = _admin_handle(client, signed_in, OWNER, None)

    assert cleared.get_json() == {"uid": OWNER, "public_handle": None}
    read = client.get(f"/api/reports/{mine}").get_json()["report"]
    assert (read["listed"], read["featured"], read["author"]) == (False, False, None)
    assert _feed(client) == {"featured": [], "reports": _feed(client)["reports"], "next_cursor": None}
    assert _ids(_feed(client)["reports"]) == [theirs]
    # With a handle again, relisting does not bring the old pin back.
    _admin_handle(client, signed_in, OWNER, "bighat")
    assert _list(client, signed_in(OWNER), mine).status_code == 200
    relisted = client.get(f"/api/reports/{mine}").get_json()["report"]
    assert (relisted["listed"], relisted["featured"]) == (True, False)
    assert _feed(client)["featured"] == []


def test_listing_is_capped_at_5_moves_per_rolling_24_hours_over_both_endpoints(
    client, signed_in, clock, report_engine
):
    _claim(client, signed_in(OWNER), "bighat")
    _claim(client, signed_in(OTHER), "other")
    start = clock.now
    published = [_published_id(client, signed_in(OWNER), {**_body(), "listed": True})]
    clock.now += timedelta(hours=1)
    published.append(_published_id(client, signed_in(OWNER), {**_body(), "listed": True}))
    drafts = [_published_id(client, signed_in(OWNER)) for _ in range(4)]
    statuses = [_list(client, signed_in(OWNER), report_id).status_code for report_id in drafts]
    assert statuses == [200, 200, 200, 429]

    refused_patch = _list(client, signed_in(OWNER), drafts[3])
    refused_publish = _publish(client, signed_in(OWNER), {**_body(), "listed": True})
    assert (refused_patch.status_code, refused_patch.get_json()) == (429, LISTED_LIMIT)
    assert (refused_publish.status_code, refused_publish.get_json()) == (429, LISTED_LIMIT)
    assert _report_count(report_engine) == 6

    # Relisting an already listed report spends nothing; unlisting always works
    # but does not give the listing back.
    assert _list(client, signed_in(OWNER), published[0]).status_code == 200
    assert _list(client, signed_in(OWNER), published[0], listed=False).status_code == 200
    assert _list(client, signed_in(OWNER), published[0]).get_json() == LISTED_LIMIT
    # Another account has a cap of its own.
    assert _published_id(client, signed_in(OTHER), {**_body(), "listed": True})

    # The window rolls: the first listing (at start + 1 s) frees its slot 24 hours later.
    clock.now = start + timedelta(hours=24) - timedelta(seconds=1)
    assert _list(client, signed_in(OWNER), drafts[3]).get_json() == LISTED_LIMIT
    assert _list(client, signed_in(OWNER), drafts[3]).status_code == 200
    assert _list(client, signed_in(OWNER), published[0]).get_json() == LISTED_LIMIT


# --- Research: the feed ----------------------------------------------------------------


def _seed_listed(reports, clock, uid, count, *, body=None):
    """Publish and list ``count`` reports through the service, six hours apart."""

    ids = []
    for _ in range(count):
        clock.now += timedelta(hours=6)
        report_id = reports.publish(uid, body or _body())["id"]
        reports.set_listed(uid, report_id, True)
        ids.append(report_id)
    return ids


def _feature(client, signed_in, report_id, featured=True):
    return client.put(
        f"/api/admin/reports/{report_id}/featured",
        json={"featured": featured},
        headers=signed_in(ADMIN, admin=True),
    )


def test_an_entry_has_the_contract_shape(client, signed_in, reports, clock):
    _claim(client, signed_in(OWNER), "bighat")
    [report_id] = _seed_listed(reports, clock, OWNER, 1)

    assert _feed(client) == {
        "featured": [],
        "reports": [
            {
                "id": report_id,
                "title": "Injury brief: Friday, Oct 24",
                "listed_at": "2026-10-28T18:00:02+00:00",
                "created_at": "2026-10-28T18:00:01+00:00",
                "date": "2026-10-24",
                "author": {"handle": "bighat"},
                "covers": {"games": [], "players": ["Jalen Brunson"]},
                "preview_url": f"/api/reports/{report_id}/preview.png",
            }
        ],
        "next_cursor": None,
    }


def test_the_feed_pages_20_newest_first_with_featured_on_top_only_once(client, signed_in, reports, clock):
    _claim(client, signed_in(OWNER), "bighat")
    _claim(client, signed_in(OTHER), "other")
    ids = _seed_listed(reports, clock, OWNER, 12) + _seed_listed(reports, clock, OTHER, 11)
    assert ids == [f"rep{number:07d}" for number in range(1, 24)]
    assert _feature(client, signed_in, "rep0000005").status_code == 200
    assert _feature(client, signed_in, "rep0000010").status_code == 200

    first = _feed(client)

    assert _ids(first["featured"]) == ["rep0000010", "rep0000005"]
    assert _ids(first["reports"]) == [
        f"rep{number:07d}" for number in (*range(23, 10, -1), 9, 8, 7, 6, 4, 3, 2)
    ]
    assert isinstance(first["next_cursor"], str)
    second = _feed(client, first["next_cursor"])
    assert second == {"featured": [], "reports": second["reports"], "next_cursor": None}
    assert _ids(second["reports"]) == ["rep0000001"]


def test_a_page_boundary_inside_a_listing_time_tie_loses_and_repeats_nothing(
    client, signed_in, reports, clock, report_engine
):
    _claim(client, signed_in(OWNER), "bighat")
    _seed_listed(reports, clock, OWNER, 22)
    with report_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE reports SET listed_at = (SELECT listed_at FROM reports WHERE id = 'rep0000003') "
                "WHERE id IN ('rep0000002', 'rep0000004')"
            )
        )

    first = _feed(client)
    second = _feed(client, first["next_cursor"])

    # Ties order by id, newest first: rep4, rep3 end page one and rep2 opens page two.
    assert _ids(first["reports"])[-3:] == ["rep0000005", "rep0000004", "rep0000003"]
    assert (_ids(second["reports"]), second["next_cursor"]) == (["rep0000002", "rep0000001"], None)


def test_a_pin_or_unpin_between_the_feeds_two_reads_never_shows_a_report_twice(
    client, signed_in, reports, clock
):
    _claim(client, signed_in(OWNER), "bighat")
    pinned, unpinned = _seed_listed(reports, clock, OWNER, 2)
    _feature(client, signed_in, unpinned)
    reads = reports._entries
    calls = []

    def flip_after_the_first_read(*args, **kwargs):
        rows = reads(*args, **kwargs)
        if not calls:
            reports.set_featured(pinned, True)
            reports.set_featured(unpinned, False)
        calls.append(rows)
        return rows

    reports._entries = flip_after_the_first_read
    page = _feed(client)

    # Both lists come from before the flip or are kept apart by id: nothing twice.
    assert (_ids(page["featured"]), _ids(page["reports"])) == ([unpinned], [])


def test_unfeaturing_returns_a_report_to_its_place_and_unlisting_drops_featured(
    client, signed_in, reports, clock
):
    _claim(client, signed_in(OWNER), "bighat")
    ids = _seed_listed(reports, clock, OWNER, 3)
    _feature(client, signed_in, ids[0])
    _feature(client, signed_in, ids[1])

    unfeatured = _feature(client, signed_in, ids[0], featured=False)

    assert unfeatured.get_json() == {
        "success": True,
        "report": {"id": ids[0], "featured": False, "featured_at": None},
    }
    assert (_ids(_feed(client)["featured"]), _ids(_feed(client)["reports"])) == ([ids[1]], [ids[2], ids[0]])
    assert client.get(f"/api/reports/{ids[1]}").get_json()["report"]["featured"] is True

    _list(client, signed_in(OWNER), ids[1], listed=False)
    reports.set_listed(OWNER, ids[1], True)
    relisted = client.get(f"/api/reports/{ids[1]}").get_json()["report"]
    assert (relisted["listed"], relisted["featured"]) == (True, False)
    assert (_ids(_feed(client)["featured"]), _ids(_feed(client)["reports"])) == ([], [ids[1], ids[2], ids[0]])


def test_deleted_and_taken_down_reports_never_appear_in_the_feed_or_author_pages(
    client, signed_in, reports, clock
):
    _claim(client, signed_in(OWNER), "bighat")
    kept, deleted, taken_down, featured_deleted, featured_taken_down = _seed_listed(reports, clock, OWNER, 5)
    for report_id in (featured_deleted, featured_taken_down):
        _feature(client, signed_in, report_id)
    for report_id in (deleted, featured_deleted):
        client.delete(f"/api/user/reports/{report_id}", headers=signed_in(OWNER))
    for report_id in (taken_down, featured_taken_down):
        client.post(f"/api/admin/reports/{report_id}/takedown", headers=signed_in(ADMIN, admin=True))

    feed = _feed(client)
    author = client.get("/api/research/authors/bighat").get_json()

    assert (_ids(feed["featured"]), _ids(feed["reports"])) == ([], [kept])
    assert (_ids(author["featured"]), _ids(author["reports"])) == ([], [kept])


def test_an_invalid_cursor_is_refused(client, reports):
    for cursor in ("not-a-cursor", "bm9wZQ", "MjAyNi0xMC0yOFQxMjowMDowMHxub3QtYmFzZTYyIQ"):
        response = client.get("/api/research", query_string={"cursor": cursor})
        assert response.status_code == 400
        assert response.get_json()["error"]["code"] == "invalid_input"


def test_covers_names_each_game_by_tricodes_and_each_player_once_in_call_order(
    client, signed_in, reports, clock
):
    _claim(client, signed_in(OWNER), "bighat")

    def call(call_id, tool, args):
        return {**_call(call_id, card="game-logs", tool=tool), "args": args}

    body = _body()
    body["calls"] = [
        call("c1", "get_matchup", {"game": "DET @ CHA", "player": "Cade Cunningham"}),
        call("c2", "query_game_logs", {"player": "Cade Cunningham (DET)", "opponent": "CHA"}),
        call("c3", "get_rotation", {"game": "Pistons at Hornets", "date": "2026-10-24"}),
        call("c4", "get_injuries", {"date": "2026-10-24"}),
        call("c5", "get_on_off", {"team": "DEN", "on": ["Nikola Jokic"]}),
        call("c6", "get_matchup", {"game": "gs@pho", "player": "  Stephen   Curry "}),
        call("c7", "get_matchup", {"game": "Somewhere @ Nowhere", "player": None}),
        call("c8", "get_rotation", {"game": "Portland Trail Blazers at New York Knicks"}),
        call("c9", "query_game_logs", {"player": "Jalen Brunson"}),
    ]
    body["cards"] = {"game-logs": "<html>logs</html>"}
    body["report"]["tabs"] = [
        {"title": "Spots", "layout": "list", "items": [{"title": "Spot", "cards": ["c1"]}]}
    ]
    _seed_listed(reports, clock, OWNER, 1, body=body)

    assert _feed(client)["reports"][0]["covers"] == {
        "games": ["DET @ CHA", "GSW @ PHX", "POR @ NYK"],
        "players": ["Cade Cunningham", "Stephen Curry", "Jalen Brunson"],
    }


# --- Research: author pages -------------------------------------------------------------


def test_an_author_page_lists_only_that_authors_reports_featured_included(client, signed_in, reports, clock):
    _claim(client, signed_in(OWNER), "bighat")
    _claim(client, signed_in(OTHER), "other")
    mine = _seed_listed(reports, clock, OWNER, 2)
    _seed_listed(reports, clock, OTHER, 1)
    _feature(client, signed_in, mine[0])

    for handle in ("bighat", "BigHat", "@bighat"):
        response = client.get(f"/api/research/authors/{handle}")
        assert response.status_code == 200
        assert response.get_json()["featured"] == []
        assert _ids(response.get_json()["reports"]) == [mine[1], mine[0]]
        assert response.get_json()["next_cursor"] is None


def test_an_author_page_pages_20_at_a_time(client, signed_in, reports, clock):
    _claim(client, signed_in(OWNER), "bighat")
    ids = _seed_listed(reports, clock, OWNER, 21)

    first = client.get("/api/research/authors/bighat").get_json()
    second = client.get(
        "/api/research/authors/bighat", query_string={"cursor": first["next_cursor"]}
    ).get_json()

    assert _ids(first["reports"]) == ids[:0:-1]
    assert (_ids(second["reports"]), second["next_cursor"]) == ([ids[0]], None)


def test_an_unknown_handle_is_not_found_and_an_author_with_nothing_listed_is_empty(client, signed_in):
    _claim(client, signed_in(OWNER), "bighat")
    _published_id(client, signed_in(OWNER))

    unknown = client.get("/api/research/authors/nobody")
    empty = client.get("/api/research/authors/bighat")

    assert unknown.status_code == 404
    assert unknown.get_json()["error"]["code"] == "resource_not_found"
    assert (empty.status_code, empty.get_json()) == (
        200,
        {"featured": [], "reports": [], "next_cursor": None},
    )


# --- Research: Featured -----------------------------------------------------------------


def test_featuring_is_admin_only_and_an_unlisted_report_is_a_conflict(client, signed_in, reports, clock):
    _claim(client, signed_in(OWNER), "bighat")
    [listed] = _seed_listed(reports, clock, OWNER, 1)
    unlisted = _published_id(client, signed_in(OWNER))

    owner_try = client.put(
        f"/api/admin/reports/{listed}/featured", json={"featured": True}, headers=signed_in(OWNER)
    )
    conflict = _feature(client, signed_in, unlisted)
    featured = _feature(client, signed_in, listed)
    again = _feature(client, signed_in, listed)

    assert owner_try.status_code == 403
    assert conflict.status_code == 409
    assert conflict.get_json()["error"] == {
        "code": "operation_conflict",
        "message": "Only a listed report can be Featured.",
    }
    assert client.get(f"/api/reports/{unlisted}").get_json()["report"]["featured"] is False
    assert featured.status_code == 200
    assert featured.get_json()["report"]["featured"] is True
    assert again.get_json() == featured.get_json()
    assert _feature(client, signed_in, "NoSuchId01").status_code == 404
    bad_body = client.put(
        f"/api/admin/reports/{listed}/featured",
        json={"featured": "yes"},
        headers=signed_in(ADMIN, admin=True),
    )
    assert bad_body.status_code == 400
