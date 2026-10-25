"""Readiness Rules: when each Matchup input family serves the Published Season.

Each family of Matchup inputs has its own Serving Season (crf04/statsplus#304,
ADR 0003).  A family whose rule names a game count keeps serving its previous
season's Publication after a Season Cutover, labelled ``prior_season``, until
every team has played that many games in the Published Season; its next
composition then activates.  A family with no count switches at the Season
Cutover, as every stream did before; its previous season is never served.
A stream in no family behaves the same way.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from app.domain.nba_teams import NBA_TEAM_ID_TO_TRICODE

#: The read status of a trailing family's previous-season Publication.
PRIOR_SEASON_STATUS = "prior_season"
#: Why a cutover or composition held a trailing family's candidate.
NOT_READY_REASON = "serving_season_not_ready"

_CANONICAL_TEAM_IDS = frozenset(NBA_TEAM_ID_TO_TRICODE)


@dataclass(frozen=True, slots=True)
class ReadinessRule:
    """One family's streams and when it takes the Published Season.

    ``min_team_games`` is the games every team must have played in the
    Published Season; ``None`` switches at the Season Cutover and never
    trails.  ``wording`` is the public ``rule`` a trailing entry names.
    """

    family: str
    streams: frozenset[str]
    min_team_games: int | None = None
    wording: str | None = None

    @property
    def trails(self) -> bool:
        return self.min_team_games is not None

    def holds(self, game_ids_by_team: Mapping[int, Collection[Any]]) -> bool:
        """Whether every canonical team has played ``min_team_games`` games.

        Set equality against the thirty canonical teams, as the Last 15
        readiness check uses, so a partial roster never passes.
        """

        if self.min_team_games is None:
            return True
        return set(game_ids_by_team) >= _CANONICAL_TEAM_IDS and all(
            len(game_ids_by_team[team_id]) >= self.min_team_games for team_id in _CANONICAL_TEAM_IDS
        )


READINESS_RULES: tuple[ReadinessRule, ...] = (
    ReadinessRule("game_logs", frozenset({"player_game_logs", "player_per36"})),
    ReadinessRule(
        "player_diets",
        frozenset(
            {
                "grouped_shot_types",
                "exact_shot_zones",
                "player_assist_locations",
                "player_rebound_types",
            }
        ),
        min_team_games=10,
        wording="every team has played 10 games",
    ),
    ReadinessRule(
        "team_defense",
        frozenset(
            {
                "traditional_opponent_season",
                "grouped_shot_types_opponent_season",
                "exact_shot_zones_opponent_season",
                "assist_locations_season",
                "rebound_types_season",
            }
        ),
        min_team_games=10,
        wording="every team has played 10 games",
    ),
    # A Synergy candidate exists only once NBA has published the season, so
    # the composition being gated is itself the first half of the rule.
    ReadinessRule(
        "synergy",
        frozenset({"synergy_play_types", "synergy_play_types_opponent_season"}),
        min_team_games=10,
        wording="NBA has published the season and every team has played 10 games",
    ),
    # Unchanged until crf04/statsplus-backend#548: a Last 15 window waits for
    # its first composition and reads ``window_not_ready`` until then.
    ReadinessRule(
        "last_15",
        frozenset(
            {
                "traditional_opponent_l15",
                "grouped_shot_types_opponent_l15",
                "exact_shot_zones_opponent_l15",
                "assist_locations_l15",
                "rebound_types_l15",
            }
        ),
    ),
)

_RULE_BY_STREAM: dict[str, ReadinessRule] = {
    stream_key: rule for rule in READINESS_RULES for stream_key in rule.streams
}


def readiness_rule(stream_key: str) -> ReadinessRule | None:
    return _RULE_BY_STREAM.get(stream_key)


def previous_season(season: str) -> str:
    """``2026-27`` -> ``2025-26``."""

    start = int(season[:4]) - 1
    return f"{start}-{(start + 1) % 100:02d}"


def next_season(season: str) -> str:
    """``2025-26`` -> ``2026-27``."""

    start = int(season[:4]) + 1
    return f"{start}-{(start + 1) % 100:02d}"


def serves_prior_season(stream_key: str, publication_season: str, season: str) -> bool:
    """Whether a read in ``season`` serves this earlier Publication as trailing."""

    rule = readiness_rule(stream_key)
    return rule is not None and rule.trails and publication_season == previous_season(season)


def trailing(reads: Iterable[Any]) -> list[dict[str, str]]:
    """The public ``trailing`` list for the reads one answer used.

    A family trails when any of its reads served its previous-season
    Publication; entries keep the Readiness Rule table's order.
    """

    seasons: dict[str, str] = {}
    for read in reads:
        rule = readiness_rule(getattr(read, "stream_key", ""))
        if rule is not None and getattr(read, "status", None) == PRIOR_SEASON_STATUS:
            seasons.setdefault(rule.family, str(read.season))
    return [
        {"family": rule.family, "serving_season": seasons[rule.family], "rule": str(rule.wording)}
        for rule in READINESS_RULES
        if rule.family in seasons
    ]


def merge_trailing(entries: Iterable[Iterable[Mapping[str, str]]]) -> list[dict[str, str]]:
    """One ``trailing`` list from several answers', in table order."""

    by_family: dict[str, dict[str, str]] = {}
    for listed in entries:
        for entry in listed:
            by_family.setdefault(entry["family"], dict(entry))
    return [by_family[rule.family] for rule in READINESS_RULES if rule.family in by_family]


__all__ = [
    "NOT_READY_REASON",
    "PRIOR_SEASON_STATUS",
    "READINESS_RULES",
    "ReadinessRule",
    "merge_trailing",
    "next_season",
    "previous_season",
    "readiness_rule",
    "serves_prior_season",
    "trailing",
]
