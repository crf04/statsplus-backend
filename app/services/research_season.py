"""Choose the available research season without changing scheduling defaults."""

from copy import copy


def research_service(service, publication_reader):
    """Scope one request to the active game-log publication's season.

    Settings and the original service remain unchanged for other requests and
    collectors. Explicit season overrides retain their existing behavior.
    """
    settings = service.settings
    if publication_reader is None or "current_season" in settings.nba.model_fields_set:
        return service
    read = publication_reader.snapshot(
        ("player_game_logs",),
        projection_only_keys=frozenset({"player_game_logs"}),
    ).read("player_game_logs")
    if (
        not read.available
        or not isinstance(read.season, str)
    ):
        return service
    scoped = copy(service)
    scoped._research_generation = (read.publication_id, read.fence, read.version)
    scoped.settings = settings.model_copy(update={
        "nba": settings.nba.model_copy(update={"current_season": read.season}),
    })
    return scoped
