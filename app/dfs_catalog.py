"""Closed catalog of reviewed DFS provider adapter names.

The names themselves are constants because call sites need to spell one
provider; *which* providers exist is decided by
:mod:`app.providers.registry`, and nothing may read a list of providers from
here.
"""

from __future__ import annotations

DFS_DABBLE = "dabble"
DFS_PRIZEPICKS = "prizepicks"
DFS_UNDERDOG = "underdog"
