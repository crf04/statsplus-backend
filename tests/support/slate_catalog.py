"""Event Catalog test-double behavior for a Slate with no stored neighbours."""

from __future__ import annotations


class NoNeighbourEvents:
    """The neighbour-search reads of a catalog that stores no other game day."""

    def get_seasons(self):
        return []

    def get_events_before(self, season, before, limit, *, inclusive=False):
        return []

    def get_events_after(self, season, after, limit):
        return []
