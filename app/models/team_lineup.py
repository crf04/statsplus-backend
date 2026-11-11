"""Each team's lineup for one game, from NBA.com's daily lineups feed (crf04/statsplus#492).

``scripts/collect_odds.py`` rewrites a team's row whenever the feed lists the
team; the rotation team view only reads it.  ``game_date`` is the US Eastern
day of the feed document the row came from, so a game moved to another date
is not served this lineup.
"""

from sqlalchemy import Column, Date, DateTime, Integer, String, Text

from . import Base


class TeamLineup(Base):
    """One team's starters and inactive players for one game."""

    __tablename__ = "team_lineups"

    game_id = Column(String(32), primary_key=True)
    team_id = Column(Integer, primary_key=True)
    tricode = Column(String(8), nullable=False)
    game_date = Column(Date, nullable=False)
    #: The newest feed ``timestamp`` among the team's rows, or NULL when it lists none.
    feed_timestamp = Column(DateTime(timezone=True), nullable=True)
    #: JSON list of ``{player_id, name, position, lineup_status}``, in feed order.
    starters = Column(Text, nullable=False)
    #: JSON list of ``{player_id, name}``, in feed order.
    inactive = Column(Text, nullable=False)
    #: The collection run that last read this team from the feed.
    collected_at = Column(DateTime(timezone=True), nullable=False)
