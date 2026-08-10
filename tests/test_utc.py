"""UTC normalization shared by persisted catalog and slate reads."""

from datetime import datetime, timedelta, timezone

from app.domain.utc import assume_utc, parse_utc_iso


def test_assume_utc_treats_naive_values_as_utc_and_converts_aware_values():
    assert assume_utc(datetime(2026, 1, 2, 10)) == datetime(
        2026, 1, 2, 10, tzinfo=timezone.utc
    )
    assert assume_utc(
        datetime(2026, 1, 2, 4, tzinfo=timezone(-timedelta(hours=6)))
    ) == datetime(2026, 1, 2, 10, tzinfo=timezone.utc)


def test_parse_utc_iso_accepts_z_and_normalizes_offsets():
    assert parse_utc_iso("2026-01-02T10:00:00Z") == datetime(
        2026, 1, 2, 10, tzinfo=timezone.utc
    )
    assert parse_utc_iso("2026-01-02T04:00:00-06:00") == datetime(
        2026, 1, 2, 10, tzinfo=timezone.utc
    )
