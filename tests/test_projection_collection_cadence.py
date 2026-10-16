from datetime import timedelta

import pytest

from app.config.settings import ProjectionCollectionSettings
from scripts.projection_collection_service import check_cadence


def test_the_default_cadence_keeps_lines_live_between_polls():
    check_cadence(ProjectionCollectionSettings())


@pytest.mark.parametrize("minutes", [10, 30])
def test_a_slow_interval_that_lets_lines_lapse_is_refused(minutes):
    settings = ProjectionCollectionSettings(slow_interval=timedelta(minutes=minutes))

    with pytest.raises(RuntimeError, match="slow_interval"):
        check_cadence(settings)


def test_eight_minutes_slow_and_five_fast_are_accepted():
    check_cadence(
        ProjectionCollectionSettings(
            slow_interval=timedelta(minutes=8), fast_interval=timedelta(minutes=5)
        )
    )


def test_a_fast_interval_that_lets_lines_lapse_is_refused():
    settings = ProjectionCollectionSettings(
        slow_interval=timedelta(minutes=8), fast_interval=timedelta(minutes=10)
    )

    with pytest.raises(RuntimeError, match="fast_interval"):
        check_cadence(settings)
