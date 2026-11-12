"""A selected 2026-27 reads its held candidates as if the cutover had run (statsplus#330).

2025-26 is the Published Season; 2026-27 candidates are held.  Only a read
scoped to an explicit selection may serve them, and only a candidate that
passes the cutover's own per-publication checks; a family's Readiness Rule is
not one of them (statsplus#386).  Families with no candidate keep their 2025-26
Publication labelled ``prior_season``; game logs that cannot serve make the
season unavailable.  Every test reads through the real reader,
repositories and services of ``tests/support/selected_season``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta

import pytest
from sqlalchemy import select, text

from app.config.settings import NBASeasonSettings
from app.models.collection_control import (
    CatalogPublication,
    CollectionManifest,
    CollectionObservation,
    PublicationVersion,
)
from app.models.game_logs import GameLogQuery
from app.services.database_first_activation import ReadSeason, SeasonMode, SeasonUnavailable
from app.services.player_service import PlayerProfileReader, PlayerService
from app.services.research_season import season_scoped
from app.services.serving_season import trailing
from tests.services.test_serving_season_matchup import _log_payload
from tests.support.season_cutover import (
    L15_STREAM,
    NEW,
    NEW_CUTOFF,
    NEW_GAMES,
    NEW_PROVENANCE,
    OLD,
    PLAYER_LOGS,
    SEASON_STREAM,
    THIN_CUTOFF,
    THIN_GAMES,
    THIN_PROVENANCE,
    accept_ledger,
    provenance,
    team_defense_waits_for_ten_games,
)
from tests.support.selected_season import (
    PLAY_TYPE_DEFENSE,
    PLAYER_ID,
    PLAYER_NAME,
    PLAYER_SYNERGY,
    SelectedWorld,
    settings,
)
from tests.test_game_log_preview import FakeRedis

KEYS = (PLAYER_LOGS, SEASON_STREAM, L15_STREAM)


@pytest.fixture
def world(tmp_path):
    built = SelectedWorld(tmp_path)
    yield built
    built.engine.dispose()


def _reads(snapshot):
    return {
        key: (read.status, read.season, read.unavailable_reason, read.publication_id)
        for key, read in snapshot.reads.items()
    }


def _live(world, key):
    with world.engine.connect() as connection:
        return connection.scalar(
            text("SELECT active_publication_id FROM publication_pointers WHERE stream_key = :k"), {"k": key}
        )


def _logs(world, season, *, selected):
    service = season_scoped(world.games, season) if selected else world.games
    return service.get_filtered_logs(PLAYER_NAME, GameLogQuery(season_filter=season))


# --- the reader ------------------------------------------------------------


def test_an_opening_week_selection_reads_logs_and_team_defense_from_2026_27(world):
    logs, defense = world.hold_opening_week()

    snapshot = world.reader.for_season(NEW).snapshot(KEYS, season=ReadSeason(OLD))

    assert (snapshot.season, snapshot.mode, snapshot.published_season) == (
        NEW,
        SeasonMode.UNPUBLISHED,
        OLD,
    )
    assert _reads(snapshot) == {
        PLAYER_LOGS: ("candidate", NEW, None, logs.publication_id),
        # One game a team is all team defense waits for.
        SEASON_STREAM: ("candidate", NEW, None, defense.publication_id),
        # No 2026-27 Last 15 composed yet: it keeps 2025-26 until one serves.
        L15_STREAM: ("prior_season", OLD, None, _live(world, L15_STREAM)),
    }


def test_an_opening_week_selection_with_no_team_defense_composition_keeps_2025_26(world):
    world.hold_opening_week(defense=False)

    snapshot = world.reader.for_season(NEW).snapshot(KEYS, season=ReadSeason(OLD))

    assert _reads(snapshot)[SEASON_STREAM] == ("prior_season", OLD, None, _live(world, SEASON_STREAM))
    assert world.games.list_seasons()["choices"][0]["families"]["team_defense"] == {
        "served_season": OLD,
        "state": "trailing",
        "reason": "season_not_composed",
    }


def test_a_selection_serves_a_family_before_its_readiness_rule_holds(world, monkeypatch):
    # A reader who chose 2026-27 judges its one game a team themselves
    # (crf04/statsplus#386); the rule still decides what the default serves.
    team_defense_waits_for_ten_games(monkeypatch)
    _logs, defense = world.hold_opening_week()

    selected = world.reader.for_season(NEW).snapshot(KEYS, season=ReadSeason(OLD))
    choice = world.games.list_seasons()["choices"][0]

    assert _reads(selected)[SEASON_STREAM] == ("candidate", NEW, None, defense.publication_id)
    assert trailing(selected.reads.values()) == [
        {"family": "last_15", "serving_season": OLD, "rule": "its first window of the season is composed"},
    ]
    assert (choice["families"]["team_defense"], choice["team_games"]) == (
        {"served_season": NEW, "state": "serving"},
        {"min": 1, "max": 1},
    )
    assert _reads(world.reader.snapshot(KEYS, season=ReadSeason(OLD)))[SEASON_STREAM] == (
        "active",
        OLD,
        None,
        _live(world, SEASON_STREAM),
    )


# --- play-type defense follows team defense (crf04/statsplus-backend#610) ---

PLAY_TYPE_KEYS = (*KEYS, PLAY_TYPE_DEFENSE, PLAYER_SYNERGY)
SYNERGY_RULE = "NBA has published the season and every team has played 10 games"


def _play_type_selection(world):
    snapshot = world.reader.for_season(NEW).snapshot(PLAY_TYPE_KEYS, season=ReadSeason(OLD))
    return (
        _reads(snapshot),
        trailing(snapshot.reads.values()),
        world.games.list_seasons()["choices"][0]["families"],
    )


def test_an_opening_week_selection_serves_play_type_defense_from_2026_27_and_player_synergy_from_2025_26(
    world,
):
    world.publish_play_types()
    world.hold_opening_week()
    defense = world.compose_new_play_types(
        PLAY_TYPE_DEFENSE, THIN_GAMES, cutoff=THIN_CUTOFF, provenance=THIN_PROVENANCE
    )

    reads, trailing_families, families = _play_type_selection(world)

    assert reads[PLAY_TYPE_DEFENSE] == ("candidate", NEW, None, defense.publication_id)
    assert reads[PLAYER_SYNERGY] == ("prior_season", OLD, None, _live(world, PLAYER_SYNERGY))
    assert [entry for entry in trailing_families if entry["family"] != "last_15"] == [
        {"family": "synergy", "serving_season": OLD, "rule": SYNERGY_RULE},
    ]
    assert (families["team_defense"], families["synergy"]) == (
        {"served_season": NEW, "state": "serving"},
        {"served_season": OLD, "state": "trailing", "reason": "upstream_missing"},
    )


def test_an_opening_week_selection_with_no_play_type_defense_composition_trails_team_defense(world):
    world.publish_play_types()
    world.hold_opening_week()

    reads, trailing_families, families = _play_type_selection(world)

    assert reads[PLAY_TYPE_DEFENSE] == ("prior_season", OLD, None, _live(world, PLAY_TYPE_DEFENSE))
    assert [entry for entry in trailing_families if entry["family"] != "last_15"] == [
        {"family": "team_defense", "serving_season": OLD, "rule": "every team has played a game"},
        {"family": "synergy", "serving_season": OLD, "rule": SYNERGY_RULE},
    ]
    assert families["team_defense"] == {
        "served_season": OLD,
        "state": "trailing",
        "reason": "season_not_composed",
    }


def test_a_selection_serves_player_synergy_before_ten_games_a_team(world):
    world.publish_play_types()
    world.hold_opening_week()
    synergy = world.compose_new_play_types(
        PLAYER_SYNERGY, THIN_GAMES, cutoff=THIN_CUTOFF, provenance=THIN_PROVENANCE
    )

    reads, trailing_families, families = _play_type_selection(world)

    assert reads[PLAYER_SYNERGY] == ("candidate", NEW, None, synergy.publication_id)
    assert [entry["family"] for entry in trailing_families] == ["team_defense", "last_15"]
    assert families["synergy"] == {"served_season": NEW, "state": "serving"}


def test_after_an_opening_week_cutover_play_type_defense_activates_and_player_synergy_is_held(world):
    world.publish_play_types()
    synergy_2025_26 = world.live(PLAYER_SYNERGY)
    world.opening_week_cutover()

    defense = world.compose_new_play_types(
        PLAY_TYPE_DEFENSE, THIN_GAMES, cutoff=THIN_CUTOFF, provenance=THIN_PROVENANCE
    )
    synergy = world.compose_new_play_types(
        PLAYER_SYNERGY, THIN_GAMES, cutoff=THIN_CUTOFF, provenance=THIN_PROVENANCE
    )

    assert (defense.status, synergy.status) == ("active", "candidate")
    assert (world.live(PLAY_TYPE_DEFENSE), world.live(PLAYER_SYNERGY)) == (
        defense.publication_id,
        synergy_2025_26,
    )


def test_ten_games_a_team_serve_the_team_defense_candidate(world):
    logs, defense = world.hold_ten_games()

    snapshot = world.reader.for_season(NEW).snapshot(KEYS, season=ReadSeason(OLD))

    assert _reads(snapshot)[SEASON_STREAM] == ("candidate", NEW, None, defense.publication_id)
    assert _reads(snapshot)[PLAYER_LOGS] == ("candidate", NEW, None, logs.publication_id)


def test_a_coupled_family_member_that_fails_its_checks_holds_the_whole_family_back(world):
    world.hold_ten_games()
    l15 = world.compose_new_l15(NEW_GAMES, cutoff=NEW_CUTOFF, provenance=NEW_PROVENANCE)
    with world.engine.begin() as connection:
        connection.execute(
            text("UPDATE publication_versions SET checksum = 'corrupt' WHERE publication_id = :p"),
            {"p": l15.publication_id},
        )

    reads = _reads(world.reader.for_season(NEW).snapshot(KEYS, season=ReadSeason(OLD)))
    families = world.games.list_seasons()["choices"][0]["families"]

    assert reads[SEASON_STREAM] == ("prior_season", OLD, None, _live(world, SEASON_STREAM))
    assert reads[L15_STREAM] == ("prior_season", OLD, None, _live(world, L15_STREAM))
    assert families["last_15"] == {"served_season": OLD, "state": "trailing", "reason": "window_not_ready"}


def test_game_logs_that_fail_their_checks_make_the_season_unavailable(world):
    logs, _defense = world.hold_ten_games()
    with world.engine.begin() as connection:
        connection.execute(
            text("UPDATE publication_versions SET checksum = 'corrupt' WHERE publication_id = :p"),
            {"p": logs.publication_id},
        )

    with pytest.raises(SeasonUnavailable) as refused:
        world.reader.for_season(NEW).snapshot(KEYS, season=ReadSeason(OLD))

    assert (refused.value.season, refused.value.published_season) == (NEW, OLD)


def test_without_a_game_log_candidate_the_selection_is_unavailable(world):
    with pytest.raises(SeasonUnavailable):
        world.reader.for_season(NEW).snapshot(KEYS, season=ReadSeason(OLD))


def test_a_season_beyond_the_next_is_unavailable(world):
    world.hold_ten_games()

    with pytest.raises(SeasonUnavailable) as refused:
        world.reader.for_season("2027-28").snapshot(KEYS, season=ReadSeason(OLD))

    assert refused.value.season == "2027-28"


def test_each_candidate_is_checked_once_per_publication(world):
    world.hold_ten_games()
    checked = []
    validate = world.publications.season_candidate_refusal

    def counting(session, stream, candidate, **kwargs):
        checked.append(candidate.publication_id)
        return validate(session, stream, candidate, **kwargs)

    world.publications.season_candidate_refusal = counting
    for _ in range(3):
        world.reader.for_season(NEW).snapshot(KEYS, season=ReadSeason(OLD))

    assert len(checked) == len(set(checked)) == 2


def test_a_ledger_correction_withdraws_a_candidate_already_served(world):
    world.hold_opening_week()
    assert [row["PTS"] for row in _logs(world, NEW, selected=True)["game_logs"]] == [12]

    # The ledger accepts corrected observations of the candidate's games; recomposition has not run yet.
    accept_ledger(world.engine, THIN_GAMES, "pbp")

    with pytest.raises(SeasonUnavailable):
        _logs(world, NEW, selected=True)


def test_a_candidate_on_games_the_ledger_since_corrected_trails_beside_one_on_its_current_games(world):
    world.hold_opening_week(defense=False)
    accept_ledger(world.engine, NEW_GAMES[len(THIN_GAMES) :], "pbp")
    world.compose_new_defense(
        NEW_GAMES,
        cutoff=NEW_CUTOFF,
        provenance={**THIN_PROVENANCE, **provenance(NEW_GAMES[len(THIN_GAMES) :])},
    )
    # The ledger then accepts newer observations of the opening week's games,
    # and the game logs are composed from them that same night.
    accept_ledger(world.engine, THIN_GAMES, "pbp")
    logs = world.publications.recompose_ledger(
        PLAYER_LOGS,
        season=NEW,
        cutoff=NEW_CUTOFF,
        payload=_log_payload(NEW, NEW_GAMES),
        provenance=NEW_PROVENANCE,
        reason="2026-27 ledger composition",
    )

    reads = _reads(world.reader.for_season(NEW).snapshot(KEYS, season=ReadSeason(OLD)))

    # One night, one manifest: only the games each candidate rests on differ.
    assert reads[PLAYER_LOGS] == ("candidate", NEW, None, logs.publication_id)
    assert reads[SEASON_STREAM] == ("prior_season", OLD, None, _live(world, SEASON_STREAM))


@pytest.mark.parametrize(
    "unaccept",
    [
        # The ledger no longer records the observation as its game's evidence.
        "DELETE FROM canonical_game_ledger_observation_evidence WHERE game_id = :game AND observation_id = :observation",
        # No manifest accepted the observation.
        "UPDATE collection_observations SET manifest_id = NULL WHERE observation_id = :observation",
    ],
    ids=["evidence_dropped", "no_manifest"],
)
def test_game_logs_on_a_game_the_ledger_never_accepted_make_the_season_unavailable(world, unaccept):
    world.hold_ten_games()
    game_id = NEW_GAMES[0][0]
    with world.engine.begin() as connection:
        connection.execute(text(unaccept), {"game": game_id, "observation": f"pbp:{game_id}"})

    with pytest.raises(SeasonUnavailable):
        world.reader.for_season(NEW).snapshot(KEYS, season=ReadSeason(OLD))


#: The observation the ledger accepted as the first game's evidence.
FIRST_GAME = NEW_GAMES[0][0]


@pytest.mark.parametrize(
    "values",
    [
        pytest.param({"provider": "nba_stats"}, id="provider"),
        pytest.param({"observation_type": "player_game_logs"}, id="observation_type"),
        pytest.param({"season": OLD}, id="season"),
        pytest.param({"cutoff": NEW_CUTOFF - timedelta(days=1)}, id="cutoff"),
        pytest.param({"schema_version": 2}, id="schema_version"),
        pytest.param(
            {"scope": json.dumps({"game_id": FIRST_GAME, "surface": "synergy_play_types"})},
            id="scope_surface",
        ),
        # The candidate records its game apart from the scope, which now names another.
        pytest.param(
            {"scope": json.dumps({"game_id": NEW_GAMES[1][0], "surface": "canonical_game_ledger"})},
            id="scope_game_id",
        ),
    ],
)
def test_evidence_that_no_longer_matches_its_manifest_withdraws_a_candidate_already_served(world, values):
    logs, _defense = world.hold_ten_games()
    selected = world.reader.for_season(NEW)
    assert _reads(selected.snapshot(KEYS, season=ReadSeason(OLD)))[PLAYER_LOGS] == (
        "candidate",
        NEW,
        None,
        logs.publication_id,
    )
    with world.engine.begin() as connection:
        connection.execute(
            CollectionObservation.__table__.update()
            .where(CollectionObservation.observation_id == f"pbp:{FIRST_GAME}")
            .values(**values)
        )

    # The candidate's once-per-publication checks already passed; its lineage
    # is judged again on this read.
    with pytest.raises(SeasonUnavailable):
        selected.snapshot(KEYS, season=ReadSeason(OLD))


def test_evidence_that_loses_its_recorded_game_withdraws_a_candidate_already_served(world):
    logs, _defense = world.hold_ten_games()
    selected = world.reader.for_season(NEW)
    assert _reads(selected.snapshot(KEYS, season=ReadSeason(OLD)))[PLAYER_LOGS][3] == logs.publication_id
    with world.engine.begin() as connection:
        connection.execute(
            CollectionObservation.__table__.update()
            .where(CollectionObservation.observation_id == f"pbp:{FIRST_GAME}")
            .values(scope=json.dumps({"surface": "canonical_game_ledger"}))
        )
        connection.execute(
            text(
                "UPDATE publication_observations SET slice_key = NULL"
                " WHERE publication_id = :publication AND observation_id = :observation"
            ),
            {"publication": logs.publication_id, "observation": f"pbp:{FIRST_GAME}"},
        )

    with pytest.raises(SeasonUnavailable):
        selected.snapshot(KEYS, season=ReadSeason(OLD))


def test_candidates_on_the_same_games_under_different_manifests_are_judged_apart(world):
    logs, defense = world.hold_ten_games()
    # A second manifest of the same night whose Event Catalog, checksummed as
    # a real one is, swaps the first game's home and away teams.  The defense
    # candidate is bound to it.
    with world.engine.begin() as connection:
        manifest = dict(
            connection.execute(
                select(CollectionManifest.__table__).where(CollectionManifest.manifest_id == logs.manifest_id)
            )
            .mappings()
            .one()
        )
        catalog = dict(
            connection.execute(
                select(CatalogPublication.__table__).where(
                    CatalogPublication.publication_id == manifest["event_catalog_publication_id"]
                )
            )
            .mappings()
            .one()
        )
        document = json.loads(catalog["payload"])
        event = next(event for event in document["events"] if event["nba_game_id"] == FIRST_GAME)
        event["home_team_id"], event["away_team_id"] = event["away_team_id"], event["home_team_id"]
        catalog["publication_id"] = "catalog-swapped"
        catalog["payload"] = json.dumps(document, sort_keys=True, separators=(",", ":"))
        catalog["checksum"] = hashlib.sha256(catalog["payload"].encode()).hexdigest()
        connection.execute(CatalogPublication.__table__.insert().values(**catalog))
        manifest.update(
            manifest_id="manifest-swapped",
            checksum="manifest-swapped",
            event_catalog_publication_id=catalog["publication_id"],
            event_catalog_checksum=catalog["checksum"],
        )
        connection.execute(CollectionManifest.__table__.insert().values(**manifest))
        connection.execute(
            PublicationVersion.__table__.update()
            .where(PublicationVersion.publication_id == defense.publication_id)
            .values(
                manifest_id=manifest["manifest_id"],
                event_catalog_publication_id=catalog["publication_id"],
                event_catalog_checksum=catalog["checksum"],
            )
        )

    reads = _reads(world.reader.for_season(NEW).snapshot(KEYS, season=ReadSeason(OLD)))

    # Same season, cutoff and games: only the manifest tells the lineages apart.
    assert reads[PLAYER_LOGS] == ("candidate", NEW, None, logs.publication_id)
    assert reads[SEASON_STREAM] == ("prior_season", OLD, None, _live(world, SEASON_STREAM))


@pytest.mark.parametrize(
    "edit",
    [
        pytest.param(lambda payload: payload + " ", id="payload_changed"),
        pytest.param(lambda payload: "{not json", id="payload_malformed"),
    ],
)
def test_an_event_catalog_changed_under_its_checksum_withdraws_a_candidate_already_served(world, edit):
    logs, _defense = world.hold_ten_games()
    selected = world.reader.for_season(NEW)
    assert _reads(selected.snapshot(KEYS, season=ReadSeason(OLD)))[PLAYER_LOGS][3] == logs.publication_id
    with world.engine.begin() as connection:
        catalog = CatalogPublication.__table__
        where = catalog.c.publication_id == logs.event_catalog_publication_id
        payload = connection.scalar(select(catalog.c.payload).where(where))
        connection.execute(catalog.update().where(where).values(payload=edit(payload)))

    # The checksum is verified before the payload is parsed.
    with pytest.raises(SeasonUnavailable):
        selected.snapshot(KEYS, season=ReadSeason(OLD))


def test_an_unscoped_reader_never_serves_a_candidate(world):
    world.hold_ten_games()

    assert world.reader.for_season(None) is world.reader
    snapshot = world.reader.snapshot(KEYS, season=ReadSeason(OLD))
    explicit = world.reader.snapshot(KEYS, season=ReadSeason(OLD, requested=NEW))

    assert (snapshot.season, snapshot.mode) == (OLD, SeasonMode.PUBLISHED)
    assert {read.status for read in snapshot.reads.values()} == {"active"}
    # A season string from an internal caller (injury scenarios) reads as today.
    assert _reads(explicit)[PLAYER_LOGS] == ("missing", OLD, "window_not_ready", _live(world, PLAYER_LOGS))


# --- game logs ---------------------------------------------------------------


def test_a_selected_2026_27_reads_its_game_logs_and_names_the_published_season(world):
    logs, _defense = world.hold_opening_week()

    body = _logs(world, NEW, selected=True)

    assert [(row["GAME_DATE"], row["MATCHUP"], row["PTS"]) for row in body["game_logs"]] == [
        ("2026-10-24", "ATL vs. BOS", 12)
    ]
    assert body["season_game_count"] == 1
    assert body["season_context"] == {"selected_season": NEW, "published_season": OLD}
    generation = {entry["stream_key"]: entry for entry in body["provenance"]["generation"]}
    assert (generation[PLAYER_LOGS]["publication_id"], generation[PLAYER_LOGS]["status"]) == (
        logs.publication_id,
        "candidate",
    )


def test_a_selected_published_season_reads_as_before_with_its_context(world):
    world.hold_opening_week()

    selected = _logs(world, OLD, selected=True)
    default = _logs(world, OLD, selected=False)

    assert selected.pop("season_context") == {"selected_season": OLD, "published_season": OLD}
    # 2025-26 is published: nothing it reads trails.
    assert selected.pop("trailing") == []
    assert selected == default
    assert "season_context" not in default


def test_an_unselected_2026_27_read_stays_empty(world):
    """Injury scenarios read the schedule season's logs; held candidates must not leak in."""

    world.hold_opening_week()

    body = _logs(world, NEW, selected=False)

    assert body["game_logs"] == []
    assert "season_context" not in body


# --- /api/players (crf04/statsplus#512) -------------------------------------


class _RookieCatalog:
    """``Player 0`` played 2025-26, renamed in 2026-27; ``Rookie`` joins him then."""

    def get_catalog(self, season, *, active_only=False, player_ids=None):
        if season != NEW:
            return [{"season": season, "player_id": PLAYER_ID, "display_name": PLAYER_NAME}]
        rows = [(PLAYER_ID, "Player Zero"), (PLAYER_ID + 1, "Rookie")]
        return [{"season": season, "player_id": player_id, "display_name": name} for player_id, name in rows]


def _player_list(world, runtime=None):
    return PlayerService(
        world.engine,
        profile_reader=PlayerProfileReader(_RookieCatalog(), world.service.player_diets),
        settings=runtime or settings(),
        publication_reader=world.reader,
    ).get_all_players()


def test_the_player_list_adds_2026_27_players_only_while_2026_27_is_offered(world):
    world.publish_play_types()

    assert _player_list(world) == [PLAYER_NAME]

    world.hold_opening_week()
    world.compose_new_play_types(PLAYER_SYNERGY, THIN_GAMES, cutoff=THIN_CUTOFF, provenance=THIN_PROVENANCE)

    assert _player_list(world) == [PLAYER_NAME, "Rookie"]


def test_a_pinned_player_list_reads_only_the_pinned_season(world):
    world.publish_play_types()
    world.hold_opening_week()
    world.compose_new_play_types(PLAYER_SYNERGY, THIN_GAMES, cutoff=THIN_CUTOFF, provenance=THIN_PROVENANCE)
    unpinned = settings()
    pinned = unpinned.model_copy(update={"nba": NBASeasonSettings(current_season=OLD)})

    assert _player_list(world, pinned) == [PLAYER_NAME]


# --- /api/games/seasons -----------------------------------------------------


def test_choices_list_2026_27_only_while_its_game_logs_serve(world):
    assert world.games.list_seasons() == {
        "seasons": [OLD],
        "default_season": OLD,
        "choices": [
            {
                "season": OLD,
                "mode": "published",
                "families": {
                    "game_logs": {"served_season": OLD, "state": "serving"},
                    "player_diets": {"served_season": OLD, "state": "serving"},
                    "team_defense": {"served_season": OLD, "state": "serving"},
                    "last_15": {"served_season": OLD, "state": "serving"},
                },
            }
        ],
    }

    world.hold_opening_week()

    [unpublished, published] = world.games.list_seasons()["choices"]
    assert unpublished == {
        "season": NEW,
        "mode": "unpublished",
        "families": {
            "game_logs": {"served_season": NEW, "state": "serving"},
            # The Diet has no 2026-27 composition; team defense has one with a game a team.
            "player_diets": {"served_season": OLD, "state": "trailing", "reason": "season_not_composed"},
            "team_defense": {"served_season": NEW, "state": "serving"},
            "last_15": {"served_season": OLD, "state": "trailing", "reason": "season_not_composed"},
        },
        "team_games": {"min": 1, "max": 1},
    }
    assert published["season"] == OLD


# --- the HTTP boundary -------------------------------------------------------


@pytest.fixture
def served(world, dependencies):
    dependencies.game_service = world.games
    return world


def _get_logs(client, season, headers=None):
    return client.get(f"/api/games/game_logs?player_name=Player+0&season_filter={season}", headers=headers)


def test_the_route_serves_a_selected_2026_27_with_its_context(served, client, authenticate):
    served.hold_opening_week()

    response = _get_logs(client, NEW, authenticate())

    assert response.status_code == 200
    body = response.get_json()
    assert [row["PTS"] for row in body["game_logs"]] == [12]
    assert body["season_context"] == {"selected_season": NEW, "published_season": OLD}


@pytest.mark.parametrize("waitlisted", [False, True], ids=["anonymous", "waitlisted"])
def test_a_caller_without_an_admitted_account_reads_the_offered_unpublished_season(
    served, client, authenticate, make_db_user, waitlisted
):
    served.hold_opening_week()
    headers = authenticate(db_user=make_db_user(access_status="waitlisted")) if waitlisted else None

    response = _get_logs(client, NEW, headers)

    assert response.status_code == 200
    body = response.get_json()
    assert [row["PTS"] for row in body["game_logs"]] == [12]
    assert body["season_context"] == {"selected_season": NEW, "published_season": OLD}


def test_the_route_refuses_an_unoffered_season_naming_the_published_one(served, client):
    response = _get_logs(client, NEW)

    assert response.status_code == 503
    error = response.get_json()["error"]
    assert (error["code"], error["details"]) == (
        "season_unavailable",
        {"selected_season": NEW, "published_season": OLD},
    )


def test_the_route_rejects_a_malformed_season(served, client):
    response = _get_logs(client, "2026-28")

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_input"


def test_an_admitted_caller_without_a_season_reads_the_published_one_without_context(
    served, client, authenticate
):
    served.hold_opening_week()

    body = client.get("/api/games/game_logs?player_name=Player+0", headers=authenticate()).get_json()

    assert [row["PTS"] for row in body["game_logs"]] == [30]
    assert "season_context" not in body


@pytest.mark.parametrize("waitlisted", [False, True], ids=["anonymous", "waitlisted"])
def test_a_link_naming_no_season_opens_a_reader_without_an_account_on_the_page_season(
    served, client, authenticate, make_db_user, waitlisted
):
    served.hold_opening_week()
    headers = authenticate(db_user=make_db_user(access_status="waitlisted")) if waitlisted else None

    body = client.get("/api/games/game_logs?player_name=Player+0", headers=headers).get_json()

    assert [row["PTS"] for row in body["game_logs"]] == [12]
    assert body["season_context"] == {"selected_season": NEW, "published_season": OLD}


def test_a_link_naming_no_season_reads_the_published_one_while_the_page_season_serves_no_game_logs(
    served, client
):
    body = client.get("/api/games/game_logs?player_name=Player+0").get_json()

    assert [row["PTS"] for row in body["game_logs"]] == [30]
    assert "season_context" not in body


def test_a_link_naming_no_season_reads_the_published_one_before_the_next_season_begins(
    served, client, monkeypatch
):
    served.hold_opening_week()
    monkeypatch.setattr("app.routes.game_routes._slate_today", lambda: date(2026, 9, 30))

    body = client.get("/api/games/game_logs?player_name=Player+0").get_json()

    assert [row["PTS"] for row in body["game_logs"]] == [30]


def _preview_values(client, dependencies, query):
    dependencies.redis_client = FakeRedis()
    response = client.get(f"/api/games/game_logs/preview.png?player_name=Player+0{query}")
    assert (response.status_code, response.mimetype) == (200, "image/png")
    [facts] = [
        json.loads(value)
        for key, value in dependencies.redis_client.values.items()
        if key.startswith("game_log_preview:v")
    ]
    return facts["values"]


@pytest.mark.parametrize("query", ["&season_filter=2026-27", ""], ids=["named", "omitted"])
def test_a_preview_draws_the_offered_2026_27(served, client, dependencies, query):
    served.hold_opening_week()

    assert _preview_values(client, dependencies, query) == [12.0]


def test_a_preview_naming_no_season_draws_the_published_one_while_2026_27_serves_no_game_logs(
    served, client, dependencies
):
    assert _preview_values(client, dependencies, "") == [30.0]


def test_a_preview_of_the_published_season_still_draws_its_games(served, client, dependencies):
    served.hold_opening_week()

    assert _preview_values(client, dependencies, f"&season_filter={OLD}") == [30.0]


def test_a_preview_naming_no_season_redraws_once_2026_27_serves(served, client, dependencies):
    dependencies.redis_client = FakeRedis()
    path = "/api/games/game_logs/preview.png?player_name=Player+0&line_date=2026-10-26"
    client.get(path)

    served.hold_opening_week()
    client.get(path)

    values = [
        json.loads(value)["values"]
        for key, value in dependencies.redis_client.values.items()
        if key.startswith("game_log_preview:v")
    ]
    assert sorted(values) == [[12.0], [30.0]]


def test_a_cached_preview_of_a_season_no_longer_offered_is_refused(served, client, dependencies):
    served.hold_opening_week()
    dependencies.redis_client = FakeRedis()
    path = "/api/games/game_logs/preview.png?player_name=Player+0&season_filter=2026-27&line_date=2026-10-26"
    assert client.get(path).status_code == 200

    accept_ledger(served.engine, THIN_GAMES, "pbp")

    response = client.get(path)
    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "season_unavailable"


@pytest.mark.parametrize("pin", [OLD, NEW])
def test_a_pinned_deployment_reads_a_link_naming_no_season_as_before(served, client, pin):
    served.hold_opening_week()
    settings = served.games.settings
    served.games.settings = settings.model_copy(
        update={"nba": settings.nba.model_copy(update={"current_season": pin})}
    )
    body = client.get("/api/games/game_logs?player_name=Player+0").get_json()

    assert "season_context" not in body
    assert [row["PTS"] for row in body["game_logs"]] == ([30] if pin == OLD else [])


def test_a_preview_of_an_unoffered_season_is_refused_and_not_cached(served, client, dependencies):
    dependencies.redis_client = FakeRedis()

    response = client.get("/api/games/game_logs/preview.png?player_name=Player+0&season_filter=2026-27")

    assert response.status_code == 503
    error = response.get_json()["error"]
    assert (error["code"], error["details"]) == (
        "season_unavailable",
        {"selected_season": NEW, "published_season": OLD},
    )
    assert "max-age" not in response.headers.get("Cache-Control", "")
    assert not [key for key in dependencies.redis_client.values if key.startswith("game_log_preview:v")]


def test_team_games_are_the_fewest_and_most_games_any_team_has(world):
    logs, _defense = world.hold_ten_games()
    with world.engine.begin() as connection:
        # Every row of the first game: its two teams now show nine games.
        connection.execute(
            text("DELETE FROM publication_player_game_logs WHERE publication_id = :p AND game_id = :g"),
            {"p": logs.publication_id, "g": NEW_GAMES[0][0]},
        )

    [unpublished, _published] = world.games.list_seasons()["choices"]

    assert unpublished["team_games"] == {"min": 9, "max": 10}
