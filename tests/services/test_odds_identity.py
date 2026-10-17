"""``RosterNameResolver``: Odds API player names against a game's two rosters (#437).

The pairs below are real: each is a name The Odds API posted in the 2025-26
closing-line backfill that the resolver left unresolved, beside the spelling the
athlete catalog holds for that player (ids are the NBA's).  The few synthetic
cases are marked, and exist to pin the "never match two players" rule.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, insert

from app.migrations import run_migrations
from app.models.athlete_catalog import AthleteCatalog
from app.providers.dfs import AthleteEvidence
from app.services.athlete_mapping_repository import AthleteMappingRepository
from app.services.athlete_resolver import AthleteResolver, MappingResolutionState
from app.services.closing_line_backfill import ReadOnlyMappings
from app.services.odds_identity import PROVIDER, RosterNameResolver

SEASON = "2025-26"
DET, BKN, MIL, GSW = 1610612765, 1610612751, 1610612749, 1610612744
THIRD_TEAM = 1610612738  # BOS: not one of the game's two teams
HUGO = 1642864


class Catalog:
    def __init__(self, rows):
        self.rows = rows

    def get_catalog(self, season, *, active_only=False):
        return [dict(row) for row in self.rows]


def athlete(player_id, name, team_id=DET, *, active=True):
    return {
        "season": SEASON,
        "player_id": player_id,
        "display_name": name,
        "roster_status": "active" if active else "inactive",
        "is_active": active,
        "is_active_for_season": active,
        "team_id": team_id,
        "team_name": str(team_id),
        "team_abbreviation": None,
    }


def resolver(rows, repository=None):
    return RosterNameResolver(catalog=Catalog(rows), mapping_repository=repository, season=SEASON)


def resolve(posted, rows, teams=(DET, BKN), repository=None):
    names = [posted] if isinstance(posted, str) else posted
    return resolver(rows, repository).resolve(names, teams)


# -- generational suffixes and nicknames the catalog spells differently --------


@pytest.mark.parametrize(
    ("posted", "catalog_name", "player_id"),
    [
        # The posted name drops the catalog's suffix ...
        ("Jimmy Butler", "Jimmy Butler III", 202710),
        ("Bobby Portis", "Bobby Portis Jr.", 1626171),
        ("Derrick Jones", "Derrick Jones Jr.", 1627884),
        ("Robert Williams", "Robert Williams III", 1629057),
        # ... or adds one the catalog does not carry, in any of its dialects.
        ("Isaiah Stewart II", "Isaiah Stewart", 1630191),
        ("Xavier Tillman, Sr.", "Xavier Tillman", 1630214),
        ("Paul Reed Jr", "Paul Reed", 1630194),
        # The same suffix spelled with and without its period already matched.
        ("Tim Hardaway Jr", "Tim Hardaway Jr.", 203501),
        # A nickname the vendor posts for a player the catalog names in full,
        # and the other way round, with and without the suffix beside it.
        ("Nicolas Claxton", "Nic Claxton", 1629651),
        ("Nic Claxton", "Nicolas Claxton", 1629651),
        ("Herb Jones", "Herbert Jones", 1630529),
        ("Carlton Carrington", "Bub Carrington", 1642267),
        ("Moe Wagner", "Moritz Wagner", 1629021),
        ("Ron Holland", "Ronald Holland II", 1641842),
        ("Vincent Williams Jr", "Vince Williams Jr.", 1631246),
    ],
)
def test_a_posted_name_resolves_to_the_rosters_spelling_of_the_same_player(posted, catalog_name, player_id):
    rows = [athlete(player_id, catalog_name), athlete(203999, "Nikola Jokić", BKN)]

    assert resolve(posted, rows) == {posted: player_id}


@pytest.mark.parametrize(
    ("posted", "catalog_name", "player_id"),
    [
        ("Nikola Jokic", "Nikola Jokić", 203999),
        ("Hugo Gonzalez", "Hugo González", HUGO),
        ("Jaren Jackson Jr.", "Jaren Jackson Jr.", 1628991),
    ],
)
def test_names_that_already_matched_still_match(posted, catalog_name, player_id):
    assert resolve(posted, [athlete(player_id, catalog_name)]) == {posted: player_id}


def test_the_whole_slate_of_one_game_resolves_in_one_call_with_the_players_it_can():
    rows = [
        athlete(1641842, "Ronald Holland II", DET),
        athlete(1630191, "Isaiah Stewart", DET),
        athlete(1629651, "Nic Claxton", BKN),
        athlete(203999, "Nikola Jokić", BKN),
    ]

    resolved = resolve(
        [
            "Ron Holland",
            "Isaiah Stewart II",
            "Nicolas Claxton",
            "Nikola Jokic",
            "Zach Unknown",
        ],
        rows,
    )

    assert resolved == {
        "Ron Holland": 1641842,
        "Isaiah Stewart II": 1630191,
        "Nicolas Claxton": 1629651,
        "Nikola Jokic": 203999,
    }


def test_a_match_carries_the_rosters_name_and_team_for_the_dfs_pipeline():
    athletes = resolver([athlete(1641842, "Ronald Holland II", DET)]).resolve_athletes(
        ["Ron Holland"], (DET, BKN)
    )

    posted = athletes["Ron Holland"]
    assert (posted.player_id, posted.team_id, posted.display_name) == (
        1641842,
        DET,
        "Ronald Holland II",
    )


# -- never two players, never another team ---------------------------------------


@pytest.mark.parametrize(
    ("posted", "catalog"),
    [
        # Same last name, another player: a suffix is not a first name.
        ("Jaren Jackson Jr", [(1641713, "GG Jackson"), (1630543, "Isaiah Jackson")]),
        ("Brandon Williams", [(1631119, "Jaylin Williams"), (1631114, "Jalen Williams")]),
        ("Spencer Jones", [(1626145, "Tyus Jones"), (1630200, "Tre Jones")]),
        # A listed nickname bridges its own player only.
        ("Herb Jones", [(1627884, "Derrick Jones Jr."), (1630200, "Tre Jones")]),
        ("Ron Holland", [(1631199, "Ron Harper Jr.")]),
        # Suffixes that disagree name different men (synthetic: father and son).
        ("Tim Hardaway Sr.", [(203501, "Tim Hardaway Jr.")]),
    ],
)
def test_a_different_player_is_never_matched(posted, catalog):
    rows = [athlete(player_id, name) for player_id, name in catalog]

    assert resolve(posted, rows) == {}


def test_a_player_on_neither_team_of_the_game_is_not_matched():
    rows = [athlete(202710, "Jimmy Butler III", THIRD_TEAM)]

    assert resolve("Jimmy Butler", rows) == {}


def test_two_roster_players_that_could_match_leave_the_name_unresolved():
    # Synthetic: a father and son on the two rosters.  "Tim Hardaway" could be
    # either, so neither is chosen.
    rows = [
        athlete(203501, "Tim Hardaway Jr.", DET),
        athlete(900001, "Tim Hardaway Sr.", BKN),
    ]

    assert resolve("Tim Hardaway", rows) == {}
    # Named in full, each is still found.
    assert resolve(["Tim Hardaway Jr", "Tim Hardaway Sr"], rows) == {
        "Tim Hardaway Jr": 203501,
        "Tim Hardaway Sr": 900001,
    }


def test_an_inactive_namesake_on_the_other_roster_still_makes_a_bare_name_ambiguous():
    # Synthetic: Sr. is on the other team's books but inactive.  "Tim Hardaway"
    # could still be him, so the active Jr. is not chosen on his behalf.
    rows = [
        athlete(203501, "Tim Hardaway Jr.", DET),
        athlete(900001, "Tim Hardaway Sr.", BKN, active=False),
    ]

    assert resolve("Tim Hardaway", rows) == {}


def test_two_players_one_nickname_apart_leave_the_name_unresolved():
    # Synthetic: "Nic Claxton" is no exact name on this roster, and both
    # Nicolas Claxton and Nic Claxton Jr. could be him.
    rows = [athlete(1629651, "Nicolas Claxton", BKN), athlete(900002, "Nic Claxton Jr.", DET)]

    assert resolve("Nic Claxton", rows) == {}


def test_an_exact_name_wins_over_a_player_who_differs_only_by_a_suffix():
    # Synthetic: Gary Payton (father) and Gary Payton II on one roster.
    rows = [athlete(900003, "Gary Payton", DET), athlete(1627780, "Gary Payton II", BKN)]

    assert resolve(["Gary Payton", "Gary Payton II"], rows) == {
        "Gary Payton": 900003,
        "Gary Payton II": 1627780,
    }


def test_a_player_the_catalog_lists_as_inactive_is_not_matched_by_a_variant():
    rows = [athlete(1626171, "Bobby Portis Jr.", MIL, active=False)]

    assert resolve("Bobby Portis", rows, teams=(MIL, GSW)) == {}


# -- governance still decides ------------------------------------------------------


@pytest.fixture
def repository(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'mapping.sqlite3'}")
    run_migrations(engine)
    return AthleteMappingRepository(engine)


def in_catalog(repository, rows):
    """The rows in the database too: an approval needs its athlete in the catalog."""

    with repository.engine.begin() as connection:
        connection.execute(
            insert(AthleteCatalog.__table__),
            [{**row, "published_at": datetime(2026, 7, 1, tzinfo=UTC)} for row in rows],
        )


def test_a_matched_variant_is_recorded_under_the_posted_name_as_an_auto_mapping(repository):
    rows = [athlete(1626171, "Bobby Portis Jr.", MIL)]

    assert resolve("Bobby Portis", rows, teams=(MIL, GSW), repository=repository) == {"Bobby Portis": 1626171}

    mapping = repository.get_mapping(PROVIDER, "name:bobbyportis")
    assert (mapping.mapping_state, mapping.canonical_player_id, mapping.is_active) == (
        "auto",
        1626171,
        True,
    )


def test_a_matched_variant_keeps_the_provider_name_that_was_posted(repository):
    rows = [athlete(1641842, "Ronald Holland II", DET)]

    resolve("Ron Holland", rows, repository=repository)

    mapping = repository.get_mapping(PROVIDER, "name:ronholland")
    assert (mapping.provider_name, mapping.canonical_name) == ("Ron Holland", "Ronald Holland II")


def test_an_approval_of_the_posted_spelling_is_honoured_and_stays_approved(repository):
    """The operator approved ``Bobby Portis`` on the evidence they reviewed; the
    variant rule must not turn that into a conflict by changing the evidence."""

    rows = [athlete(1626171, "Bobby Portis Jr.", MIL)]
    in_catalog(repository, rows)
    repository.approve(
        PROVIDER,
        "name:bobbyportis",
        1626171,
        season=SEASON,
        operator_id="operator",
        reason="the vendor's spelling",
        provider_evidence=AthleteEvidence(provider_id="name:bobbyportis", name="Bobby Portis"),
    )

    assert resolve("Bobby Portis", rows, teams=(MIL, GSW), repository=repository) == {"Bobby Portis": 1626171}
    assert repository.get_mapping(PROVIDER, "name:bobbyportis").mapping_state == "manual_approved"


def test_an_approval_decides_for_a_posted_name_a_variant_would_have_matched_otherwise(repository):
    """Synthetic: the operator mapped ``Bobby Portis`` to another player on the
    roster; the variant rule, which would pick the Jr., yields to that decision."""

    rows = [athlete(1626171, "Bobby Portis Jr.", MIL), athlete(1630224, "Ryan Rollins", MIL)]
    in_catalog(repository, rows)
    repository.approve(
        PROVIDER,
        "name:bobbyportis",
        1630224,
        season=SEASON,
        operator_id="operator",
        reason="the operator's call",
    )

    for mappings in (repository, ReadOnlyMappings(repository)):
        assert resolve("Bobby Portis", rows, teams=(MIL, GSW), repository=mappings) == {
            "Bobby Portis": 1630224
        }


def test_a_stored_conflict_keeps_a_variant_unresolved_through_the_read_only_view(repository):
    # Synthetic: the bare name fit the Jr. in one game, then a Sr. in another, so
    # the shared identity became a conflict (as two namesakes' always did).
    jr = [athlete(1626171, "Bobby Portis Jr.", MIL)]
    sr = [athlete(900002, "Bobby Portis Sr.", MIL)]
    assert resolve("Bobby Portis", jr, teams=(MIL, GSW), repository=repository) == {"Bobby Portis": 1626171}
    assert resolve("Bobby Portis", sr, teams=(MIL, GSW), repository=repository) == {}
    assert repository.get_mapping(PROVIDER, "name:bobbyportis").mapping_state == "mapping_conflict"

    assert resolve("Bobby Portis", jr, teams=(MIL, GSW), repository=repository) == {}
    read_only = ReadOnlyMappings(repository)
    assert resolve("Bobby Portis", jr, teams=(MIL, GSW), repository=read_only) == {}


def test_an_earlier_automatic_mapping_does_not_let_an_ambiguous_name_resolve(repository):
    # Synthetic: "Tim Hardaway" was mapped to the Jr. when he was the only fit.
    # Once a Sr. is on the other roster the name could be either; it is not kept.
    jr = athlete(203501, "Tim Hardaway Jr.", DET)
    both = [jr, athlete(900001, "Tim Hardaway Sr.", BKN)]
    assert resolve("Tim Hardaway", [jr], repository=repository) == {"Tim Hardaway": 203501}

    assert resolve("Tim Hardaway", both, repository=ReadOnlyMappings(repository)) == {}
    assert resolve("Tim Hardaway", both, repository=repository) == {}


def test_a_rejected_posted_name_stays_unresolved_whatever_the_roster_spelling(repository):
    repository.reject(PROVIDER, "name:bobbyportis", operator_id="operator", reason="not this player")
    rows = [athlete(1626171, "Bobby Portis Jr.", MIL)]

    assert resolve("Bobby Portis", rows, teams=(MIL, GSW), repository=repository) == {}


def test_a_name_that_already_matched_is_recorded_as_posted_not_as_the_roster_spells_it(
    repository,
):
    """Only a name the resolver could not place before is rewritten to the roster's
    spelling; the mapping of a name that matched all along keeps what was posted."""

    rows = [athlete(203999, "Nikola Jokić", BKN)]

    assert resolve("Nikola Jokic", rows, repository=repository) == {"Nikola Jokic": 203999}

    mapping = repository.get_mapping(PROVIDER, "name:nikolajokic")
    assert (mapping.provider_name, mapping.canonical_name) == ("Nikola Jokic", "Nikola Jokić")


def test_a_read_only_view_of_the_mappings_still_applies_a_rejection_and_an_approval(
    repository,
):
    rows = [athlete(1626171, "Bobby Portis Jr.", MIL)]
    in_catalog(repository, rows)
    repository.reject(PROVIDER, "name:bobbyportis", operator_id="operator", reason="no")
    repository.approve(
        PROVIDER,
        "name:bobbyp",
        1626171,
        season=SEASON,
        operator_id="operator",
        reason="the vendor's short form",
    )
    resolver_ = AthleteResolver(Catalog(rows), mapping_repository=ReadOnlyMappings(repository))

    rejected = resolver_.resolve(
        PROVIDER, AthleteEvidence(provider_id="name:bobbyportis", name="Bobby Portis Jr."), SEASON
    )
    approved = resolver_.resolve(PROVIDER, AthleteEvidence(provider_id="name:bobbyp", name="Bobby P"), SEASON)

    assert rejected.state is MappingResolutionState.REJECTED
    assert approved.state is MappingResolutionState.MANUAL_APPROVED
    assert approved.canonical_player_id == 1626171
