"""What the benchmark retains as query-plan evidence, and what it rejects.

The benchmark authorizes a production activation, so two properties matter and
neither had coverage: the retained artifact must not contain bound values, and
the access-path check must accept every legitimate indexed plan while still
catching a whole-relation read of a table that grows with the season.

Plans are captured as structured nodes rather than vendor text.  Vendor text
echoes constants back in fields like ``Index Cond``, and parsing it by regex
mistook ``USING``, ``ON``, and ``BACKWARD`` for relation names.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import create_engine

from app.migrations import run_migrations
from app.services.database_first_benchmark import (
    _BoundParameters,
    _PlanEvidence,
    _PlanNode,
    _plans_are_indexed,
    _postgres_plan_nodes,
    _query_plans,
    _safe_parameters,
    _sqlite_plan_nodes,
    _statement_aliases,
)


def _db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'benchmark.sqlite3'}")
    run_migrations(engine)
    return engine


def _evidence(*nodes: _PlanNode, statement: str = "SELECT 1 FROM publication_versions") -> tuple[_PlanEvidence, ...]:
    return (_PlanEvidence(statement=statement, parameters=None, nodes=nodes, available=True),)


# --- the artifact must not carry bound values -------------------------------


def test_rendered_evidence_keeps_no_predicate_or_constant():
    node = _PlanNode(
        node_type="Index Scan",
        relation="team_matchup_surface_observations",
        index_name="ix_observations_season",
    )
    parameters = _BoundParameters(
        raw={"as_of_date_1": date(2026, 4, 12), "season_1": "2025-26"},
        safe=_safe_parameters({"as_of_date_1": date(2026, 4, 12), "season_1": "2025-26"}),
    )
    rendered = _PlanEvidence(
        statement="SELECT max(as_of_date) FROM team_matchup_surface_observations",
        parameters=parameters,
        nodes=(node,),
        available=True,
    ).render()

    assert "Index Scan using ix_observations_season on team_matchup_surface_observations" in rendered
    # The date reached the database, but only its type name reaches the
    # artifact.  Primitive binds such as the season are retained deliberately
    # by _safe_parameters and are not affected by this.
    assert "2026-04-12" not in rendered
    assert "'date'" in rendered


def test_bound_parameters_send_real_values_but_report_sanitized_ones():
    parameters = _BoundParameters(raw={"as_of": date(2026, 4, 12)}, safe={"as_of": "date"})

    assert parameters.raw["as_of"] == date(2026, 4, 12)
    assert repr(parameters) == repr({"as_of": "date"})


def test_captured_plans_bind_real_values_and_stay_available(tmp_path):
    engine = _db(tmp_path)
    statement = (
        "SELECT max(as_of_date) FROM team_matchup_surface_observations "
        "WHERE season = ? AND as_of_date <= ?"
    )
    parameters = _BoundParameters(raw=("2025-26", date(2026, 4, 12)), safe=("2025-26", "date"))

    evidence = _query_plans(engine, measured_statements=((statement, parameters),))

    # A sanitized value here would make the statement unexplainable.
    assert evidence[0].available is True
    assert "2026-04-12" not in evidence[0].render()


# --- access paths that must be accepted -------------------------------------


@pytest.mark.parametrize(
    "node_type",
    [
        "Index Scan",
        "Index Only Scan",
        "Index Scan Backward",
        "Index Only Scan Backward",
        "Bitmap Heap Scan",
        "Bitmap Index Scan",
    ],
)
def test_indexed_and_bitmap_access_is_accepted(node_type):
    evidence = _evidence(_PlanNode(node_type=node_type, relation="player_game_logs", index_name="ix"))

    assert _plans_are_indexed(evidence) is True


def test_a_plan_touching_only_bounded_registries_is_accepted():
    # Roughly one row per stream: a planner reads all of it by choice.
    evidence = _evidence(
        _PlanNode(node_type="Seq Scan", relation="publication_streams"),
        _PlanNode(node_type="Seq Scan", relation="publication_pointers"),
        statement="SELECT stream_key FROM publication_streams",
    )

    assert _plans_are_indexed(evidence) is True


def test_ungoverned_relations_are_not_policed():
    evidence = _evidence(_PlanNode(node_type="Seq Scan", relation="alembic_version"))

    assert _plans_are_indexed(evidence) is True


# --- access paths that must be rejected -------------------------------------


@pytest.mark.parametrize(
    "relation",
    [
        "publication_versions",
        "player_game_logs",
        "team_matchup_surface_observations",
        "canonical_game_ledger_games",
        "event_catalog",
    ],
)
def test_a_full_read_of_a_season_sized_relation_is_rejected(relation):
    evidence = _evidence(_PlanNode(node_type="Seq Scan", relation=relation))

    assert _plans_are_indexed(evidence) is False


def test_a_bounded_registry_cannot_mask_an_unbounded_full_read():
    evidence = _evidence(
        _PlanNode(node_type="Seq Scan", relation="publication_streams"),
        _PlanNode(node_type="Seq Scan", relation="publication_versions"),
    )

    assert _plans_are_indexed(evidence) is False


def test_unavailable_evidence_is_rejected():
    evidence = (_PlanEvidence("SELECT 1", None, (), available=False),)

    assert _plans_are_indexed(evidence) is False
    assert evidence[0].render().endswith("=> unavailable")


def test_no_evidence_is_rejected():
    assert _plans_are_indexed(()) is False


# --- dialect parsing --------------------------------------------------------


def test_postgres_tree_is_walked_into_whitelisted_nodes():
    document = [{"Plan": {
        "Node Type": "Nested Loop",
        "Plans": [
            {
                "Node Type": "Seq Scan",
                "Relation Name": "publication_streams",
                "Filter": "(season = '2025-26'::text)",
            },
            {
                "Node Type": "Index Scan",
                "Relation Name": "publication_versions",
                "Index Name": "publication_versions_pkey",
                "Index Cond": "(as_of_date <= '2026-04-12'::date)",
            },
        ],
    }}]

    nodes = _postgres_plan_nodes(document)

    assert [node.node_type for node in nodes] == ["Nested Loop", "Seq Scan", "Index Scan"]
    assert nodes[2].index_name == "publication_versions_pkey"
    # Filter and Index Cond carry constants and must not survive.
    rendered = " ".join(node.render() for node in nodes)
    assert "2026-04-12" not in rendered and "2025-26" not in rendered


def test_sqlite_aliases_resolve_back_to_the_real_relation():
    statement = (
        "SELECT p.stream_key FROM publication_pointers p "
        "JOIN publication_versions v ON v.publication_id = p.active_publication_id"
    )
    aliases = _statement_aliases(statement)
    rows = [(0, 0, 0, "SCAN p"), (0, 0, 0, "SEARCH v USING INDEX sqlite_autoindex_1 (publication_id=?)")]

    nodes = _sqlite_plan_nodes(rows, aliases)

    assert aliases == {"p": "publication_pointers", "v": "publication_versions"}
    # Without alias resolution the bounded registry would read as relation "p".
    assert nodes[0].relation == "publication_pointers"
    assert nodes[0].node_type == "Seq Scan"
    assert nodes[1].relation == "publication_versions"
    assert nodes[1].node_type == "Index Scan"


def test_statement_aliases_ignore_keywords_that_follow_a_relation():
    aliases = _statement_aliases("SELECT * FROM player_game_logs WHERE season = ?")

    assert aliases == {}


def test_sqlite_primary_key_search_counts_as_indexed():
    rows = [(0, 0, 0, "SEARCH player_game_logs USING PRIMARY KEY (season=?)")]

    nodes = _sqlite_plan_nodes(rows, {})

    assert nodes[0].node_type == "Index Scan"
    assert _plans_are_indexed(_evidence(*nodes)) is True


def test_real_sqlite_capture_reports_a_resolved_relation(tmp_path):
    engine = _db(tmp_path)
    statement = (
        "SELECT p.stream_key FROM publication_pointers p "
        "JOIN publication_versions v ON v.publication_id = p.active_publication_id"
    )

    evidence = _query_plans(engine, measured_statements=((statement, None),))

    assert evidence[0].available is True
    relations = {node.relation for node in evidence[0].nodes}
    assert relations <= {"publication_pointers", "publication_versions"}


def test_captured_evidence_survives_a_statement_that_cannot_be_explained(tmp_path):
    engine = _db(tmp_path)

    evidence = _query_plans(engine, measured_statements=(("SELECT * FROM no_such_table", None),))

    assert evidence[0].available is False
    assert _plans_are_indexed(evidence) is False
