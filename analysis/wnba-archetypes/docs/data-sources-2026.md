# 2026 WNBA data sources for scoring archetypes and matchup analysis

**Research date:** 2026-08-11
**Scope:** Independent WNBA scoring archetypes and opponent matchup comparisons for PTS/min and FGA/min. This report separates observed data from derived estimates and archetype features from contextual or validation variables.

## Executive conclusion

There are useful additions beyond Synergy and PBP Stats, but no single free source is complete, current, reliable, and clearly licensed for betting use.

For a private methodology prototype, the strongest accessible combination is WNBA Stats game logs and shot data, PBP Stats possessions, official WNBA injury reports, and carefully freshness-checked SportsDataverse files. For a real betting workflow, use a provider contract that expressly permits betting analysis—most plausibly Sportradar or SportsDataIO for official statistics/injuries and SportsDataIO or The Odds API for timestamped player props and closing lines. The [WNBA Terms of Use](https://www.wnba.com/terms-of-use) expressly prohibit using WNBA-provided statistics in connection with gambling, including legal gambling, so public accessibility is not equivalent to licensed betting use.

The most valuable new predictors are role and availability variables, not more clustering dimensions: starter status, expected minutes, teammates unavailable, vacated FGA/min, roster transactions, opponent lineup composition, and timestamped market totals/props. RAPM, DARKO, lineup ratings, odds, injuries, and rest-of-roster context should not define the scoring archetypes.

## Ranking

### Use now for research/prototyping

1. **PBP Stats:** possession boundaries, lineups, shot zones, putbacks, assisted status, and possession start type. It supports WNBA data and exposes detailed possession-derived fields through its [documentation](https://pbpstats.readthedocs.io/en/latest/) and [API](https://api.pbpstats.com/docs). Use it alongside, not instead of, box/game logs.
2. **Official WNBA injury reports:** the league requires status reporting by 5 p.m. local time the day before most games and by 1 p.m. for the second game of a back-to-back; reports are continually updated. This is the best free availability source, although the public page/PDF workflow is not a stable documented API. [Official injury-report policy and page](https://www.wnba.com/wnba-injury-report).
3. **SportsDataverse releases, with freshness tests:** convenient CSV/Parquet/RDS files for ESPN and WNBA Stats PBP, shots, logs, rosters, lineups, officials, schedules, college data, and player-impact models. The project documents its pipelines and schedules in its [data repository](https://github.com/sportsdataverse/sportsdataverse-data), and the access code is in [wehoop](https://github.com/sportsdataverse/wehoop). Treat the release timestamp, maximum game date, game count, and schema as separate health checks.
4. **ESPN first-party JSON as a cross-check only:** live game summaries expose exact postgame starters and game-level opening/closing odds, while ESPN also exposes current injuries and transactions. These endpoints are useful for detecting errors in bulk files, but Disney's terms restrict automated extraction and dataset construction without written permission; therefore they are not a clean production data license. [Disney/ESPN terms](https://disneytermsofuse.com/english/).

### Useful optional paid/licensed sources

1. **Sportradar WNBA v8:** official-source schedules, rosters, profiles, injuries, transfers, PBP, game summaries, and seasonal statistics. Sportradar says its WNBA data is collected by on-venue statisticians and monitored internally, and publishes endpoint update frequencies. [WNBA API basics](https://developer.sportradar.com/basketball/docs/wnba-ig-api-basics), [update frequencies](https://developer.sportradar.com/basketball/docs/wnba-ig-update-frequencies), and [historical availability](https://developer.sportradar.com/basketball/docs/wnba-ig-historical-data). Production and betting rights depend on the contract; a trial key does not establish production rights.
2. **SportsDataIO WNBA:** one licensed family for injuries, player availability, live/final box scores, game lines, line movement, player props, and closing prices. Its WNBA workflow states that pregame lines and player props include opening, movement, and closing timestamps, while confirmed pregame WNBA lineups and depth charts are not supplied. Its ordinary free trial does not provide WNBA access. [WNBA workflow](https://sportsdata.io/developers/workflow-guide/wnba), [API documentation](https://sportsdata.io/developers/api-documentation/wnba), and [data dictionary](https://sportsdata.io/developers/data-dictionary/wnba).
3. **The Odds API:** current WNBA game markets plus paid historical odds and player-prop access. Historical featured WNBA markets go back to May 2022 and other markets to May 2023. Its terms permit analytical and commercial applications but prohibit raw-data resale or redistribution. [WNBA coverage](https://the-odds-api.com/sports/wnba-odds.html), [API guide](https://the-odds-api.com/liveapi/guides/v4/), and [terms](https://the-odds-api.com/terms-and-conditions.html).
4. **RotoWire syndication:** paid injuries, transactions, lineup changes, expected/confirmed lineups, and projections; its own product material describes projections as fantasy- and wagering-optimized. This can be particularly useful for expected minutes and late availability alerts. [Official content-syndication overview](https://www.rotowire.com/advertise/content-syndication.pdf?v2026=).
5. **Sportradar NCAA Women's Basketball plus WNBA ID linking:** a licensed route to college and rookie priors. Sportradar added NCAA/G League reference IDs to WNBA player profiles, permitting cross-league joins when available. [Official player-ID linking notice](https://developer.sportradar.com/sportradar-updates/changelog/basketball-apis-player-id-linking).

### Skip or use only as a warning signal

- Public WNBA `SynergyPlayTypes` wrappers when the underlying endpoint is empty. `nba_api`, `wehoop`, `nbastatpy`, and similar packages do not create an independent feed; they wrap upstream endpoints.
- Public WNBA tracking endpoints that return zero rows for catch-and-shoot, pull-ups, drives, or defensive matchup data. An empty response is not evidence that the metric is zero.
- ESPN spread columns embedded in the 2026 SportsDataverse PBP bulk file. They are demonstrably placeholders, not odds.
- Unversioned scraping projects, Kaggle snapshots, or RapidAPI relays with unclear provenance, freshness, or betting rights.
- Basketball-Reference as a primary 2026 feed. It can help with historical checks, but adds little unique scoring-volume information and is still a scraped website rather than a licensed betting API.
- Any provider's win probability or impact metric as an archetype feature. These describe performance/context, not scoring style or volume.

## Source matrix

| Source | 2026 observation on 2026-08-11 | Access/cost | Useful fields | Role in workflow | Recommendation |
|---|---|---|---|---|---|
| WNBA Stats / WNBA CDN | Public endpoints and current-season schedule exist; some tracking/play-type endpoints are empty or fragile | Free, undocumented web endpoints | Game logs, box totals, shot coordinates/zones, usage/scoring splits, schedule | Core observed scoring volume and shot mix; schedule validation | Research only because of explicit WNBA betting restriction |
| Official WNBA injury reports | Current page states formal deadlines and continual updates | Free web page/PDF; no stable public API documented | Player, team, game, status, reason, update time | Availability and expected-minutes context | Use now; archive timestamped snapshots |
| PBP Stats | Current WNBA possessions/totals available | Free public API at time of test; no production SLA established | Lineups, possessions, shot zones, assisted/unassisted, putbacks, start type | Derive early offense, second chance, and on-court context | Use now for prototype; clarify license before production |
| SportsDataverse ESPN WNBA bulk | Assets updated 2026-08-11, but tested PBP content ended 2026-08-01 | Free GitHub releases; MIT software does not grant upstream ESPN rights | PBP, shots, boxes, schedules, rosters, officials | Convenient cache and cross-check | Use only with automated freshness/schema tests |
| SportsDataverse WNBA Stats bulk | 2026 shots, logs, lineups, rosters, and model assets exist; several tested assets were last refreshed 2026-07-29 | Free GitHub releases | Official-derived logs/shots/lineups plus model outputs | Reproducible snapshots and diagnostics | Optional cache; never infer freshness from asset presence alone |
| ESPN live JSON | Current injuries, transactions, postgame starters, and game open/close lines observed | No key, but not a documented public developer product | Injury comments/status, starter flags, transactions, game odds | Cross-source validation and emergency gap detection | Do not base a production betting product on it without permission |
| Sportradar WNBA v8 | Current documented product; credential required | Trial/licensed | Official PBP, injuries, transfers, rosters, game/season stats | Production-grade observed and context data | Best licensed all-around candidate |
| SportsDataIO WNBA | Current docs cover injuries, live/final stats, lines and player props | Paid; normal free trial excludes WNBA | Availability, box/PBP, line movement, props, close | Betting labels, expected role, and licensed data | Strong paid candidate |
| The Odds API | WNBA current and historical market coverage documented | Key required; history/props plan-dependent | Timestamped books, lines, prices, events, props | Backtesting against close and measuring market value | Strong odds-specific candidate |
| RotoWire | WNBA coverage should be confirmed in contract/feed schema | Paid syndication | Injuries, news, lineups, projections | Expected minutes and late-breaking role context | Useful optional supplement |
| ESPN/SportsDataverse WBB | Historical/current college PBP and boxes are published through wehoop pipelines | Free but subject to ESPN upstream terms | College FGA/min, shot mix, role, competition history | Rookie priors and cold-start archetypes | Prototype only; licensed NCAA feed preferred for production |
| SportsDataverse player impact | `wnba_player_impact_2026` exists | Free release | RAPM, adjusted RAPM, SPM, BPM, WAR, DARKO | Reliability/context diagnostics and shrinkage priors | Never use as archetype inputs |

## Validated public-data findings

### SportsDataverse bulk freshness and false odds

The GitHub release API showed these 2026 assets on 2026-08-11: `espn_wnba_pbp/play_by_play_2026.csv`, `espn_wnba_shots/shots_2026.csv`, and `espn_wnba_player_boxscores/player_box_2026.csv`. Direct parsing of the PBP asset produced **90,198 records from 219 games with a maximum game date of 2026-08-01**, even though the asset itself had been updated on 2026-08-11. This proves that file modification time is not sufficient evidence of game freshness. The authoritative owner-side release location is the [SportsDataverse data releases repository](https://github.com/sportsdataverse/sportsdataverse-data/releases).

All 219 games in that PBP file had `game_spread=2.5`, `home_team_spread=2.5`, `home_favorite=true`, and `game_spread_available=false`. These values are internally contradictory and invariant, so they must be treated as placeholders or a processing bug—not historical odds. Real odds should come from a timestamped odds feed.

### ESPN injury schema drift

The live [ESPN WNBA injury endpoint](https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/injuries) returned an `injuries` array grouped by team, with each group containing a nested `injuries` array. At the same time, the current [wehoop injury helper source](https://github.com/sportsdataverse/wehoop/blob/main/R/espn_basketball_injuries_helpers.R) iterates the top-level array as though every element directly contains `team`, `athlete`, and `injury`. Therefore, test the wrapper's non-null IDs and row counts before using it; calling the helper successfully would not prove correct parsing.

### Exact postgame starters and game odds

The first-party ESPN game-summary JSON for a completed 2026 game contained a Boolean `starter` field for every box-score athlete and a `pickcenter` object with provider, opening line, closing line, moneyline, total, and prices. These are observed provider records, unlike the placeholder fields in the bulk PBP. They are useful validation data, but automated collection is restricted by [Disney's terms](https://disneytermsofuse.com/english/) and the endpoint has no public archival SLA.

### Player-impact model data

`wnba_player_impact_2026.csv` contains `o_rapm`, `d_rapm`, `rapm`, adjusted RAPM, OSPM/DSPM/SPM, OBPM/DBPM/BPM/WAR, and DARKO filtered/projected ratings and uncertainty. The publisher's [model builder](https://github.com/sportsdataverse/wehoop-wnba-stats-data/blob/main/python/wnba_model_publish/builders.py) documents that these are derived from possession lineups and box logs, with cross-season priors and modeling assumptions. They are inferred metrics—not directly observed events—and belong in diagnostics, role priors, or sensitivity analysis, not in the scoring-archetype clustering matrix.

## Recommended data stack

### Private research prototype

1. Ingest official/PBP game logs and shots for FGA, 3PA, FTA, points, minutes, opponents, game IDs, and shot zones.
2. Add PBP Stats possession and lineup data for putbacks, second chances, assisted status, possession starts, and on-court combinations.
3. Capture every official WNBA injury-report version with retrieval timestamp and game linkage.
4. Use SportsDataverse only as a cache; reject an update unless its maximum game date and unique-game count match the schedule/source expectation.
5. Cross-check starters, transactions, and selected completed games against first-party ESPN JSON, without treating that access as production-licensed.
6. Store raw snapshots immutably so historical analyses use only information available before the game.

### Licensed betting workflow

1. Contract for WNBA statistical and availability rights with Sportradar or SportsDataIO; confirm that the agreement expressly permits the intended betting use and derived-model storage.
2. Obtain timestamped player props and open/close lines from SportsDataIO or The Odds API.
3. Add Synergy only if exact play-type possession labels materially improve out-of-sample performance.
4. Add RotoWire projections/news only if its expected-minutes and alert latency improve forecasts after accounting for the licensing cost.
5. Use a licensed NCAA women's feed and stable player IDs for rookies; Sportradar's cross-league reference IDs are the cleanest documented join.

## Concrete variables

### Archetype-construction features

These describe scoring style and volume and intentionally exclude efficiency:

- FGA/min, 3PA/min, and FTA/min.
- Rim, short-midrange, long-midrange, corner-three, and above-break-three attempt shares.
- Assisted and unassisted two-point/three-point attempt or make shares, depending on what is observed consistently.
- Putback FGA share and other second-chance FGA share.
- Early-offense FGA share after live-ball turnovers and defensive rebounds, using a predeclared timing window and sensitivity tests.
- Half-court non-putback FGA share.
- Points-in-paint, fast-break, second-chance, and points-off-turnover shares only when the underlying feed reports them consistently.

Do not include FG%, eFG%, TS%, PPP, shot quality, plus-minus, RAPM, WAR, DARKO, height, home status, or back-to-back status in clustering.

### Matchup/context features

Keep these outside clustering and display them beside the archetype-vs-team result:

- Player active/status flag and time of last official update.
- Expected starter and expected minutes; postgame starter is an observed validation label.
- Number and typical minutes/FGA of unavailable teammates.
- Vacated team FGA/min and usage opportunity from unavailable players.
- Days since roster transaction and hardship/rest-of-season contract status.
- Expected opponent starting lineup and minutes-weighted archetype composition.
- Teammate/opponent on-court combinations and lineup continuity.
- Team implied total, game total, spread, player's points/FGA-related prop, price, book, and exact snapshot time.
- Opening-to-closing prop/price movement, used as an evaluation target rather than an archetype feature.
- Rookie prior from college FGA/min, 3PA/FGA, FTA/FGA, shot-zone shares, and role/minutes—never college efficiency as an archetype dimension.
- RAPM/DARKO/BPM and lineup ratings only as contextual reliability or shrinkage diagnostics.

### Primary matchup outputs

Continue reporting minutes-weighted archetype performance relative to that archetype's league average, separately for PTS/min, FGA/min, 3PA/min, FTA/min, rim FGA/min, early-offense FGA/min, and putback FGA/min. Do not silently adjust the headline index for home court, back-to-backs, odds, injuries, or impact ratings; expose those as context and sensitivity slices.

## Quality controls and caveats

- **Observed versus inferred:** box events, shots, minutes, starters, official status designations, and posted prices are observed. Possession assignments, lineups reconstructed from substitutions, transition proxies, expected minutes, archetypes, RAPM/DARKO, and matchup indices are derived.
- **No look-ahead:** retain source and retrieval timestamps. A closing line, final starter flag, or late injury update cannot be used to simulate an earlier bet.
- **Freshness:** test maximum game date, unique games, expected schedule coverage, row counts, duplicate keys, and null IDs. Asset timestamps alone are inadequate.
- **Schema drift:** validate nested shapes and semantic invariants. The ESPN/wehoop injury mismatch is a concrete example.
- **Cross-source IDs:** maintain a versioned player/team/game crosswalk; names alone are insufficient, especially for transactions and rookies.
- **Small samples:** shrink or flag subtype-team cells with low minutes. Report raw subtype minutes, player count, games, confidence intervals, and leave-one-player-out sensitivity.
- **Timing proxies:** label PBP-derived fast play as `early offense`, not exact Synergy transition. Test multiple timing thresholds.
- **Market evaluation:** judge predictive value against out-of-sample outcomes and the price available at decision time, not merely whether a direction was correct.
- **Licensing:** public URLs, open-source wrappers, and MIT-licensed transformation code do not necessarily grant rights to the underlying sports data. The WNBA restriction is explicit, and ESPN automation restrictions are material. Obtain provider confirmation for any betting or commercial deployment; this report is technical research, not legal advice.

## Bottom line

The biggest immediate improvement is not another archetype statistic. It is a time-aware availability and role layer built from official injury reports, starters, transactions, and vacated volume, followed by clean market snapshots for evaluation. For archetype construction, PBP-derived early offense and putback/second-chance volume are the strongest additions. For production betting, the public stack should be treated as a prototype and validation environment until the statistics and odds are obtained under licenses that permit the intended use.
