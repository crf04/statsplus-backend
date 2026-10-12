"""Put a held candidate on its stream's pointer, for reader fixtures.

Composition holds a candidate of any season other than the published one;
in production only the operator cutover moves it, after proving the cohort
complete and bound (covered by ``tests/services/test_season_cutover.py``).
Fixtures that test *readers* across a season rollover compose bare payloads
with no manifest or Event Catalog, so they make the single pointer move a
promotion makes -- fenced, with pointer history and activation evidence --
without the cutover's authority checks, which they are not about.
"""

from __future__ import annotations

from app.models.collection_control import PublicationPointer, PublicationVersion
from app.services.collection_control import _move_pointer_to_candidate


def cut_over_if_held(
    publications, version, *, session=None, reason="season rollover fixture",
):
    """Return the stream's active version, promoting ``version`` first if held.

    With ``session`` the move joins the caller's transaction and the composed
    ``version`` is returned as is.
    """

    if version is None or version.status != "candidate":
        return version

    def move(active_session):
        candidate = active_session.get(PublicationVersion, version.publication_id)
        pointer = active_session.get(PublicationPointer, candidate.stream_key)
        _move_pointer_to_candidate(
            active_session, pointer, candidate,
            current=active_session.get(
                PublicationVersion, pointer.active_publication_id
            ),
            actor="test", reason=reason, now=publications.clock(),
        )

    if session is not None:
        move(session)
        return version
    with publications.session() as owned, owned.begin():
        move(owned)
    return publications.current(version.stream_key)
