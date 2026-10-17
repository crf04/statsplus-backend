"""Slate, ladder and game-log HTTP adapters.

The slate, slate-edges and ladder reads are anonymous (crf04/statsplus#211):
they carry no auth decorator, so an ``Authorization`` header is ignored and
the rate limiter counts the caller by IP. Every other route here requires
authentication.

The slate route delegates one optional ET date to the persisted-schedule
service. The game-log route parses query parameters into one typed
:class:`GameLogQuery` and calls :meth:`GameService.get_filtered_logs`: Flask
serves requests with worker threads, so no event loop is created per request
(#10).  Malformed filters raise a 400 ``invalid_input`` response; provider
timeouts keep the documented 503 ``provider_unavailable`` contract.
"""

import re
import threading
from typing import Any

import requests
from flask import Blueprint, current_app, jsonify, request
from pydantic import ValidationError
from werkzeug.datastructures import MultiDict
from sqlalchemy.exc import SQLAlchemyError

from ..errors import (
    InvalidInputError,
    ProviderUnavailableError,
    ResourceNotFoundError,
    redact_public_value,
    sanitize_public_value,
)
from ..models.game_logs import GameLogFilterError, GameLogQuery
from ..services.nba_stats_adapter import validate_canonical_season
from ..utils.auth import require_auth
from ._service_proxy import CurrentAppService


# Initialize blueprint and services
game_bp = Blueprint('games', __name__)
#: Matchups with no Slate game (crf04/statsplus#95), served at
#: ``/api/matchups``; they share this module's Matchup parameter rules.
matchups_bp = Blueprint("matchups", __name__)
#: The stored official injury report (crf04/statsplus#220), served at
#: ``/api/injuries``.
injuries_bp = Blueprint("injuries", __name__)
#: Injury Scenarios built from the report, game logs and on/off splits
#: (crf04/statsplus#220), served at ``/api/injury_scenarios``.
injury_scenarios_bp = Blueprint("injury_scenarios", __name__)


game_service = CurrentAppService("game")
slate_service = CurrentAppService("slate")
slate_edges_service = CurrentAppService("slate_edges")
matchup_service = CurrentAppService("matchup")
matchup_selection_service = CurrentAppService("matchup_selection")
injuries_service = CurrentAppService("injury_listing")
rotation_service = CurrentAppService("rotation")
injury_scenarios_service = CurrentAppService("injury_scenario")

_MATCHUP_PARAMETERS = frozenset({"game_id"})
_SELECTION_PARAMETERS = frozenset({"game_id", "player_id"})
_CANONICAL_PLAYER_ID = re.compile(r"[1-9][0-9]*\Z")
_MAX_CANONICAL_PLAYER_ID = (1 << 63) - 1
_UNSCHEDULED_SELECTORS = ("player_id", "player_name", "team")
_UNSCHEDULED_PARAMETERS = frozenset(
    {*_UNSCHEDULED_SELECTORS, "player_team", "opponent"}
)
_TRICODE = re.compile(r"[A-Za-z]{3}\Z")
_ROTATION_SELECTORS = ("player_name", "team", "game_id")
_ROTATION_PARAMETERS = frozenset(
    {*_ROTATION_SELECTORS, "player_team", "last_n", "season"}
)
_ROTATION_DEFAULT_LAST_N = 5
_ROTATION_MAX_LAST_N = 20
_LAST_N = re.compile(r"[0-9]{1,2}\Z")
_NBA_GAME_ID = re.compile(r"[0-9]{10}\Z")


def _is_canonical_player_id(value: str) -> bool:
    return (
        _CANONICAL_PLAYER_ID.fullmatch(value) is not None
        and len(value) <= 19
        and int(value) <= _MAX_CANONICAL_PLAYER_ID
    )


@game_bp.route('/slate', methods=['GET'])
def get_slate():
    """Return the persisted current-season slate for one ET calendar date."""
    return jsonify(
        slate_service.get_slate(
            request.args.get("date"), fallback=request.args.get("fallback")
        )
    )


@game_bp.route("/slate/edges", methods=["GET"])
def get_slate_edges():
    """Return each slate game's biggest points effects and the slate's picks."""
    return jsonify(
        slate_edges_service.get_edges(
            request.args.get("date"), fallback=request.args.get("fallback")
        )
    )


@injuries_bp.route("", methods=["GET"], strict_slashes=False)
@require_auth
def get_injuries():
    """List the stored official injury report for one ET date, or one team."""
    unknown = sorted(set(request.args) - {"date", "team"})
    if unknown:
        raise InvalidInputError(
            "The injuries parameters are invalid.",
            public_details={
                "parameters": [sanitize_public_value(name) for name in unknown]
            },
        )
    return jsonify(
        injuries_service.list_injuries(
            request.args.get("date"), request.args.get("team")
        )
    )


@injury_scenarios_bp.route("", methods=["GET"], strict_slashes=False)
@require_auth
def get_injury_scenarios():
    """Scenarios for each Questionable, Doubtful or Out player of one ET date."""
    unknown = sorted(set(request.args) - {"date", "game_id", "player"})
    if unknown:
        raise InvalidInputError(
            "The injury scenarios parameters are invalid.",
            public_details={
                "parameters": [sanitize_public_value(name) for name in unknown]
            },
        )
    return jsonify(
        injury_scenarios_service.scenarios(
            request.args.get("date"),
            game_id=request.args.get("game_id"),
            player=request.args.get("player"),
        )
    )


@game_bp.route("/matchup", methods=["GET"])
@require_auth
def get_matchup():
    """Return one stored matchup document for a canonical NBA game."""
    game_ids = request.args.getlist("game_id")
    if (
        set(request.args) != _MATCHUP_PARAMETERS
        or len(game_ids) != 1
        or not game_ids[0]
        or game_ids[0] != game_ids[0].strip()
    ):
        raise InvalidInputError("The matchup parameters are invalid.")
    return jsonify(matchup_service.get_matchup(game_id=game_ids[0]))


@game_bp.route("/matchup/selection", methods=["GET"])
@require_auth
def get_matchup_selection():
    """Return the stored-log tables for one canonical matchup selection."""
    if set(request.args) != _SELECTION_PARAMETERS:
        raise InvalidInputError("The matchup selection parameters are invalid.")
    game_ids = request.args.getlist("game_id")
    player_ids = request.args.getlist("player_id")
    if (
        len(game_ids) != 1
        or not game_ids[0]
        or game_ids[0] != game_ids[0].strip()
        or len(player_ids) != 1
        or not _is_canonical_player_id(player_ids[0])
    ):
        raise InvalidInputError("The matchup selection parameters are invalid.")
    return jsonify(
        matchup_selection_service.get_selection(
            game_id=game_ids[0],
            player_id=int(player_ids[0]),
        )
    )


def _invalid_unscheduled(*parameters: str) -> InvalidInputError:
    names = ", ".join(parameters)
    return InvalidInputError(
        f"The unscheduled matchup parameters are invalid: {names}.",
        public_details={"parameters": list(parameters)},
    )


@matchups_bp.route("/unscheduled", methods=["GET"])
@require_auth
def get_unscheduled_matchup():
    """Score a player, or a team's players, against one opponent, no game."""
    unknown = sorted(set(request.args) - _UNSCHEDULED_PARAMETERS)
    if unknown:
        # Only names from the documented vocabulary are echoed back.
        raise _invalid_unscheduled(*(sanitize_public_value(name) for name in unknown))
    selectors = [name for name in _UNSCHEDULED_SELECTORS if name in request.args]
    if len(selectors) != 1:
        raise _invalid_unscheduled(*(selectors or _UNSCHEDULED_SELECTORS))
    (selector,) = selectors
    if "player_team" in request.args and selector != "player_name":
        # player_team only narrows a name; an id or a team needs no narrowing.
        raise _invalid_unscheduled("player_team")
    values = {
        name: request.args.getlist(name)
        for name in (selector, "opponent", "player_team")
        if name in request.args or name != "player_team"
    }
    for name, submitted in values.items():
        if len(submitted) != 1:
            raise _invalid_unscheduled(name)
    value = values[selector][0]
    opponent = values["opponent"][0]
    if _TRICODE.fullmatch(opponent) is None:
        raise _invalid_unscheduled("opponent")
    opponent = opponent.upper()
    selected: dict[str, Any] = {
        "player_id": None,
        "player_name": None,
        "player_team": None,
        "team": None,
    }
    if selector == "player_id":
        if not _is_canonical_player_id(value):
            raise _invalid_unscheduled("player_id")
        selected["player_id"] = int(value)
    elif selector == "player_name":
        if not value.strip():
            raise _invalid_unscheduled("player_name")
        selected["player_name"] = value
        if "player_team" in values:
            player_team = values["player_team"][0]
            if _TRICODE.fullmatch(player_team) is None:
                raise _invalid_unscheduled("player_team")
            selected["player_team"] = player_team.upper()
    else:
        if _TRICODE.fullmatch(value) is None:
            raise _invalid_unscheduled("team")
        selected["team"] = value.upper()
        if selected["team"] == opponent:
            raise _invalid_unscheduled("opponent")
    return jsonify(
        matchup_service.get_unscheduled_matchup(opponent=opponent, **selected)
    )


def _default_season() -> str:
    """The season used when a request omits ``season_filter``."""
    return game_service.default_season()


def _parse_game_log_filters() -> tuple[str, GameLogQuery]:
    """Parse the request's query parameters into one :class:`GameLogQuery`."""

    return _game_log_query_from(request.args)


def _game_log_query_from(args: Any) -> tuple[str, GameLogQuery]:
    """Parse game-log parameters (a ``MultiDict``) into a typed query."""

    player_name = args.get("player_name")
    if not player_name:
        raise InvalidInputError("player_name is required.")

    # Only an omitted season discovers the published default.
    season_filter = args.get("season_filter")
    filters = {
        "season_filter": (
            _default_season() if season_filter is None else season_filter
        ),
        "minutes_filter": args.get("minutes_filter", "0,48"),
        "players_on": args.getlist("players_on[]"),
        "players_off": args.getlist("players_off[]"),
        "date_filter": args.get("date_filter"),
        "date_to": args.get("date_to"),
        "line_date": args.get("line_date"),
        "teams_against": args.getlist("teams_against[]"),
        "rank_filter": args.getlist("rank_filter[]"),
        "opponent_tricode": args.get("opponent_tricode"),
        "location_filter": args.get("location_filter", "Both"),
        "game_filter": args.get("game_filter"),
        "playstyle_range": [
            args.get("playstyle_RTG_min", "0"),
            args.get("playstyle_RTG_max", "200"),
        ],
        # Keep repeated ``self_filters[STAT]`` parameters as ordered pairs;
        # collapsing them into a dict would silently drop same-stat bounds.
        "self_filters": [
            (key[len("self_filters[") : -1], value)
            for key, value in args.items(multi=True)
            if key.startswith("self_filters[") and key.endswith("]")
        ],
    }

    try:
        return player_name, GameLogQuery(**filters)
    except ValidationError as error:
        raise InvalidInputError(
            "One or more game log filters are invalid.",
            detail=error,
            public_details=_game_log_validation_details(error, filters, args),
        ) from error


#: Scalar parameters whose grammar is pydantic's own, which normalizes the
#: value before any check sees it. The name decides two things at the HTTP
#: seam, both publishing the original submitted string rather than the
#: normalized one:
#:
#: - a pydantic-native rejection of one of them, which no typed cause owns,
#:   is translated into facts naming the parameter and that string; and
#: - a typed ``GameLogFilterError`` naming one of them (``date_to`` earlier
#:   than ``date_filter``) reports that string instead of the model's
#:   normalized value.
#:
#: Acceptance is untouched either way.
_PYDANTIC_SCALAR_PARAMETER_NAMES = frozenset(
    {"date_filter", "date_to", "line_date", "location_filter", "game_filter"}
)


def _playstyle_range_failures(
    filters: dict[str, Any],
    args: Any,
) -> list[dict[str, str]]:
    """Facts for an inverted playstyle range, per submitted bound.

    The range spans two query parameters. Only the ones the caller actually
    submitted are named with the bound as it arrived, so a default the
    caller never sent is never blamed for a rejection.
    """

    low, high = filters["playstyle_range"]
    bounds = (
        ("playstyle_RTG_min", low),
        ("playstyle_RTG_max", high),
    )
    return [
        {
            "parameter": parameter,
            # The filters hold exactly the queried bound (the omitted one
            # replaced by its documented default), as submitted strings.
            "values": [sanitize_public_value(str(bound))],
        }
        for parameter, bound in bounds
        if args.getlist(parameter)
    ]


def _redacted_split_values(
    fragments: list[str],
    complete_values: list[str],
) -> list[str]:
    """Publish fragments that keep the sanitization context of the whole value.

    Validators report the bounds or parts of one submitted value
    individually, and splitting on ``,`` can break the context a credential
    pattern needs to match (a quoted value spanning the split). When a
    fragment came from a complete value the sanitizer redacts, the
    sanitized complete value is published instead; ordinary invalid
    fragments -- including long ones the sanitizer only truncates -- are
    untouched, because redaction and length bounding are compared apart.
    """

    published = []
    for fragment in fragments:
        complete = next(
            (
                candidate
                for candidate in complete_values
                if fragment in candidate
                and redact_public_value(candidate) != candidate
            ),
            None,
        )
        published.append(
            sanitize_public_value(complete)
            if complete is not None
            else sanitize_public_value(fragment)
        )
    return list(dict.fromkeys(published))


def _game_log_rejected_filter(
    cause: GameLogFilterError | None,
    validation_error: dict[str, Any],
    filters: dict[str, Any],
    args: Any,
) -> dict[str, Any] | list[dict[str, Any]] | None:
    """One rejected filter as published facts, or none if not safely known."""

    if isinstance(cause, GameLogFilterError):
        if cause.parameter == "playstyle_RTG_range":
            # Internal two-parameter marker, never published as a name:
            # report the bounds the caller actually submitted.
            return _playstyle_range_failures(filters, args)
        submitted = (
            filters.get(cause.parameter)
            if cause.parameter in _PYDANTIC_SCALAR_PARAMETER_NAMES
            else None
        )
        if isinstance(submitted, str):
            # A pydantic-parsed scalar (``date_to`` earlier than
            # ``date_filter``) reaches the model normalized: an epoch or
            # other lax spelling would be reported as a date the caller
            # never sent, so publish the value exactly as submitted.
            values = [sanitize_public_value(submitted)]
        else:
            values = _redacted_split_values(
                list(cause.values),
                [cause.context] if isinstance(cause.context, str) else [],
            )
        facts = {
            # Caller-supplied content can appear inside a parameter name
            # (a self_filter's stat), so it is redacted like the values.
            "parameter": sanitize_public_value(cause.parameter),
            "values": values,
        }
        if cause.supported_values is not None:
            facts["supported_values"] = list(cause.supported_values)
        if cause.supported_aliases is not None:
            facts["supported_aliases"] = list(cause.supported_aliases)
        return facts

    # A parser pydantic owns (``date_filter``, ``date_to``,
    # ``location_filter``) has no
    # typed cause, but the route holds exactly the value the caller
    # submitted and it is a scalar string, so publish that. Anything still
    # unknown is skipped, keeping the other rejected filters' facts.
    field = validation_error.get("loc", ())
    field = field[0] if field and isinstance(field[0], str) else None
    submitted = (
        filters.get(field)
        if field in _PYDANTIC_SCALAR_PARAMETER_NAMES
        else None
    )
    if isinstance(submitted, str):
        return {
            "parameter": field,
            "values": [sanitize_public_value(submitted)],
        }
    return None


def _game_log_validation_details(
    error: ValidationError,
    filters: dict[str, Any],
    args: Any,
) -> dict[str, Any] | None:
    """The failed game-log parameters a caller can act on, or none.

    Each rejection is translated separately: typed
    :class:`GameLogFilterError` causes name the parameter, the unusable
    submitted values, and where the service owns the vocabulary, the
    canonical accepted values. A failure with no safely actionable detail
    -- an unknown internal one -- is skipped rather than blanking the
    whole payload, and none of them publishes Pydantic context or input:
    only redacted, bounded scalars reach ``details``.
    """

    failed_filters = []
    for validation_error in error.errors():
        cause = (validation_error.get("ctx") or {}).get("error")
        facts = _game_log_rejected_filter(cause, validation_error, filters, args)
        if isinstance(facts, list):
            failed_filters.extend(facts)
        elif facts is not None:
            failed_filters.append(facts)
    if not failed_filters:
        return None
    return {"filters": failed_filters}


@game_bp.route("/seasons", methods=["GET"])
@require_auth
def get_game_log_seasons():
    """Return the seasons whose game logs are readable, newest first."""
    try:
        return jsonify(game_service.list_seasons())
    except SQLAlchemyError as error:
        raise ProviderUnavailableError(detail=error) from error


def _filtered_logs(player_name: str, query: GameLogQuery) -> dict[str, Any]:
    """Run one game-log query, mapping failures to the public error contract."""
    try:
        return game_service.get_filtered_logs(player_name, query)
    except requests.exceptions.Timeout as error:
        raise ProviderUnavailableError(
            "The upstream stats provider timed out. Please try again shortly.",
            detail=error,
        ) from error
    except requests.exceptions.RequestException as error:
        raise ProviderUnavailableError(detail=error) from error
    except ResourceNotFoundError:
        raise
    except ValueError as error:
        if "No matching player found" in str(error):
            raise ResourceNotFoundError(
                "The requested player was not found.", detail=error
            ) from error
        raise InvalidInputError("The game log request is invalid.", detail=error) from error


@game_bp.route('/game_logs', methods=['GET'])
@require_auth
def get_game_logs():
    player_name, query = _parse_game_log_filters()
    return jsonify(_filtered_logs(player_name, query))


def _invalid_rotation(*parameters: str) -> InvalidInputError:
    names = ", ".join(parameters)
    return InvalidInputError(
        f"The rotation parameters are invalid: {names}.",
        public_details={"parameters": list(parameters)},
    )


@game_bp.route("/rotation", methods=["GET"])
@require_auth
def get_rotation():
    """Starters, minutes and on-court stints for one player, one team, or one game."""
    unknown = sorted(set(request.args) - _ROTATION_PARAMETERS)
    if unknown:
        raise _invalid_rotation(*(sanitize_public_value(name) for name in unknown))
    selectors = [name for name in _ROTATION_SELECTORS if name in request.args]
    if len(selectors) != 1:
        raise _invalid_rotation(*(selectors or _ROTATION_SELECTORS))
    (selector,) = selectors
    if "player_team" in request.args and selector != "player_name":
        raise _invalid_rotation("player_team")
    for name in request.args:
        if len(request.args.getlist(name)) != 1:
            raise _invalid_rotation(name)
    if selector == "game_id":
        extras = [name for name in ("last_n", "season") if name in request.args]
        if extras:
            raise _invalid_rotation(*extras)
        if _NBA_GAME_ID.fullmatch(request.args["game_id"]) is None:
            raise _invalid_rotation("game_id")
        return jsonify(
            rotation_service.get_game_rotation(game_id=request.args["game_id"])
        )
    last_n = _ROTATION_DEFAULT_LAST_N
    if "last_n" in request.args:
        submitted = request.args["last_n"]
        if (
            _LAST_N.fullmatch(submitted) is None
            or not 1 <= int(submitted) <= _ROTATION_MAX_LAST_N
        ):
            raise _invalid_rotation("last_n")
        last_n = int(submitted)
    season = request.args.get("season")
    if season is not None:
        try:
            if validate_canonical_season(season) != season:
                raise ValueError(season)
        except ValueError:
            raise _invalid_rotation("season") from None
    value = request.args[selector]
    if selector == "team":
        if _TRICODE.fullmatch(value) is None:
            raise _invalid_rotation("team")
        return jsonify(
            rotation_service.get_team_rotation(
                team=value.upper(), last_n=last_n, season=season
            )
        )
    if not value.strip():
        raise _invalid_rotation("player_name")
    player_team = request.args.get("player_team")
    if player_team is not None:
        if _TRICODE.fullmatch(player_team) is None:
            raise _invalid_rotation("player_team")
        player_team = player_team.upper()
    return jsonify(
        rotation_service.get_player_rotation(
            player_name=value, player_team=player_team, last_n=last_n, season=season
        )
    )


#: The ladder answers this season and no other (crf04/statsplus#217): its
#: Filter Sets pin it, so the answer never follows the published default and
#: is computed once per process.
_LADDER_SEASON = "2025-26"
#: The backend-owned Filter Sets behind the home page ladder, in order. Each
#: is a set of ``/api/games/game_logs`` parameters (crf04/statsplus#211).
_LADDER_RUNGS = (
    ("start", (("player_name", "Jalen Johnson"), ("season_filter", _LADDER_SEASON))),
    (
        "narrow",
        (
            ("player_name", "Jalen Johnson"),
            ("players_off[]", "Trae Young"),
            ("season_filter", _LADDER_SEASON),
        ),
    ),
    (
        "stack",
        (
            ("player_name", "Jalen Johnson"),
            ("players_off[]", "Trae Young"),
            ("teams_against[]", "OPP_PTS"),
            ("rank_filter[]", "10"),
            ("minutes_filter", "25,48"),
            ("season_filter", _LADDER_SEASON),
        ),
    ),
)


class _LadderMemo:
    """The successful ladder answer of one application, computed at most once.

    A failure raises out of the computation and stores nothing, and so does an
    answer with an empty rung (a deployment that has not published the season
    yet), so only a complete answer is permanent.  One lock covers the
    computation, so concurrent first requests compute it once.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._answer: dict[str, Any] | None = None

    def get(self, compute) -> dict[str, Any]:
        with self._lock:
            if self._answer is None:
                answer = compute()
                if all(rung["games"] for rung in answer["rungs"]):
                    self._answer = answer
                return answer
            return self._answer


def _compute_ladder() -> dict[str, Any]:
    rungs = []
    for rung_id, parameters in _LADDER_RUNGS:
        player_name, query = _game_log_query_from(MultiDict(parameters))
        result = _filtered_logs(player_name, query)
        logs = result["game_logs"]
        averages = result["averages"]
        rungs.append(
            {
                "id": rung_id,
                "points": float(averages[0]["PTS"]) if logs and averages else None,
                "games": len(logs),
                "series": [log["PTS"] for log in logs],
            }
        )
    return {"rungs": rungs}


@game_bp.route("/ladder", methods=["GET"])
def get_ladder():
    """Return the points answer of each home page ladder rung, anonymously."""
    if request.args:
        raise InvalidInputError("The ladder takes no query parameters.")
    memo = current_app.extensions.setdefault("ladder_memo", _LadderMemo())
    return jsonify(memo.get(_compute_ladder))
