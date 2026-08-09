"""Integration tests that run against a real Postgres database.

Production uses Postgres while the default test suite uses SQLite, so dialect
differences (timezone-aware timestamps, identifier casing, DDL support) are
invisible everywhere else. These tests close that gap.

They are skipped unless ``TEST_DATABASE_URL`` is set. That variable is
deliberately distinct from ``DATABASE_URL`` so a developer's real database can
never be dropped by a test run.
"""

import os
from datetime import datetime, timezone

import pandas as pd
import pytest
from sqlalchemy import create_engine, text

from app.models import Base

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def postgres_url():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping Postgres integration tests")
    return url


@pytest.fixture
def pg_engine(postgres_url):
    """Provide a Postgres engine with a freshly created schema."""
    engine = create_engine(postgres_url)

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    yield engine

    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def user_service(pg_engine, postgres_url):
    """Point UserService at the Postgres test database."""
    from app.config.settings import load_settings
    from app.services.user_service import UserService

    settings = load_settings(overrides={"DATABASE_URL": postgres_url})

    return UserService(pg_engine, settings=settings)


FIREBASE_USER = {
    "uid": "pg-uid-1",
    "email": "pg-user@example.com",
    "name": "Postgres User",
    "picture": "https://example.com/pg.jpg",
}


# --- schema ----------------------------------------------------------------


def test_user_model_ddl_applies_to_postgres(pg_engine):
    """The User model's columns and indexes must be creatable on Postgres."""
    with pg_engine.connect() as connection:
        columns = connection.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'users'"
            )
        ).scalars().all()
        indexes = connection.execute(
            text("SELECT indexname FROM pg_indexes WHERE tablename = 'users'")
        ).scalars().all()

    assert set(columns) == {
        "firebase_uid",
        "email",
        "display_name",
        "photo_url",
        "created_at",
        "last_login",
        "is_active",
    }
    assert "idx_users_email" in indexes
    assert "idx_users_last_login" in indexes
    assert "idx_users_created_at" in indexes


# --- UserService round trips ----------------------------------------------


def test_create_then_update_user_persists_changes(user_service):
    created = user_service.create_or_update_user(FIREBASE_USER)
    assert created is not None

    updated = user_service.create_or_update_user(
        {**FIREBASE_USER, "name": "Renamed User"}
    )

    assert updated is not None
    fetched = user_service.get_user_by_firebase_uid(FIREBASE_USER["uid"])
    assert fetched.display_name == "Renamed User"
    assert fetched.email == FIREBASE_USER["email"]


def test_lookup_by_email_matches_the_created_user(user_service):
    user_service.create_or_update_user(FIREBASE_USER)

    found = user_service.get_user_by_email(FIREBASE_USER["email"])

    assert found is not None
    assert found.firebase_uid == FIREBASE_USER["uid"]


def test_update_last_login_reports_success(user_service):
    user_service.create_or_update_user(FIREBASE_USER)

    assert user_service.update_last_login(FIREBASE_USER["uid"]) is True


def test_update_last_login_reports_failure_for_an_unknown_user(user_service):
    assert user_service.update_last_login("does-not-exist") is False


def test_deactivate_user_removes_them_from_the_active_count(user_service):
    user_service.create_or_update_user(FIREBASE_USER)
    assert user_service.get_all_active_users_count() == 1

    assert user_service.deactivate_user(FIREBASE_USER["uid"]) is True

    assert user_service.get_all_active_users_count() == 0


def test_deactivate_reports_failure_for_an_unknown_user(user_service):
    assert user_service.deactivate_user("does-not-exist") is False


def test_user_stats_are_computable_against_postgres_timestamps(user_service):
    """Postgres returns timezone-aware timestamps; the stats math must cope."""
    user_service.create_or_update_user(FIREBASE_USER)

    stats = user_service.get_user_stats(FIREBASE_USER["uid"])

    assert stats is not None
    assert stats["is_active"] is True
    assert stats["account_age_days"] == 0
    assert stats["days_since_last_login"] == 0


def test_user_stats_are_none_for_an_unknown_user(user_service):
    assert user_service.get_user_stats("does-not-exist") is None


def test_last_login_is_stored_as_utc_not_server_local_time(user_service):
    """A naive UTC write into a timestamptz column lands in the future.

    Postgres reads a naive value as server-local time, so writing
    ``datetime.utcnow()`` skews the stored instant by the session's UTC offset.
    """
    from datetime import datetime, timezone

    user_service.create_or_update_user(FIREBASE_USER)

    fetched = user_service.get_user_by_firebase_uid(FIREBASE_USER["uid"])
    skew = (datetime.now(timezone.utc) - fetched.last_login).total_seconds()

    assert skew >= 0, f"last_login is {-skew:.0f}s in the future"
    assert skew < 300


# --- dialect-sensitive table reads ----------------------------------------


def test_game_service_reads_a_normalized_table_on_postgres(pg_engine, monkeypatch):
    """The unquoted identifiers GameService emits must resolve on Postgres."""
    from app.services import game_service as game_service_module

    pd.DataFrame(
        [
            {"team": "LAL", "Transition": 1.10},
            {"team": "GSW", "Transition": 1.30},
        ]
    ).to_sql("team_play_types", pg_engine, index=False, if_exists="replace")

    monkeypatch.setattr(
        game_service_module, "get_redis_client", lambda *args, **kwargs: None
    )
    service = game_service_module.GameService(pg_engine)

    df = service._fetch_data_from_table("team_play_types")

    assert sorted(df["team"].tolist()) == ["GSW", "LAL"]

    with pg_engine.connect() as connection:
        connection.execute(text("DROP TABLE IF EXISTS team_play_types"))
        connection.commit()


def test_user_round_trip_preserves_the_serialized_shape(user_service):
    """to_dict must stay JSON-serializable with real Postgres timestamps."""
    user_service.create_or_update_user(FIREBASE_USER)

    fetched = user_service.get_user_by_firebase_uid(FIREBASE_USER["uid"])
    payload = fetched.to_dict()

    assert payload["firebase_uid"] == FIREBASE_USER["uid"]
    assert payload["is_active"] is True
    assert isinstance(payload["created_at"], str)
    assert isinstance(payload["last_login"], str)


# --- provider athlete mappings --------------------------------------------


@pytest.fixture
def mapping_engine(postgres_url):
    """Provide a migrated Postgres database for the mapping repository."""
    from sqlalchemy import insert

    from app.migrations import run_migrations
    from app.models.athlete_catalog import AthleteCatalog

    engine = create_engine(postgres_url)
    Base.metadata.drop_all(engine)
    # The bookkeeping table is not part of the model metadata, so it is dropped
    # explicitly; a leftover row would otherwise skip the migration.
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS schema_migrations"))
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(
            insert(AthleteCatalog.__table__),
            [
                {
                    "season": "2024-25",
                    "player_id": player_id,
                    "display_name": name,
                    "roster_status": "active",
                    "is_active": True,
                    "is_active_for_season": True,
                    "team_id": 1610612747,
                    "team_name": "Los Angeles Lakers",
                    "team_abbreviation": "LAL",
                    "published_at": datetime(2026, 8, 9, 12, tzinfo=timezone.utc),
                }
                for player_id, name in ((15, "Nikola Jokic"), (23, "LeBron James"))
            ],
        )

    yield engine

    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS schema_migrations"))
    Base.metadata.drop_all(engine)
    engine.dispose()


def _mapping_resolution(
    name="Nikola Jokic",
    provider_id="pp-15",
    rows=None,
    team_canonical_id=1610612747,
    observed_at=None,
):
    from app.providers.dfs import AthleteEvidence, TeamEvidence
    from app.services.athlete_resolver import AthleteResolver

    rows = rows or [
        {
            "season": "2024-25",
            "player_id": 15,
            "display_name": "Nikola Jokic",
            "roster_status": "active",
            "is_active": True,
            "is_active_for_season": True,
            "team_id": 1610612747,
            "team_name": "Los Angeles Lakers",
            "team_abbreviation": "LAL",
        }
    ]

    class _Catalog:
        def get_catalog(self, season, *, active_only=False):
            return rows

    return AthleteResolver(_Catalog()).resolve(
        "prizepicks",
        AthleteEvidence(
            provider_id=provider_id,
            name=name,
            team=TeamEvidence(provider_id="pp-lal", canonical_id=team_canonical_id),
        ),
        "2024-25",
        observed_at=observed_at,
    )


def test_athlete_mapping_round_trip_on_postgres(mapping_engine):
    """Mapping state, decisions, and candidates must round-trip on Postgres."""
    from app.services.athlete_mapping_repository import AthleteMappingRepository

    repository = AthleteMappingRepository(mapping_engine)

    automatic = repository.persist_auto_decision(_mapping_resolution())
    assert automatic.state == "auto"
    assert automatic.persisted is True

    ambiguous_rows = [
        {
            "season": "2024-25",
            "player_id": player_id,
            "display_name": "LeBron James",
            "roster_status": "active",
            "is_active": True,
            "is_active_for_season": True,
            "team_id": 1610612747,
            "team_name": "Los Angeles Lakers",
            "team_abbreviation": "LAL",
        }
        for player_id in (15, 23)
    ]
    repository.record_resolution(
        _mapping_resolution(
            name="LeBron James", provider_id="pp-77", rows=ambiguous_rows
        )
    )

    unresolved = repository.list_unresolved(provider="prizepicks")
    assert [item.provider_athlete_id for item in unresolved] == ["pp-77"]
    assert [
        candidate.canonical_player_id for candidate in unresolved[0].candidates
    ] == [15, 23]
    assert unresolved[0].provider_team_canonical_id == 1610612747

    approved = repository.approve(
        "prizepicks",
        "pp-77",
        23,
        season="2024-25",
        operator_id="ops@example.com",
        reason="reviewed source identity",
    )
    assert approved.mapping.canonical_player_id == 23
    # The identity is decided, so it leaves the operator queue.
    assert repository.list_unresolved(provider="prizepicks") == []

    repository.reject(
        "prizepicks",
        "pp-15",
        operator_id="ops@example.com",
        reason="provider identity is not trusted",
    )
    assert repository.is_rejected("prizepicks", "pp-15")
    assert repository.clear_rejection(
        "prizepicks", "pp-15", operator_id="ops@example.com", reason="new evidence"
    )
    assert repository.is_rejected("prizepicks", "pp-15") is False


def test_concurrent_first_mapping_writes_one_row_on_postgres(mapping_engine, postgres_url):
    """Overlapping writers must serialize in the database, not in one process.

    Each worker owns a separate engine and repository, so the repository's
    process-local identity lock is per-engine and cannot stand in for the
    database guarantee. A barrier releases every worker at once so the
    transactions genuinely overlap, and the duplicate lock row must not abort
    the surrounding transaction: PostgreSQL keeps a transaction unusable while
    a failed savepoint is still open, so it has to be rolled back.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from app.services.athlete_mapping_repository import AthleteMappingRepository

    workers = 4
    barrier = threading.Barrier(workers, timeout=30)
    engines = [create_engine(postgres_url) for _ in range(workers)]
    resolution = _mapping_resolution()

    def _write(engine):
        repository = AthleteMappingRepository(engine)
        barrier.wait()
        return repository.persist_auto_decision(resolution)

    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(_write, engines))
        # Distinct engines mean distinct process-local locks, so the single
        # write below was decided by the database rather than by this process.
        assert (
            len(
                {
                    id(AthleteMappingRepository._identity_locks[
                        (id(engine), "prizepicks", "pp-15")
                    ])
                    for engine in engines
                }
            )
            == workers
        )
    finally:
        for engine in engines:
            engine.dispose()

    reader = AthleteMappingRepository(mapping_engine)
    assert sum(result.persisted for result in results) == 1
    assert len(reader.list_mappings()) == 1
    assert len(reader.history(provider="prizepicks")) == 1


def test_a_stale_observation_never_supersedes_a_manual_decision_on_postgres(
    mapping_engine, postgres_url
):
    """Governance wins over a stale board read when the two genuinely overlap.

    The resolver reads outside the identity transaction, so its unmatched
    result may already be obsolete by the time it is persisted. Each worker
    owns its own engine, so the ordering is decided by the database rather than
    by this process, and either order must end with the operator's decision.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from app.services.athlete_mapping_repository import AthleteMappingRepository

    stale = _mapping_resolution(name="King James", provider_id="pp-77")
    assert stale.state.value == "unmatched"
    barrier = threading.Barrier(2, timeout=30)
    engines = [create_engine(postgres_url) for _ in range(2)]

    def _observe(engine):
        repository = AthleteMappingRepository(engine)
        barrier.wait()
        return repository.record_resolution(stale)

    def _approve(engine):
        repository = AthleteMappingRepository(engine)
        barrier.wait()
        return repository.approve(
            "prizepicks",
            "pp-77",
            23,
            season="2024-25",
            operator_id="ops@example.com",
            reason="reviewed source identity",
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(_observe, engines[0]),
                executor.submit(_approve, engines[1]),
            ]
            for future in futures:
                future.result(timeout=60)
    finally:
        for engine in engines:
            engine.dispose()

    reader = AthleteMappingRepository(mapping_engine)
    states = [
        item.decision_state
        for item in reader.history(provider="prizepicks", provider_athlete_id="pp-77")
    ]
    assert states[-1] == "manual_approved"
    assert states.count("manual_approved") == 1
    assert reader.list_unresolved(provider="prizepicks") == []
    mapping = reader.get_active_mapping("prizepicks", "pp-77")
    assert mapping is not None
    assert mapping.mapping_state == "manual_approved"


def test_team_conflict_promotes_a_racing_auto_mapping_on_postgres(
    mapping_engine, postgres_url
):
    """The promotion must be decided inside the identity transaction.

    The automatic mapping is committed by another engine after the resolver
    read and before the observing append takes the lock, so a lookup outside
    the transaction cannot see it. Postgres row locks order the two writes.
    """
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager

    from app.services.athlete_mapping_repository import AthleteMappingRepository

    conflicting = _mapping_resolution(team_canonical_id=1610612743)
    assert conflicting.state.value == "team_conflict"

    observer = AthleteMappingRepository(mapping_engine)
    serialized = AthleteMappingRepository._transaction.__get__(observer)
    racing_engine = create_engine(postgres_url)
    raced = []

    @contextmanager
    def _transaction_after_a_racing_commit(provider, provider_id):
        if not raced:
            raced.append(True)
            racer = AthleteMappingRepository(racing_engine)
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(
                    racer.record_resolution, _mapping_resolution()
                ).result(timeout=60)
        with serialized(provider, provider_id) as connection:
            yield connection

    observer._transaction = _transaction_after_a_racing_commit
    try:
        result = observer.record_resolution(conflicting)
    finally:
        racing_engine.dispose()

    assert raced
    assert result.state == "mapping_conflict"
    reader = AthleteMappingRepository(mapping_engine)
    mapping = reader.get_mapping("prizepicks", "pp-15")
    assert mapping is not None
    assert mapping.is_active is False
    assert mapping.canonical_player_id == 15
    assert mapping.conflict_canonical_player_id == 15
    assert mapping.provider_team_canonical_id == 1610612743
    assert [
        item.decision_state
        for item in reader.history(provider="prizepicks", provider_athlete_id="pp-15")
    ] == ["auto", "mapping_conflict"]
    assert reader.list_unresolved(provider="prizepicks") == []


#: Fixed clearing timestamp for direct-insert constraint cases.
_CLEARED_AT = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "clearing",
    [
        {"is_active": True, "cleared_at": _CLEARED_AT},
        {"is_active": True, "cleared_by": "ops@example.com"},
        {"is_active": True, "clear_reason": "identity was reinstated"},
        {"is_active": False},
        {"is_active": False, "cleared_at": _CLEARED_AT},
        {"is_active": False, "cleared_by": "ops@example.com", "clear_reason": "fixed"},
    ],
)
def test_rejection_clear_check_rejects_partial_evidence_on_postgres(
    mapping_engine, clearing
):
    """The clearing check has to hold on the production dialect too."""
    from sqlalchemy import insert
    from sqlalchemy.exc import IntegrityError

    from app.models.athlete_mapping import AthleteMappingRejection

    with pytest.raises(IntegrityError), mapping_engine.begin() as connection:
        connection.execute(
            insert(AthleteMappingRejection.__table__).values(
                provider="prizepicks",
                provider_athlete_id="pp-99",
                reason="duplicate provider identity",
                operator_id="ops@example.com",
                created_at=_CLEARED_AT,
                **clearing,
            )
        )


def test_repeated_state_after_a_different_observation_persists_on_postgres(mapping_engine):
    """Suppression is per transition on a real database as well as SQLite."""
    from app.services.athlete_mapping_repository import AthleteMappingRepository

    repository = AthleteMappingRepository(mapping_engine)
    ambiguous_rows = [
        {
            "season": "2024-25",
            "player_id": player_id,
            "display_name": "Nikola Jokic",
            "roster_status": "active",
            "is_active": True,
            "is_active_for_season": True,
            "team_id": 1610612747,
            "team_name": "Los Angeles Lakers",
            "team_abbreviation": "LAL",
        }
        for player_id in (15, 23)
    ]

    repository.record_resolution(_mapping_resolution())
    repository.record_resolution(_mapping_resolution(rows=ambiguous_rows))
    repeated = repository.record_resolution(_mapping_resolution())

    assert repeated.persisted is True
    assert [
        item.decision_state
        for item in repository.history(provider="prizepicks", provider_athlete_id="pp-15")
    ] == ["auto", "ambiguous", "auto"]


@pytest.mark.parametrize(
    "conflict",
    [
        {"conflict_canonical_player_id": 23},
        {"conflict_canonical_name": "LeBron James"},
    ],
)
def test_conflict_columns_check_rejects_a_non_conflict_row_on_postgres(
    mapping_engine, conflict
):
    """Only a current conflict may name a conflicting athlete on Postgres too."""
    from sqlalchemy import insert
    from sqlalchemy.exc import IntegrityError

    from app.models.athlete_mapping import ProviderAthleteMapping

    with pytest.raises(IntegrityError), mapping_engine.begin() as connection:
        connection.execute(
            insert(ProviderAthleteMapping.__table__).values(
                provider="prizepicks",
                provider_athlete_id="pp-99",
                mapping_state="auto",
                is_active=True,
                first_seen_at=_CLEARED_AT,
                last_seen_at=_CLEARED_AT,
                **conflict,
            )
        )


def test_a_delayed_observation_cannot_undo_a_clearance_on_postgres(mapping_engine):
    """Observation order has to hold against real timestamptz columns."""
    from app.services.athlete_mapping_repository import AthleteMappingRepository

    observed_before = datetime(2026, 8, 9, 11, tzinfo=timezone.utc)
    observed_after = datetime(2026, 8, 9, 13, tzinfo=timezone.utc)
    reused_rows = [
        {
            "season": "2024-25",
            "player_id": 23,
            "display_name": "LeBron James",
            "roster_status": "active",
            "is_active": True,
            "is_active_for_season": True,
            "team_id": 1610612747,
            "team_name": "Los Angeles Lakers",
            "team_abbreviation": "LAL",
        }
    ]
    # The operator decisions are taken between the two provider reads.
    repository = AthleteMappingRepository(mapping_engine, clock=lambda: _CLEARED_AT)
    stale = _mapping_resolution(observed_at=observed_before)

    assert repository.record_resolution(stale).state == "auto"
    repository.reject(
        "prizepicks",
        "pp-15",
        operator_id="ops@example.com",
        reason="provider identity is not trusted",
    )
    assert repository.clear_rejection(
        "prizepicks",
        "pp-15",
        operator_id="ops@example.com",
        reason="the identity was reinstated",
    )
    reused = _mapping_resolution(
        name="LeBron James", rows=reused_rows, observed_at=observed_after
    )
    assert repository.record_resolution(reused).state == "auto"

    replayed = repository.record_resolution(stale)

    assert replayed.persisted is False
    mapping = repository.get_active_mapping("prizepicks", "pp-15")
    assert mapping is not None
    assert mapping.canonical_player_id == 23
    assert mapping.conflict_canonical_player_id is None
    assert repository.list_conflicts() == []
    assert [
        item.decision_state
        for item in repository.history(provider="prizepicks", provider_athlete_id="pp-15")
    ] == ["auto", "rejected", "rejection_cleared", "auto"]
