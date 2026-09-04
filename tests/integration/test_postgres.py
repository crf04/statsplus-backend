"""Integration tests that run against a real Postgres database.

Production uses Postgres while the default test suite uses SQLite, so dialect
differences (timezone-aware timestamps, identifier casing, DDL support) are
invisible everywhere else. These tests close that gap.

They are skipped unless ``TEST_DATABASE_URL`` is set. That variable is
deliberately distinct from ``DATABASE_URL`` so a developer's real database can
never be dropped by a test run.
"""

import os
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError

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


def test_saved_filter_set_ddl_applies_to_postgres(pg_engine):
    """The functional uniqueness index must be creatable on Postgres."""
    with pg_engine.connect() as connection:
        columns = connection.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'saved_filter_sets'"
            )
        ).scalars().all()
        indexes = connection.execute(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE tablename = 'saved_filter_sets'"
            )
        ).scalars().all()

    assert set(columns) == {
        "id",
        "firebase_uid",
        "name",
        "query_string",
        "created_at",
        "updated_at",
    }
    # Postgres renders the indexed expression with an explicit cast, e.g.
    # ``lower((name)::text)``, so match the function call loosely rather than
    # the exact source spelling.
    assert any(
        "UNIQUE INDEX uq_saved_filter_sets_owner_name" in definition
        and "lower(" in definition
        for definition in indexes
    )


def test_saved_filter_sets_round_trip_on_postgres(user_service):
    user_service.create_or_update_user(FIREBASE_USER)

    created = user_service.create_saved_filter_set(
        FIREBASE_USER["uid"],
        name="Jokic at home",
        query_string="player=Nikola+Jokic&location_filter=Home",
    )
    renamed = user_service.rename_saved_filter_set(
        FIREBASE_USER["uid"], created["id"], name="Jokic home splits"
    )

    assert renamed["name"] == "Jokic home splits"
    assert user_service.list_saved_filter_sets(FIREBASE_USER["uid"]) == [renamed]

    user_service.delete_saved_filter_set(FIREBASE_USER["uid"], created["id"])
    assert user_service.list_saved_filter_sets(FIREBASE_USER["uid"]) == []


def test_postgres_refuses_a_duplicate_saved_filter_set_name_ignoring_case(
    user_service,
):
    from app.errors import ConflictError

    user_service.create_or_update_user(FIREBASE_USER)
    user_service.create_saved_filter_set(
        FIREBASE_USER["uid"], name="Jokic at home", query_string="player=Jokic"
    )

    with pytest.raises(ConflictError):
        user_service.create_saved_filter_set(
            FIREBASE_USER["uid"], name="JOKIC AT HOME", query_string="player=Jokic"
        )


def test_deleting_a_postgres_account_cascades_to_its_saved_filter_sets(
    pg_engine, user_service
):
    user_service.create_or_update_user(FIREBASE_USER)
    user_service.create_saved_filter_set(
        FIREBASE_USER["uid"], name="Doomed", query_string="player=Jokic"
    )

    with pg_engine.begin() as connection:
        connection.execute(
            text("DELETE FROM users WHERE firebase_uid = :uid"),
            {"uid": FIREBASE_USER["uid"]},
        )

    with pg_engine.connect() as connection:
        assert connection.execute(
            text("SELECT COUNT(*) FROM saved_filter_sets")
        ).scalar() == 0


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
            {"id": 101, "full_name": "Player One"},
            {"id": 202, "full_name": "Player Two"},
        ]
    ).to_sql("player_information", pg_engine, index=False, if_exists="replace")

    monkeypatch.setattr(
        game_service_module, "get_redis_client", lambda *args, **kwargs: None
    )
    service = game_service_module.GameService(pg_engine)

    df = service._fetch_data_from_table("player_information")

    assert sorted(df["full_name"].tolist()) == ["Player One", "Player Two"]

    with pg_engine.connect() as connection:
        connection.execute(text("DROP TABLE IF EXISTS player_information"))
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
    repository=None,
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

    return AthleteResolver(_Catalog(), mapping_repository=repository).resolve(
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


def test_a_repeated_observation_fences_a_delayed_read_on_postgres(mapping_engine):
    """The observation clock has to round-trip through a real timestamptz."""
    from sqlalchemy import and_, select

    from app.models.athlete_mapping import AthleteMappingLock
    from app.services.athlete_mapping_repository import AthleteMappingRepository

    observed_before = datetime(2026, 8, 9, 11, tzinfo=timezone.utc)
    observed_between = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
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
    repository = AthleteMappingRepository(mapping_engine, clock=lambda: _CLEARED_AT)

    assert repository.record_resolution(
        _mapping_resolution(observed_at=observed_before)
    ).persisted is True
    # The same evidence again: one durable decision, a newer observation clock.
    assert repository.record_resolution(
        _mapping_resolution(observed_at=observed_after)
    ).persisted is False
    with mapping_engine.connect() as connection:
        stored = connection.execute(
            select(AthleteMappingLock.last_observed_at).where(
                and_(
                    AthleteMappingLock.provider == "prizepicks",
                    AthleteMappingLock.provider_athlete_id == "pp-15",
                )
            )
        ).scalar()
    assert stored == observed_after

    delayed = repository.record_resolution(
        _mapping_resolution(
            name="LeBron James", rows=reused_rows, observed_at=observed_between
        )
    )

    assert delayed.persisted is False
    mapping = repository.get_active_mapping("prizepicks", "pp-15")
    assert mapping is not None
    assert mapping.canonical_player_id == 15
    assert repository.list_conflicts() == []
    assert [
        item.decision_state
        for item in repository.history(provider="prizepicks", provider_athlete_id="pp-15")
    ] == ["auto"]


def test_an_inactive_only_observation_withdraws_the_mapping_on_postgres(mapping_engine):
    """The withdrawn state has to satisfy the Postgres check constraints."""
    from app.services.athlete_mapping_repository import AthleteMappingRepository

    inactive_rows = [
        {
            "season": "2024-25",
            "player_id": 15,
            "display_name": "Nikola Jokic",
            "roster_status": "inactive",
            "is_active": False,
            "is_active_for_season": False,
            "team_id": 1610612747,
            "team_name": "Los Angeles Lakers",
            "team_abbreviation": "LAL",
        }
    ]
    repository = AthleteMappingRepository(mapping_engine, clock=lambda: _CLEARED_AT)
    assert repository.record_resolution(_mapping_resolution()).state == "auto"

    withdrawn = repository.record_resolution(_mapping_resolution(rows=inactive_rows))

    assert withdrawn.state == "inactive_only"
    assert repository.get_active_mapping("prizepicks", "pp-15") is None
    mapping = repository.get_mapping("prizepicks", "pp-15")
    assert mapping is not None
    assert mapping.mapping_state == "inactive_only"
    assert mapping.canonical_player_id == 15
    assert [
        item.decision_state for item in repository.list_unresolved(provider="prizepicks")
    ] == ["inactive_only"]

    assert repository.record_resolution(_mapping_resolution()).state == "auto"

    remapped = repository.get_active_mapping("prizepicks", "pp-15")
    assert remapped is not None
    assert remapped.mapping_state == "auto"
    assert remapped.conflict_canonical_player_id is None
    assert repository.list_unresolved(provider="prizepicks") == []


def test_a_vanished_catalog_row_withdraws_the_mapping_on_postgres(mapping_engine):
    """The unmatched withdrawal has to satisfy the Postgres check constraints."""
    from app.services.athlete_mapping_repository import AthleteMappingRepository

    other_rows = [
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
    repository = AthleteMappingRepository(mapping_engine, clock=lambda: _CLEARED_AT)
    assert repository.record_resolution(_mapping_resolution()).state == "auto"

    withdrawn = repository.record_resolution(
        _mapping_resolution(rows=other_rows, repository=repository)
    )

    assert withdrawn.state == "unmatched"
    assert repository.get_active_mapping("prizepicks", "pp-15") is None
    mapping = repository.get_mapping("prizepicks", "pp-15")
    assert mapping is not None
    assert mapping.mapping_state == "unmatched"
    assert mapping.canonical_player_id == 15
    queued = repository.list_unresolved(provider="prizepicks")
    assert [item.decision_state for item in queued] == ["unmatched"]
    assert [candidate.canonical_player_id for candidate in queued[0].candidates] == [15]

    assert repository.record_resolution(_mapping_resolution()).state == "auto"

    remapped = repository.get_active_mapping("prizepicks", "pp-15")
    assert remapped is not None
    assert remapped.mapping_state == "auto"
    assert remapped.canonical_player_id == 15


def test_contradictory_evidence_round_trips_on_postgres(mapping_engine):
    """Every evidence a contradiction was recorded over must be durable."""
    from dataclasses import replace

    from app.services.athlete_mapping_repository import AthleteMappingRepository
    from app.services.athlete_resolver import MappingResolutionState

    repository = AthleteMappingRepository(mapping_engine)
    observed = [
        _mapping_resolution(name=name, team_canonical_id=team_canonical_id)
        for name, team_canonical_id in (
            ("Nikola Jokic", 1610612747),
            ("Unknown Player", 1610612738),
        )
    ]
    contradiction = replace(
        observed[0],
        state=MappingResolutionState.MAPPING_CONFLICT,
        candidates=(observed[0].canonical_athlete,),
        contradictory_evidence=tuple(item.provider_evidence for item in observed),
        reason="contradictory_provider_evidence",
    )

    first = repository.record_resolution(contradiction)
    repeated = repository.record_resolution(contradiction)

    assert first.persisted is True
    # The same contradiction observed again is not a second observation.
    assert repeated.persisted is False
    (queued,) = repository.list_conflicts(provider="prizepicks")
    evidence = queued.latest_decision.contradictory_evidence
    assert [item.provider_name for item in evidence] == [
        "Nikola Jokic",
        "Unknown Player",
    ]
    assert [item.provider_team_canonical_id for item in evidence] == [
        1610612747,
        1610612738,
    ]
    assert [item.provider_athlete_id for item in evidence] == ["pp-15", "pp-15"]
    (recorded,) = repository.history(
        provider="prizepicks", provider_athlete_id="pp-15"
    )
    assert recorded.contradictory_evidence == evidence


# --- provider event mappings ----------------------------------------------


EVENT_SEASON = "2025-26"
EVENT_LAL = 1610612747
EVENT_SAS = 1610612759
EVENT_TIP_OFF = datetime(2025, 10, 23, tzinfo=timezone.utc)
EVENT_NOW = datetime(2025, 10, 22, 12, tzinfo=timezone.utc)


def _catalog_values(game_id, *, offset_hours=0.0, home_team_id=EVENT_LAL):
    return {
        "nba_game_id": game_id,
        "season": EVENT_SEASON,
        "scheduled_at": EVENT_TIP_OFF + timedelta(hours=offset_hours),
        "home_team_id": home_team_id,
        "home_team_name": "Los Angeles Lakers",
        "home_team_tricode": "LAL",
        "away_team_id": EVENT_SAS,
        "away_team_name": "San Antonio Spurs",
        "away_team_tricode": "SAS",
        "status_text": "Scheduled",
        "status_code": 1,
        "postponed_status": None,
        "postponement_evidence": None,
        "classification": "Regular Season",
        "first_seen_at": EVENT_NOW,
        "last_seen_at": EVENT_NOW,
    }


@pytest.fixture
def event_mapping_engine(postgres_url):
    """Provide a migrated Postgres database for the event mapping repository."""
    from sqlalchemy import insert

    from app.migrations import run_migrations
    from app.models.event_catalog import EventCatalogEntry, EventCatalogRefresh

    engine = create_engine(postgres_url)
    Base.metadata.drop_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS schema_migrations"))
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(
            insert(EventCatalogEntry.__table__),
            [
                _catalog_values("0022500001"),
                _catalog_values("0022500002", offset_hours=48),
            ],
        )
        connection.execute(
            insert(EventCatalogRefresh.__table__).values(
                season=EVENT_SEASON,
                last_attempt_at=EVENT_NOW,
                last_success_at=EVENT_NOW,
                event_count=2,
            )
        )

    yield engine

    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS schema_migrations"))
    Base.metadata.drop_all(engine)
    engine.dispose()


class _EventCatalogRows:
    """The two read seams the event resolver needs, without a refresh path."""

    def __init__(self, rows):
        self.rows = rows

    def get_events(self, season):
        return [row for row in self.rows if row["season"] == season]

    def get_freshness(self, season):
        return {"season": season, "fresh": True, "last_success_at": EVENT_NOW}


def _event_evidence(
    *,
    provider_id="ud-1",
    canonical_id=None,
    label="Lakers vs Spurs",
    starts_at=EVENT_TIP_OFF,
    home="LAL",
    away_name=None,
    status_label="scheduled",
):
    from app.providers.dfs import EventEvidence, TeamEvidence

    return EventEvidence(
        provider_id=provider_id,
        canonical_id=canonical_id,
        label=label,
        starts_at=starts_at,
        status_label=status_label,
        home_team=TeamEvidence(provider_id=f"ud-{home.lower()}", abbreviation=home),
        away_team=TeamEvidence(
            provider_id="ud-sas", abbreviation="SAS", name=away_name
        ),
    )


def _event_resolution(
    *,
    evidence=None,
    rows=None,
    repository=None,
    observed_at=None,
):
    from app.services.event_resolver import EventResolver

    rows = rows if rows is not None else [_catalog_values("0022500001")]
    return EventResolver(
        _EventCatalogRows(rows), mapping_repository=repository
    ).resolve(
        "underdog",
        evidence if evidence is not None else _event_evidence(),
        EVENT_SEASON,
        observed_at=observed_at,
    )


def _event_contradiction(*, away_name=None):
    """One fail-closed observation asserting two events for one identity."""
    from app.services.event_resolver import EventResolution, EventResolutionState

    asserted = (
        _event_evidence(canonical_id="ud-evt-1", home="LAL"),
        _event_evidence(
            canonical_id="ud-evt-2",
            label="Celtics vs Spurs",
            home="BOS",
            away_name=away_name,
            status_label="live",
        ),
    )
    return EventResolution(
        provider="underdog",
        provider_evidence=asserted[0],
        season=EVENT_SEASON,
        state=EventResolutionState.MAPPING_CONFLICT,
        contradictory_evidence=asserted,
        reason="contradictory_provider_evidence",
    )


def test_migration_008_event_schema_applies_to_postgres(event_mapping_engine):
    """The event mapping tables, including contradictions, must exist here.

    Migration 008 owns the whole event mapping schema, so a child table added
    to it has to be created by the same migration on a real database rather
    than only by the model metadata a test fixture creates.
    """
    with event_mapping_engine.connect() as connection:
        tables = set(
            connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )
            ).scalars().all()
        )
        columns = set(
            connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'event_mapping_decision_contradictions'"
                )
            ).scalars().all()
        )
        primary_key = connection.execute(
            text(
                "SELECT a.attname FROM pg_index i "
                "JOIN pg_attribute a ON a.attrelid = i.indrelid "
                "AND a.attnum = ANY(i.indkey) "
                "WHERE i.indrelid = "
                "'event_mapping_decision_contradictions'::regclass "
                "AND i.indisprimary"
            )
        ).scalars().all()
        cascade = connection.execute(
            text(
                "SELECT c.confdeltype FROM pg_constraint c "
                "WHERE c.conrelid = "
                "'event_mapping_decision_contradictions'::regclass "
                "AND c.contype = 'f'"
            )
        ).scalars().all()
        checks = set(
            connection.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = 'provider_event_mappings'::regclass "
                    "AND contype = 'c'"
                )
            ).scalars().all()
        )

    assert {
        "provider_event_mappings",
        "event_mapping_decisions",
        "event_mapping_decision_candidates",
        "event_mapping_decision_contradictions",
        "event_mapping_rejections",
        "event_mapping_locks",
    } <= tables
    assert {
        "decision_id",
        "evidence_index",
        "provider_event_id",
        "provider_canonical_event_id",
        "provider_starts_at",
        "provider_ends_at",
        "provider_updated_at",
        "provider_home_team_abbreviation",
        "provider_away_team_name",
    } <= columns
    assert sorted(primary_key) == ["decision_id", "evidence_index"]
    # 'c' is ON DELETE CASCADE: a decision's evidence may not outlive it.
    assert cascade == ["c"]
    assert {
        "ck_provider_event_mapping_state",
        "ck_provider_event_mapping_active_state",
        "ck_provider_event_mapping_conflict_fields",
    } <= checks
    assert "provider_canonical_event_id" in set(
        _postgres_columns(event_mapping_engine, "provider_event_mappings")
    )
    assert "provider_canonical_event_id" in set(
        _postgres_columns(event_mapping_engine, "event_mapping_decisions")
    )


def _postgres_columns(engine, table_name):
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = :table"
            ),
            {"table": table_name},
        ).scalars().all()


@pytest.mark.parametrize(
    ("state", "is_active", "conflict_event_id"),
    [
        ("auto", False, None),
        ("ambiguous", True, None),
        ("auto", True, "0022500002"),
    ],
)
def test_the_event_schema_rejects_an_incoherent_row_on_postgres(
    event_mapping_engine, state, is_active, conflict_event_id
):
    """The governance checks must be enforced by Postgres, not only by SQLite."""
    from sqlalchemy import insert
    from sqlalchemy.exc import IntegrityError

    from app.models.event_mapping import ProviderEventMapping

    with pytest.raises(IntegrityError):
        with event_mapping_engine.begin() as connection:
            connection.execute(
                insert(ProviderEventMapping.__table__).values(
                    provider="underdog",
                    provider_event_id="ud-1",
                    mapping_state=state,
                    is_active=is_active,
                    conflict_canonical_event_id=conflict_event_id,
                    first_seen_at=EVENT_NOW,
                    last_seen_at=EVENT_NOW,
                )
            )


def test_event_mapping_round_trip_on_postgres(event_mapping_engine):
    """State, decisions, candidates, and the provider claim must round-trip."""
    from app.services.event_mapping_repository import EventMappingRepository

    repository = EventMappingRepository(event_mapping_engine)

    automatic = repository.record_resolution(
        _event_resolution(evidence=_event_evidence(canonical_id="ud-evt-1"))
    )
    assert automatic.state == "auto"
    assert automatic.persisted is True
    assert automatic.mapping.provider_canonical_event_id == "ud-evt-1"

    ambiguous = repository.record_resolution(
        _event_resolution(
            evidence=_event_evidence(provider_id="ud-2"),
            rows=[
                _catalog_values("0022500001"),
                _catalog_values("0022500003", offset_hours=3),
            ],
        )
    )
    assert ambiguous.state == "ambiguous"
    (queued,) = repository.list_unresolved(provider="underdog")
    assert [candidate.canonical_event_id for candidate in queued.candidates] == [
        "0022500001",
        "0022500003",
    ]
    assert queued.candidates[1].start_offset_seconds == 3 * 3600

    approved = repository.approve(
        "underdog",
        "ud-2",
        "0022500002",
        season=EVENT_SEASON,
        operator_id="ops@example.com",
        reason="reviewed the schedule",
    )
    assert approved.mapping.canonical_event_id == "0022500002"
    assert repository.list_unresolved(provider="underdog") == []

    repository.reject(
        "underdog", "ud-1", operator_id="ops@example.com", reason="not an NBA fixture"
    )
    assert repository.is_rejected("underdog", "ud-1")
    assert repository.clear_rejection(
        "underdog", "ud-1", operator_id="ops@example.com", reason="new evidence"
    )
    assert repository.is_rejected("underdog", "ud-1") is False


def test_deleting_an_event_decision_cascades_its_children_on_postgres(
    event_mapping_engine,
):
    """Candidates and contradictions belong to the decision that recorded them."""
    from sqlalchemy import delete, func, select

    from app.models.event_mapping import (
        EventMappingDecision,
        EventMappingDecisionCandidate,
        EventMappingDecisionContradiction,
    )
    from app.services.event_mapping_repository import EventMappingRepository

    repository = EventMappingRepository(event_mapping_engine)
    repository.record_resolution(
        _event_resolution(
            evidence=_event_evidence(provider_id="ud-2"),
            rows=[
                _catalog_values("0022500001"),
                _catalog_values("0022500003", offset_hours=3),
            ],
        )
    )
    repository.record_resolution(_event_contradiction())

    def _counts(connection):
        return tuple(
            connection.execute(select(func.count()).select_from(table)).scalar()
            for table in (
                EventMappingDecisionCandidate.__table__,
                EventMappingDecisionContradiction.__table__,
            )
        )

    with event_mapping_engine.begin() as connection:
        assert _counts(connection) == (2, 2)
        connection.execute(delete(EventMappingDecision.__table__))
        assert _counts(connection) == (0, 0)


def test_an_event_contradiction_round_trips_and_is_idempotent_on_postgres(
    event_mapping_engine,
):
    """Every evidence a contradiction asserted is durable, once.

    The same contradiction observed again is not a second observation, and
    reversing the order it was asserted in does not make it one; changing any
    part of it -- including the evidence the decision was not recorded on --
    does.
    """
    from dataclasses import replace

    from app.services.event_mapping_repository import EventMappingRepository

    repository = EventMappingRepository(event_mapping_engine)
    contradiction = _event_contradiction()

    first = repository.record_resolution(contradiction)
    repeated = repository.record_resolution(contradiction)
    reordered = repository.record_resolution(
        replace(
            contradiction,
            contradictory_evidence=tuple(
                reversed(contradiction.contradictory_evidence)
            ),
        )
    )
    changed = repository.record_resolution(
        _event_contradiction(away_name="San Antonio Spurs")
    )

    assert first.persisted is True
    assert repeated.persisted is False
    assert reordered.persisted is False
    assert changed.persisted is True

    decisions = repository.history(provider="underdog", provider_event_id="ud-1")
    assert [decision.decision_state for decision in decisions] == [
        "mapping_conflict",
        "mapping_conflict",
    ]
    evidence = decisions[0].contradictory_evidence
    assert [item.provider_canonical_event_id for item in evidence] == [
        "ud-evt-1",
        "ud-evt-2",
    ]
    assert [item.provider_home_team_abbreviation for item in evidence] == ["LAL", "BOS"]
    assert [item.provider_event_id for item in evidence] == ["ud-1", "ud-1"]
    assert {item.provider_starts_at for item in evidence} == {
        EVENT_TIP_OFF.isoformat()
    }
    assert [item.provider_away_team_name for item in evidence] == [None, None]
    assert [
        item.provider_away_team_name for item in decisions[1].contradictory_evidence
    ] == [None, "San Antonio Spurs"]
    # The operator's queue reads the same durable rows.
    (conflict,) = repository.list_conflicts(provider="underdog")
    assert conflict.latest_decision.contradictory_evidence == (
        decisions[1].contradictory_evidence
    )


def test_concurrent_first_event_write_writes_one_row_on_postgres(
    event_mapping_engine, postgres_url
):
    """Overlapping event writers must serialize in the database.

    Each worker owns its own engine, so the repository's process-local identity
    lock cannot stand in for the row lock, and the duplicate lock-row insert has
    to be rolled back to its savepoint or Postgres would leave the surrounding
    transaction aborted.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from app.services.event_mapping_repository import EventMappingRepository

    workers = 4
    barrier = threading.Barrier(workers, timeout=30)
    engines = [create_engine(postgres_url) for _ in range(workers)]
    resolution = _event_resolution()

    def _write(engine):
        repository = EventMappingRepository(engine)
        barrier.wait()
        return repository.persist_auto_decision(resolution)

    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = list(executor.map(_write, engines))
        assert (
            len(
                {
                    id(
                        EventMappingRepository._identity_locks[
                            (id(engine), "underdog", "ud-1")
                        ]
                    )
                    for engine in engines
                }
            )
            == workers
        )
    finally:
        for engine in engines:
            engine.dispose()

    reader = EventMappingRepository(event_mapping_engine)
    assert sum(result.persisted for result in results) == 1
    assert len(reader.list_mappings()) == 1
    assert len(reader.history(provider="underdog")) == 1


def test_a_stale_event_observation_never_supersedes_a_manual_decision_on_postgres(
    event_mapping_engine,
):
    """Operator precedence and the observation clock hold on Postgres too.

    Timestamps come back from Postgres with a time zone and from SQLite without
    one, so the fencing comparison is exactly the kind of rule a SQLite-only
    suite cannot prove.
    """
    from app.services.event_mapping_repository import EventMappingRepository

    repository = EventMappingRepository(
        event_mapping_engine, clock=lambda: EVENT_NOW
    )
    repository.record_resolution(
        _event_resolution(observed_at=EVENT_NOW - timedelta(hours=1))
    )
    repository.approve(
        "underdog",
        "ud-1",
        "0022500002",
        season=EVENT_SEASON,
        operator_id="ops@example.com",
        reason="the provider labels this fixture wrongly",
    )

    stale = repository.record_resolution(
        _event_resolution(
            repository=repository, observed_at=EVENT_NOW - timedelta(hours=2)
        )
    )
    fresh = repository.record_resolution(
        _event_resolution(
            repository=repository, observed_at=EVENT_NOW + timedelta(hours=1)
        )
    )

    assert stale.state == "manual_approved"
    assert stale.persisted is False
    assert fresh.state == "manual_approved"
    assert fresh.persisted is False
    mapping = repository.get_active_mapping("underdog", "ud-1")
    assert mapping.canonical_event_id == "0022500002"
    assert [
        decision.decision_state
        for decision in repository.history(provider="underdog", provider_event_id="ud-1")
    ] == ["auto", "manual_approved"]


def test_the_configured_match_window_governs_the_recheck_on_postgres(
    event_mapping_engine,
):
    """The window the repository rechecks with is the configured one."""
    from app.config.settings import CatalogSettings, RuntimeSettings
    from app.services.event_mapping_repository import EventMappingRepository
    from app.services.event_resolver import EventResolver

    settings = RuntimeSettings(
        environment="testing",
        catalog=CatalogSettings(event_match_window_hours=1),
    )
    repository = EventMappingRepository(
        event_mapping_engine, clock=lambda: EVENT_NOW, settings=settings
    )
    repository.approve(
        "underdog",
        "ud-1",
        "0022500001",
        season=EVENT_SEASON,
        operator_id="ops@example.com",
        reason="reviewed",
        provider_evidence=_event_evidence(),
    )

    rescheduled = EventResolver(
        _EventCatalogRows([_catalog_values("0022500001")]),
        mapping_repository=repository,
        match_window=timedelta(hours=1),
    ).resolve(
        "underdog",
        _event_evidence(starts_at=EVENT_TIP_OFF + timedelta(hours=2)),
        EVENT_SEASON,
    )
    result = repository.record_resolution(rescheduled)

    assert result.state == "mapping_conflict"
    assert repository.get_mapping("underdog", "ud-1").mapping_state == (
        "mapping_conflict"
    )


# --- atomic publication repair groups (#230) --------------------------------


def _repair_group_engine(postgres_url):
    from app.migrations import run_migrations

    engine = create_engine(postgres_url)
    Base.metadata.drop_all(engine)
    # ``schema_migrations`` is not part of ``Base.metadata``, so dropping the
    # model tables alone leaves the applied-version ledger behind.  A later
    # ``run_migrations`` would then believe the head was already applied and
    # create nothing.  Drop the ledger too so this really is a from-scratch
    # migration run against the shared integration database.
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS schema_migrations"))
    run_migrations(engine)
    return engine


@pytest.fixture
def repair_group_engine(postgres_url):
    engine = _repair_group_engine(postgres_url)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


def _seed_group_prerequisites(engine, *, manifest_id="manifest-1"):
    """Insert the manifest and publication a repair group is declared against."""
    from app.models.collection_control import (
        CatalogPublication,
        CollectionManifest,
        PublicationVersion,
    )

    moment = datetime(2026, 8, 11, tzinfo=timezone.utc)
    with engine.begin() as connection:
        connection.execute(CatalogPublication.__table__.insert().values(
            publication_id="catalog-1", season="2025-26", catalog_type="event",
            cutoff=moment, version="v1", checksum="c" * 64,
            payload="{}", complete=True, published_at=moment,
        ))
        connection.execute(CollectionManifest.__table__.insert().values(
            manifest_id=manifest_id, season="2025-26", cutoff=moment,
            collect_before=moment + timedelta(hours=1),
            accepted_versions="[1, 2]", scopes="[]",
            checksum=f"{manifest_id}-checksum",
            event_catalog_publication_id="catalog-1",
            event_catalog_checksum="c" * 64,
            status="active", created_at=moment,
        ))
        connection.execute(PublicationVersion.__table__.insert().values(
            publication_id="publication-1",
            stream_key="exact_shot_zones_opponent_season",
            season="2025-26", cutoff=moment, version=1, status="active",
            checksum="a" * 64, payload="{}", created_at=moment, fence=1,
        ))
    return moment


def test_repair_group_ddl_applies_to_postgres(repair_group_engine):
    """Migration 049's tables, constraints, and indexes must build on Postgres."""
    with repair_group_engine.connect() as connection:
        group_columns = connection.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'publication_repair_groups'"
        )).scalars().all()
        member_columns = connection.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'publication_repair_group_members'"
        )).scalars().all()
        constraints = connection.execute(text(
            "SELECT conname FROM pg_constraint WHERE conrelid IN ("
            "'publication_repair_groups'::regclass, "
            "'publication_repair_group_members'::regclass)"
        )).scalars().all()

    assert set(group_columns) == {
        "group_id", "manifest_id", "season", "cutoff", "reason", "checksum",
        "created_at", "promoted_at",
    }
    assert set(member_columns) == {
        "group_id", "stream_key", "expected_publication_id", "expected_fence",
        "created_at",
    }
    assert {"ck_repair_group_reason", "ck_repair_group_member_fence"} <= set(constraints)


def test_postgres_enforces_one_repair_group_per_manifest(repair_group_engine):
    from app.models.collection_control import PublicationRepairGroup

    moment = _seed_group_prerequisites(repair_group_engine)
    rows = [{
        "group_id": f"group-{index}", "manifest_id": "manifest-1",
        "season": "2025-26", "cutoff": moment, "reason": "broken per48",
        "checksum": "d" * 64, "created_at": moment,
    } for index in (1, 2)]

    with repair_group_engine.begin() as connection:
        connection.execute(PublicationRepairGroup.__table__.insert().values(rows[0]))
    with pytest.raises(IntegrityError):
        with repair_group_engine.begin() as connection:
            connection.execute(PublicationRepairGroup.__table__.insert().values(rows[1]))


def test_postgres_rejects_an_empty_reason_and_a_negative_expected_fence(
    repair_group_engine,
):
    from app.models.collection_control import (
        PublicationRepairGroup,
        PublicationRepairGroupMember,
    )

    moment = _seed_group_prerequisites(repair_group_engine)
    with pytest.raises(IntegrityError):
        with repair_group_engine.begin() as connection:
            connection.execute(PublicationRepairGroup.__table__.insert().values(
                group_id="group-blank", manifest_id="manifest-1",
                season="2025-26", cutoff=moment, reason="",
                checksum="d" * 64, created_at=moment,
            ))

    with repair_group_engine.begin() as connection:
        connection.execute(PublicationRepairGroup.__table__.insert().values(
            group_id="group-1", manifest_id="manifest-1", season="2025-26",
            cutoff=moment, reason="broken per48", checksum="d" * 64,
            created_at=moment,
        ))
    with pytest.raises(IntegrityError):
        with repair_group_engine.begin() as connection:
            connection.execute(PublicationRepairGroupMember.__table__.insert().values(
                group_id="group-1",
                stream_key="exact_shot_zones_opponent_season",
                expected_publication_id="publication-1",
                expected_fence=-1, created_at=moment,
            ))


def test_postgres_refuses_to_drop_a_publication_a_repair_group_guards(
    repair_group_engine,
):
    """The declared rollback target must survive until the group is resolved."""
    from app.models.collection_control import (
        PublicationRepairGroup,
        PublicationRepairGroupMember,
        PublicationVersion,
    )

    moment = _seed_group_prerequisites(repair_group_engine)
    with repair_group_engine.begin() as connection:
        connection.execute(PublicationRepairGroup.__table__.insert().values(
            group_id="group-1", manifest_id="manifest-1", season="2025-26",
            cutoff=moment, reason="broken per48", checksum="d" * 64,
            created_at=moment,
        ))
        connection.execute(PublicationRepairGroupMember.__table__.insert().values(
            group_id="group-1",
            stream_key="exact_shot_zones_opponent_season",
            expected_publication_id="publication-1",
            expected_fence=1, created_at=moment,
        ))

    with pytest.raises(IntegrityError):
        with repair_group_engine.begin() as connection:
            connection.execute(PublicationVersion.__table__.delete().where(
                PublicationVersion.publication_id == "publication-1"
            ))

    # Removing the group releases its members with it.
    with repair_group_engine.begin() as connection:
        connection.execute(PublicationRepairGroup.__table__.delete().where(
            PublicationRepairGroup.group_id == "group-1"
        ))
    with repair_group_engine.connect() as connection:
        assert connection.execute(
            select(PublicationRepairGroupMember.__table__)
        ).all() == []


@pytest.fixture
def promotion(postgres_url, monkeypatch):
    """The grouped-repair scenario, built against a real Postgres database."""
    from tests.services.test_publication_repair_promotion import (
        build_repair_environment,
    )

    engine = _repair_group_engine(postgres_url)
    yield build_repair_environment(engine, monkeypatch)
    Base.metadata.drop_all(engine)
    engine.dispose()


def _promotion_state(engine):
    from app.models.collection_control import PublicationPointer, PublicationVersion

    with engine.connect() as connection:
        pointers = {
            row.stream_key: (row.active_publication_id,
                             row.previous_publication_id, int(row.fence))
            for row in connection.execute(select(PublicationPointer))
        }
        statuses = {
            row.publication_id: row.status
            for row in connection.execute(select(PublicationVersion))
        }
    return pointers, statuses


def test_grouped_promotion_commits_as_one_transaction_on_postgres(promotion):
    from app.models.collection_control import AuditEvent, PublicationRepairGroup
    from tests.services.test_publication_repair_promotion import (
        L15_ZONES,
        SEASON_ZONES,
    )

    engine = promotion["engine"]
    before, _ = _promotion_state(engine)

    promotion["operations"].promote_repair_group(
        promotion["manifest"].manifest_id,
        actor="operator@example.com",
        reason="replace the broken opponent zone pair",
    )

    after, statuses = _promotion_state(engine)
    for stream_key in (SEASON_ZONES, L15_ZONES):
        active, previous, fence = after[stream_key]
        assert active != before[stream_key][0]
        assert fence == before[stream_key][2] + 1
        assert previous is None
        displaced = promotion["displaced"][stream_key].publication_id
        assert statuses[displaced] == "superseded"

    with engine.connect() as connection:
        audits = connection.execute(select(AuditEvent).where(
            AuditEvent.action == "publication.repair_group.promote"
        )).all()
        group = connection.execute(select(PublicationRepairGroup)).one()
    assert len(audits) == 1
    assert group.promoted_at is not None


def test_an_advance_after_the_declaration_fences_the_group_on_postgres(promotion):
    """The declared guard fences a member that moved since it was declared.

    This is the sequential case -- the advance has already committed. Lock
    contention between two simultaneous transactions is covered separately by
    ``test_a_held_pointer_lock_blocks_the_promotion_on_postgres``.
    """
    from app.services.collection_control import ControlPlaneError
    from tests.services.test_publication_repair_promotion import (
        L15_ZONES,
        REPAIR_CUTOFF,
        SEASON,
    )

    engine = promotion["engine"]
    # A member advanced after the declaration: its rollback target is no
    # longer the one the operator agreed to discard.
    promotion["publications"].compose(
        L15_ZONES, season=SEASON, cutoff=REPAIR_CUTOFF,
        payload=None, expected_fence=promotion["displaced"][L15_ZONES].fence,
        manifest_id=promotion["manifest"].manifest_id,
    )
    before, statuses = _promotion_state(engine)

    with pytest.raises(ControlPlaneError, match="repair_group_guard_stale"):
        promotion["operations"].promote_repair_group(
            promotion["manifest"].manifest_id,
            actor="operator", reason="repair the pair",
        )

    assert _promotion_state(engine) == (before, statuses)


def test_a_held_pointer_lock_blocks_the_promotion_on_postgres(promotion, postgres_url):
    """A second live transaction holding a member pointer stalls the promotion.

    Phase 1 takes ``SELECT ... FOR UPDATE`` on every member before composing
    anything, so a pointer already locked elsewhere must make the promotion
    wait rather than read a row someone else is about to move. The promoting
    connection gets a short ``lock_timeout`` so that wait surfaces as an error
    instead of hanging the suite; without the row lock there would be nothing
    to wait on and the promotion would simply succeed.
    """
    from sqlalchemy import event
    from sqlalchemy.exc import DBAPIError

    from app.services.collection_control import (
        CollectionOperationsService,
        PublicationService,
    )
    from tests.services.test_publication_repair_promotion import SEASON_ZONES

    engine = promotion["engine"]
    fenced = create_engine(postgres_url)

    @event.listens_for(fenced, "connect")
    def _bound_lock_wait(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("SET SESSION lock_timeout = '1500ms'")
        cursor.close()
        dbapi_connection.commit()

    source = promotion["publications"]
    operations = CollectionOperationsService(
        fenced,
        publication_service=PublicationService(
            fenced, clock=source.clock,
            l15_expectation_resolver=source.l15_expectation_resolver,
        ),
        clock=source.clock,
    )
    before, statuses = _promotion_state(engine)

    # Members are locked in stream-key order, so holding the *second* one lets
    # the promotion get past its first lock before it stalls.
    holder = engine.connect()
    holding = holder.begin()
    try:
        holder.execute(
            text(
                "SELECT 1 FROM publication_pointers "
                "WHERE stream_key = :stream_key FOR UPDATE"
            ),
            {"stream_key": SEASON_ZONES},
        ).all()
        with pytest.raises(DBAPIError):
            operations.promote_repair_group(
                promotion["manifest"].manifest_id,
                actor="operator", reason="repair the pair",
            )
    finally:
        holding.rollback()
        holder.close()

    assert _promotion_state(engine) == (before, statuses)

    # Once the contending transaction lets go, the identical promotion runs.
    operations.promote_repair_group(
        promotion["manifest"].manifest_id, actor="operator", reason="repair the pair",
    )
    after, _ = _promotion_state(engine)
    assert after[SEASON_ZONES][0] != before[SEASON_ZONES][0]
    assert after[SEASON_ZONES][1] is None
    fenced.dispose()


def test_a_failed_promotion_rolls_back_and_can_be_retried_on_postgres(
    promotion, monkeypatch,
):
    from app.models.collection_control import AuditEvent, PublicationRepairGroup
    from app.services.collection_control import ControlPlaneError
    from tests.services.test_publication_repair_promotion import (
        L15_ZONES,
        SEASON_ZONES,
    )

    engine = promotion["engine"]
    before, statuses = _promotion_state(engine)
    publications = promotion["publications"]
    advance = publications._compose_active_in_session
    calls = {"count": 0}

    def fail_after_the_first_pointer_moves(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] > 1:
            raise ControlPlaneError("injected_repair_failure")
        return advance(*args, **kwargs)

    monkeypatch.setattr(
        publications, "_compose_active_in_session",
        fail_after_the_first_pointer_moves,
    )
    with pytest.raises(ControlPlaneError, match="injected_repair_failure"):
        promotion["operations"].promote_repair_group(
            promotion["manifest"].manifest_id,
            actor="operator", reason="repair the pair",
        )

    # Postgres rolls the first member's advance back with everything else.
    assert _promotion_state(engine) == (before, statuses)
    with engine.connect() as connection:
        assert connection.execute(select(AuditEvent).where(
            AuditEvent.action == "publication.repair_group.promote"
        )).all() == []
        assert connection.execute(
            select(PublicationRepairGroup)
        ).one().promoted_at is None

    # The declaration survives the failure, so the operator can retry it.
    monkeypatch.setattr(publications, "_compose_active_in_session", advance)
    promotion["operations"].promote_repair_group(
        promotion["manifest"].manifest_id,
        actor="operator", reason="repair the pair",
    )
    after, _ = _promotion_state(engine)
    for stream_key in (SEASON_ZONES, L15_ZONES):
        assert after[stream_key][0] != before[stream_key][0]
        assert after[stream_key][1] is None
