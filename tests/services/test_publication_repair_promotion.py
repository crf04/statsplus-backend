"""Grouped publication repair promotion contracts (#232).

A repair group's members move together or not at all.  These tests own the
transaction: guards, evidence, composition, pointer advancement, the discarded
rollback targets, and the single operator audit.  Provider-shaped derivation of
an opponent-zone payload is exercised end to end in
``tests/test_residential_collector.py``; here it is replaced by a deterministic
candidate so the assertions are about the promotion, not the provider.
"""

from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest
from sqlalchemy import create_engine, select

import app.services.collection_control as collection_control_module
from app.domain.nba_teams import NBA_TEAM_ID_TO_TRICODE
from app.domain.team_matchup_taxonomy import (
    NBA_PUBLICATION_TAXONOMY,
    SHOT_ZONE_SLICES,
)
from app.migrations import run_migrations
from app.models.athlete_catalog import AthleteCatalog
from app.models.collection_control import (
    AuditEvent,
    CollectionObservation,
    CompositionJob,
    PublicationPointer,
    PublicationRepairGroup,
    PublicationVersion,
)
from app.models.event_catalog import EventCatalogEntry
from app.services.collection_control import (
    NBA_TEAM_IDS,
    CollectionControlService,
    CollectionOperationsService,
    ControlPlaneError,
    PublicationService,
)
from tests.services.test_collection_control import (
    L15_ZONES,
    SEASON_ZONES,
    ZONE_REPAIR_SCOPES,
    _zone_collector_claims,
)

UTC = timezone.utc
SEASON = "2025-26"
CANONICAL_TEAM_IDS = tuple(sorted(NBA_TEAM_ID_TO_TRICODE))
NOW = datetime(2026, 8, 12, tzinfo=UTC)
BROKEN_CUTOFF = datetime(2026, 8, 10, tzinfo=UTC)
REPAIR_CUTOFF = datetime(2026, 8, 11, tzinfo=UTC)
SCHEDULED_AT = datetime(2026, 8, 9, tzinfo=UTC)
REPAIR_REASON = "Opponent zone Totals were published from the broken Per48 mode."


# The Last-15 window is validated as exactly fifteen governed games per team,
# so the seeded schedule gives every team fifteen completed games.
ROUNDS = 15
PAIRS = 15


def _game_id(round_index, pair_index):
    return f"game-{round_index}-{pair_index}"


def _events():
    team_ids = sorted(NBA_TEAM_IDS)
    return [{
        "nba_game_id": _game_id(round_index, pair_index),
        "home_team_id": team_ids[pair_index * 2],
        "away_team_id": team_ids[pair_index * 2 + 1],
        "phase": "Regular Season",
        "status": "Final",
        "scheduled_at": SCHEDULED_AT.isoformat(),
        "athlete_ids": ["1"],
    } for round_index in range(ROUNDS) for pair_index in range(PAIRS)]


def _catalog_document(kind):
    if kind == "event":
        return {"events": _events()}
    return {"identities": [{
        "player_id": "1", "team_id": sorted(NBA_TEAM_IDS)[0], "status": "active",
        "event_ids": [
            _game_id(round_index, pair_index)
            for round_index in range(ROUNDS) for pair_index in range(PAIRS)
        ],
    }]}


def _seed_governed_evidence(engine):
    team_ids = sorted(NBA_TEAM_IDS)
    with engine.begin() as connection:
        for round_index in range(ROUNDS):
            for pair_index in range(PAIRS):
                connection.execute(EventCatalogEntry.__table__.insert().values(
                    nba_game_id=_game_id(round_index, pair_index), season=SEASON,
                    home_team_id=int(team_ids[pair_index * 2]),
                    home_team_name="Home", home_team_tricode="ATL",
                    away_team_id=int(team_ids[pair_index * 2 + 1]),
                    away_team_name="Away", away_team_tricode="BOS",
                    scheduled_at=SCHEDULED_AT, status_text="Final", status_code=3,
                    postponed_status=None, postponement_evidence=None,
                    classification="Regular Season", first_seen_at=NOW,
                    last_seen_at=NOW,
                ))
        connection.execute(AthleteCatalog.__table__.insert().values(
            season=SEASON, player_id=1, display_name="Player One",
            roster_status="active", is_active=True, is_active_for_season=True,
            team_id=int(team_ids[0]), team_name="Home", team_abbreviation="ATL",
            published_at=NOW,
        ))


def _expected_game_ids():
    """The fifteen governed games each team played in the seeded schedule."""

    team_ids = sorted(NBA_TEAM_IDS)
    expected: dict[int, frozenset[str]] = {}
    for pair_index in range(PAIRS):
        games = frozenset(
            _game_id(round_index, pair_index) for round_index in range(ROUNDS)
        )
        expected[int(team_ids[pair_index * 2])] = games
        expected[int(team_ids[pair_index * 2 + 1])] = games
    return expected


class _GovernanceStub:
    """The immutable window authority a governed publication is bound to."""

    def __init__(self, expected):
        self.expected = expected

    def resolve_team_game_ids(self, season, cutoff, *, window, manifest_id=None,
                              event_catalog_publication_id=None,
                              event_catalog_checksum=None):
        return self.expected

    def resolve_l15_date_from_by_team(self, season, cutoff, *, manifest_id,
                                      event_catalog_publication_id,
                                      event_catalog_checksum):
        return {team_id: "2026-08-01" for team_id in self.expected}


def _derived_payload(expected, *, per48_value):
    metrics = sorted(NBA_PUBLICATION_TAXONOMY["shot_zones"])
    return {"rows": [{
        "team_id": team_id,
        "team_tricode": NBA_TEAM_ID_TO_TRICODE[team_id],
        "game_ids": sorted(expected[team_id]),
        "game_count": len(expected[team_id]),
        "per48": {key: per48_value for key in metrics},
        "league_average": {key: 1.0 for key in metrics},
        "population_sigma": {key: 1.0 for key in metrics},
        "competition_rank": {key: 1 for key in metrics},
    } for team_id in CANONICAL_TEAM_IDS]}


def _insert_zone_evidence(engine, *, manifest_id, cutoff, marker):
    """Insert base-complete opponent shot-zone evidence for both windows."""

    rows = []
    for window in ("season", "l15"):
        for index, slice_key in enumerate(sorted(SHOT_ZONE_SLICES)):
            payload = json.dumps({
                "base": "shot_zones",
                "records": [
                    {"base": "shot_zones", "slice_key": slice_key,
                     "team_id": team_id}
                    for team_id in sorted(NBA_TEAM_IDS)
                ],
            }, separators=(",", ":"))
            rows.append({
                "observation_id": f"obs-{marker}-{window}-{index}",
                "client_observation_id": f"client-{marker}-{window}-{index}",
                "collector_id": "collector", "manifest_id": manifest_id,
                "environment": "testing", "provider": "nba",
                "observation_type": "shot_zones_opponent",
                "scope": json.dumps({"window": window}),
                "season": SEASON, "cutoff": cutoff, "schema_version": 2,
                "checksum": hashlib.sha256(payload.encode()).hexdigest(),
                "payload": payload, "payload_bytes": len(payload),
                "retrieved_at": cutoff, "accepted_at": cutoff,
            })
    with engine.begin() as connection:
        connection.execute(CollectionObservation.__table__.insert(), rows)


def _queue_jobs(engine, *, manifest_id, cutoff):
    with engine.begin() as connection:
        connection.execute(CompositionJob.__table__.insert(), [{
            "job_id": f"{stream_key}-{manifest_id}", "stream_key": stream_key,
            "manifest_id": manifest_id, "season": SEASON, "cutoff": cutoff,
            "status": "queued", "attempts": 0, "created_at": cutoff,
            "updated_at": cutoff,
        } for stream_key in (SEASON_ZONES, L15_ZONES)])


def build_repair_environment(engine, monkeypatch):
    """Build a displaced opponent-zone pair and a manifest repairing it.

    Taking the engine as an argument lets the credential-free SQLite suite and
    the PostgreSQL integration seam exercise the identical scenario.
    """

    _seed_governed_evidence(engine)
    control = CollectionControlService(engine, clock=lambda: NOW)
    expected = _expected_game_ids()
    publications = PublicationService(
        engine, clock=lambda: NOW,
        l15_expectation_resolver=_GovernanceStub(expected),
    )
    for stream_key, window in ((SEASON_ZONES, "season"), (L15_ZONES, "l15")):
        publications.register_stream(
            stream_key, provider="nba", owner="residential_collector",
            required_observations=["shot_zones_opponent"],
            publication_strategy="snapshot_replace", supported_windows=[window],
            completeness_rule="base_complete", enabled=True,
        )
    control.activate_season(SEASON, actor="operator")

    per48 = {"value": 1.0}

    def derive(*args, **kwargs):
        return _derived_payload(expected, per48_value=per48["value"])

    monkeypatch.setattr(
        collection_control_module, "_compose_nba_observation_payload", derive,
    )

    scopes = [SEASON_ZONES, L15_ZONES, "canonical_game_ledger"]

    def publish_catalogs(cutoff):
        for kind in ("event", "athlete"):
            request = control.create_bootstrap_request(SEASON, kind, cutoff=cutoff)
            control.publish_catalog(
                request.request_id, _catalog_document(kind),
                version=f"{kind}-{cutoff.date()}",
            )

    # The defect: both windows published from the broken provider mode, at an
    # earlier cutoff, each leaving the other as its rollback target.
    publish_catalogs(BROKEN_CUTOFF)
    broken_manifest = control.create_manifest(
        SEASON, cutoff=BROKEN_CUTOFF, scopes=scopes,
        collect_before=NOW + timedelta(hours=1),
    )
    _insert_zone_evidence(
        engine, manifest_id=broken_manifest.manifest_id,
        cutoff=BROKEN_CUTOFF, marker="broken",
    )
    displaced = {
        stream_key: publications.compose_from_observations(
            stream_key, season=SEASON, cutoff=BROKEN_CUTOFF,
            manifest_id=broken_manifest.manifest_id,
        )
        for stream_key in (SEASON_ZONES, L15_ZONES)
    }

    per48["value"] = 2.0
    publish_catalogs(REPAIR_CUTOFF)
    repair_manifest = control.create_manifest(
        SEASON, cutoff=REPAIR_CUTOFF, scopes=scopes,
        collect_before=NOW + timedelta(hours=1),
        repair_group={"reason": REPAIR_REASON, "members": [{
            "stream_key": stream_key,
            "expected_publication_id": displaced[stream_key].publication_id,
            "expected_fence": int(displaced[stream_key].fence),
        } for stream_key in (SEASON_ZONES, L15_ZONES)]},
    )
    _insert_zone_evidence(
        engine, manifest_id=repair_manifest.manifest_id,
        cutoff=REPAIR_CUTOFF, marker="repair",
    )
    _queue_jobs(
        engine, manifest_id=repair_manifest.manifest_id, cutoff=REPAIR_CUTOFF,
    )
    operations = CollectionOperationsService(
        engine, publication_service=publications, collection_control=control,
        clock=lambda: NOW,
    )
    return {
        "engine": engine,
        "control": control,
        "publications": publications,
        "operations": operations,
        "manifest": repair_manifest,
        "displaced": displaced,
        "expected": expected,
        "per48": per48,
    }


@pytest.fixture
def repair(tmp_path, monkeypatch):
    """A displaced opponent-zone pair and a manifest declaring their repair."""

    engine = create_engine(f"sqlite:///{tmp_path / 'repair.sqlite3'}")
    run_migrations(engine)
    return build_repair_environment(engine, monkeypatch)


def read_pointers(engine):
    with engine.connect() as connection:
        return {
            row.stream_key: (row.active_publication_id,
                             row.previous_publication_id, int(row.fence))
            for row in connection.execute(select(PublicationPointer))
        }


def read_statuses(engine):
    with engine.connect() as connection:
        return {
            row.publication_id: row.status
            for row in connection.execute(select(PublicationVersion))
        }


def test_grouped_promotion_advances_both_pointers_and_records_one_audit(repair):
    engine, displaced = repair["engine"], repair["displaced"]
    before = read_pointers(engine)

    result = repair["operations"].promote_repair_group(
        repair["manifest"].manifest_id,
        actor="operator@example.com",
        reason="replace the broken opponent zone pair",
    )
    promotion = result.resource

    after = read_pointers(engine)
    for stream_key in (SEASON_ZONES, L15_ZONES):
        active, previous, fence = after[stream_key]
        assert active != before[stream_key][0]
        assert fence == before[stream_key][2] + 1
        # The displaced version is the defect, so it must not remain
        # reachable as this stream's rollback target.
        assert previous is None

    statuses = read_statuses(engine)
    for stream_key in (SEASON_ZONES, L15_ZONES):
        assert statuses[displaced[stream_key].publication_id] == "superseded"
        assert statuses[after[stream_key][0]] == "active"

    # Both replacements carry the repaired value, so the pair is coherent.
    with engine.connect() as connection:
        payloads = {
            row.stream_key: json.loads(row.payload)
            for row in connection.execute(select(PublicationVersion).where(
                PublicationVersion.publication_id.in_(
                    [after[stream_key][0] for stream_key in (SEASON_ZONES, L15_ZONES)]
                )
            ))
        }
    for payload in payloads.values():
        assert {value for row in payload["rows"] for value in row["per48"].values()} == {2.0}

    assert {item["stream_key"] for item in promotion.discarded} == {
        SEASON_ZONES, L15_ZONES,
    }
    assert {item["publication_id"] for item in promotion.discarded} == {
        displaced[SEASON_ZONES].publication_id,
        displaced[L15_ZONES].publication_id,
    }

    with engine.connect() as connection:
        audits = [
            row for row in connection.execute(select(AuditEvent))
            if row.action == "publication.repair_group.promote"
        ]
    assert len(audits) == 1
    details = json.loads(audits[0].details)
    assert details["repair_reason"] == REPAIR_REASON
    assert {item["publication_id"] for item in details["discarded_publications"]} == {
        displaced[SEASON_ZONES].publication_id,
        displaced[L15_ZONES].publication_id,
    }
    assert audits[0].reason == "replace the broken opponent zone pair"

    # The held jobs are settled by the promotion that replaced them.
    with engine.connect() as connection:
        assert {
            row.status for row in connection.execute(select(CompositionJob))
        } == {"succeeded"}


def test_rollback_is_unavailable_until_a_later_publication_is_trustworthy(repair):
    repair["operations"].promote_repair_group(
        repair["manifest"].manifest_id, actor="operator", reason="repair the pair",
    )

    for stream_key in (SEASON_ZONES, L15_ZONES):
        with pytest.raises(ControlPlaneError, match="rollback_unavailable"):
            repair["publications"].rollback(stream_key, reason="undo the repair")

    # A later ordinary publication re-establishes a trustworthy previous
    # version, and only then does rollback become available again.
    repair["per48"]["value"] = 3.0
    later = repair["publications"].compose_from_observations(
        SEASON_ZONES, season=SEASON, cutoff=REPAIR_CUTOFF,
        manifest_id=repair["manifest"].manifest_id,
    )
    assert later.status == "active"
    restored = repair["publications"].rollback(
        SEASON_ZONES, reason="undo the later publication",
    )
    assert restored.status == "rollback"


def test_missing_evidence_leaves_the_whole_prior_state_untouched(repair):
    engine = repair["engine"]
    # One Last-15 zone slice never arrived, so that member's evidence is not
    # complete for the manifest's season and cutoff.
    with engine.begin() as connection:
        connection.execute(CollectionObservation.__table__.delete().where(
            CollectionObservation.observation_id == "obs-repair-l15-0"
        ))
    before, statuses = read_pointers(engine), read_statuses(engine)

    with pytest.raises(ControlPlaneError, match="base_incomplete"):
        repair["operations"].promote_repair_group(
            repair["manifest"].manifest_id, actor="operator", reason="repair the pair",
        )

    assert read_pointers(engine) == before
    assert read_statuses(engine) == statuses
    with engine.connect() as connection:
        assert connection.execute(select(AuditEvent).where(
            AuditEvent.action == "publication.repair_group.promote"
        )).all() == []
        assert connection.execute(select(PublicationRepairGroup)).one().promoted_at is None


def test_a_stale_guard_refuses_the_promotion_without_touching_any_pointer(repair):
    engine = repair["engine"]
    with engine.begin() as connection:
        connection.execute(PublicationPointer.__table__.update().where(
            PublicationPointer.stream_key == L15_ZONES
        ).values(fence=99))
    before, statuses = read_pointers(engine), read_statuses(engine)

    with pytest.raises(ControlPlaneError, match="repair_group_guard_stale"):
        repair["operations"].promote_repair_group(
            repair["manifest"].manifest_id, actor="operator", reason="repair the pair",
        )

    assert read_pointers(engine) == before
    assert read_statuses(engine) == statuses


def test_a_failure_between_pointer_updates_rolls_the_whole_group_back(repair, monkeypatch):
    engine = repair["engine"]
    before, statuses = read_pointers(engine), read_statuses(engine)
    publications = repair["publications"]
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
        repair["operations"].promote_repair_group(
            repair["manifest"].manifest_id, actor="operator", reason="repair the pair",
        )

    assert calls["count"] == 2
    # The first member's advance is undone with the rest: active pointers,
    # previous pointers, version status, and the success audit roll back
    # together.
    assert read_pointers(engine) == before
    assert read_statuses(engine) == statuses
    with engine.connect() as connection:
        assert connection.execute(select(AuditEvent).where(
            AuditEvent.action == "publication.repair_group.promote"
        )).all() == []
        assert connection.execute(select(PublicationRepairGroup)).one().promoted_at is None


def test_a_failed_repair_does_not_block_unrelated_publication_work(repair):
    engine = repair["engine"]
    with engine.begin() as connection:
        connection.execute(PublicationPointer.__table__.update().where(
            PublicationPointer.stream_key == L15_ZONES
        ).values(fence=99))
    with pytest.raises(ControlPlaneError, match="repair_group_guard_stale"):
        repair["operations"].promote_repair_group(
            repair["manifest"].manifest_id, actor="operator", reason="repair the pair",
        )

    # An unrelated stream on a manifest with no group still publishes.
    control, publications = repair["control"], repair["publications"]
    publications.register_stream(
        "unrelated", provider="nba", owner="collector",
        required_observations=[], publication_strategy="replace",
        supported_windows=["season"], completeness_rule="provider_readable",
        enabled=True,
    )
    unrelated = publications.compose(
        "unrelated", season=SEASON, cutoff=REPAIR_CUTOFF, payload={"published": True},
    )
    assert publications.current("unrelated").publication_id == unrelated.publication_id
    assert control.repair_group_state(
        repair["manifest"].manifest_id
    )["promotable"] is False


def test_a_promoted_group_releases_its_members_and_cannot_be_replayed(repair):
    engine, control = repair["engine"], repair["control"]
    repair["operations"].promote_repair_group(
        repair["manifest"].manifest_id, actor="operator", reason="repair the pair",
    )

    state = control.repair_group_state(repair["manifest"].manifest_id)
    assert state["state"] == "promoted"
    assert state["promotable"] is False
    assert state["promoted_at"] is not None

    # A collector polling the same manifest sees the consumed declaration too,
    # and still never its pointer guards.
    claims = _zone_collector_claims(
        engine, now=NOW, label="promoted-view",
        surfaces=[SEASON_ZONES, L15_ZONES],
    )
    view = control.get_manifest(
        repair["manifest"].manifest_id, claims=claims,
    )._repair_group
    assert view["execution"] == "promoted"
    assert "expected_fence" not in json.dumps(view)

    with pytest.raises(ControlPlaneError, match="repair_group_already_promoted"):
        repair["operations"].promote_repair_group(
            repair["manifest"].manifest_id, actor="operator", reason="repair again",
        )

    # The declaration is consumed, so the members publish independently again.
    repair["per48"]["value"] = 4.0
    published = repair["publications"].compose_from_observations(
        L15_ZONES, season=SEASON, cutoff=REPAIR_CUTOFF,
        manifest_id=repair["manifest"].manifest_id,
    )
    assert published.status == "active"
    assert read_pointers(engine)[L15_ZONES][1] is not None


def test_ordinary_manifests_still_publish_season_and_last15_independently(repair):
    """The non-grouped path is unchanged: one window can advance alone."""

    engine, control = repair["engine"], repair["control"]
    ordinary = control.create_manifest(
        SEASON, cutoff=REPAIR_CUTOFF,
        scopes=[SEASON_ZONES, L15_ZONES, "canonical_game_ledger"],
        collect_before=NOW + timedelta(hours=1),
    )
    _insert_zone_evidence(
        engine, manifest_id=ordinary.manifest_id, cutoff=REPAIR_CUTOFF,
        marker="ordinary",
    )
    assert control.repair_group_state(ordinary.manifest_id) is None

    repair["per48"]["value"] = 5.0
    season_only = repair["publications"].compose_from_observations(
        SEASON_ZONES, season=SEASON, cutoff=REPAIR_CUTOFF,
        manifest_id=ordinary.manifest_id,
    )
    pointers = read_pointers(engine)
    assert pointers[SEASON_ZONES][0] == season_only.publication_id
    # The Last-15 stream is untouched: no group forced it to move in step.
    assert pointers[L15_ZONES][0] == repair["displaced"][L15_ZONES].publication_id


def test_promotion_requires_a_declared_group(repair):
    control = repair["control"]
    ordinary = control.create_manifest(
        SEASON, cutoff=REPAIR_CUTOFF,
        scopes=[SEASON_ZONES, L15_ZONES, "canonical_game_ledger"],
        collect_before=NOW + timedelta(hours=1),
    )
    with pytest.raises(ControlPlaneError, match="repair_group_not_found"):
        repair["operations"].promote_repair_group(
            ordinary.manifest_id, actor="operator", reason="nothing to repair",
        )


def test_a_validation_failure_on_one_member_rolls_the_whole_group_back(
    repair, monkeypatch,
):
    """A replacement that fails validation stops the group before any advance."""

    engine = repair["engine"]
    before, statuses = read_pointers(engine), read_statuses(engine)
    expected = repair["expected"]

    def derive(*args, **kwargs):
        payload = _derived_payload(expected, per48_value=2.0)
        if kwargs["stream_key"] == L15_ZONES:
            # One team short of the governed thirty: the composed replacement
            # is real, but it cannot pass activation validation.
            payload["rows"] = payload["rows"][:-1]
        return payload

    monkeypatch.setattr(
        collection_control_module, "_compose_nba_observation_payload", derive,
    )
    with pytest.raises(ControlPlaneError, match="publication_candidate_invalid"):
        repair["operations"].promote_repair_group(
            repair["manifest"].manifest_id, actor="operator", reason="repair the pair",
        )

    assert read_pointers(engine) == before
    assert read_statuses(engine) == statuses
    with engine.connect() as connection:
        assert connection.execute(select(AuditEvent).where(
            AuditEvent.action == "publication.repair_group.promote"
        )).all() == []
        assert connection.execute(
            select(PublicationRepairGroup)
        ).one().promoted_at is None
        assert {
            row.status for row in connection.execute(select(CompositionJob))
        } == {"queued"}


def test_a_superseded_manifest_can_no_longer_promote_its_group(repair):
    """A manifest that lost the season's authority must not discard anything."""

    engine, control = repair["engine"], repair["control"]
    before, statuses = read_pointers(engine), read_statuses(engine)

    # Any later manifest for the season supersedes this one, and with it the
    # catalog binding the declaration was made against.
    control.create_manifest(
        SEASON,
        cutoff=REPAIR_CUTOFF,
        scopes=list(ZONE_REPAIR_SCOPES),
        collect_before=NOW + timedelta(hours=1),
    )

    with pytest.raises(ControlPlaneError, match="repair_group_manifest_inactive"):
        repair["operations"].promote_repair_group(
            repair["manifest"].manifest_id, actor="operator", reason="repair the pair",
        )

    assert read_pointers(engine) == before
    assert read_statuses(engine) == statuses
