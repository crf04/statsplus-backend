"""Closed catalog of reviewed DFS provider adapter names."""

from __future__ import annotations

DFS_DABBLE = "dabble"
DFS_PRIZEPICKS = "prizepicks"
DFS_UNDERDOG = "underdog"
DFS_PROVIDER_NAMES = (DFS_DABBLE, DFS_PRIZEPICKS, DFS_UNDERDOG)
DFS_PROVIDER_NAME_SET = frozenset(DFS_PROVIDER_NAMES)
