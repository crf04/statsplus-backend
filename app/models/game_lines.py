"""Each game's line from ESPN's scoreboard, stored for the Matchup (crf04/statsplus#493).

One row per Event Catalog game: the home team's spread, the total, the
sportsbook ESPN names and when the line was last read.  The favourite and each
team's implied total follow from the spread and total.  ``scripts/collect_odds.py``
writes it only while ESPN lists the game as not started, so after tip the row
keeps the last line read before it; the Matchup only reads it.
"""

from sqlalchemy import Column, DateTime, Float, Integer, String

from . import Base


class GameLine(Base):
    """The last line read for one Event Catalog game."""

    __tablename__ = "game_lines"

    game_id = Column(String(32), primary_key=True)
    espn_event_id = Column(String(32), nullable=False)
    away_team_id = Column(Integer, nullable=False)
    away_tricode = Column(String(8), nullable=False)
    home_team_id = Column(Integer, nullable=False)
    home_tricode = Column(String(8), nullable=False)
    #: Negative when the home team is favoured, 0 for a pick'em.
    home_spread = Column(Float, nullable=False)
    total = Column(Float, nullable=False)
    provider = Column(String(64), nullable=False)
    read_at = Column(DateTime(timezone=True), nullable=False)
