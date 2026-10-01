"""Choose the season a request reads without changing collector defaults.

Two rules, both deferring to an explicit ``current_season`` (the
``NBA_CURRENT_SEASON`` pin) so a pinned deployment behaves exactly as before:

* Published reads follow the active player-game-log publication's season, so
  an October rollover never selects a season with no published evidence.
* Schedule reads for a date follow the season containing that date, falling
  back to the previous season while the new one has no stored schedule.
* Schedule reads for one game (its event, Player Pool, and injuries) follow
  the season its game ID names, so a game the Slate shows is always readable
  while its evidence still comes from the published season.

Collectors and schedule ingestion keep the calendar default in settings.
"""

from copy import copy
from datetime import date

from app.config.settings import current_nba_season
from app.domain.nba_events import season_for_game_id


def season_is_pinned(settings) -> bool:
    return "current_season" in settings.nba.model_fields_set


def _published_read(settings, publication_reader):
    if publication_reader is None or season_is_pinned(settings):
        return None
    read = publication_reader.snapshot(
        ("player_game_logs",),
        projection_only_keys=frozenset({"player_game_logs"}),
    ).read("player_game_logs")
    if not read.available or not isinstance(read.season, str):
        return None
    return read


def research_season(settings, publication_reader) -> str:
    """The season one published read uses: the publication's, else settings'."""

    read = _published_read(settings, publication_reader)
    return settings.nba.current_season if read is None else read.season


def research_service(service, publication_reader):
    """Scope one request to the active game-log publication's season.

    Settings and the original service remain unchanged for other requests and
    collectors. Explicit season overrides retain their existing behavior.
    """
    settings = service.settings
    read = _published_read(settings, publication_reader)
    if read is None:
        return service
    scoped = copy(service)
    scoped._research_generation = (read.publication_id, read.fence, read.version)
    scoped.settings = settings.model_copy(update={
        "nba": settings.nba.model_copy(update={"current_season": read.season}),
    })
    return scoped


def previous_nba_season(season: str) -> str:
    start = int(season[:4]) - 1
    return f"{start}-{str(start + 1)[-2:]}"


def schedule_season(settings, event_catalog, on_date: date) -> str:
    """The season whose schedule covers ``on_date``.

    The season containing the date, or the previous season while the
    containing one has no stored events (the offseason before collection).
    When neither has events the containing season is returned, so the caller
    reports the schedule unavailable exactly as before.
    """

    if season_is_pinned(settings):
        return settings.nba.current_season
    season = current_nba_season(on_date)
    if event_catalog is None or event_catalog.count_events(season) > 0:
        return season
    previous = previous_nba_season(season)
    return previous if event_catalog.count_events(previous) > 0 else season


def event_season(settings, game_id: str, evidence_season: str) -> str:
    """The season holding one game's schedule facts.

    The season the game ID names, so opening night's games resolve while the
    published evidence is still last season's; ``evidence_season`` for an ID
    that names none.
    """

    if season_is_pinned(settings):
        return settings.nba.current_season
    return season_for_game_id(str(game_id)) or evidence_season
