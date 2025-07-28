# NBA Team Matchup Page - Design & Implementation Specification

## Overview
A comprehensive team matchup page inspired by Robinhood's sleek, modern design philosophy, integrated with the existing NBA game logs React application. The page provides deep analytical insights into upcoming games through advanced statistical comparisons and historical data analysis.

## Design Philosophy
- **Robinhood-Inspired Aesthetics**: Clean, minimal interface with premium feel
- **Dark Theme Consistency**: Maintains existing color scheme (#000000, #1a1a1a, #2a2a2a)
- **Information Density**: Maximum data with optimal readability
- **Progressive Disclosure**: Complex data organized in digestible sections
- **Mobile-First Responsive**: Seamless experience across all devices

## Visual Design Specification

### Color Palette
```css
:root {
  --bg-primary: #000000;
  --bg-secondary: #1a1a1a;
  --bg-tertiary: #2a2a2a;
  --accent-primary: #f59e0b;
  --accent-secondary: #d97706;
  --text-primary: #ffffff;
  --text-secondary: #9ca3af;
  --text-muted: #6b7280;
  --border-primary: rgba(245, 158, 11, 0.2);
  --border-hover: rgba(245, 158, 11, 0.5);
  --shadow-premium: 0 8px 32px rgba(245, 158, 11, 0.1);
}
```

### Typography System
```css
.text-hero { font: 700 32px/1.2 'Inter', sans-serif; }
.text-h1 { font: 600 24px/1.3 'Inter', sans-serif; }
.text-h2 { font: 500 18px/1.4 'Inter', sans-serif; }
.text-body { font: 400 16px/1.5 'Inter', sans-serif; }
.text-caption { font: 400 14px/1.4 'Inter', sans-serif; }
.text-small { font: 400 12px/1.3 'Inter', sans-serif; }
```

### Component Styling Framework
```css
/* Premium Card Base */
.premium-card {
  background: linear-gradient(135deg, var(--bg-secondary) 0%, var(--bg-tertiary) 100%);
  border: 1px solid var(--border-primary);
  border-radius: 12px;
  backdrop-filter: blur(10px);
  transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
  overflow: hidden;
}

.premium-card:hover {
  border-color: var(--border-hover);
  transform: translateY(-2px);
  box-shadow: var(--shadow-premium);
}

/* Comparison Progress Bars */
.comparison-bar {
  height: 8px;
  background: linear-gradient(90deg, var(--accent-primary) 0%, var(--accent-secondary) 100%);
  border-radius: 4px;
  position: relative;
  overflow: hidden;
}

.comparison-indicator {
  position: absolute;
  top: 0;
  left: 0;
  height: 100%;
  background: rgba(255, 255, 255, 0.3);
  border-radius: inherit;
  transition: width 0.8s cubic-bezier(0.4, 0, 0.2, 1);
}

/* Tab Navigation */
.tab-navigation {
  display: flex;
  background: var(--bg-secondary);
  border-radius: 12px;
  padding: 4px;
  margin-bottom: 24px;
}

.tab-button {
  flex: 1;
  padding: 12px 20px;
  background: transparent;
  border: none;
  color: var(--text-secondary);
  font: 500 14px/1 'Inter', sans-serif;
  border-radius: 8px;
  cursor: pointer;
  transition: all 0.2s ease-in-out;
  position: relative;
}

.tab-button.active {
  background: var(--accent-primary);
  color: var(--bg-primary);
  font-weight: 600;
}

.tab-button:not(.active):hover {
  background: rgba(245, 158, 11, 0.1);
  color: var(--text-primary);
}
```

## Component Architecture

### 1. Main Container Component
```jsx
// TeamMatchupPage.jsx
import React, { useState, useEffect } from 'react';
import { Container, Row, Col } from 'react-bootstrap';
import MatchupHeader from './components/MatchupHeader';
import QuickStatsOverview from './components/QuickStatsOverview';
import AnalysisSection from './components/AnalysisSection';
import InsightsSummary from './components/InsightsSummary';
import { useTeamMatchupData } from '../hooks/useTeamMatchupData';
import './TeamMatchupPage.css';

const TeamMatchupPage = ({ homeTeam, awayTeam, gameDate }) => {
  const { data, loading, error } = useTeamMatchupData(homeTeam, awayTeam, gameDate);
  const [activeTab, setActiveTab] = useState('offense');

  if (loading) return <LoadingSpinner />;
  if (error) return <ErrorMessage error={error} />;

  return (
    <Container fluid className="team-matchup-page">
      <MatchupHeader 
        homeTeam={data.teams.home}
        awayTeam={data.teams.away}
        gameInfo={data.gameInfo}
        bettingOdds={data.bettingOdds}
      />
      
      <QuickStatsOverview 
        matchupEdges={data.quickStats}
        keyIndicators={data.indicators}
      />
      
      <AnalysisSection 
        activeTab={activeTab}
        onTabChange={setActiveTab}
        offenseData={data.offense}
        defenseData={data.defense}
        playerMatchups={data.playerMatchups}
        trendsData={data.trends}
        historyData={data.history}
      />
      
      <InsightsSummary 
        predictions={data.predictions}
        keyInsights={data.insights}
      />
    </Container>
  );
};

export default TeamMatchupPage;
```

### 2. Header Component System
```jsx
// components/MatchupHeader.jsx
import React from 'react';
import { Row, Col, Card } from 'react-bootstrap';
import TeamLogo from './ui/TeamLogo';
import BettingOdds from './BettingOdds';

const MatchupHeader = ({ homeTeam, awayTeam, gameInfo, bettingOdds }) => {
  return (
    <Card className="premium-card matchup-header mb-4">
      <Card.Body className="p-4">
        <Row className="align-items-center">
          <Col xs={12} md={4} className="text-center text-md-start">
            <div className="team-info">
              <TeamLogo team={awayTeam} size="large" />
              <div className="team-details">
                <h2 className="team-name">{awayTeam.name}</h2>
                <p className="team-record">{awayTeam.record} ({awayTeam.ranking})</p>
              </div>
            </div>
          </Col>
          
          <Col xs={12} md={4} className="text-center">
            <div className="matchup-center">
              <div className="vs-indicator">VS</div>
              <div className="game-details">
                <div className="game-date">{gameInfo.date}</div>
                <div className="game-time">{gameInfo.time} • {gameInfo.venue}</div>
                <div className="broadcast-info">{gameInfo.tv} • {gameInfo.radio}</div>
              </div>
            </div>
          </Col>
          
          <Col xs={12} md={4} className="text-center text-md-end">
            <div className="team-info">
              <TeamLogo team={homeTeam} size="large" />
              <div className="team-details">
                <h2 className="team-name">{homeTeam.name}</h2>
                <p className="team-record">{homeTeam.record} ({homeTeam.ranking})</p>
              </div>
            </div>
          </Col>
        </Row>
        
        <Row className="mt-4">
          <Col>
            <BettingOdds odds={bettingOdds} />
          </Col>
        </Row>
      </Card.Body>
    </Card>
  );
};

export default MatchupHeader;
```

### 3. Quick Stats Overview Component
```jsx
// components/QuickStatsOverview.jsx
import React from 'react';
import { Row, Col, Card } from 'react-bootstrap';
import StatCard from './ui/StatCard';
import ComparisonBar from './ui/ComparisonBar';

const QuickStatsOverview = ({ matchupEdges, keyIndicators }) => {
  const edgeCards = [
    {
      title: 'Pace Advantage',
      leader: matchupEdges.pace.leader,
      value: matchupEdges.pace.difference,
      description: 'Fast Break Points',
      percentage: matchupEdges.pace.percentage
    },
    {
      title: 'Offensive Efficiency',
      leader: matchupEdges.offense.leader,
      value: matchupEdges.offense.difference,
      description: 'Points Per 100 Poss',
      percentage: matchupEdges.offense.percentage
    },
    {
      title: 'Defensive Efficiency',
      leader: matchupEdges.defense.leader,
      value: matchupEdges.defense.difference,
      description: 'Opp Points Per 100',
      percentage: matchupEdges.defense.percentage
    },
    {
      title: 'Rebounding Edge',
      leader: matchupEdges.rebounding.leader,
      value: matchupEdges.rebounding.difference,
      description: 'Offensive Rebounds',
      percentage: matchupEdges.rebounding.percentage
    }
  ];

  return (
    <Card className="premium-card mb-4">
      <Card.Header className="border-0 pb-0">
        <h3 className="text-h1 mb-0">Matchup Edge Analysis</h3>
      </Card.Header>
      <Card.Body>
        <Row>
          {edgeCards.map((edge, index) => (
            <Col xs={12} sm={6} lg={3} key={index} className="mb-3 mb-lg-0">
              <StatCard
                title={edge.title}
                leader={edge.leader}
                value={edge.value}
                description={edge.description}
                percentage={edge.percentage}
              />
            </Col>
          ))}
        </Row>
      </Card.Body>
    </Card>
  );
};

export default QuickStatsOverview;
```

### 4. Analysis Section with Tabs
```jsx
// components/AnalysisSection.jsx
import React from 'react';
import { Card } from 'react-bootstrap';
import TabNavigation from './ui/TabNavigation';
import OffenseTab from './tabs/OffenseTab';
import DefenseTab from './tabs/DefenseTab';
import MatchupsTab from './tabs/MatchupsTab';
import TrendsTab from './tabs/TrendsTab';
import HistoryTab from './tabs/HistoryTab';

const AnalysisSection = ({ 
  activeTab, 
  onTabChange, 
  offenseData, 
  defenseData, 
  playerMatchups, 
  trendsData, 
  historyData 
}) => {
  const tabs = [
    { id: 'offense', label: 'Offense', icon: '⚡' },
    { id: 'defense', label: 'Defense', icon: '🛡️' },
    { id: 'matchups', label: 'Key Matchups', icon: '⚔️' },
    { id: 'trends', label: 'Trends', icon: '📈' },
    { id: 'history', label: 'H2H History', icon: '📊' }
  ];

  const renderTabContent = () => {
    switch (activeTab) {
      case 'offense':
        return <OffenseTab data={offenseData} />;
      case 'defense':
        return <DefenseTab data={defenseData} />;
      case 'matchups':
        return <MatchupsTab data={playerMatchups} />;
      case 'trends':
        return <TrendsTab data={trendsData} />;
      case 'history':
        return <HistoryTab data={historyData} />;
      default:
        return <OffenseTab data={offenseData} />;
    }
  };

  return (
    <Card className="premium-card mb-4">
      <Card.Body>
        <TabNavigation 
          tabs={tabs}
          activeTab={activeTab}
          onTabChange={onTabChange}
        />
        
        <div className="tab-content">
          {renderTabContent()}
        </div>
      </Card.Body>
    </Card>
  );
};

export default AnalysisSection;
```

### 5. Offense Tab Component
```jsx
// components/tabs/OffenseTab.jsx
import React from 'react';
import { Row, Col, Card } from 'react-bootstrap';
import PlayTypesCard from '../cards/PlayTypesCard';
import ShotZonesCard from '../cards/ShotZonesCard';
import AssistPatternsCard from '../cards/AssistPatternsCard';
import PaceRhythmCard from '../cards/PaceRhythmCard';

const OffenseTab = ({ data }) => {
  return (
    <Row>
      <Col xs={12} lg={6} className="mb-4">
        <PlayTypesCard 
          homeTeam={data.homeTeam.playtypes}
          awayTeam={data.awayTeam.playtypes}
          leagueAvg={data.leagueAverages.playtypes}
        />
      </Col>
      
      <Col xs={12} lg={6} className="mb-4">
        <ShotZonesCard 
          homeTeam={data.homeTeam.shotZones}
          awayTeam={data.awayTeam.shotZones}
          courtVisualization={true}
        />
      </Col>
      
      <Col xs={12} lg={6} className="mb-4">
        <AssistPatternsCard 
          homeTeam={data.homeTeam.assists}
          awayTeam={data.awayTeam.assists}
        />
      </Col>
      
      <Col xs={12} lg={6} className="mb-4">
        <PaceRhythmCard 
          homeTeam={data.homeTeam.pace}
          awayTeam={data.awayTeam.pace}
        />
      </Col>
    </Row>
  );
};

export default OffenseTab;
```

## Data Integration Layer

### 1. Custom Hook for Data Fetching
```jsx
// hooks/useTeamMatchupData.js
import { useState, useEffect } from 'react';
import axios from 'axios';

const API_BASE = 'http://127.0.0.1:5000/api';

export const useTeamMatchupData = (homeTeam, awayTeam, gameDate) => {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    const fetchMatchupData = async () => {
      try {
        setLoading(true);
        
        const [
          homeTraditional,
          awayTraditional,
          homePlaytypes,
          awayPlaytypes,
          homeAssists,
          awayAssists,
          homeShooting,
          awayShooting,
          historicalH2H,
          playerMatchups
        ] = await Promise.all([
          axios.get(`${API_BASE}/teams/stats?team=${homeTeam}&category=Traditional`),
          axios.get(`${API_BASE}/teams/stats?team=${awayTeam}&category=Traditional`),
          axios.get(`${API_BASE}/teams/stats?team=${homeTeam}&category=Playtypes`),
          axios.get(`${API_BASE}/teams/stats?team=${awayTeam}&category=Playtypes`),
          axios.get(`${API_BASE}/teams/stats?team=${homeTeam}&category=Assists`),
          axios.get(`${API_BASE}/teams/stats?team=${awayTeam}&category=Assists`),
          axios.get(`${API_BASE}/teams/stats?team=${homeTeam}&category=Zone Shooting`),
          axios.get(`${API_BASE}/teams/stats?team=${awayTeam}&category=Zone Shooting`),
          axios.get(`${API_BASE}/games/game_logs?teams_against[]=${homeTeam}&teams_against[]=${awayTeam}`),
          fetchPlayerMatchups(homeTeam, awayTeam)
        ]);

        const processedData = {
          teams: {
            home: processTeamData(homeTraditional.data, homePlaytypes.data, homeAssists.data, homeShooting.data),
            away: processTeamData(awayTraditional.data, awayPlaytypes.data, awayAssists.data, awayShooting.data)
          },
          gameInfo: {
            date: gameDate,
            time: '7:30 PM PT',
            venue: 'Crypto.com Arena',
            tv: 'ESPN',
            radio: 'ESPN LA'
          },
          quickStats: calculateMatchupEdges(homeTraditional.data, awayTraditional.data),
          offense: {
            homeTeam: processOffensiveData(homePlaytypes.data, homeAssists.data, homeShooting.data),
            awayTeam: processOffensiveData(awayPlaytypes.data, awayAssists.data, awayShooting.data),
            leagueAverages: getLeagueAverages()
          },
          playerMatchups: playerMatchups.data,
          history: processHistoricalData(historicalH2H.data),
          predictions: generatePredictions(homeTraditional.data, awayTraditional.data)
        };

        setData(processedData);
      } catch (err) {
        setError(err.message);
        console.error('Error fetching matchup data:', err);
      } finally {
        setLoading(false);
      }
    };

    if (homeTeam && awayTeam) {
      fetchMatchupData();
    }
  }, [homeTeam, awayTeam, gameDate]);

  return { data, loading, error };
};

// Helper function to calculate matchup edges
const calculateMatchupEdges = (homeStats, awayStats) => {
  return {
    pace: {
      leader: homeStats.PACE > awayStats.PACE ? 'home' : 'away',
      difference: Math.abs(homeStats.PACE - awayStats.PACE).toFixed(1),
      percentage: Math.max(homeStats.PACE, awayStats.PACE) / Math.min(homeStats.PACE, awayStats.PACE) * 100 - 100
    },
    offense: {
      leader: homeStats.OFF_RATING > awayStats.OFF_RATING ? 'home' : 'away',
      difference: Math.abs(homeStats.OFF_RATING - awayStats.OFF_RATING).toFixed(1),
      percentage: (Math.max(homeStats.OFF_RATING, awayStats.OFF_RATING) / Math.min(homeStats.OFF_RATING, awayStats.OFF_RATING) - 1) * 100
    },
    defense: {
      leader: homeStats.DEF_RATING < awayStats.DEF_RATING ? 'home' : 'away',
      difference: Math.abs(homeStats.DEF_RATING - awayStats.DEF_RATING).toFixed(1),
      percentage: (Math.max(homeStats.DEF_RATING, awayStats.DEF_RATING) / Math.min(homeStats.DEF_RATING, awayStats.DEF_RATING) - 1) * 100
    },
    rebounding: {
      leader: homeStats.REB_PCT > awayStats.REB_PCT ? 'home' : 'away',
      difference: Math.abs(homeStats.REB_PCT - awayStats.REB_PCT).toFixed(1),
      percentage: (Math.max(homeStats.REB_PCT, awayStats.REB_PCT) / Math.min(homeStats.REB_PCT, awayStats.REB_PCT) - 1) * 100
    }
  };
};
```

### 2. Player Matchup Fetching
```jsx
// utils/playerMatchupUtils.js
export const fetchPlayerMatchups = async (homeTeam, awayTeam) => {
  try {
    const [homeRoster, awayRoster] = await Promise.all([
      axios.get(`${API_BASE}/players?team=${homeTeam}`),
      axios.get(`${API_BASE}/players?team=${awayTeam}`)
    ]);

    const keyMatchups = identifyKeyMatchups(homeRoster.data, awayRoster.data);
    
    const matchupPromises = keyMatchups.map(async (matchup) => {
      const [homePlayerStats, awayPlayerStats, h2hHistory] = await Promise.all([
        axios.get(`${API_BASE}/players/profile?player_name=${matchup.homePlayers.name}&category=Traditional`),
        axios.get(`${API_BASE}/players/profile?player_name=${matchup.awayPlayer.name}&category=Traditional`),
        fetchH2HPlayerHistory(matchup.homePlayer.name, matchup.awayPlayer.name)
      ]);

      return {
        homePlayer: { ...matchup.homePlayer, stats: homePlayerStats.data },
        awayPlayer: { ...matchup.awayPlayer, stats: awayPlayerStats.data },
        matchupRating: calculatePlayerMatchupRating(homePlayerStats.data, awayPlayerStats.data),
        history: h2hHistory,
        battleType: determineBattleType(matchup.homePlayer, matchup.awayPlayer)
      };
    });

    return await Promise.all(matchupPromises);
  } catch (error) {
    console.error('Error fetching player matchups:', error);
    return [];
  }
};

const identifyKeyMatchups = (homeRoster, awayRoster) => {
  // Logic to identify key position battles and star player matchups
  const keyPositions = ['PG', 'SG', 'SF', 'PF', 'C'];
  const matchups = [];

  keyPositions.forEach(position => {
    const homePlayer = homeRoster.find(p => p.position === position && p.isStarter);
    const awayPlayer = awayRoster.find(p => p.position === position && p.isStarter);
    
    if (homePlayer && awayPlayer) {
      matchups.push({ homePlayer, awayPlayer, position });
    }
  });

  // Add star player matchups
  const homeStars = homeRoster.filter(p => p.ppg > 25 || p.isAllStar);
  const awayStars = awayRoster.filter(p => p.ppg > 25 || p.isAllStar);
  
  homeStars.forEach(homeStar => {
    const bestMatchup = awayStars.find(awayStar => 
      Math.abs(homeStar.usage - awayStar.usage) < 5
    );
    if (bestMatchup) {
      matchups.push({ 
        homePlayer: homeStar, 
        awayPlayer: bestMatchup, 
        battleType: 'star' 
      });
    }
  });

  return matchups.slice(0, 6); // Return top 6 matchups
};
```

## UI Component Library

### 1. StatCard Component
```jsx
// components/ui/StatCard.jsx
import React from 'react';
import { Card } from 'react-bootstrap';
import ComparisonBar from './ComparisonBar';

const StatCard = ({ title, leader, value, description, percentage, icon }) => {
  return (
    <div className="stat-card">
      <Card className="premium-card h-100">
        <Card.Body className="p-3">
          <div className="stat-header d-flex align-items-center mb-2">
            {icon && <span className="stat-icon me-2">{icon}</span>}
            <h6 className="text-h2 mb-0">{title}</h6>
          </div>
          
          <div className="stat-value mb-2">
            <span className="leader-team">{leader.toUpperCase()}</span>
            <span className="advantage-value"> +{value}</span>
          </div>
          
          <ComparisonBar percentage={percentage} leader={leader} />
          
          <p className="text-caption text-muted mt-2 mb-0">
            {description}
          </p>
        </Card.Body>
      </Card>
    </div>
  );
};

export default StatCard;
```

### 2. ComparisonBar Component
```jsx
// components/ui/ComparisonBar.jsx
import React from 'react';

const ComparisonBar = ({ percentage, leader, showLabels = false }) => {
  const homePercentage = leader === 'home' ? percentage : 100 - percentage;
  const awayPercentage = 100 - homePercentage;

  return (
    <div className="comparison-bar-container">
      {showLabels && (
        <div className="comparison-labels d-flex justify-content-between mb-1">
          <span className="text-caption">Home</span>
          <span className="text-caption">Away</span>
        </div>
      )}
      
      <div className="comparison-bar-track">
        <div 
          className="comparison-bar-fill home"
          style={{ width: `${homePercentage}%` }}
        />
        <div 
          className="comparison-bar-fill away"
          style={{ width: `${awayPercentage}%`, left: `${homePercentage}%` }}
        />
      </div>
      
      {!showLabels && (
        <div className="comparison-indicator">
          <span className="indicator-dot" style={{ left: `${percentage}%` }} />
        </div>
      )}
    </div>
  );
};

export default ComparisonBar;
```

### 3. Court Visualization Component
```jsx
// components/ui/CourtVisualization.jsx
import React from 'react';

const CourtVisualization = ({ homeData, awayData, metric = 'fg_pct' }) => {
  const zones = [
    { name: 'Paint', key: 'paint', x: 50, y: 75, size: 'large' },
    { name: 'At Rim', key: 'rim', x: 50, y: 85, size: 'medium' },
    { name: 'Arc 3', key: 'arc3', x: 50, y: 25, size: 'large' },
    { name: 'Corner 3 L', key: 'corner3_l', x: 15, y: 45, size: 'small' },
    { name: 'Corner 3 R', key: 'corner3_r', x: 85, y: 45, size: 'small' },
    { name: 'Mid Range L', key: 'mid_l', x: 25, y: 55, size: 'medium' },
    { name: 'Mid Range R', key: 'mid_r', x: 75, y: 55, size: 'medium' }
  ];

  const getZoneColor = (homeValue, awayValue) => {
    const diff = homeValue - awayValue;
    if (diff > 5) return '#10b981'; // Green for home advantage
    if (diff < -5) return '#ef4444'; // Red for away advantage
    return '#f59e0b'; // Amber for neutral
  };

  const getZoneRadius = (value, size) => {
    const baseRadius = size === 'large' ? 8 : size === 'medium' ? 6 : 4;
    return baseRadius + (value / 10); // Scale based on value
  };

  return (
    <div className="court-visualization">
      <svg viewBox="0 0 100 100" className="court-svg">
        {/* Court outline */}
        <rect x="10" y="10" width="80" height="80" 
              fill="none" stroke="var(--text-secondary)" strokeWidth="0.5" />
        
        {/* Free throw lane */}
        <rect x="35" y="70" width="30" height="20" 
              fill="none" stroke="var(--text-secondary)" strokeWidth="0.3" />
        
        {/* Three-point arc */}
        <path d="M 15 45 Q 50 15 85 45" 
              fill="none" stroke="var(--text-secondary)" strokeWidth="0.3" />
        
        {/* Zone indicators */}
        {zones.map(zone => {
          const homeValue = homeData[zone.key] || 0;
          const awayValue = awayData[zone.key] || 0;
          const color = getZoneColor(homeValue, awayValue);
          const radius = getZoneRadius(Math.max(homeValue, awayValue), zone.size);
          
          return (
            <g key={zone.key}>
              <circle
                cx={zone.x}
                cy={zone.y}
                r={radius}
                fill={`${color}40`} // 40% opacity
                stroke={color}
                strokeWidth="1"
                className="zone-indicator"
              />
              <text
                x={zone.x}
                y={zone.y + 1}
                textAnchor="middle"
                className="zone-label"
                fontSize="3"
                fill="var(--text-primary)"
              >
                {Math.max(homeValue, awayValue).toFixed(0)}
              </text>
            </g>
          );
        })}
      </svg>
      
      <div className="court-legend mt-2">
        <div className="legend-item">
          <span className="legend-color" style={{ backgroundColor: '#10b981' }}></span>
          <span className="text-caption">Home Advantage</span>
        </div>
        <div className="legend-item">
          <span className="legend-color" style={{ backgroundColor: '#ef4444' }}></span>
          <span className="text-caption">Away Advantage</span>
        </div>
        <div className="legend-item">
          <span className="legend-color" style={{ backgroundColor: '#f59e0b' }}></span>
          <span className="text-caption">Neutral</span>
        </div>
      </div>
    </div>
  );
};

export default CourtVisualization;
```

## Responsive Design Implementation

### Mobile-First CSS Grid System
```css
/* Mobile-first responsive grid */
.matchup-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 1rem;
  padding: 1rem;
}

/* Tablet Layout */
@media (min-width: 768px) {
  .matchup-grid {
    grid-template-columns: repeat(2, 1fr);
    gap: 1.5rem;
    padding: 1.5rem;
  }
  
  .matchup-header .team-info {
    flex-direction: row;
    text-align: left;
  }
  
  .tab-navigation {
    flex-direction: row;
  }
}

/* Desktop Layout */
@media (min-width: 1200px) {
  .matchup-grid {
    grid-template-columns: repeat(4, 1fr);
    gap: 2rem;
    padding: 2rem;
  }
  
  .analysis-section {
    grid-template-columns: 1fr 1fr;
  }
  
  .court-visualization svg {
    max-width: 400px;
    height: auto;
  }
}

/* Touch-friendly mobile interactions */
@media (max-width: 767px) {
  .tab-button {
    min-height: 48px;
    font-size: 14px;
    padding: 12px 8px;
  }
  
  .premium-card {
    border-radius: 8px;
  }
  
  .stat-card {
    margin-bottom: 1rem;
  }
  
  .player-matchup-card {
    padding: 1.5rem;
  }
  
  .comparison-bar-container {
    margin: 0.5rem 0;
  }
}

/* Performance optimizations */
.premium-card {
  contain: layout style;
  will-change: transform;
}

.tab-content {
  contain: layout;
}

.chart-container {
  contain: size layout;
}
```

## Performance & Optimization

### 1. Code Splitting
```jsx
// Lazy loading for tab components
const OffenseTab = React.lazy(() => import('./tabs/OffenseTab'));
const DefenseTab = React.lazy(() => import('./tabs/DefenseTab'));
const MatchupsTab = React.lazy(() => import('./tabs/MatchupsTab'));
const TrendsTab = React.lazy(() => import('./tabs/TrendsTab'));
const HistoryTab = React.lazy(() => import('./tabs/HistoryTab'));

// Usage with Suspense
<Suspense fallback={<TabLoadingSpinner />}>
  {renderTabContent()}
</Suspense>
```

### 2. Memoization Strategy
```jsx
// Memoized expensive calculations
const QuickStatsOverview = React.memo(({ matchupEdges, keyIndicators }) => {
  const memoizedEdges = useMemo(() => 
    calculateMatchupEdges(matchupEdges), 
    [matchupEdges]
  );
  
  return (
    // Component JSX
  );
});

// Memoized chart data processing
const useChartData = (rawData) => {
  return useMemo(() => {
    return processChartData(rawData);
  }, [rawData]);
};
```

### 3. API Response Caching
```jsx
// Cache strategy for repeated requests
const cache = new Map();

const fetchWithCache = async (url, ttl = 300000) => { // 5 minute TTL
  const cached = cache.get(url);
  
  if (cached && Date.now() - cached.timestamp < ttl) {
    return cached.data;
  }
  
  const response = await axios.get(url);
  cache.set(url, {
    data: response.data,
    timestamp: Date.now()
  });
  
  return response.data;
};
```

## Integration with Existing Architecture

### 1. Route Integration
```jsx
// Add to existing routing structure
import TeamMatchupPage from './components/TeamMatchupPage';

// In main routing component
<Route 
  path="/matchup/:homeTeam/:awayTeam" 
  element={<TeamMatchupPage />} 
/>

// Navigation from game logs page
const navigateToMatchup = (homeTeam, awayTeam) => {
  navigate(`/matchup/${encodeURIComponent(homeTeam)}/${encodeURIComponent(awayTeam)}`);
};
```

### 2. State Management Integration
```jsx
// If using existing context/state management
const { teams, players, gameData } = useContext(NBADataContext);

// Pass existing data to matchup page
<TeamMatchupPage 
  homeTeam={teams.home}
  awayTeam={teams.away}
  existingGameData={gameData}
/>
```

### 3. Styling Integration
```css
/* Import existing styles */
@import './GameLogFilter.css';

/* Extend existing theme variables */
.team-matchup-page {
  @extend .game-log-container;
  
  .premium-card {
    @extend .dark-card;
  }
  
  .tab-navigation {
    @extend .filter-toggle-group;
  }
}
```

This comprehensive specification provides everything needed to implement a premium, Robinhood-inspired team matchup page that seamlessly integrates with your existing NBA application architecture while providing rich analytical insights and a modern user experience.