"""Aggregate observed player Synergy stints without inventing possessions."""

from collections import defaultdict
from collections.abc import Iterable, Mapping
from fractions import Fraction
from math import ceil, floor, isfinite

from app.domain.play_type_matchup import complete_play_type_shares
from app.domain.team_matchup_taxonomy import PLAY_TYPES

_FIELDS = frozenset(
    {
        "player_id",
        "team_id",
        "games_played",
        "possessions",
        "possession_share",
        "category",
    }
)
_HALF_ROUNDING_UNIT = Fraction(1, 2000)


def _unique_denominator(records: list[dict]) -> int | None:
    """Intersect inclusive rounding intervals; never choose among candidates."""
    lower = max(1, sum(record["possessions"] for record in records))
    upper = None
    for record in records:
        possessions = record["possessions"]
        share = Fraction(str(record["possession_share"]))
        low_share = max(Fraction(0), share - _HALF_ROUNDING_UNIT)
        high_share = min(Fraction(1), share + _HALF_ROUNDING_UNIT)
        if possessions == 0:
            if low_share > 0:
                return None
            continue
        lower = max(lower, ceil(possessions / high_share))
        if low_share > 0:
            bound = floor(possessions / low_share)
            upper = bound if upper is None else min(upper, bound)
    return lower if lower == upper else None


def aggregate_player_synergy(
    records: Iterable[Mapping],
) -> tuple[list[dict], dict[int, str]]:
    """Return Diet rows and player-level withholding reasons.

    Malformed records fail the source as a whole. Validly shaped but
    inconsistent player evidence is withheld as a whole. A single observed
    stint retains provider shares. Multiple stints require a uniquely proven
    integer possession denominator for every team, with inclusive ±0.0005
    bounds around the provider's three-decimal percentages. Omitted categories
    remain omitted; their possessions are never inferred.
    """
    players = defaultdict(lambda: defaultdict(list))
    seen = set()
    for record in records:
        if not isinstance(record, Mapping) or set(record) != _FIELDS:
            raise ValueError("invalid player Synergy record fields")
        normalized = dict(record)
        for field in ("player_id", "team_id", "games_played", "possessions"):
            value = record[field]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
                or value != int(value)
                or value < (0 if field == "possessions" else 1)
            ):
                raise ValueError(f"invalid player Synergy {field}")
            normalized[field] = int(value)
        share = record["possession_share"]
        if (
            isinstance(share, bool)
            or not isinstance(share, (int, float))
            or not isfinite(share)
            or not 0 <= share <= 1
        ):
            raise ValueError("invalid player Synergy possession_share")
        category = record["category"]
        if not isinstance(category, str) or category not in PLAY_TYPES:
            raise ValueError("invalid player Synergy category")
        key = (normalized["player_id"], normalized["team_id"], category)
        if key in seen:
            raise ValueError("duplicate player Synergy team/category")
        seen.add(key)
        players[key[0]][key[1]].append(normalized)

    rows = []
    withheld = {}
    for player_id, teams in sorted(players.items()):
        reason = None
        for team_records in teams.values():
            if any(
                record["possessions"] == 0
                and Fraction(str(record["possession_share"])) > _HALF_ROUNDING_UNIT
                for record in team_records
            ):
                reason = "zero possessions contradict the rounded share"
                break
            if len({record["games_played"] for record in team_records}) != 1:
                reason = "inconsistent team games played"
                break
            if (
                complete_play_type_shares(
                    (record["category"], record["possession_share"])
                    for record in team_records
                )
                is None
            ):
                reason = "invalid team play-type partition"
                break
        if reason:
            withheld[player_id] = reason
            continue
        totals = defaultdict(int)
        for team_records in teams.values():
            for record in team_records:
                totals[record["category"]] += record["possessions"]
        games = sum(team_records[0]["games_played"] for team_records in teams.values())
        if len(teams) == 1:
            shares = {
                record["category"]: record["possession_share"]
                for record in next(iter(teams.values()))
            }
        else:
            denominators = [_unique_denominator(stint) for stint in teams.values()]
            if any(value is None for value in denominators):
                withheld[player_id] = (
                    "team possession denominator is not uniquely proven"
                )
                continue
            denominator = sum(denominators)
            shares = {
                category: volume / denominator for category, volume in totals.items()
            }
        if complete_play_type_shares(shares.items()) is None:
            withheld[player_id] = "invalid player play-type partition"
            continue
        rows.extend(
            {
                "player_id": player_id,
                "slice_key": category,
                "share": shares[category],
                "volume": totals[category],
                "games_played": games,
                "volume_unit": "possessions",
                "provider": "nba_synergy",
            }
            for category in PLAY_TYPES
            if category in shares
        )
    return rows, withheld
