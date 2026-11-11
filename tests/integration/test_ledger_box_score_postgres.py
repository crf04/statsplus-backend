"""Box-score differences are recorded and listed on Postgres (crf04/statsplus-backend#763)."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text

from tests.services.test_ledger_box_score import (
    assert_the_command_lists_open_differences_until_the_provider_agrees,
)

pytestmark = [pytest.mark.integration, pytest.mark.real_migrations]


@pytest.fixture
def engine():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    yield engine
    engine.dispose()


def test_the_command_lists_open_differences_until_the_provider_agrees(engine, capsys):
    assert_the_command_lists_open_differences_until_the_provider_agrees(engine, capsys)
