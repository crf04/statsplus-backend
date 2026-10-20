"""Season player stats (crf04/statsplus#277, crf04/statsplus-backend#509).

One row per player per season and season type, holding the NBA
``LeagueDashPlayerStats`` Base box score at ``PerMode=Totals``.  A plain table
beside, not inside, the publication pipeline: the loader replaces a whole
season at a time and the ``GET /api/players/season_stats`` read derives every
per-game and percentage value from these totals.  A traded player has one
combined row and ``team_abbreviation`` is the last team NBA reports.
"""

from sqlalchemy import Column, DateTime, Float, Integer, String

from . import Base

REGULAR_SEASON = "Regular Season"


class PlayerSeasonStats(Base):
    """One player's season totals."""

    __tablename__ = "player_season_stats"

    season = Column(String(7), primary_key=True)
    season_type = Column(String(32), primary_key=True)
    player_id = Column(Integer, primary_key=True)
    player_name = Column(String(128), nullable=False)
    team_abbreviation = Column(String(8), nullable=False)
    age = Column(Integer, nullable=False)
    gp = Column(Integer, nullable=False)
    w = Column(Integer, nullable=False)
    l = Column(Integer, nullable=False)  # noqa: E741 - NBA's column name
    min = Column(Float, nullable=False)
    fgm = Column(Integer, nullable=False)
    fga = Column(Integer, nullable=False)
    fg3m = Column(Integer, nullable=False)
    fg3a = Column(Integer, nullable=False)
    ftm = Column(Integer, nullable=False)
    fta = Column(Integer, nullable=False)
    oreb = Column(Integer, nullable=False)
    dreb = Column(Integer, nullable=False)
    reb = Column(Integer, nullable=False)
    ast = Column(Integer, nullable=False)
    stl = Column(Integer, nullable=False)
    blk = Column(Integer, nullable=False)
    tov = Column(Integer, nullable=False)
    pf = Column(Integer, nullable=False)
    pts = Column(Integer, nullable=False)
    plus_minus = Column(Integer, nullable=False)
    fetched_at = Column(DateTime(timezone=True), nullable=False)
