"""The one canonical semantic content of a normalized provider market.

Two seams need to answer the same question -- *is this market the same offering
stated again, or a different one?* -- and they must answer it identically.  A
provider snapshot rejects a repeated source identity whose content changed; the
comparison board derives references, orders retained evidence, and collapses
semantic repeats.  If either derived its own answer, a market a provider merely
relisted in another selection order, or wrote one threshold at another scale,
would be malformed at one seam and a duplicate at the other.

This module is that single authority.  It reads any normalized market by the
attributes the provider contract defines, importing no provider or service
module, so both sides can depend on it without a cycle and neither can drift.

It distinguishes two things a market carries:

*Semantic content* is what the offering says: exact numbers whatever scale they
were written at, and selections in an order derived from their own facts rather
than the provider's listing.  Two markets equal here are one offering.

*Retained audit content* is what a board keeps beyond that: the provider's own
threshold text and the scale each exact decimal was written at.  It never
changes what an offering is, but it does give a total order over evidence that
would otherwise tie and fall back on arrival order.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from collections.abc import Sequence
from typing import Any


def aware_utc(value: datetime, *, field: str) -> datetime:
    """One timezone-aware timestamp in UTC, never an assumed timezone."""

    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def canonical_decimal(value: Decimal) -> str:
    """The one canonical token for an exact decimal, independent of scale.

    This is internal reference material, not a rendering: it is a value's sign,
    its significant digits, and the base-ten exponent those digits sit at,
    written as ``<sign><coefficient>E<adjusted exponent>``.  ``25.5`` and
    ``25.50`` are the same number, so they produce one token; ``0`` and ``-0``
    are the same number too, and every zero is the single token ``0`` whatever
    scale or sign it was written with.

    The form is read straight off the value's own digits.  No arithmetic and no
    normalization takes part, because both are performed under the ambient
    decimal context and would round a value carrying more digits than that
    context permits -- which would make two distinct provider numbers one
    reference, and would make a reference depend on the context a caller
    happened to be inside.

    Nothing here is materialized from the exponent, so a legitimate provider
    value such as ``1E+1000000`` costs its own digits rather than a million
    characters of positional zeroes.
    """

    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("a reference requires a finite Decimal")
    sign, digits, exponent = value.as_tuple()
    significant = list(digits)
    # Insignificant zeroes are written scale, not part of the number: dropping a
    # trailing one raises the exponent, and a leading one names nothing at all.
    while significant and significant[-1] == 0:
        significant.pop()
        exponent += 1
    while significant and significant[0] == 0:
        significant.pop(0)
    if not significant:
        return "0"
    coefficient = "".join(str(digit) for digit in significant)
    # The exponent of the leading significant digit, so one number has one
    # exponent whatever scale its coefficient was written at.
    adjusted = exponent + len(coefficient) - 1
    return f"{'-' if sign else ''}{coefficient}E{adjusted}"


def _framed(tag: str, text: str) -> bytes:
    """One length-framed, typed token; no payload can span two fields."""

    payload = text.encode("utf-8")
    return f"{tag}:{len(payload)}:".encode("ascii") + payload


def encode_canonical(value: object) -> bytes:
    """Canonically encode one typed value, injectively.

    Every token carries its type tag and its byte length, and every sequence
    carries its element count, so distinct structures always encode to distinct
    bytes -- whatever separators the underlying strings happen to contain.
    """

    if value is None:
        return b"n:0:"
    if isinstance(value, bool):
        return _framed("b", "1" if value else "0")
    if isinstance(value, Enum):
        return _framed("e", f"{type(value).__name__}={value.value}")
    if isinstance(value, Decimal):
        return _framed("d", canonical_decimal(value))
    if isinstance(value, int):
        return _framed("i", str(value))
    if isinstance(value, str):
        return _framed("s", value)
    if isinstance(value, datetime):
        return _framed("t", aware_utc(value, field="reference timestamp").isoformat())
    if isinstance(value, (tuple, list)):
        body = b"".join(encode_canonical(item) for item in value)
        return f"q:{len(value)}:{len(body)}:".encode("ascii") + body
    raise ValueError(
        f"a reference cannot be derived from a {type(value).__name__} value"
    )


# -- semantic facts --------------------------------------------------------


def _team_facts(team: Any | None) -> object:
    if team is None:
        return None
    return (team.provider_id, team.canonical_id, team.name, team.abbreviation)


def _athlete_facts(athlete: Any | None) -> object:
    if athlete is None:
        return None
    return (
        athlete.provider_id,
        athlete.canonical_id,
        athlete.name,
        _team_facts(athlete.team),
    )


def _event_facts(event: Any | None) -> object:
    """Every normalized fact an event carries, including its canonical claim."""

    if event is None:
        return None
    return (
        event.provider_id,
        event.canonical_id,
        event.label,
        event.starts_at,
        event.ends_at,
        event.updated_at,
        event.status_label,
        _team_facts(event.home_team),
        _team_facts(event.away_team),
    )


def _statistic_facts(statistic: Any | None) -> object:
    if statistic is None:
        return None
    return (
        statistic.provider_id,
        statistic.canonical_id,
        statistic.label,
        tuple(statistic.components),
    )


def _named_facts(evidence: Any | None) -> object:
    if evidence is None:
        return None
    return (evidence.provider_id, evidence.canonical_id, evidence.label)


def _competition_facts(competition: Any | None) -> object:
    if competition is None:
        return None
    return (
        competition.provider_id,
        competition.canonical_id,
        competition.label,
        _named_facts(competition.sport),
    )


def _appearance_facts(appearance: Any | None) -> object:
    if appearance is None:
        return None
    return (appearance.provider_id, appearance.appearance_type, appearance.label)


def _threshold_facts(threshold: Any | None) -> object:
    """The exact number a threshold states, never the text it was written as.

    ``original_value`` is retained on the board for audit, but a provider that
    spells one threshold ``25.50`` and another ``25.5`` is offering the same
    line, so its spelling must not split one offering into two.
    """

    if threshold is None:
        return None
    return (threshold.value, threshold.unit)


def _modifier_facts(modifier: Any) -> object:
    return (modifier.value, modifier.kind, modifier.scope, modifier.label)


def selection_facts(selection: Any) -> object:
    """Every normalized fact that defines one offered selection."""

    return (
        selection.selection_id,
        selection.label,
        selection.direction,
        selection.direction_label,
        selection.status,
        tuple(_modifier_facts(modifier) for modifier in selection.modifiers),
        selection.american_price,
        selection.decimal_price,
    )


def statistic_resolution_facts(match: object) -> object:
    """What a catalog resolved about a market's statistic, if anything."""

    if match is None:
        return None
    return (
        getattr(match, "state", None),
        getattr(match, "scoring_period", None),
        getattr(match, "canonical_id", None),
        getattr(getattr(match, "unit", None), "value", None),
        getattr(getattr(match, "reason", None), "value", None),
    )


# -- retained audit facts --------------------------------------------------


def _written_form(value: Decimal | None) -> object:
    """How one retained exact decimal was written, which its value alone drops.

    A canonical value is deliberately scale-independent, so it cannot order two
    retained decimals that state one number in two spellings; the sign and
    exponent the provider wrote can, and cost nothing to carry.
    """

    if value is None:
        return None
    sign, _digits, exponent = value.as_tuple()
    return (sign, int(exponent))


def _selection_audit_facts(selection: Any) -> object:
    """The spellings of one selection's exact decimals, which its facts drop."""

    return (
        _written_form(selection.decimal_price),
        tuple(_written_form(modifier.value) for modifier in selection.modifiers),
    )


def _selection_order(selection: Any) -> bytes:
    """A total order over one selection: its semantics, then its spellings.

    Semantics lead, so the order two selections take is decided by what they
    offer wherever they offer anything different.  Spellings only separate
    selections that are semantically one and the same, which would otherwise
    tie and be left in the order the provider happened to list them.  Because
    the tiebreak applies only inside a semantic tie, the sequence of semantic
    facts this order produces is itself scale-independent, so a reference
    derived from it still cannot depend on how a number was written.
    """

    return encode_canonical(selection_facts(selection)) + encode_canonical(
        _selection_audit_facts(selection)
    )


def canonical_selections(selections: Sequence[Any]) -> tuple[Any, ...]:
    """One market's selections in an order derived only from their own content.

    A provider is free to list two equivalent selections in either order, and
    that ordering is not a fact about the offering.  Ordering them by their
    complete retained content -- every normalized fact, then every audit
    spelling a board keeps beyond them -- makes the market's reference, the
    selections the board returns, and the evidence keys derived from them all
    independent of the order they arrived in.  Nothing is collapsed: two
    selections that differ in any retained fact -- price, modifier, status,
    direction, or label -- stay two selections.
    """

    return tuple(sorted(selections, key=_selection_order))


def reported_evidence_facts(market: Any) -> object:
    """Every normalized fact one market reports about the offering it names."""

    return (
        market.provider,
        _athlete_facts(market.athlete),
        _event_facts(market.event),
        _team_facts(market.team),
        _team_facts(market.opponent),
        _named_facts(market.league),
        _competition_facts(market.competition),
        _named_facts(market.sport),
        _statistic_facts(market.statistic),
        _threshold_facts(market.threshold),
        market.status,
        market.status_label,
        market.variant,
        market.variant_label,
        market.scoring_period,
        market.scoring_period_label,
        market.starts_at,
        market.updated_at,
        _appearance_facts(market.appearance),
        tuple(
            selection_facts(selection)
            for selection in canonical_selections(market.selections)
        ),
    )


def _retained_audit_facts(market: Any) -> object:
    """The retained audit spellings a market's canonical semantics drop."""

    threshold = market.threshold
    return (
        None
        if threshold is None
        else (threshold.original_value, _written_form(threshold.value)),
        tuple(
            _selection_audit_facts(selection)
            for selection in canonical_selections(market.selections)
        ),
    )


def market_content_key(market: Any) -> bytes:
    """What one market says, in the semantics its identity is decided in.

    Two observations with this key equal are one offering stated twice, not two
    offerings contradicting each other: selections are read in canonical order
    because listing order is not a fact about an offering, and a threshold is
    read as the exact number it states, so ``25.50`` and ``25.5`` are one line.
    An exact semantic repeat therefore collapses instead of being read as a
    conflicting market identity.
    """

    return encode_canonical(
        (
            market.market_id,
            reported_evidence_facts(market),
            statistic_resolution_facts(getattr(market, "statistic_match", None)),
        )
    )


def market_evidence_key(market: Any) -> bytes:
    """A total, content-derived order over one market's retained evidence.

    Two markets that contest one identity must be retained -- or collapsed to
    one of themselves -- in an order neither the provider's listing order nor a
    board's iteration order can change, so they are ordered by their own
    complete retained content: every canonical fact, and then every audit field
    kept beyond them, down to the provider's original threshold text and the
    scale each exact decimal was written at.  Two retained observations
    therefore tie here only when nothing retained tells them apart.
    """

    # Both halves are self-delimiting, so their concatenation stays injective.
    return market_content_key(market) + encode_canonical(_retained_audit_facts(market))


__all__ = [
    "aware_utc",
    "canonical_decimal",
    "canonical_selections",
    "encode_canonical",
    "market_content_key",
    "market_evidence_key",
    "reported_evidence_facts",
    "selection_facts",
    "statistic_resolution_facts",
]
