"""Compare a ``player_assist_locations`` candidate against legacy Diet facts.

Reads one inactive or active candidate publication for the stream (decoded
through the same seam every reader uses) and the legacy
``player_diet_facts`` rows for the ``assist_locations`` base, then reports
per-``(player_id, slice_key)`` agreement so an operator can adjudicate a
candidate before activation (statsplus#280 item 3).
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.domain.utc import assume_utc  # noqa: E402
from app.models.collection_control import PublicationVersion  # noqa: E402
from app.models.player_diet import PlayerDietFactRow  # noqa: E402
from app.services.database_first_activation import decode_player_diet  # noqa: E402


#: The governed stream and Diet base this script is scoped to.  Every other
#: Diet base already has an activated, ledger-independent parity path; this
#: script exists only for the ledger-composed assist-location stream.
STREAM_KEY = "player_assist_locations"
BASE = "assist_locations"

#: Maximum non-exact identities printed in the report body.  The counts
#: always cover the full compared set; this bound keeps the report readable
#: when a candidate disagrees broadly.
_MAX_REPORTED_DIFFERENCES = 50


@dataclass(frozen=True, slots=True)
class AssistDietValue:
    share: float
    volume: float
    games_played: int

    def as_payload(self) -> dict[str, float | int]:
        return {
            "share": self.share,
            "volume": self.volume,
            "games_played": self.games_played,
        }


@dataclass(frozen=True, slots=True)
class AssistDietIdentity:
    player_id: int
    slice_key: str
    status: str  # "exact" | "differs" | "candidate_only" | "legacy_only"
    candidate: AssistDietValue | None
    legacy: AssistDietValue | None


@dataclass(frozen=True, slots=True)
class AssistDietParityReport:
    exact_count: int
    differs_count: int
    candidate_only_count: int
    legacy_only_count: int
    identities: tuple[AssistDietIdentity, ...]

    @property
    def all_exact(self) -> bool:
        return not (
            self.differs_count or self.candidate_only_count or self.legacy_only_count
        )


def _field(row: object, name: str):
    if hasattr(row, name):
        return getattr(row, name)
    return row[name]  # type: ignore[index]


def _value_from(row: object) -> AssistDietValue:
    return AssistDietValue(
        share=float(_field(row, "share")),
        volume=float(_field(row, "volume")),
        games_played=int(_field(row, "games_played")),
    )


def compare_assist_diet(
    candidate_facts,
    legacy_rows,
    *,
    tolerance: float = 1e-6,
) -> AssistDietParityReport:
    """Compare candidate and legacy assist-location Diet facts by identity.

    Both arguments are iterables of rows exposing ``player_id``, ``slice_key``,
    ``share``, ``volume``, and ``games_played`` -- either by attribute (a
    decoded ``PlayerDietFact``) or by key (a mapping row).  An identity is
    ``exact`` when ``share`` matches within ``tolerance`` and ``volume`` and
    ``games_played`` match exactly; otherwise it is ``differs``,
    ``candidate_only``, or ``legacy_only``.
    """

    candidates = {
        (int(_field(row, "player_id")), str(_field(row, "slice_key"))): _value_from(row)
        for row in candidate_facts
    }
    legacy = {
        (int(_field(row, "player_id")), str(_field(row, "slice_key"))): _value_from(row)
        for row in legacy_rows
    }
    identities = []
    exact_count = differs_count = candidate_only_count = legacy_only_count = 0
    for key in sorted(set(candidates) | set(legacy)):
        player_id, slice_key = key
        candidate_value = candidates.get(key)
        legacy_value = legacy.get(key)
        if candidate_value is not None and legacy_value is not None:
            if (
                abs(candidate_value.share - legacy_value.share) <= tolerance
                and candidate_value.volume == legacy_value.volume
                and candidate_value.games_played == legacy_value.games_played
            ):
                status = "exact"
                exact_count += 1
            else:
                status = "differs"
                differs_count += 1
        elif candidate_value is not None:
            status = "candidate_only"
            candidate_only_count += 1
        else:
            status = "legacy_only"
            legacy_only_count += 1
        identities.append(AssistDietIdentity(
            player_id=player_id, slice_key=slice_key, status=status,
            candidate=candidate_value, legacy=legacy_value,
        ))
    return AssistDietParityReport(
        exact_count=exact_count,
        differs_count=differs_count,
        candidate_only_count=candidate_only_count,
        legacy_only_count=legacy_only_count,
        identities=tuple(identities),
    )


def _report_payload(
    report: AssistDietParityReport,
    *,
    players: frozenset[int] | None,
    candidate: dict[str, object],
) -> dict:
    def _identity_payload(identity: AssistDietIdentity) -> dict:
        return {
            "player_id": identity.player_id,
            "slice_key": identity.slice_key,
            "status": identity.status,
            "candidate": None if identity.candidate is None else identity.candidate.as_payload(),
            "legacy": None if identity.legacy is None else identity.legacy.as_payload(),
        }

    non_exact = [
        _identity_payload(identity)
        for identity in report.identities
        if identity.status != "exact"
    ][:_MAX_REPORTED_DIFFERENCES]
    return {
        "candidate": candidate,
        "counts": {
            "exact": report.exact_count,
            "differs": report.differs_count,
            "candidate_only": report.candidate_only_count,
            "legacy_only": report.legacy_only_count,
            "compared": len(report.identities),
        },
        "non_exact": non_exact,
        # The full per-slice rows for the requested (or, absent --players,
        # every compared) player -- the manual-review evidence an operator
        # reads before adjudicating the candidate.
        "rows": [
            _identity_payload(identity)
            for identity in report.identities
            if players is None or identity.player_id in players
        ],
    }


def _latest_candidate_publication(
    session: Session, *, season: str
) -> PublicationVersion | None:
    return session.scalar(
        select(PublicationVersion)
        .where(
            PublicationVersion.stream_key == STREAM_KEY,
            PublicationVersion.season == season,
            PublicationVersion.status.in_(("candidate", "active")),
        )
        .order_by(PublicationVersion.version.desc())
        .limit(1)
    )


def _explicit_candidate_publication(
    session: Session, *, publication_id: str, season: str
) -> PublicationVersion | None:
    """Return the named publication only when it is a usable candidate here.

    An explicit ``--publication-id`` must still be bound to this stream, this
    season, and a promotable status; a mismatch (wrong season, wrong stream,
    superseded/rolled-back status, or an unknown id) returns ``None`` rather
    than silently comparing the wrong candidate.
    """

    publication = session.get(PublicationVersion, publication_id)
    if (
        publication is None
        or publication.stream_key != STREAM_KEY
        or publication.season != season
        or publication.status not in ("candidate", "active")
    ):
        return None
    return publication


def _candidate_payload(publication: PublicationVersion) -> dict[str, object]:
    return {
        "publication_id": publication.publication_id,
        "status": publication.status,
        "version": publication.version,
        "cutoff": assume_utc(publication.cutoff).isoformat(),
    }


def _read_candidate_facts(
    publication: PublicationVersion, *, players: frozenset[int] | None,
    retrieved_at: datetime,
):
    payload = json.loads(publication.payload)
    facts = decode_player_diet(payload, base=BASE, retrieved_at=retrieved_at)
    if players is None:
        return facts
    return tuple(fact for fact in facts if fact.player_id in players)


def _read_legacy_rows(session: Session, *, season: str, players: frozenset[int] | None):
    query = select(
        PlayerDietFactRow.player_id,
        PlayerDietFactRow.slice_key,
        PlayerDietFactRow.share,
        PlayerDietFactRow.volume,
        PlayerDietFactRow.games_played,
    ).where(
        PlayerDietFactRow.season == season,
        PlayerDietFactRow.base == BASE,
    )
    if players is not None:
        query = query.where(PlayerDietFactRow.player_id.in_(players))
    return session.execute(query).mappings().all()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url", help="SQLAlchemy database URL (or set DATABASE_URL)"
    )
    parser.add_argument("--season", required=True)
    parser.add_argument(
        "--publication-id",
        help=(
            "candidate to compare; defaults to the latest candidate/active "
            f"{STREAM_KEY} publication for --season"
        ),
    )
    parser.add_argument(
        "--players",
        help="comma-separated player ids to restrict the report (default: all)",
    )
    parser.add_argument(
        "--tolerance", type=float, default=1e-6, help="share comparison tolerance"
    )
    return parser


def _parse_players(parser: argparse.ArgumentParser, raw: str | None) -> frozenset[int] | None:
    if not raw:
        return None
    try:
        return frozenset(int(value.strip()) for value in raw.split(",") if value.strip())
    except ValueError:
        parser.error("--players must be a comma-separated list of integers")
        raise AssertionError("unreachable")  # pragma: no cover


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    database_url = args.database_url or os.getenv("DATABASE_URL")
    if not database_url:
        parser.error("a database target is required: pass --database-url or set DATABASE_URL")
    players = _parse_players(parser, args.players)

    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            if args.publication_id is not None:
                publication = _explicit_candidate_publication(
                    session, publication_id=args.publication_id, season=args.season,
                )
                if publication is None:
                    print(
                        f"{args.publication_id!r} is not a candidate or active "
                        f"{STREAM_KEY} publication for season {args.season!r}",
                        file=sys.stderr,
                    )
                    return 2
            else:
                publication = _latest_candidate_publication(session, season=args.season)
                if publication is None:
                    print(json.dumps({"error": "no_candidate_found"}, sort_keys=True))
                    return 1
            candidate_facts = _read_candidate_facts(
                publication,
                players=players,
                retrieved_at=datetime.now(timezone.utc),
            )
            legacy_rows = _read_legacy_rows(session, season=args.season, players=players)
    finally:
        engine.dispose()

    report = compare_assist_diet(candidate_facts, legacy_rows, tolerance=args.tolerance)
    payload = _report_payload(
        report, players=players, candidate=_candidate_payload(publication),
    )
    print(json.dumps(payload, sort_keys=True))
    return 0 if report.all_exact else 1


if __name__ == "__main__":
    raise SystemExit(main())
