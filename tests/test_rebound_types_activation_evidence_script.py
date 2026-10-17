"""Offline contract for the Rebound Types activation evidence script (#446)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.migrations import run_migrations
from app.models.collection_control import PublicationPointer, PublicationVersion
from scripts.rebound_types_activation_evidence import check_identity, main

NOW = datetime(2026, 10, 17, tzinfo=timezone.utc)
SLICES = (
    "DefTwoPtRebounds",
    "DefThreePtRebounds",
    "FTDefRebounds",
    "OffTwoPtRebounds",
    "OffThreePtRebounds",
    "FTOffRebounds",
)
#: One team's six counts over 15 games; they add up to its 75 OPP_REB.
COUNTS = dict(zip(SLICES, (30, 15, 15, 7, 8, 0)))


def _rebound_row(team_id, *, counts=None, game_ids=("g1", "g2")):
    counts = counts or COUNTS
    per48 = {name: value / 15 for name, value in counts.items()}
    return {
        "team_id": team_id,
        "team_tricode": f"T{team_id:02d}",
        "game_ids": list(game_ids),
        "game_count": len(game_ids),
        "counts": dict(counts),
        "per48": per48,
        "league_average": dict(per48),
        "population_sigma": {name: 0.0 for name in counts},
        "competition_rank": {name: 1 for name in counts},
        "team_minutes": 720.0,
    }


def _traditional_row(team_id, *, rebounds=75, game_ids=("g1", "g2")):
    return {
        "team_id": team_id,
        "game_ids": list(game_ids),
        "counts": {"rebounds": rebounds},
        "per48": {"rebounds": rebounds / 15},
    }


def _seed(tmp_path, *, rebound_rows, traditional_rows, windows=("season", "l15"), activate_traditional=True):
    engine = create_engine(f"sqlite:///{tmp_path / 'evidence.sqlite3'}")
    run_migrations(engine)
    with Session(engine) as session, session.begin():
        for window in windows:
            session.add(
                PublicationVersion(
                    publication_id=f"rebound-{window}",
                    stream_key=f"rebound_types_{window}",
                    season="2025-26",
                    cutoff=NOW,
                    version=1,
                    status="candidate",
                    checksum="c" * 64,
                    payload=json.dumps(rebound_rows),
                    created_at=NOW,
                    fence=0,
                )
            )
            session.add(
                PublicationVersion(
                    publication_id=f"traditional-{window}",
                    stream_key=f"traditional_opponent_{window}",
                    season="2025-26",
                    cutoff=NOW,
                    version=1,
                    status="active" if activate_traditional else "candidate",
                    checksum="t" * 64,
                    payload=json.dumps(traditional_rows),
                    created_at=NOW,
                    fence=1,
                )
            )
            if activate_traditional:
                session.add(
                    PublicationPointer(
                        stream_key=f"traditional_opponent_{window}",
                        fence=1,
                        active_publication_id=f"traditional-{window}",
                        updated_at=NOW,
                    )
                )
    return engine


def _league(builder):
    return [builder(team_id) for team_id in range(1, 31)]


def _run(engine, capsys, *extra):
    code = main(["--database-url", str(engine.url), "--season", "2025-26", *extra])
    return code, json.loads(capsys.readouterr().out)


def test_every_team_and_window_passes_when_the_slices_add_up_to_opp_reb(tmp_path, capsys):
    engine = _seed(
        tmp_path,
        rebound_rows=_league(_rebound_row),
        traditional_rows=_league(_traditional_row),
    )

    code, report = _run(engine, capsys)

    assert code == 0
    assert report["passed"] is True and report["failures"] == []
    assert report["teams_checked"] == 60
    assert report["windows"]["season"]["candidate_publication_id"] == "rebound-season"
    assert report["windows"]["l15"]["traditional_publication_id"] == "traditional-l15"


def test_a_team_whose_slices_miss_opp_reb_fails_with_both_counts(tmp_path, capsys):
    short = dict(COUNTS, DefTwoPtRebounds=29)
    rows = [_rebound_row(team_id, counts=short if team_id == 7 else None) for team_id in range(1, 31)]
    engine = _seed(
        tmp_path,
        rebound_rows=rows,
        traditional_rows=_league(_traditional_row),
    )

    code, report = _run(engine, capsys)

    assert code == 1 and report["passed"] is False
    identity = [f for f in report["failures"] if f["failure"] == "slices_do_not_add_up_to_opp_reb"]
    assert identity == [
        {
            "window": "season",
            "team_id": 7,
            "failure": "slices_do_not_add_up_to_opp_reb",
            "slice_sum": 74,
            "opp_reb": 75,
        },
        {
            "window": "l15",
            "team_id": 7,
            "failure": "slices_do_not_add_up_to_opp_reb",
            "slice_sum": 74,
            "opp_reb": 75,
        },
    ]


def test_publications_over_different_games_are_not_compared(tmp_path, capsys):
    engine = _seed(
        tmp_path,
        rebound_rows=_league(lambda t: _rebound_row(t, game_ids=("g1", "g3"))),
        traditional_rows=_league(_traditional_row),
    )

    code, report = _run(engine, capsys)

    assert code == 1
    assert {f["failure"] for f in report["failures"]} == {"game_set_mismatch"}


def test_a_league_short_of_thirty_teams_fails(tmp_path, capsys):
    engine = _seed(
        tmp_path,
        rebound_rows=[_rebound_row(team_id) for team_id in range(1, 30)],
        traditional_rows=_league(_traditional_row),
    )

    code, report = _run(engine, capsys)

    assert code == 1
    failures = {f["failure"] for f in report["failures"]}
    assert {"league_not_30_teams", "team_sets_differ"} <= failures


def test_a_missing_active_traditional_publication_fails_closed(tmp_path, capsys):
    engine = _seed(
        tmp_path,
        rebound_rows=_league(_rebound_row),
        traditional_rows=_league(_traditional_row),
        activate_traditional=False,
    )

    code, report = _run(engine, capsys)

    assert code == 1
    assert report["windows"]["season"]["failures"] == [
        {
            "window": "season",
            "failure": "publication_missing",
            "rebound_types_candidate": True,
            "active_traditional": False,
        }
    ]


def test_a_candidate_the_strict_decoder_refuses_fails(tmp_path, capsys):
    rows = _league(_rebound_row)
    for row in rows:
        del row["counts"]["FTOffRebounds"]
    engine = _seed(
        tmp_path,
        rebound_rows=rows,
        traditional_rows=_league(_traditional_row),
    )

    code, report = _run(engine, capsys)

    assert code == 1
    assert "candidate_not_decodable" in {f["failure"] for f in report["failures"]}


def test_the_per48_values_must_add_up_too():
    rebound = [_rebound_row(1)]
    rebound[0]["per48"]["DefTwoPtRebounds"] += 0.5
    checked, failures = check_identity(rebound, [_traditional_row(1)], window="season")

    assert checked == 1
    assert [f["failure"] for f in failures if f.get("team_id") == 1] == ["per48_does_not_add_up_to_opp_reb"]
