"""UTC normalization shared by persisted catalog and slate reads."""

from datetime import datetime, timedelta, timezone, tzinfo

from app.domain.utc import assume_utc, parse_utc_iso
from app.services.event_resolver import stored_timestamp


def test_assume_utc_treats_naive_values_as_utc_and_converts_aware_values():
    assert assume_utc(datetime(2026, 1, 2, 10)) == datetime(
        2026, 1, 2, 10, tzinfo=timezone.utc
    )
    assert assume_utc(
        datetime(2026, 1, 2, 4, tzinfo=timezone(-timedelta(hours=6)))
    ) == datetime(2026, 1, 2, 10, tzinfo=timezone.utc)


def test_assume_utc_treats_tzinfo_without_an_offset_as_naive():
    class NoOffset(tzinfo):
        def utcoffset(self, value):
            return None

    assert assume_utc(datetime(2026, 1, 2, 10, tzinfo=NoOffset())) == datetime(
        2026, 1, 2, 10, tzinfo=timezone.utc
    )


def test_parse_utc_iso_accepts_z_and_normalizes_offsets():
    assert parse_utc_iso("2026-01-02T10:00:00Z") == datetime(
        2026, 1, 2, 10, tzinfo=timezone.utc
    )
    assert parse_utc_iso("2026-01-02T04:00:00-06:00") == datetime(
        2026, 1, 2, 10, tzinfo=timezone.utc
    )


def test_persisted_event_timestamp_uses_shared_iso_and_naive_utc_rules():
    assert stored_timestamp("2026-01-02T04:00:00-06:00") == datetime(
        2026, 1, 2, 10, tzinfo=timezone.utc
    )
    assert stored_timestamp(datetime(2026, 1, 2, 10)) == datetime(
        2026, 1, 2, 10, tzinfo=timezone.utc
    )
