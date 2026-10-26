"""CourtAI reports: publish, read, meta, preview, delete and takedown (crf04/statsplus#342).

The routes run against the real :class:`ReportService` on a migrated SQLite
database, so ownership, soft deletes and the author join are exercised rather
than stubbed.
"""

from __future__ import annotations

import copy
import io
import json
from datetime import UTC, datetime

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


@pytest.fixture
def reports(report_engine, dependencies):
    service = ReportService(report_engine)
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
    assert set(report) == {"id", "created_at", "author", "is_author", "document", "calls", "cards"}
    assert report["id"] == report_id
    assert report["author"] is None
    assert report["is_author"] is False
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
        (True, "@bighatprops", {"label": "@bighatprops"}),
        (True, None, None),
        (False, "@bighatprops", None),
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
        assert set_handle.get_json() == {"uid": OWNER, "public_handle": handle}
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
            "/api/admin/users/ghost/public-handle", json={"public_handle": "@a"}, headers=admin
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


def test_the_preview_is_a_1200_by_630_png_cached_for_an_hour(client, signed_in):
    report_id = _published_id(client, signed_in())

    response = client.get(f"/api/reports/{report_id}/preview.png")

    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert response.headers["Cache-Control"] == "public, max-age=3600"
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


def test_only_the_author_can_delete_and_then_nobody_can_read_it(client, signed_in):
    report_id = _published_id(client, signed_in(OWNER))

    foreign = client.delete(f"/api/user/reports/{report_id}", headers=signed_in(OTHER))
    assert foreign.status_code == 404
    assert foreign.get_json() == NOT_FOUND
    assert client.get(f"/api/reports/{report_id}").status_code == 200

    deleted = client.delete(f"/api/user/reports/{report_id}", headers=signed_in(OWNER))
    assert deleted.status_code == 200
    assert deleted.get_json() == {"success": True, "message": "Report deleted"}

    for headers in ({}, signed_in(OWNER), signed_in(OTHER)):
        assert client.get(f"/api/reports/{report_id}", headers=headers).get_json() == NOT_FOUND
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
    assert client.get(f"/api/reports/{report_id}", headers=signed_in(OWNER)).get_json() == NOT_FOUND
    assert (
        client.post(
            "/api/admin/reports/missing0001/takedown", headers=signed_in(ADMIN, admin=True)
        ).status_code
        == 404
    )


def test_missing_deleted_and_taken_down_reports_look_the_same(client, signed_in):
    deleted = _published_id(client, signed_in(OWNER))
    client.delete(f"/api/user/reports/{deleted}", headers=signed_in(OWNER))
    taken_down = _published_id(client, signed_in(OWNER))
    client.post(f"/api/admin/reports/{taken_down}/takedown", headers=signed_in(ADMIN, admin=True))

    for report_id in ("NoSuchId01", deleted, taken_down, "not-base62!"):
        for response in _reads(client, report_id):
            assert response.status_code == 404
            assert response.get_json() == NOT_FOUND
        assert _reads(client, report_id)[2].headers["Cache-Control"] == "no-store"


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
