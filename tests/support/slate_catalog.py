"""Event Catalog test-double behavior for a Slate with no stored neighbours."""

from __future__ import annotations


class NoNeighbourEvents:
    """The neighbour-search reads of a catalog that stores no other game day."""

    def get_adjacent_season(self, season, *, below, inclusive=False):
        return None

    def get_events_before(self, season, before, limit, *, before_id=None):
        return []

    def get_events_after(self, season, after, limit, *, after_id=None):
        return []
