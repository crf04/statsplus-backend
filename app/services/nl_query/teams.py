"""One NBA team named in free text, as its tricode.

The parser and the LLM path both resolve a written team through
``resolve_team``, so "Blazers", "Portland" and "POR" mean the same team on
either path, and anything else means none.
"""

from __future__ import annotations

import re

from app.domain.nba_teams import NBA_TEAM_FULL_NAME_TO_TRICODE

#: Each team's city or region, nickname and common short forms, lowercased.
#: Full names and tricodes come from the domain catalog.
TEAM_ALIASES = {
    "ATL": ("atlanta", "hawks"),
    "BOS": ("boston", "celtics", "celts"),
    "BKN": ("brooklyn", "nets"),
    "CHA": ("charlotte", "hornets"),
    "CHI": ("chicago", "bulls"),
    "CLE": ("cleveland", "cavaliers", "cavs"),
    "DAL": ("dallas", "mavericks", "mavs"),
    "DEN": ("denver", "nuggets", "nugs"),
    "DET": ("detroit", "pistons"),
    "GSW": ("golden state", "warriors", "dubs", "gs"),
    "HOU": ("houston", "rockets"),
    "IND": ("indiana", "pacers"),
    "LAC": ("los angeles", "clippers", "clips", "la clippers"),
    "LAL": ("los angeles", "lakers", "la lakers"),
    "MEM": ("memphis", "grizzlies", "grizz"),
    "MIA": ("miami", "heat"),
    "MIL": ("milwaukee", "bucks"),
    "MIN": ("minnesota", "timberwolves", "wolves", "t-wolves", "twolves"),
    "NOP": ("new orleans", "pelicans", "pels"),
    "NYK": ("new york", "knicks", "ny"),
    "OKC": ("oklahoma city", "thunder"),
    "ORL": ("orlando", "magic"),
    "PHI": ("philadelphia", "76ers", "sixers", "philly"),
    "PHX": ("phoenix", "suns", "pho"),
    "POR": ("portland", "trail blazers", "blazers", "trailblazers"),
    "SAC": ("sacramento", "kings"),
    "SAS": ("san antonio", "spurs"),
    "TOR": ("toronto", "raptors"),
    "UTA": ("utah", "jazz"),
    "WAS": ("washington", "wizards", "wiz"),
}


def _alias_index() -> dict[str, str]:
    """Every alias that names exactly one team; a shared city names none."""
    claims: dict[str, set[str]] = {}
    for tricode, aliases in TEAM_ALIASES.items():
        for alias in (tricode.lower(), *aliases):
            claims.setdefault(alias, set()).add(tricode)
    for name, tricode in NBA_TEAM_FULL_NAME_TO_TRICODE.items():
        claims.setdefault(name.lower(), set()).add(tricode)
    return {alias: tricodes.pop() for alias, tricodes in claims.items() if len(tricodes) == 1}


TEAM_BY_ALIAS = _alias_index()

_ALIASES = "|".join(re.escape(alias) for alias in sorted(TEAM_BY_ALIAS, key=len, reverse=True))
OPPONENT_TEAM_PATTERN = re.compile(
    rf"(?<![a-z0-9])(?:vs\.?|v\.?|versus|against)\s+(?:the\s+)?(?P<team>{_ALIASES})(?![a-z0-9])",
    re.IGNORECASE,
)


def resolve_team(text: str | None) -> str | None:
    """The tricode of the one team ``text`` names, or None."""
    if not text:
        return None
    name = " ".join(text.lower().split())
    return TEAM_BY_ALIAS.get(name.removeprefix("the "))
