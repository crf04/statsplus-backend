"""Target storage, service rules, and CRUD routes (#244).

A Target pairs one opponent with the Qualifiers a player has to meet; it never
stores a defensive reading and never stores its own title (ADR 0001).  The
service tests run against a real migrated SQLite database so the per-account
uniqueness index and both ``ON DELETE CASCADE`` edges are exercised rather
than described.  The route tests stay at the HTTP seam with a stub service,
matching ``test_saved_filter_sets``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.domain.player_diet_taxonomy import (
    PLAYER_DIET_BASE_SLICES,
    PLAYER_DIET_QUALIFIER_SLICES,
    PLAYER_DIET_SLICE_LABELS,
)
from app.errors import ConflictError, InvalidInputError, ResourceNotFoundError
from app.migrations import run_migrations
from app.models.target import (
    TARGET_COMPARATOR_SYMBOLS,
    TARGET_COMPARATOR_TESTS,
    TARGET_COMPARATORS,
    TARGET_NOTE_MAX_LENGTH,
    Target,
    TargetQualifier,
)
from app.models.user import User
from app.services.player_diet import PLAYER_DIET_BASES
from app.services.user_service import (
    TARGET_LIMIT,
    TARGET_QUALIFIER_LIMIT,
    UserService,
)

OWNER = "owner-uid"
OTHER = "other-uid"

CORNER_THREE = {
    "base": "shot_zones",
    "slice_key": "Corner 3",
    "comparator": "at_or_above",
    "threshold": 0.4,
}
TRANSITION = {
    "base": "play_types",
    "slice_key": "Transition",
    "comparator": "at_or_above",
    "threshold": 0.2,
}
LOW_RIM = {
    "base": "shot_zones",
    "slice_key": "Restricted Area",
    "comparator": "at_or_below",
    "threshold": 0.25,
}


@pytest.fixture
def target_engine(tmp_path):
    """A migrated application database holding two distinct accounts."""

    engine = create_engine(f"sqlite:///{tmp_path / 'targets.sqlite3'}")
    run_migrations(engine)
    with engine.begin() as connection:
        for uid in (OWNER, OTHER):
            connection.execute(
                User.__table__.insert(),
                {
                    "firebase_uid": uid,
                    "email": f"{uid}@example.com",
                    "display_name": uid,
                    "photo_url": None,
                    "created_at": datetime(2026, 8, 1, tzinfo=UTC),
                    "last_login": datetime(2026, 8, 1, tzinfo=UTC),
                    "is_active": True,
                },
            )
    yield engine
    engine.dispose()


@pytest.fixture
def targets(target_engine, runtime_settings):
    return UserService(target_engine, settings=runtime_settings)


def _seed(engine, firebase_uid, count, *, opponent_pool=None):
    """Insert ``count`` rows directly so cap tests do not pay for the service."""

    pool = opponent_pool or sorted(
        {
            "ATL",
            "BOS",
            "BKN",
            "CHA",
            "CHI",
            "CLE",
            "DAL",
            "DEN",
            "DET",
            "GSW",
            "HOU",
            "IND",
            "LAC",
            "LAL",
            "MEM",
            "MIA",
            "MIL",
            "MIN",
            "NOP",
            "NYK",
            "OKC",
            "ORL",
            "PHI",
            "PHX",
            "POR",
            "SAC",
            "SAS",
            "TOR",
            "UTA",
            "WAS",
        }
    )
    created = datetime(2026, 8, 1, tzinfo=UTC)
    with engine.begin() as connection:
        for index in range(count):
            opponent = pool[index % len(pool)]
            threshold = round(0.1 + index * 0.001, 6)
            result = connection.execute(
                Target.__table__.insert(),
                {
                    "firebase_uid": firebase_uid,
                    "opponent": opponent,
                    "note": None,
                    "qualifier_signature": (f"shot_zones:Corner 3:at_or_above:{threshold:.6f}"),
                    "created_at": created + timedelta(minutes=index),
                    "updated_at": created + timedelta(minutes=index),
                },
            )
            connection.execute(
                TargetQualifier.__table__.insert(),
                {
                    "target_id": result.inserted_primary_key[0],
                    "position": 0,
                    "base": "shot_zones",
                    "slice_key": "Corner 3",
                    "comparator": "at_or_above",
                    "threshold": threshold,
                },
            )


# --- storage ---------------------------------------------------------------


def test_the_tables_cascade_from_the_account_and_from_the_target(target_engine):
    inspector = inspect(target_engine)

    owner_key = inspector.get_foreign_keys("targets")
    assert len(owner_key) == 1
    assert owner_key[0]["referred_table"] == "users"
    assert owner_key[0]["referred_columns"] == ["firebase_uid"]
    assert owner_key[0]["constrained_columns"] == ["firebase_uid"]
    assert owner_key[0]["options"].get("ondelete") == "CASCADE"

    qualifier_key = inspector.get_foreign_keys("target_qualifiers")
    assert len(qualifier_key) == 1
    assert qualifier_key[0]["referred_table"] == "targets"
    assert qualifier_key[0]["referred_columns"] == ["id"]
    assert qualifier_key[0]["constrained_columns"] == ["target_id"]
    assert qualifier_key[0]["options"].get("ondelete") == "CASCADE"


def test_the_validated_note_length_is_the_stored_column_length():
    """One source for the limit, so the column cannot outgrow its check."""

    assert Target.__table__.c.note.type.length == TARGET_NOTE_MAX_LENGTH


def test_only_the_service_writes_the_timestamps(targets):
    columns = Target.__table__.c
    assert columns.created_at.server_default is None
    assert columns.updated_at.server_default is None
    assert columns.updated_at.onupdate is None

    created = targets.create_target(OWNER, opponent="OKC", qualifiers=[CORNER_THREE], note=None)

    assert created["created_at"] is not None
    assert created["created_at"] == created["updated_at"]


def test_deleting_an_account_removes_its_targets_and_their_qualifiers(target_engine):
    _seed(target_engine, OWNER, 2)
    _seed(target_engine, OTHER, 1)

    with target_engine.begin() as connection:
        connection.execute(User.__table__.delete().where(User.__table__.c.firebase_uid == OWNER))

    with target_engine.connect() as connection:
        remaining = connection.execute(text("SELECT firebase_uid FROM targets")).scalars().all()
        qualifiers = connection.execute(text("SELECT COUNT(*) FROM target_qualifiers")).scalar()
    assert remaining == [OTHER]
    assert qualifiers == 1


def test_a_target_cannot_name_an_account_that_does_not_exist(target_engine):
    with pytest.raises(Exception) as failure:
        _seed(target_engine, "ghost-uid", 1)

    assert "FOREIGN KEY" in str(failure.value).upper()


def test_the_database_refuses_a_second_target_with_the_same_signature(target_engine):
    _seed(target_engine, OWNER, 1, opponent_pool=["OKC"])

    with pytest.raises(IntegrityError):
        _seed(target_engine, OWNER, 1, opponent_pool=["OKC"])


def test_two_accounts_may_hold_the_same_target(target_engine):
    _seed(target_engine, OWNER, 1, opponent_pool=["OKC"])
    _seed(target_engine, OTHER, 1, opponent_pool=["OKC"])

    with target_engine.connect() as connection:
        stored = connection.execute(text("SELECT COUNT(*) FROM targets WHERE opponent = 'OKC'")).scalar()
    assert stored == 2


# --- service: create and list ---------------------------------------------


def test_a_target_round_trips_through_the_list(targets):
    created = targets.create_target(
        OWNER,
        opponent="OKC",
        qualifiers=[CORNER_THREE, TRANSITION],
        note="Leaks corner threes",
    )

    assert set(created) == {
        "conditions",
        "stat_preferences",
        "id",
        "opponent",
        "title",
        "note",
        "qualifiers",
        "created_at",
        "updated_at",
    }
    assert created["opponent"] == "OKC"
    assert created["note"] == "Leaks corner threes"
    assert created["qualifiers"] == [CORNER_THREE, TRANSITION]
    assert targets.list_targets(OWNER) == [created]


def test_the_title_is_derived_from_the_opponent_and_the_qualifiers(targets):
    created = targets.create_target(OWNER, opponent="OKC", qualifiers=[CORNER_THREE, TRANSITION], note=None)

    assert created["title"] == "OKC vs Corner 3 ≥ 40%, Transition ≥ 20%"


def test_the_at_or_below_comparator_reads_as_its_own_symbol(targets):
    created = targets.create_target(OWNER, opponent="MIA", qualifiers=[LOW_RIM], note=None)

    assert created["title"] == "MIA vs Restricted area ≤ 25%"


def test_a_fractional_share_keeps_one_decimal_place_in_the_title(targets):
    created = targets.create_target(
        OWNER,
        opponent="MIA",
        qualifiers=[{**CORNER_THREE, "threshold": 0.405}],
        note=None,
    )

    assert created["title"] == "MIA vs Corner 3 ≥ 40.5%"


def test_the_title_is_never_stored(targets):
    targets.create_target(OWNER, opponent="OKC", qualifiers=[CORNER_THREE], note=None)

    assert "title" not in Target.__table__.c


def test_the_qualifier_order_the_caller_sent_is_the_order_that_comes_back(targets):
    created = targets.create_target(OWNER, opponent="OKC", qualifiers=[TRANSITION, CORNER_THREE], note=None)

    assert created["qualifiers"] == [TRANSITION, CORNER_THREE]
    assert created["title"] == "OKC vs Transition ≥ 20%, Corner 3 ≥ 40%"


def test_the_list_is_newest_first(targets):
    first = targets.create_target(OWNER, opponent="OKC", qualifiers=[CORNER_THREE], note=None)
    second = targets.create_target(OWNER, opponent="MIA", qualifiers=[CORNER_THREE], note=None)
    third = targets.create_target(OWNER, opponent="BOS", qualifiers=[CORNER_THREE], note=None)

    listed = targets.list_targets(OWNER)

    assert [item["id"] for item in listed] == [third["id"], second["id"], first["id"]]


def test_a_tricode_is_stored_canonically(targets):
    created = targets.create_target(OWNER, opponent=" gs ", qualifiers=[CORNER_THREE], note=None)

    assert created["opponent"] == "GSW"
    assert created["title"].startswith("GSW vs ")


def test_a_note_is_stored_trimmed_and_an_empty_note_is_absent(targets):
    padded = targets.create_target(OWNER, opponent="OKC", qualifiers=[CORNER_THREE], note="  Why  ")
    blank = targets.create_target(OWNER, opponent="MIA", qualifiers=[CORNER_THREE], note="   ")

    assert padded["note"] == "Why"
    assert blank["note"] is None


def test_the_same_qualifiers_against_another_opponent_are_allowed(targets):
    targets.create_target(OWNER, opponent="OKC", qualifiers=[CORNER_THREE], note=None)
    targets.create_target(OWNER, opponent="MIA", qualifiers=[CORNER_THREE], note=None)

    assert len(targets.list_targets(OWNER)) == 2


def test_a_duplicate_opponent_and_qualifier_set_is_refused(targets):
    targets.create_target(OWNER, opponent="OKC", qualifiers=[CORNER_THREE], note=None)

    with pytest.raises(ConflictError):
        targets.create_target(OWNER, opponent="OKC", qualifiers=[CORNER_THREE], note="A second copy")

    assert len(targets.list_targets(OWNER)) == 1


def test_the_same_qualifiers_in_another_order_are_the_same_target(targets):
    """The uniqueness rule reads the Qualifiers as a set, not as a sequence."""

    targets.create_target(OWNER, opponent="OKC", qualifiers=[CORNER_THREE, TRANSITION], note=None)

    with pytest.raises(ConflictError):
        targets.create_target(OWNER, opponent="OKC", qualifiers=[TRANSITION, CORNER_THREE], note=None)


def test_the_same_slice_with_another_comparator_is_a_different_target(targets):
    targets.create_target(OWNER, opponent="OKC", qualifiers=[CORNER_THREE], note=None)

    other = targets.create_target(
        OWNER,
        opponent="OKC",
        qualifiers=[{**CORNER_THREE, "comparator": "at_or_below"}],
        note=None,
    )

    assert other["title"] == "OKC vs Corner 3 ≤ 40%"
    assert len(targets.list_targets(OWNER)) == 2


def _let_the_first_uniqueness_check_pass(monkeypatch):
    """Simulate a competing writer committing between check and commit."""

    real = UserService._target_signature_taken
    calls = []

    def racing(session, firebase_uid, opponent, signature, *, exclude_id=None):
        calls.append(signature)
        if len(calls) == 1:
            return False
        return real(session, firebase_uid, opponent, signature, exclude_id=exclude_id)

    monkeypatch.setattr(UserService, "_target_signature_taken", staticmethod(racing))
    return calls


def test_a_target_claimed_between_the_check_and_the_commit_is_a_conflict(targets, monkeypatch):
    targets.create_target(OWNER, opponent="OKC", qualifiers=[CORNER_THREE], note=None)
    calls = _let_the_first_uniqueness_check_pass(monkeypatch)

    with pytest.raises(ConflictError) as conflict:
        targets.create_target(OWNER, opponent="OKC", qualifiers=[CORNER_THREE], note=None)

    assert conflict.value.status_code == 409
    assert len(calls) == 2
    assert len(targets.list_targets(OWNER)) == 1


def test_an_integrity_failure_that_is_not_a_duplicate_is_not_a_conflict(targets):
    """A dangling owner is a server defect, not input the caller can fix."""

    with pytest.raises(IntegrityError):
        targets.create_target("ghost-uid", opponent="OKC", qualifiers=[CORNER_THREE], note=None)


def test_a_duplicate_target_in_another_account_is_allowed(targets):
    targets.create_target(OWNER, opponent="OKC", qualifiers=[CORNER_THREE], note=None)

    targets.create_target(OTHER, opponent="OKC", qualifiers=[CORNER_THREE], note=None)

    assert len(targets.list_targets(OTHER)) == 1


def test_the_account_cap_refuses_one_more_target(targets, target_engine):
    _seed(target_engine, OWNER, TARGET_LIMIT)

    with pytest.raises(ConflictError):
        targets.create_target(OWNER, opponent="OKC", qualifiers=[TRANSITION], note="One too many")

    assert len(targets.list_targets(OWNER)) == TARGET_LIMIT


def test_another_accounts_targets_do_not_count_towards_the_cap(targets, target_engine):
    _seed(target_engine, OTHER, TARGET_LIMIT)

    created = targets.create_target(OWNER, opponent="OKC", qualifiers=[CORNER_THREE], note=None)

    assert created["opponent"] == "OKC"


# --- service: validation ---------------------------------------------------


@pytest.mark.parametrize("opponent", ["", "   ", None, 42, "XXX", "Oklahoma City"])
def test_an_opponent_that_is_not_an_nba_tricode_is_refused(targets, opponent):
    with pytest.raises(InvalidInputError):
        targets.create_target(OWNER, opponent=opponent, qualifiers=[CORNER_THREE], note=None)


@pytest.mark.parametrize("qualifiers", [[], None, {}, "Corner 3", 42])
def test_a_target_without_qualifiers_is_refused(targets, qualifiers):
    with pytest.raises(InvalidInputError):
        targets.create_target(OWNER, opponent="OKC", qualifiers=qualifiers, note=None)


def test_more_qualifiers_than_the_limit_are_refused(targets):
    every_slice = [
        {
            "base": base,
            "slice_key": slice_key,
            "comparator": "at_or_above",
            "threshold": 0.1,
        }
        for base, slices in PLAYER_DIET_QUALIFIER_SLICES.items()
        for slice_key in slices
    ]
    assert len(every_slice) > TARGET_QUALIFIER_LIMIT

    with pytest.raises(InvalidInputError):
        targets.create_target(
            OWNER,
            opponent="OKC",
            qualifiers=every_slice[: TARGET_QUALIFIER_LIMIT + 1],
            note=None,
        )


@pytest.mark.parametrize(
    "qualifier",
    [
        "Corner 3",
        {"slice_key": "Corner 3", "comparator": "at_or_above", "threshold": 0.4},
        {**CORNER_THREE, "base": "shot_locations"},
        {**CORNER_THREE, "base": None},
        {**CORNER_THREE, "slice_key": "Deep 3"},
        {**CORNER_THREE, "slice_key": "Transition"},
        {**TRANSITION, "slice_key": "Misc"},
        {**CORNER_THREE, "slice_key": None},
        {**CORNER_THREE, "comparator": "above"},
        {**CORNER_THREE, "comparator": ">="},
        {**CORNER_THREE, "comparator": None},
        {**CORNER_THREE, "threshold": -0.01},
        {**CORNER_THREE, "threshold": 1.01},
        {**CORNER_THREE, "threshold": 40},
        {**CORNER_THREE, "threshold": "0.4"},
        {**CORNER_THREE, "threshold": True},
        {**CORNER_THREE, "threshold": None},
        {**CORNER_THREE, "threshold": float("nan")},
    ],
)
def test_an_unusable_qualifier_is_refused(targets, qualifier):
    with pytest.raises(InvalidInputError):
        targets.create_target(OWNER, opponent="OKC", qualifiers=[qualifier], note=None)


def test_a_repeated_qualifier_is_refused(targets):
    with pytest.raises(InvalidInputError):
        targets.create_target(OWNER, opponent="OKC", qualifiers=[CORNER_THREE, CORNER_THREE], note=None)


def test_the_threshold_bounds_are_inclusive(targets):
    floor = targets.create_target(
        OWNER,
        opponent="OKC",
        qualifiers=[{**CORNER_THREE, "threshold": 0}],
        note=None,
    )
    ceiling = targets.create_target(
        OWNER,
        opponent="MIA",
        qualifiers=[{**CORNER_THREE, "threshold": 1}],
        note=None,
    )

    assert floor["title"] == "OKC vs Corner 3 ≥ 0%"
    assert ceiling["title"] == "MIA vs Corner 3 ≥ 100%"


def test_every_diet_base_can_carry_a_qualifier(targets):
    for opponent, base in zip(("OKC", "MIA", "BOS", "DEN"), PLAYER_DIET_BASES, strict=False):
        slice_key = PLAYER_DIET_QUALIFIER_SLICES[base][0]

        created = targets.create_target(
            OWNER,
            opponent=opponent,
            qualifiers=[
                {
                    "base": base,
                    "slice_key": slice_key,
                    "comparator": "at_or_above",
                    "threshold": 0.3,
                }
            ],
            note=None,
        )

        label = PLAYER_DIET_SLICE_LABELS[slice_key]
        assert created["title"] == f"{opponent} vs {label} ≥ 30%"


def test_the_title_reads_the_slice_label_rather_than_the_stored_key(targets):
    """The stored key stays provider vocabulary; the title is what a user reads."""

    created = targets.create_target(
        OWNER,
        opponent="OKC",
        qualifiers=[
            {
                "base": "play_types",
                "slice_key": "PRBallHandler",
                "comparator": "at_or_above",
                "threshold": 0.35,
            }
        ],
        note=None,
    )

    assert created["title"] == "OKC vs P&R ball handler ≥ 35%"
    assert created["qualifiers"][0]["slice_key"] == "PRBallHandler"


def test_every_comparator_has_both_a_title_symbol_and_a_share_test():
    """A comparator missing either is accepted on write and fails on read."""

    assert set(TARGET_COMPARATORS) == set(TARGET_COMPARATOR_SYMBOLS)
    assert set(TARGET_COMPARATORS) == set(TARGET_COMPARATOR_TESTS)


def test_every_qualifiable_slice_has_a_label():
    """A collector adding a slice must not leave a title rendering a raw key."""

    for base, slices in PLAYER_DIET_QUALIFIER_SLICES.items():
        for slice_key in slices:
            assert slice_key in PLAYER_DIET_SLICE_LABELS, (base, slice_key)


def test_the_residual_play_type_bucket_is_not_a_qualifier_slice(targets):
    """``Misc`` is collected and reported, but it is not something to filter on."""

    assert "Misc" in PLAYER_DIET_BASE_SLICES["play_types"]
    assert "Misc" not in PLAYER_DIET_QUALIFIER_SLICES["play_types"]
    assert "Misc" not in PLAYER_DIET_SLICE_LABELS

    with pytest.raises(InvalidInputError):
        targets.create_target(
            OWNER,
            opponent="OKC",
            qualifiers=[{**TRANSITION, "slice_key": "Misc"}],
            note=None,
        )


@pytest.mark.parametrize("note", [42, ["a"], "x" * (TARGET_NOTE_MAX_LENGTH + 1)])
def test_an_unusable_note_is_refused(targets, note):
    with pytest.raises(InvalidInputError):
        targets.create_target(OWNER, opponent="OKC", qualifiers=[CORNER_THREE], note=note)


def test_a_note_at_the_length_limit_is_accepted(targets):
    note = "x" * TARGET_NOTE_MAX_LENGTH

    created = targets.create_target(OWNER, opponent="OKC", qualifiers=[CORNER_THREE], note=note)

    assert created["note"] == note


# --- service: draft validation ---------------------------------------------


def test_a_draft_validates_to_the_listed_shape_without_an_id_or_timestamps(targets):
    draft = targets.validate_target_draft(
        opponent="okc", qualifiers=[CORNER_THREE], note="  Leaks corner threes  "
    )

    assert draft == {
        "conditions": None,
        "stat_preferences": None,
        "opponent": "OKC",
        "title": "OKC vs Corner 3 ≥ 40%",
        "note": "Leaks corner threes",
        "qualifiers": [CORNER_THREE],
    }
    assert targets.list_targets(OWNER) == []


def test_a_draft_reads_exactly_as_the_target_it_would_save(targets):
    draft = targets.validate_target_draft(opponent="OKC", qualifiers=[TRANSITION, CORNER_THREE], note="Why")
    created = targets.create_target(OWNER, opponent="OKC", qualifiers=[TRANSITION, CORNER_THREE], note="Why")

    assert draft == {key: created[key] for key in draft}
    assert set(created) - set(draft) == {"id", "created_at", "updated_at"}


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            {"opponent": "XXX", "qualifiers": [CORNER_THREE]},
            "A target needs one NBA team as its opponent.",
        ),
        (
            {"opponent": "OKC", "qualifiers": []},
            "A target needs at least one qualifier.",
        ),
        (
            {"opponent": "OKC", "qualifiers": [{**CORNER_THREE, "threshold": 1.01}]},
            "A qualifier needs a known diet base, a slice of that base, a "
            "comparator of at_or_above or at_or_below, and a threshold share "
            "between 0 and 1.",
        ),
        (
            {"opponent": "OKC", "qualifiers": [{**CORNER_THREE, "base": "moods"}]},
            "A qualifier needs a known diet base, a slice of that base, a "
            "comparator of at_or_above or at_or_below, and a threshold share "
            "between 0 and 1.",
        ),
        (
            {"opponent": "OKC", "qualifiers": [CORNER_THREE, CORNER_THREE]},
            "A target cannot repeat the same qualifier.",
        ),
        (
            {"opponent": "OKC", "qualifiers": [CORNER_THREE], "note": 42},
            "A target note must be text.",
        ),
    ],
)
def test_an_unusable_draft_is_refused_with_the_create_message(targets, body, message):
    with pytest.raises(InvalidInputError) as refused:
        targets.validate_target_draft(**body)

    assert refused.value.public_message == message


def test_a_draft_over_the_qualifier_limit_is_refused(targets):
    too_many = [
        {**CORNER_THREE, "threshold": round(0.1 + index * 0.01, 2)}
        for index in range(TARGET_QUALIFIER_LIMIT + 1)
    ]

    with pytest.raises(InvalidInputError):
        targets.validate_target_draft(opponent="OKC", qualifiers=too_many)


def test_a_draft_is_validated_over_the_cap_and_against_a_duplicate(targets, target_engine):
    """A draft is not a write, so neither write-time conflict applies."""

    _seed(target_engine, OWNER, TARGET_LIMIT)
    created = targets.list_targets(OWNER)[0]

    draft = targets.validate_target_draft(opponent=created["opponent"], qualifiers=created["qualifiers"])

    assert draft["title"] == created["title"]
    assert len(targets.list_targets(OWNER)) == TARGET_LIMIT


# --- service: update -------------------------------------------------------


def test_editing_the_qualifiers_changes_the_title(targets):
    created = targets.create_target(OWNER, opponent="OKC", qualifiers=[CORNER_THREE], note="Why")

    updated = targets.update_target(OWNER, created["id"], changes={"qualifiers": [CORNER_THREE, TRANSITION]})

    assert updated["title"] == "OKC vs Corner 3 ≥ 40%, Transition ≥ 20%"
    assert updated["qualifiers"] == [CORNER_THREE, TRANSITION]
    assert updated["note"] == "Why"
    assert updated["created_at"] == created["created_at"]
    assert updated["updated_at"] >= created["updated_at"]
    assert targets.list_targets(OWNER) == [updated]


def test_editing_the_note_never_changes_the_title(targets):
    created = targets.create_target(OWNER, opponent="OKC", qualifiers=[CORNER_THREE], note="Why")

    updated = targets.update_target(OWNER, created["id"], changes={"note": "  OKC smothers the rim  "})

    assert updated["note"] == "OKC smothers the rim"
    assert updated["title"] == created["title"]


def test_a_note_can_be_cleared(targets):
    created = targets.create_target(OWNER, opponent="OKC", qualifiers=[CORNER_THREE], note="Why")

    updated = targets.update_target(OWNER, created["id"], changes={"note": None})

    assert updated["note"] is None


def test_editing_replaces_the_qualifier_rows_rather_than_adding_to_them(targets, target_engine):
    created = targets.create_target(OWNER, opponent="OKC", qualifiers=[CORNER_THREE, TRANSITION], note=None)

    targets.update_target(OWNER, created["id"], changes={"qualifiers": [LOW_RIM]})

    with target_engine.connect() as connection:
        stored = connection.execute(text("SELECT slice_key FROM target_qualifiers")).scalars().all()
    assert stored == ["Restricted Area"]


def test_editing_onto_another_targets_qualifier_set_is_refused(targets):
    targets.create_target(OWNER, opponent="OKC", qualifiers=[CORNER_THREE], note=None)
    edited = targets.create_target(OWNER, opponent="OKC", qualifiers=[TRANSITION], note=None)

    with pytest.raises(ConflictError):
        targets.update_target(OWNER, edited["id"], changes={"qualifiers": [CORNER_THREE]})


def test_editing_a_target_back_onto_its_own_qualifier_set_is_allowed(targets):
    created = targets.create_target(OWNER, opponent="OKC", qualifiers=[CORNER_THREE], note=None)

    updated = targets.update_target(
        OWNER, created["id"], changes={"qualifiers": [CORNER_THREE], "note": "Same"}
    )

    assert updated["note"] == "Same"


def test_a_target_claimed_between_the_check_and_an_edit_is_a_conflict(targets, monkeypatch):
    targets.create_target(OWNER, opponent="OKC", qualifiers=[CORNER_THREE], note=None)
    edited = targets.create_target(OWNER, opponent="OKC", qualifiers=[TRANSITION], note=None)
    calls = _let_the_first_uniqueness_check_pass(monkeypatch)

    with pytest.raises(ConflictError) as conflict:
        targets.update_target(OWNER, edited["id"], changes={"qualifiers": [CORNER_THREE]})

    assert conflict.value.status_code == 409
    assert len(calls) == 2


def test_an_update_that_changes_nothing_is_refused(targets):
    created = targets.create_target(OWNER, opponent="OKC", qualifiers=[CORNER_THREE], note=None)

    with pytest.raises(InvalidInputError):
        targets.update_target(OWNER, created["id"], changes={})


def test_an_update_cannot_move_a_target_to_another_opponent(targets):
    created = targets.create_target(OWNER, opponent="OKC", qualifiers=[CORNER_THREE], note=None)

    updated = targets.update_target(OWNER, created["id"], changes={"opponent": "MIA", "note": "Moved?"})

    assert updated["opponent"] == "OKC"


def test_editing_rejects_unusable_qualifiers(targets):
    created = targets.create_target(OWNER, opponent="OKC", qualifiers=[CORNER_THREE], note=None)

    with pytest.raises(InvalidInputError):
        targets.update_target(OWNER, created["id"], changes={"qualifiers": []})


def test_editing_an_unknown_target_is_not_found(targets):
    with pytest.raises(ResourceNotFoundError):
        targets.update_target(OWNER, 4242, changes={"note": "Anything"})


# --- service: delete -------------------------------------------------------


def test_deleting_removes_it_from_the_list(targets):
    kept = targets.create_target(OWNER, opponent="OKC", qualifiers=[CORNER_THREE], note=None)
    removed = targets.create_target(OWNER, opponent="MIA", qualifiers=[CORNER_THREE], note=None)

    targets.delete_target(OWNER, removed["id"])

    assert targets.list_targets(OWNER) == [kept]


def test_deleting_removes_the_qualifier_rows(targets, target_engine):
    created = targets.create_target(OWNER, opponent="OKC", qualifiers=[CORNER_THREE, TRANSITION], note=None)

    targets.delete_target(OWNER, created["id"])

    with target_engine.connect() as connection:
        remaining = connection.execute(text("SELECT COUNT(*) FROM target_qualifiers")).scalar()
    assert remaining == 0


def test_deleting_frees_the_opponent_and_qualifier_set_for_reuse(targets):
    created = targets.create_target(OWNER, opponent="OKC", qualifiers=[CORNER_THREE], note=None)
    targets.delete_target(OWNER, created["id"])

    replacement = targets.create_target(OWNER, opponent="OKC", qualifiers=[CORNER_THREE], note=None)

    assert replacement["opponent"] == "OKC"


def test_deleting_an_unknown_target_is_not_found(targets):
    with pytest.raises(ResourceNotFoundError):
        targets.delete_target(OWNER, 4242)


# --- service: account scoping ---------------------------------------------


def test_another_accounts_target_is_never_listed(targets):
    targets.create_target(OTHER, opponent="OKC", qualifiers=[CORNER_THREE], note=None)

    assert targets.list_targets(OWNER) == []


def test_another_accounts_target_cannot_be_edited(targets):
    foreign = targets.create_target(OTHER, opponent="OKC", qualifiers=[CORNER_THREE], note="Private")

    with pytest.raises(ResourceNotFoundError):
        targets.update_target(OWNER, foreign["id"], changes={"note": "Stolen"})

    assert targets.list_targets(OTHER)[0]["note"] == "Private"


def test_another_accounts_target_cannot_be_deleted(targets):
    foreign = targets.create_target(OTHER, opponent="OKC", qualifiers=[CORNER_THREE], note=None)

    with pytest.raises(ResourceNotFoundError):
        targets.delete_target(OWNER, foreign["id"])

    assert len(targets.list_targets(OTHER)) == 1


def test_the_vocabulary_stays_importable_from_the_diet_service(targets):
    """Existing importers read these off the service; keep that path exact."""

    from app.services import player_diet

    assert player_diet.PLAYER_DIET_BASE_SLICES is PLAYER_DIET_BASE_SLICES
    assert player_diet.PLAYER_DIET_QUALIFIER_SLICES is PLAYER_DIET_QUALIFIER_SLICES
    assert player_diet.PLAYER_DIET_SLICE_LABELS is PLAYER_DIET_SLICE_LABELS


def test_the_service_records_the_authors_order_as_the_qualifier_position(targets, target_engine):
    """Position is the durable record of the order, not a constant."""

    created = targets.create_target(OWNER, opponent="OKC", qualifiers=[TRANSITION, CORNER_THREE], note=None)

    with target_engine.connect() as connection:
        stored = connection.execute(
            text(
                "SELECT position, slice_key FROM target_qualifiers "
                "WHERE target_id = :target_id ORDER BY position"
            ),
            {"target_id": created["id"]},
        ).all()

    assert stored == [(0, "Transition"), (1, "Corner 3")]


def test_editing_renumbers_the_positions_from_the_new_order(targets, target_engine):
    created = targets.create_target(OWNER, opponent="OKC", qualifiers=[CORNER_THREE], note=None)

    targets.update_target(OWNER, created["id"], changes={"qualifiers": [LOW_RIM, TRANSITION]})

    with target_engine.connect() as connection:
        stored = connection.execute(
            text(
                "SELECT position, slice_key FROM target_qualifiers "
                "WHERE target_id = :target_id ORDER BY position"
            ),
            {"target_id": created["id"]},
        ).all()

    assert stored == [(0, "Restricted Area"), (1, "Transition")]


def test_the_qualifiers_read_back_in_position_order_not_insertion_order(targets, target_engine):
    """Position is the author's order; row order is an accident of writing."""

    created = targets.create_target(OWNER, opponent="OKC", qualifiers=[CORNER_THREE], note=None)
    with target_engine.begin() as connection:
        connection.execute(TargetQualifier.__table__.delete())
        # Written back to front, so the later position gets the lower row id.
        for position, qualifier in ((1, TRANSITION), (0, CORNER_THREE)):
            connection.execute(
                TargetQualifier.__table__.insert(),
                {"target_id": created["id"], "position": position, **qualifier},
            )

    listed = targets.list_targets(OWNER)[0]

    assert listed["qualifiers"] == [CORNER_THREE, TRANSITION]
    assert listed["title"] == "OKC vs Corner 3 ≥ 40%, Transition ≥ 20%"


def test_editing_moves_updated_at_strictly_past_created_at(targets, monkeypatch):
    """A frozen clock, so the bump is asserted rather than merely permitted."""

    from app.services import user_service as service_module

    instants = iter(
        [
            datetime(2026, 9, 5, 12, 0, tzinfo=UTC),
            datetime(2026, 9, 5, 12, 5, tzinfo=UTC),
        ]
    )
    monkeypatch.setattr(
        service_module,
        "datetime",
        SimpleNamespace(now=lambda _timezone=None: next(instants)),
    )

    created = targets.create_target(OWNER, opponent="OKC", qualifiers=[CORNER_THREE], note=None)
    updated = targets.update_target(OWNER, created["id"], changes={"note": "Sharpened"})

    assert created["created_at"] == created["updated_at"]
    assert updated["created_at"] == created["created_at"]
    assert updated["updated_at"] > created["updated_at"]


def _qualifier_vocabulary():
    """Every qualifiable slice as a distinct Qualifier."""

    return [
        {
            "base": base,
            "slice_key": slice_key,
            "comparator": "at_or_above",
            "threshold": 0.1,
        }
        for base, slices in PLAYER_DIET_QUALIFIER_SLICES.items()
        for slice_key in slices
    ]


def test_exactly_the_qualifier_limit_is_accepted(targets):
    at_limit = _qualifier_vocabulary()[:TARGET_QUALIFIER_LIMIT]
    assert len(at_limit) == TARGET_QUALIFIER_LIMIT

    created = targets.create_target(OWNER, opponent="OKC", qualifiers=at_limit, note=None)

    assert created["qualifiers"] == at_limit


def test_two_thresholds_that_a_coarser_signature_would_merge_are_two_targets(
    targets,
):
    """0.4 and 0.44 differ, so a signature rounded to one place is not enough."""

    first = targets.create_target(OWNER, opponent="OKC", qualifiers=[CORNER_THREE], note=None)
    second = targets.create_target(
        OWNER,
        opponent="OKC",
        qualifiers=[{**CORNER_THREE, "threshold": 0.44}],
        note=None,
    )

    assert first["title"] == "OKC vs Corner 3 ≥ 40%"
    assert second["title"] == "OKC vs Corner 3 ≥ 44%"
    assert len(targets.list_targets(OWNER)) == 2


def test_editing_moves_the_stored_signature_with_the_qualifiers(targets):
    """The set a target moved off is free; the set it moved onto is taken."""

    moved = targets.create_target(OWNER, opponent="OKC", qualifiers=[CORNER_THREE], note=None)

    targets.update_target(OWNER, moved["id"], changes={"qualifiers": [TRANSITION]})

    replacement = targets.create_target(OWNER, opponent="OKC", qualifiers=[CORNER_THREE], note=None)
    assert replacement["title"] == "OKC vs Corner 3 ≥ 40%"

    with pytest.raises(ConflictError):
        targets.create_target(OWNER, opponent="OKC", qualifiers=[TRANSITION], note=None)


# --- routes ----------------------------------------------------------------


ITEM = {
    "id": 7,
    "opponent": "OKC",
    "title": "OKC vs Corner 3 ≥ 40%",
    "note": None,
    "qualifiers": [CORNER_THREE],
    "created_at": "2026-09-05T12:00:00+00:00",
    "updated_at": "2026-09-05T12:00:00+00:00",
}


def _assert_error(response, status, code, message):
    assert response.status_code == status
    assert response.get_json()["error"] == {"code": code, "message": message}


@pytest.fixture
def user_service(monkeypatch):
    """Swap the route module's service handle for a stub."""

    from app.routes import user_routes

    stub = SimpleNamespace()
    monkeypatch.setattr(user_routes, "user_service", stub)
    return stub


@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/api/user/targets", "get"),
        ("/api/user/targets", "post"),
        ("/api/user/targets/7", "patch"),
        ("/api/user/targets/7", "delete"),
    ],
)
def test_target_routes_reject_missing_authentication(client, authenticate, path, method):
    authenticate()

    response = getattr(client, method)(path)

    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "authentication_required"


def test_list_route_returns_the_callers_targets(client, authenticate, user_service):
    headers = authenticate()
    asked = []
    user_service.list_targets = lambda uid: asked.append(uid) or [ITEM]

    response = client.get("/api/user/targets", headers=headers)

    assert response.status_code == 200
    assert response.get_json() == {"success": True, "targets": [ITEM]}
    assert asked == ["test-uid"]


def test_create_route_returns_the_created_target(client, authenticate, user_service):
    headers = authenticate()
    submitted = {}

    def capture(firebase_uid, *, opponent, qualifiers, note):
        submitted.update(
            firebase_uid=firebase_uid,
            opponent=opponent,
            qualifiers=qualifiers,
            note=note,
        )
        return ITEM

    user_service.create_target = capture

    response = client.post(
        "/api/user/targets",
        headers=headers,
        json={"opponent": "OKC", "qualifiers": [CORNER_THREE], "note": "Why"},
    )

    assert response.status_code == 201
    assert response.get_json() == {"success": True, "target": ITEM}
    assert submitted == {
        "firebase_uid": "test-uid",
        "opponent": "OKC",
        "qualifiers": [CORNER_THREE],
        "note": "Why",
    }


def test_create_route_rejects_a_body_that_is_not_an_object(client, authenticate, user_service):
    headers = authenticate()

    response = client.post("/api/user/targets", headers=headers)

    _assert_error(response, 400, "invalid_input", "No target data was provided.")


def test_create_route_reports_a_duplicate_as_a_conflict(client, authenticate, user_service):
    headers = authenticate()

    def refuse(firebase_uid, *, opponent, qualifiers, note):
        raise ConflictError("A target for that opponent already has those qualifiers.")

    user_service.create_target = refuse

    response = client.post(
        "/api/user/targets",
        headers=headers,
        json={"opponent": "OKC", "qualifiers": [CORNER_THREE]},
    )

    _assert_error(
        response,
        409,
        "operation_conflict",
        "A target for that opponent already has those qualifiers.",
    )


def test_create_route_reports_invalid_input_as_a_bad_request(client, authenticate, user_service):
    headers = authenticate()

    def refuse(firebase_uid, *, opponent, qualifiers, note):
        raise InvalidInputError("A target needs at least one qualifier.")

    user_service.create_target = refuse

    response = client.post("/api/user/targets", headers=headers, json={"opponent": "OKC", "qualifiers": []})

    _assert_error(response, 400, "invalid_input", "A target needs at least one qualifier.")


def test_create_route_reports_an_unexpected_failure_safely(client, authenticate, user_service):
    headers = authenticate()

    def explode(firebase_uid, *, opponent, qualifiers, note):
        raise RuntimeError("database is down")

    user_service.create_target = explode

    response = client.post(
        "/api/user/targets",
        headers=headers,
        json={"opponent": "OKC", "qualifiers": [CORNER_THREE]},
    )

    _assert_error(response, 500, "operation_failed", "Failed to save the target.")


def test_update_route_forwards_only_the_submitted_changes(client, authenticate, user_service):
    headers = authenticate()
    submitted = {}

    def capture(firebase_uid, target_id, *, changes):
        submitted.update(firebase_uid=firebase_uid, target_id=target_id, changes=changes)
        return ITEM

    user_service.update_target = capture

    response = client.patch("/api/user/targets/7", headers=headers, json={"note": "Sharpened"})

    assert response.status_code == 200
    assert response.get_json() == {"success": True, "target": ITEM}
    assert submitted == {
        "firebase_uid": "test-uid",
        "target_id": 7,
        "changes": {"note": "Sharpened"},
    }


def test_update_route_rejects_a_missing_body(client, authenticate, user_service):
    headers = authenticate()

    response = client.patch("/api/user/targets/7", headers=headers)

    _assert_error(response, 400, "invalid_input", "No target data was provided.")


def test_update_route_reports_a_foreign_id_as_not_found(client, authenticate, user_service):
    headers = authenticate()

    def refuse(firebase_uid, target_id, *, changes):
        raise ResourceNotFoundError("The requested target was not found.")

    user_service.update_target = refuse

    response = client.patch("/api/user/targets/7", headers=headers, json={"note": "Stolen"})

    _assert_error(response, 404, "resource_not_found", "The requested target was not found.")


def test_delete_route_confirms_the_removal(client, authenticate, user_service):
    headers = authenticate()
    removed = []
    user_service.delete_target = lambda uid, target_id: removed.append((uid, target_id))

    response = client.delete("/api/user/targets/7", headers=headers)

    assert response.status_code == 200
    assert response.get_json()["success"] is True
    assert removed == [("test-uid", 7)]


def test_delete_route_reports_a_foreign_id_as_not_found(client, authenticate, user_service):
    headers = authenticate()

    def refuse(firebase_uid, target_id):
        raise ResourceNotFoundError("The requested target was not found.")

    user_service.delete_target = refuse

    response = client.delete("/api/user/targets/7", headers=headers)

    _assert_error(response, 404, "resource_not_found", "The requested target was not found.")


def test_a_non_numeric_identifier_is_not_found(client, authenticate, user_service):
    headers = authenticate()

    response = client.delete("/api/user/targets/not-an-id", headers=headers)

    assert response.status_code == 404


# --- end to end ------------------------------------------------------------


def test_the_contract_walks_end_to_end_against_a_real_database(
    tmp_path, make_client, authenticate, make_db_user
):
    """Every contract behavior over HTTP with the real service and database."""

    from app.utils.db import get_engine

    database_url = f"sqlite:///{tmp_path / 'targets-end-to-end.sqlite3'}"
    get_engine.cache_clear()
    engine = create_engine(database_url)
    run_migrations(engine)
    with engine.begin() as connection:
        for uid in (OWNER, OTHER):
            connection.execute(
                User.__table__.insert(),
                {
                    "firebase_uid": uid,
                    "email": f"{uid}@example.com",
                    "display_name": uid,
                    "photo_url": None,
                    "created_at": datetime(2026, 8, 1, tzinfo=UTC),
                    "last_login": datetime(2026, 8, 1, tzinfo=UTC),
                    "is_active": True,
                },
            )
    engine.dispose()

    client = make_client(database_url)
    headers = authenticate(claims={"uid": OWNER}, db_user=make_db_user(firebase_uid=OWNER))

    assert client.get("/api/user/targets", headers=headers).get_json() == {
        "success": True,
        "targets": [],
    }

    created = client.post(
        "/api/user/targets",
        headers=headers,
        json={
            "opponent": "OKC",
            "qualifiers": [CORNER_THREE, TRANSITION],
            "note": "Leaks corner threes",
        },
    )
    assert created.status_code == 201
    item = created.get_json()["target"]
    assert item["title"] == "OKC vs Corner 3 ≥ 40%, Transition ≥ 20%"

    # 409 for the same opponent and Qualifier set, whatever the order.
    duplicate = client.post(
        "/api/user/targets",
        headers=headers,
        json={"opponent": "OKC", "qualifiers": [TRANSITION, CORNER_THREE]},
    )
    assert duplicate.status_code == 409

    # 400 for a slice that does not belong to the base.
    invalid = client.post(
        "/api/user/targets",
        headers=headers,
        json={"opponent": "OKC", "qualifiers": [{**CORNER_THREE, "base": "play_types"}]},
    )
    assert invalid.status_code == 400
    assert invalid.get_json()["error"]["code"] == "invalid_input"

    edited = client.patch(
        f"/api/user/targets/{item['id']}",
        headers=headers,
        json={"qualifiers": [LOW_RIM], "note": "Rim, not threes"},
    )
    assert edited.status_code == 200
    assert edited.get_json()["target"]["title"] == "OKC vs Restricted area ≤ 25%"
    assert edited.get_json()["target"]["note"] == "Rim, not threes"

    listed = client.get("/api/user/targets", headers=headers).get_json()
    assert [entry["title"] for entry in listed["targets"]] == ["OKC vs Restricted area ≤ 25%"]

    # The other account can neither see, edit, nor delete it.
    other_headers = authenticate(claims={"uid": OTHER}, db_user=make_db_user(firebase_uid=OTHER))
    assert client.get("/api/user/targets", headers=other_headers).get_json()["targets"] == []
    assert (
        client.patch(
            f"/api/user/targets/{item['id']}",
            headers=other_headers,
            json={"note": "Stolen"},
        ).status_code
        == 404
    )
    assert client.delete(f"/api/user/targets/{item['id']}", headers=other_headers).status_code == 404

    # ``authenticate`` installs one verifier for the whole process, so the
    # owner's identity has to be reinstalled before the last two calls.
    headers = authenticate(claims={"uid": OWNER}, db_user=make_db_user(firebase_uid=OWNER))
    assert client.delete(f"/api/user/targets/{item['id']}", headers=headers).status_code == 200
    assert client.get("/api/user/targets", headers=headers).get_json() == {
        "success": True,
        "targets": [],
    }
    get_engine.cache_clear()


# --- Position and Average Qualifiers (crf04/statsplus#351) -----------------

WING_BIG = {"base": "position", "groups": ["Wing", "Big"]}
THREE_PA_AVERAGE = {
    "base": "season_average",
    "stat": "3PA",
    "comparator": "at_or_above",
    "threshold": 0.3,
}


def test_a_position_and_an_average_are_stored_and_listed_as_sent(targets):
    created = targets.create_target(
        OWNER, opponent="MEM", qualifiers=[WING_BIG, THREE_PA_AVERAGE, CORNER_THREE], note=None
    )

    assert created["qualifiers"] == [WING_BIG, THREE_PA_AVERAGE, CORNER_THREE]
    assert targets.list_targets(OWNER)[0]["qualifiers"] == [WING_BIG, THREE_PA_AVERAGE, CORNER_THREE]
    assert targets.get_target(OWNER, created["id"])["qualifiers"] == [
        WING_BIG,
        THREE_PA_AVERAGE,
        CORNER_THREE,
    ]


def test_groups_come_back_in_guard_wing_big_order_whatever_order_they_were_sent(targets):
    created = targets.create_target(
        OWNER,
        opponent="MEM",
        qualifiers=[{"base": "position", "groups": ["Big", "Guard", "Wing"]}],
        note=None,
    )

    assert created["qualifiers"] == [{"base": "position", "groups": ["Guard", "Wing", "Big"]}]
    assert created["title"] == "MEM vs Guard/Wing/Big"


@pytest.mark.parametrize(
    ("qualifiers", "title"),
    [
        ([{"base": "position", "groups": ["Big"]}], "MEM vs Big"),
        ([WING_BIG], "MEM vs Wing/Big"),
        ([THREE_PA_AVERAGE], "MEM vs 3PA avg ≥ 0.3"),
        (
            [{**THREE_PA_AVERAGE, "stat": "3PA/36", "comparator": "at_or_below", "threshold": 2}],
            "MEM vs 3PA/36 avg ≤ 2",
        ),
        ([{**THREE_PA_AVERAGE, "stat": "FG%", "threshold": 45.55}], "MEM vs FG% avg ≥ 45.55"),
        ([{**THREE_PA_AVERAGE, "threshold": 0.004}], "MEM vs 3PA avg ≥ 0.004"),
        ([{**THREE_PA_AVERAGE, "threshold": 0.304}], "MEM vs 3PA avg ≥ 0.304"),
        ([CORNER_THREE, WING_BIG], "MEM vs Wing/Big, Corner 3 ≥ 40%"),
        (
            [CORNER_THREE, THREE_PA_AVERAGE, {"base": "position", "groups": ["Big"]}],
            "MEM vs Big, Corner 3 ≥ 40%, 3PA avg ≥ 0.3",
        ),
        ([TRANSITION, CORNER_THREE], "MEM vs Transition ≥ 20%, Corner 3 ≥ 40%"),
    ],
)
def test_the_title_reads_the_position_first_then_the_qualifiers_in_entry_order(targets, qualifiers, title):
    assert targets.create_target(OWNER, opponent="MEM", qualifiers=qualifiers, note=None)["title"] == title
    assert targets.validate_target_draft(opponent="MEM", qualifiers=qualifiers)["title"] == title


def test_an_average_threshold_of_zero_is_accepted(targets):
    created = targets.create_target(
        OWNER, opponent="MEM", qualifiers=[{**THREE_PA_AVERAGE, "threshold": 0}], note=None
    )

    assert created["qualifiers"][0]["threshold"] == 0.0
    assert created["title"] == "MEM vs 3PA avg ≥ 0"


def test_the_same_groups_in_another_order_are_a_duplicate(targets):
    targets.create_target(OWNER, opponent="MEM", qualifiers=[WING_BIG], note=None)

    with pytest.raises(ConflictError):
        targets.create_target(
            OWNER, opponent="MEM", qualifiers=[{"base": "position", "groups": ["Big", "Wing"]}], note=None
        )
    # Other groups, or another opponent, are other Targets.
    targets.create_target(
        OWNER, opponent="MEM", qualifiers=[{"base": "position", "groups": ["Big"]}], note=None
    )
    targets.create_target(OWNER, opponent="POR", qualifiers=[WING_BIG], note=None)


def test_average_thresholds_are_compared_to_two_decimals(targets):
    targets.create_target(OWNER, opponent="MEM", qualifiers=[THREE_PA_AVERAGE], note=None)

    with pytest.raises(ConflictError):
        targets.create_target(
            OWNER, opponent="MEM", qualifiers=[{**THREE_PA_AVERAGE, "threshold": 0.304}], note=None
        )
    targets.create_target(
        OWNER, opponent="MEM", qualifiers=[{**THREE_PA_AVERAGE, "threshold": 0.31}], note=None
    )
    targets.create_target(
        OWNER, opponent="MEM", qualifiers=[{**THREE_PA_AVERAGE, "comparator": "at_or_below"}], note=None
    )
    targets.create_target(
        OWNER, opponent="MEM", qualifiers=[{**THREE_PA_AVERAGE, "stat": "3PA/36"}], note=None
    )


def test_the_same_average_twice_in_one_request_is_refused(targets):
    with pytest.raises(InvalidInputError) as refused:
        targets.validate_target_draft(
            opponent="MEM", qualifiers=[THREE_PA_AVERAGE, {**THREE_PA_AVERAGE, "threshold": 0.299}]
        )

    assert refused.value.public_message == "A target cannot repeat the same qualifier."


def test_editing_to_a_position_changes_the_title_and_the_stored_rows(targets):
    created = targets.create_target(OWNER, opponent="MEM", qualifiers=[CORNER_THREE], note=None)

    updated = targets.update_target(OWNER, created["id"], changes={"qualifiers": [CORNER_THREE, WING_BIG]})

    assert updated["title"] == "MEM vs Wing/Big, Corner 3 ≥ 40%"
    assert targets.get_target(OWNER, created["id"])["qualifiers"] == [CORNER_THREE, WING_BIG]


_POSITION_REFUSAL = "A position qualifier needs a non-empty list of distinct groups from Guard, Wing and Big."
_AVERAGE_REFUSAL = (
    "An average qualifier needs a known stat, a comparator of at_or_above or "
    "at_or_below, and a finite threshold of 0 or more."
)


@pytest.mark.parametrize(
    ("qualifiers", "message"),
    [
        ([{"base": "role", "groups": ["Big"]}], None),
        ([{"base": "position"}], _POSITION_REFUSAL),
        ([{"base": "position", "groups": []}], _POSITION_REFUSAL),
        ([{"base": "position", "groups": "Big"}], _POSITION_REFUSAL),
        ([{"base": "position", "groups": ["Center"]}], _POSITION_REFUSAL),
        ([{"base": "position", "groups": ["big"]}], _POSITION_REFUSAL),
        ([{"base": "position", "groups": ["Big", "Big"]}], _POSITION_REFUSAL),
        ([{"base": "position", "groups": [None]}], _POSITION_REFUSAL),
        (
            [WING_BIG, {"base": "position", "groups": ["Guard"]}],
            "A target may hold at most one position qualifier.",
        ),
        ([{**THREE_PA_AVERAGE, "stat": "3pa"}], _AVERAGE_REFUSAL),
        ([{**THREE_PA_AVERAGE, "stat": "NOPE"}], _AVERAGE_REFUSAL),
        ([{**THREE_PA_AVERAGE, "stat": None}], _AVERAGE_REFUSAL),
        ([{**THREE_PA_AVERAGE, "comparator": "equals"}], _AVERAGE_REFUSAL),
        ([{**THREE_PA_AVERAGE, "threshold": -0.1}], _AVERAGE_REFUSAL),
        ([{**THREE_PA_AVERAGE, "threshold": float("nan")}], _AVERAGE_REFUSAL),
        ([{**THREE_PA_AVERAGE, "threshold": float("inf")}], _AVERAGE_REFUSAL),
        ([{**THREE_PA_AVERAGE, "threshold": "0.3"}], _AVERAGE_REFUSAL),
        ([{**THREE_PA_AVERAGE, "threshold": True}], _AVERAGE_REFUSAL),
        ([{**THREE_PA_AVERAGE, "threshold": None}], _AVERAGE_REFUSAL),
    ],
)
def test_an_unusable_position_or_average_is_the_invalid_qualifier_refusal(targets, qualifiers, message):
    with pytest.raises(InvalidInputError) as refused:
        targets.create_target(OWNER, opponent="MEM", qualifiers=qualifiers, note=None)

    assert refused.value.public_message == message or (
        message is None and refused.value.public_message.startswith("A qualifier needs a known diet base")
    )
    assert targets.list_targets(OWNER) == []


def test_an_average_threshold_above_one_is_not_a_share(targets):
    created = targets.create_target(
        OWNER, opponent="MEM", qualifiers=[{**THREE_PA_AVERAGE, "stat": "PTS", "threshold": 24.5}], note=None
    )

    assert created["qualifiers"][0]["threshold"] == 24.5


def test_the_qualifier_limit_counts_every_kind(targets):
    nine_diet = [{**CORNER_THREE, "threshold": round(0.1 + index * 0.01, 2)} for index in range(8)]
    ten = [*nine_diet, WING_BIG, THREE_PA_AVERAGE]
    assert len(ten) == TARGET_QUALIFIER_LIMIT

    assert len(targets.create_target(OWNER, opponent="MEM", qualifiers=ten, note=None)["qualifiers"]) == 10
    with pytest.raises(InvalidInputError):
        targets.create_target(
            OWNER,
            opponent="POR",
            qualifiers=[*ten, {**THREE_PA_AVERAGE, "stat": "REB"}],
            note=None,
        )


def test_position_and_average_targets_walk_end_to_end_over_http(
    tmp_path, make_client, authenticate, make_db_user
):
    from app.utils.db import get_engine

    database_url = f"sqlite:///{tmp_path / 'targets-player-rules.sqlite3'}"
    get_engine.cache_clear()
    engine = create_engine(database_url)
    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(
            User.__table__.insert(),
            {
                "firebase_uid": OWNER,
                "email": f"{OWNER}@example.com",
                "display_name": OWNER,
                "photo_url": None,
                "created_at": datetime(2026, 8, 1, tzinfo=UTC),
                "last_login": datetime(2026, 8, 1, tzinfo=UTC),
                "is_active": True,
            },
        )
    engine.dispose()
    client = make_client(database_url)
    headers = authenticate(claims={"uid": OWNER}, db_user=make_db_user(firebase_uid=OWNER))

    created = client.post(
        "/api/user/targets",
        headers=headers,
        json={"opponent": "MEM", "qualifiers": [THREE_PA_AVERAGE, {"base": "position", "groups": ["Big"]}]},
    )
    assert created.status_code == 201
    target = created.get_json()["target"]
    assert target["title"] == "MEM vs Big, 3PA avg ≥ 0.3"
    assert target["qualifiers"] == [THREE_PA_AVERAGE, {"base": "position", "groups": ["Big"]}]

    reordered = client.post(
        "/api/user/targets",
        headers=headers,
        json={"opponent": "MEM", "qualifiers": [{"base": "position", "groups": ["Big"]}, THREE_PA_AVERAGE]},
    )
    assert reordered.status_code == 409

    for qualifiers in (
        [{"base": "position", "groups": []}],
        [{"base": "position", "groups": ["Big"]}, {"base": "position", "groups": ["Wing"]}],
        [{**THREE_PA_AVERAGE, "stat": "NOPE"}],
        [{**THREE_PA_AVERAGE, "threshold": -1}],
        [{"base": "archetype", "groups": ["Big"]}],
    ):
        refused = client.post(
            "/api/user/targets", headers=headers, json={"opponent": "POR", "qualifiers": qualifiers}
        )
        assert refused.status_code == 400
        assert refused.get_json()["error"]["code"] == "invalid_input"

    edited = client.patch(
        f"/api/user/targets/{target['id']}",
        headers=headers,
        json={"qualifiers": [{"base": "position", "groups": ["Guard", "Wing"]}]},
    )
    assert edited.status_code == 200
    assert edited.get_json()["target"]["title"] == "MEM vs Guard/Wing"
    listed = client.get("/api/user/targets", headers=headers).get_json()["targets"]
    assert [entry["qualifiers"] for entry in listed] == [[{"base": "position", "groups": ["Guard", "Wing"]}]]
    get_engine.cache_clear()
