"""Move a fixture's pointers into a new season the way production does.

Composition holds a candidate of any season other than the published one;
only an operator cutover moves the cohort.  Fixtures that roll a season over
compose as before and then cut over when the composition was held.
"""

from __future__ import annotations


def cut_over_if_held(
    publications, version, *, session=None, reason="season rollover fixture",
):
    """Return the stream's active version, cutting over first if ``version`` was held.

    With ``session`` the cutover joins the caller's transaction and the
    composed ``version`` is returned as is.
    """

    if version is None or version.status != "candidate":
        return version
    publications.cutover_season(
        version.season, actor="test", reason=reason, session=session,
    )
    if session is not None:
        return version
    return publications.current(version.stream_key)
