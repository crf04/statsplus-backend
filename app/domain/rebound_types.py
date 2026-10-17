"""Canonical vocabulary for the six miss-type rebound counts (#234).

The PBP ``FullGame`` wire splits each player's rebounds by the kind of miss
they came off: defensive and offensive rebounds off two-point misses,
three-point misses and free-throw misses.  This module is deliberately below
the service layer: the ledger, the team and player derivations, the Defense
Sheet taxonomy and the Diet vocabulary all consume the same identities from
here instead of keeping parallel copies of the provider spellings.

Every identity has two spellings.  The *attribute* is the snake-case name of
the typed ledger fact and its column; the *wire* name is the provider field,
and it is also the slice key the Defense Sheet rows, the publications and the
Diet shares are keyed by.
"""

from __future__ import annotations

#: ``(attribute, wire name)`` in publication order: the three defensive
#: types, then the three offensive ones.
REBOUND_TYPE_FIELDS: tuple[tuple[str, str], ...] = (
    ("def_two_pt_rebounds", "DefTwoPtRebounds"),
    ("def_three_pt_rebounds", "DefThreePtRebounds"),
    ("ft_def_rebounds", "FTDefRebounds"),
    ("off_two_pt_rebounds", "OffTwoPtRebounds"),
    ("off_three_pt_rebounds", "OffThreePtRebounds"),
    ("ft_off_rebounds", "FTOffRebounds"),
)
REBOUND_TYPE_ATTRIBUTES: tuple[str, ...] = tuple(
    attribute for attribute, _wire in REBOUND_TYPE_FIELDS
)
REBOUND_TYPE_WIRE_NAMES: tuple[str, ...] = tuple(
    wire for _attribute, wire in REBOUND_TYPE_FIELDS
)
REBOUND_TYPE_ATTRIBUTE_BY_WIRE = {
    wire: attribute for attribute, wire in REBOUND_TYPE_FIELDS
}
#: The three types that add up to a fact's defensive rebounds.
DEFENSIVE_REBOUND_TYPE_ATTRIBUTES: tuple[str, ...] = REBOUND_TYPE_ATTRIBUTES[:3]
#: The three types that add up to a fact's offensive rebounds.
OFFENSIVE_REBOUND_TYPE_ATTRIBUTES: tuple[str, ...] = REBOUND_TYPE_ATTRIBUTES[3:]

#: The Diet and Defense Sheet base these counts publish under.
REBOUND_TYPES_BASE = "rebound_types"
#: The unit the player Diet's volume is stated in.
REBOUND_TYPES_VOLUME_UNIT = "rebounds"


__all__ = [
    "DEFENSIVE_REBOUND_TYPE_ATTRIBUTES",
    "OFFENSIVE_REBOUND_TYPE_ATTRIBUTES",
    "REBOUND_TYPES_BASE",
    "REBOUND_TYPES_VOLUME_UNIT",
    "REBOUND_TYPE_ATTRIBUTES",
    "REBOUND_TYPE_ATTRIBUTE_BY_WIRE",
    "REBOUND_TYPE_FIELDS",
    "REBOUND_TYPE_WIRE_NAMES",
]
