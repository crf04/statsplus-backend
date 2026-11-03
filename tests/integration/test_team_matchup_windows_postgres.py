"""Postgres checks for the Defense Sheet window's stored-fact summaries
(crf04/statsplus-backend#672): PostgreSQL stores NaN and the infinities that
SQLite cannot, and the summary must never prove one of them rankable."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

from app.migrations import run_migrations
from tests.services.test_team_matchup_windows import (
    PUBLISHED_AT,
    REPLACED_BASE_CASES,
    check_replaced_base_freshness,
)

pytestmark = [pytest.mark.integration, pytest.mark.real_migrations]


@pytest.fixture
def engine():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    engine = create_engine(url, pool_size=4)
    _empty_schema(engine)
    run_migrations(engine)
    yield engine
    # Leave the worker's database as empty as it was found: a fixture that
    # drops only model tables would trip over this module's migrated schema.
    _empty_schema(engine)
    engine.dispose()


def _empty_schema(engine):
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))


@pytest.mark.parametrize(
    ("damage", "answer", "rows_read"),
    [
        *REPLACED_BASE_CASES,
        pytest.param({"raw_value": float("nan")}, PUBLISHED_AT, 1, id="a NaN value"),
        pytest.param({"denominator_value": float("nan")}, PUBLISHED_AT, 1, id="a NaN denominator"),
        pytest.param({"denominator_value": float("inf")}, PUBLISHED_AT, 1, id="an infinite denominator"),
        pytest.param({"raw_value": float("-inf")}, PUBLISHED_AT, 1, id="a negative infinite value"),
    ],
)
def test_a_publication_backed_base_answers_as_its_ranked_stored_facts_would(
    engine, damage, answer, rows_read
):
    check_replaced_base_freshness(engine, damage, answer=answer, rows_read=rows_read)
