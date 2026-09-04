# Architecture

This document records the interfaces and seams that are expensive to rediscover from code. The endpoint catalog lives in [API_DOCUMENTATION.md](API_DOCUMENTATION.md), and natural-language parsing details live in [NLP_SYSTEM.md](NLP_SYSTEM.md).

## Runtime shape

`app.create_app()` constructs the Flask application, initializes optional dependencies, registers JSON error handlers, and attaches blueprints. `run.py` is the local entry point; Gunicorn imports `wsgi:app` in production.

Blueprints are intentionally thin HTTP adapters:

| Seam | Modules | Responsibility |
| --- | --- | --- |
| HTTP | `app/routes/` | Parse requests, apply auth, call services, shape status codes |
| Business logic | `app/services/` | Fetch, combine, filter, and serialize NBA data |
| Natural language | `app/services/nl_query/`, `nl_service.py`, `llm_service.py` | Deterministic parsing first, optional LLM fallback |
| Infrastructure | `app/utils/` | Database engine, Firebase, Redis, provider HTTP configuration |
| Persistence | `app/models/`, `app/migrations.py` | SQLAlchemy models, sessions, and application-schema migrations |

Application errors cross the HTTP boundary through `app.errors`. Routes and
services may raise `InvalidInputError`, `ResourceNotFoundError`,
`ProviderUnavailableError`, or `InvalidConfigurationError`; the app factory
registers handlers that return the documented `{ "error": { "code", "message"
} }` shape. Optional `detail` values are logged for operators and never sent
to clients.

`app.dependencies.build_dependencies()` is the single production assembly
point for the database engine, cache client, providers, services, durable job
coordinator, and provider-health service. The app factory constructs that graph
once and stores it in `app.extensions["dependencies"]`. Route modules contain
read-only handles that resolve objects from the active app; importing a route
never selects a database, connects to Redis, initializes Firebase, or loads a
parser. Tests can provide a complete replacement graph through the
`DEPENDENCIES` app-factory override without patching module globals.

## Data-source seams

The app reads from five distinct sources:

| Source | Access path | Expected behavior |
| --- | --- | --- |
| Bundled SQLite demo data | `app.utils.db.get_engine()` | Default, offline-capable read path |
| NBA Stats | `app.providers.nba_stats.NBAStatsAdapter` → `nba_api` → `stats.nba.com` | All live NBA calls use one injected, instrumented adapter with schema validation and a process-shared bound; bounded by `NBA_STATS_TIMEOUT_SECONDS` |
| PBP Stats | `app.providers.pbp_stats.PBPStatsAdapter` → shared `requests.Session` → `api.pbpstats.com` | Normalized play-by-play aggregates, refreshes, retries, telemetry, and the separate PBP health probe |
| NBA LiveData | `app.providers.nba_live_data.NBALiveDataBoxscoreAdapter` → shared retrying session → NBA-hosted LiveData S3 | Fail-closed traditional box-score fallback for governed ledger games whose primary PBP evidence is malformed; accepted composite observations retain both source documents |
| RotoWire injuries | gated `app.providers.rotowire.RotoWireInjuryProvider` → injected `requests.Session` | Disabled by default; one current JSON observation only after both the feature and permission gates are explicit |

Redis is an optional cache. Connection failure disables caching without blocking startup. After startup, a Redis connection or timeout error opens a per-process breaker in `NBAGameCache` (`REDIS_FAILURE_COOLDOWN_SECONDS`, 30 s) during which `get`/`set`/`delete` bypass Redis instead of paying the socket timeout on every call; `get_cache_stats()` reports `circuit_open`. OpenAI is an optional fallback for low-confidence natural-language parsing. Firebase is optional for local development but should be configured in production.

The default database URL is `sqlite:///nba_play_types.db`, relative to the current working directory. Run commands from the repository root or set an absolute `DATABASE_URL`.

Runtime configuration is loaded and validated once by
`app.config.settings.load_settings()`. The resulting typed `RuntimeSettings`
object is attached to the app and passed into request services; see
[SETTINGS.md](SETTINGS.md) for the field and environment-variable contract.
The app factory configures Flask-CORS from `RuntimeSettings.cors`; it never
falls back to a wildcard origin. Local development uses the explicit
`http://localhost:3000` default, while production requires
`CORS_ALLOWED_ORIGINS`.

Protected requests verify their Firebase bearer token on every request. The
verified profile is still resolved against the durable user row each time, but
an unchanged row writes `last_login` at most once per 15-minute interval;
profile changes and new users persist immediately. This bounds authentication
write amplification without caching authorization, claims, or revocation
decisions.

### Projection Provider registry

`app.providers.registry` is the single authority on which DFS providers exist.
One `DFSProviderRegistration` carries a provider's name, the callable that
builds its adapter, and the static entry payout tables the provider publishes
outside its API. Everything that needs to name a provider reads the registry
and nothing else: `DFS_ENABLED_PROVIDERS` and `PROJECTION_ARCHIVE_READ_PROVIDER`
validation, adapter construction in `app.dependencies`, the board's known and
disabled provider sets, the `providers` query filter, the projection archive
read and recording scopes, the statistic catalog's provider vocabulary, and the
pytest parameterization of the shared compliance suite.

A name the registry does not admit fails configuration with
`ConfigurationError`. A registration whose builder returns something that is
not a `ProviderSnapshotProvider` fails `build_dependencies` with the same
error, so a nonconforming provider stops the process rather than the first
collection run. `registered_dfs_provider` admits one further registration for
the duration of a block; it exists so onboarding can be demonstrated to be a
registration plus adapter evidence and nothing else.

Onboarding a provider is therefore: one registration, its statistic-catalog
label mappings, and recorded evidence in
`tests/providers/test_dfs_adapter_contract.py`. That suite is parameterized
from the registry and requires, per provider, a complete retrieval, an empty
board, a partial retrieval whose omitted record carries typed coverage, a typed
upstream failure, a market identity that does not move between two retrievals
of one payload, selections whose prices and modifiers are in the closed
vocabularies, a canonical archive document that round-trips byte for byte,
numbers and documents inside the archive's bounds, and one board that reaches
Latest Player Projections, the database-first Player Pool, and a Closing
Projection Set. `tests/providers/fourth_provider.py` is a recorded fourth
provider that passes it with no application code naming it.

### Comparable Projection prices

Providers publish prices in different shapes: Underdog quotes American odds per
selection, Dabble a payout multiplier, PrizePicks nothing at all in its API.
Every `Selection` therefore states one canonical triple, additive to the
existing `american_price`/`decimal_price` fields, which are unchanged:

- `price_kind` ∈ `american`, `decimal`, `multiplier`, `unpriced`
- `price_value` — the exact published `Decimal`, `None` when unpriced
- `price_scope` ∈ `selection` (odds for this leg alone), `entry` (the slip-level
  payout the leg takes part in, which depends on the entry's leg count)

An adapter states the triple only when the provider prices a selection outside
its payload — PrizePicks' entry payout table, declared in the registry. Every
other case is derived once, at the shared normalization seam, from the fields
the adapter already retained: an American price, then a decimal price, then a
single payout-multiplier modifier priced at entry scope, and otherwise
`unpriced`. All the priced sides of one market must agree on kind and scope; a
market whose sides disagree is a `MalformedProviderResponseError`.

`SelectionModifier.kind` is the closed vocabulary `payout_multiplier`,
`line_adjustment`, `promo`; Underdog's `payout_multiplier` and Dabble's
`multiplier` both normalize to `payout_multiplier`, and an unrecognized kind is
a `CoverageRecordMalformed` rather than a retained string. `MarketVariant`
likewise maps PrizePicks' `demon` and `goblin` to `ALTERNATE` and Underdog's
`balanced` to `STANDARD`, always retaining the provider's own word as
`variant_label`. A variant word outside that closed map stays `UNKNOWN`: it is
retained and archived, and it is never targetable, so naming a new provider
word is a reviewed change to one map rather than a silent drop downstream.

An archived observation carries the market's triple in
`projection_observations.price_kind`, `price_value`, and `price_scope`.
`price_value` is null whenever each side of the market states its own number —
two-sided odds — because that is a fact about the sides, which stay in the
archived source document. A market that offers no priced side is evidence
rather than something to select from: it is archived, and it is excluded from
`targetable`, from Latest Player Projections, from the database-first Player
Pool, and from closing membership. A mapping replay recomputes that decision
from the observation's own archived source document — not from the stored price
columns — so a row archived before migration 046 (whose columns were
backfilled to `unpriced`) keeps the price its document still carries, and its
targetability, across the replay.

The canonical archive document carries the triple as schema version 2. A
document is read back and re-encoded in the version it declares, so every
snapshot archived before this change keeps its exact bytes and therefore its
checksum. Changing the canonical form again means bumping
`SNAPSHOT_SCHEMA_VERSION`, adding the new version to
`SUPPORTED_SNAPSHOT_SCHEMA_VERSIONS`, and teaching
`serialize_provider_snapshot` to emit the older shape when asked for it —
never rewriting an archived document in place.

## Provider telemetry and correlation IDs

Every request is correlated with one safe ID. `app.utils.request_id` accepts an
inbound `X-Request-ID` only when it matches `^[A-Za-z0-9._:-]{1,128}$` and
otherwise generates a fresh UUID; the app binds it to `flask.g.request_id` in a
`before_request` and echoes it on the `X-Request-ID` response header. The same
ID flows into provider telemetry events, so a log, a provider event, and a
response header share one correlation key.

External provider invocations and explicit local provider-normalization seams
are wrapped in one structured event (`app.utils.telemetry.ProviderEvent`).
`provider_call` identifies upstream work; `provider_normalization_call` is an
adapter-owned normalization/empty-result decision.  The latter is emitted
only when the local retrieval outcome says no upstream malformed or timeout
event already represents the terminal failure, so one underlying defect is
not counted twice:

| Provider | Seam | Operations |
| --- | --- | --- |
| NBA Stats | `NBAStatsAdapter` (via `nba_api`) | The closed `NBA_STATS_OPERATIONS` catalog in `app.utils.telemetry`: `health_probe`, `player_game_logs`, `player_game_logs_season`, `player_game_logs_recorded`, `player_diets_recorded`, `player_roster`, `player_roster_recorded`, `league_opponent_team_stats`, `league_opponent_shot_chart`, `league_opponent_shooting_zone`, `team_game_log`, `league_player_shot_type`, `synergy_team_play_types`, `synergy_player_play_types`, `player_per36_stats`, `player_totals_stats`, `player_shooting_zone`, `player_shot_chart`, `player_gamelogs_against`, `schedule_whole_season` |
| NBA LiveData | `NBALiveDataBoxscoreAdapter` | `game_boxscore`; used only after primary governed PBP evidence fails validation, with both source documents retained in the accepted composite observation |
| PBP Stats | `PBPTotalsAdapter` (shared retrying session) | The closed `PBP_STATS_OPERATIONS` catalog in `app.utils.telemetry`: `get_totals_player`, `get_totals_player_diet`, `get_totals_opponent`, `health_probe`, `player_game_logs`, `game_player_stats`, `team_game_log` |
| Dabble | `DabbleAdapter` (shared DFS snapshot contract) | Competition discovery, fixture fan-out, and fixture details are upstream invocation events (`competition_lookup`, `competition_fixtures`, `fixture_details`); the bounded snapshot normalization/empty-result decision is an explicit local seam (`snapshot_normalization`). Production requests share one pooled Requests session built once per adapter (pool sized for the capped detail fan-out); explicitly injected sessions serialize only their `get` call. The shared DFS transport owns one safe-GET retry. |
| PrizePicks | `PrizePicksAdapter` (shared DFS snapshot contract) | Projection pagination remains inside the adapter; the closed telemetry operation is `get_snapshot`. No retry strategy is configured. |
| Underdog | `UnderdogAdapter` (shared DFS snapshot contract) | Appearance, player, and game joins remain inside the adapter; the closed telemetry operation is `get_snapshot`. No retry strategy is configured. |
| RotoWire | `RotoWireInjuryProvider` | One league-wide injury-table read with the closed operation `get_injuries`; raw statuses are retained and only Probable, Questionable, Doubtful, and Out normalize canonically. |

The DFS provider seam is `ProviderSnapshotProvider.get_snapshot(query,
context)`. Dabble, PrizePicks, and Underdog accept the same pregame NBA query,
eligible market statuses, and absolute retrieval deadline and return immutable,
market-centric provider snapshots. Canonical athlete, event, and statistic
filters remain central because provider IDs are only evidence until later
resolution. The shared model retains nullable provider identity and typed
source evidence, exact decimal thresholds and modifiers, original labels,
coverage, and complete/partial status. Adapters exclude ineligible offerings
without guessing missing facts; they expose no provider-specific public routes.
`NBAMarketQuery` rejects a non-canonical season before any provider call: the
two-digit end year must be the calendar year after the four-digit start year,
so `2024-99` fails at construction rather than reaching an adapter.

Every provider number — a `MarketThreshold` value, a `SelectionModifier`
value, and a selection's decimal price — is converted once, at that single
boundary, into the **normalized numeric domain**: a finite `Decimal` whose
leading significant digit sits no higher than `1E+128` and whose last written
digit no lower than `1E-128`, the reviewed
`NORMALIZED_DECIMAL_PLACE_LIMIT`. The bound exists because an exact difference
costs one digit per base-ten place between its operands, so two accepted
values whose exponents were far apart would allocate that separation rather
than the digits a provider actually wrote. The range is far wider than any
real projection line, price, or multiplier; a value outside it is one
malformed provider field, refused with a typed `NumericDomainError` — a
`ValueError`, so each adapter's existing conversion into a typed malformed
record and `malformed_record` coverage code applies unchanged, and the message
never quotes the offending value. Membership is decided from a value's own
exponent and digit count, never by materializing the places between them, so
`1E+999999999` is rejected at the cost of its own two digits. Because the
domain is enforced there, nothing the contract accepts can later refuse to
enter a Comparison Group.

A selection's **American price** passes through that same boundary and is then
required to be a whole number: an integer, or a numeric value or numeric string
naming an integer exactly. A fractional value is refused rather than truncated
into a price the provider never quoted, and so are a boolean, a nonfinite value,
and a magnitude outside the normalized numeric domain. Each refusal is the same
`NumericDomainError`, so it reaches an adapter as the `ValueError` it already
converts into one typed malformed record instead of escaping as an
`OverflowError` past that translation.

`ProviderSnapshotCache` is an injected decorator around that seam. It stores
only complete normalized snapshots in Redis under a provider/query key that
includes the adapter-contract version; it never serializes a `DFSBoard`. The
cache sits below Statistic Catalog resolution, so a cached market carries
provider evidence only: a resolved market is refused on the way in, and a wire
value with a `statistic_match` is rejected as corrupt rather than decoded.
Fresh hits retain the snapshot's `retrieved_at` and expose bounded age metadata
as exact decimal seconds counted from whole microseconds, so the cache and the
comparison board state one number for one observation.

`app.domain.freshness` owns that boundary for every seam, and it states it once:
**a fresh window is exclusive at its endpoint and a maximum age is inclusive at
its own.** An observation exactly one fresh window old is therefore a miss
rather than a hit, and exactly `stale_if_error_seconds` old is still a permitted
stale fallback. Both canonical catalog TTLs are maximum ages and are inclusive
in the same way. The module also owns the exact time-window domain every
configured window is admitted through — no finer than one microsecond, no
longer than `1E+9` seconds — enforced at startup, so nothing a configuration
accepted can be refused by a request. Every window reaches its service through
that authority and only then becomes a whole-microsecond `timedelta`: the two
cache windows, both catalog TTLs, and the event-mapping match window, whether
they came from the environment or from a direct constructor override. The same
authority states the cache policy itself — a fresh window may never exceed the
stale-if-error age — wherever a cache is constructed, not only where settings
are read, so an injected coordinator cannot serve a value as fresh past the age
its provider permits it to be served at all. An observation age crosses these
seams the same way: `ProviderOutcome` normalizes `cache_age_seconds` once, on
construction, to an exact finite non-negative decimal, so no reader, comparison,
or serialized document is ever handed a NaN, an infinity, or a number that only
fails when a board finally compares it.
Partial refreshes are returned to the current caller but never written over a
complete value. A complete value past its fresh window is used only as a
stale-if-error fallback after a later expected total refresh failure. Redis
failure bypasses the cache without an in-process stale copy, and
`ProviderSnapshotCacheCoordinator` suppresses duplicate refreshes only within
one worker (there is no distributed lock). The cache is read before a flight is
registered, so a caller whose read missed while an owner was in flight can
still register once that flight retires; it reads the cache again inside its
own flight, before the provider, and adopts a fresh published value rather than
repeating the work. That second read is bounded by the same absolute deadline
as the first, and calls no provider once the budget is gone. A refresh caused
by a failed read does not look again, so Redis failure still bypasses the cache
for that call. Only a complete refresh is ever published, so when the earlier
owner returned a partial snapshot there is nothing to adopt and the later
caller does refresh again. An adopted value is reported as the hit it is, with
the age decided when its freshness was, and is never written back, so its
expiry stays where the publishing refresh put it. One flight shares the whole
cache decision: a follower adopts the owner's result or failure verbatim,
including
its cache status, age, and sanitized refresh-failure provenance, and never
substitutes a stale value from its own Redis read. When the owner's deadline
elapses before an uncancellable refresh finishes, that deadline failure becomes
the flight's decision immediately: the flight stays active, and every follower
receives the owner's failure verbatim however much later its own deadline is.
The late result then only drains and validates the abandoned work and retires
the key — it publishes nothing, and never turns the shared failure into a
success carrying cache provenance the abandoned request never had. A follower's
own deadline still applies to itself alone; it never abandons the refresh.

Publication is decided at a single instant, before the write: work that
finished at or after the deadline is never written, and a value written before
that instant is never retracted, so no concurrent reader can observe a value
that a later cleanup would remove again. A write that itself outlives the
budget still stands, but its caller is not served past its own deadline. The
absolute deadline is also enforced after the Redis read — including after a
read that fails slowly, which calls no provider at all — and before any value
is returned. Only an unusable payload is cleaned up, comparing and deleting
atomically so a newer concurrent value survives.

A cached payload is used only when its bytes are already exactly the canonical
document this codec writes, compared against the stored text rather than a
normalizing re-dump, so surrounding whitespace, reordered keys, and alternate
escapes are all rejected. A duplicate key at any nesting level is rejected
before decoding, because JSON's last-value-wins would otherwise let one payload
carry a second, conflicting document. Anything a constructor would normalize,
drop, canonicalize from an alias, or deduplicate is corrupt too, as is a value
no domain constructor can represent — a wire number whose conversion raises
`OverflowError`, for example. Every such failure is contained at this seam: the
key is deleted and the request becomes a miss. A closed vocabulary the codec
itself wrote — a scoring period, a selection direction — is decoded as the
member it names rather than as a provider label the snapshot never carried, so
the canonical check compares like with like; a provider alias stays a string
and is rejected by that same check.

Cache decisions are recorded once per request as bounded cache counters only;
the cache never emits a provider-operation event, and provider events never
increment the cache counters. A retrieval that makes many upstream calls (the
Dabble fixture-detail fan-out) therefore records many provider events and still
exactly one cache decision.

`DFSBoardService.get_board(query, context)` is the internal collector seam. Its
provider registry is injected explicitly; it never discovers or constructs
providers while collecting. Enabled providers run concurrently behind one
absolute `RetrievalContext` deadline (15 seconds by default), with at most
three concurrent provider workers. The child context is capped at the lesser
of the caller deadline and the configured deadline, never above 15 seconds. A
complete empty snapshot is usable, while
partial snapshots remain one coherent provider observation with their original
`CoverageEvidence`; failed providers cannot remove usable snapshots from other
providers. Expected upstream failures become sanitized `ProviderOutcome`
reason codes (`timeout`, `deadline_exceeded`, `rate_limited`,
`access_denied`, `upstream_error`, or `malformed_response`), while unexpected
implementation defects propagate. Results and disabled-provider metadata are
sorted deterministically. The collector does not build comparison groups.
Before returning a board it does apply
the injected immutable Statistic Catalog to each market, preserving the
provider `StatisticEvidence` and attaching a typed canonical or unmapped
`MatchState`, and — when the governed mapping collaborators are injected — it
observes each market's athlete and event evidence through the resolvers
described below, reporting the governed outcomes on `board.mapping_outcomes` and
`board.event_mapping_outcomes` without ever removing a market.

### Statistic Catalog

`app/config/statistic_catalog.yaml` is the version-controlled source of truth
for the reviewed cross-provider statistic identities. The
`statistic_catalog_schema` loader owns YAML decoding and the implemented schema
version; the shared `app.domain.statistics` module owns the closed
`ScoringPeriod`, `StatisticUnit`, `MatchState`, and `MatchReason` vocabularies
plus the immutable typed `StatisticMatch`, while the catalog service owns
resolution. Schema v1's field vocabulary is exact and closed: a document has
`schema_version` (required, exactly `1`), optional `component_order`, and
`statistics`; each statistic has `id`, `label`, `unit`, `scoring_periods`,
`components`, and optional `provider_mappings`, `comparable`, and
`market_category`. `market_category` is the single governed source of Player
Pool eligibility and its public PTS/REB/etc. spelling. There are no
aliases — `version`, `canonical_statistics`, `canonical_id`, `display_name`,
`name`, `period`, `periods`, `ordered_components`, `mappings`,
`provider_labels`, `comparison_allowed`, and mapping `alias`/`aliases` are all
rejected, so two spellings of one reviewed fact can never disagree. The
document's values are read exactly rather than normalized: identifiers, units,
scoring periods, and provider names must already be the canonical value, labels
must carry no surrounding whitespace, and list fields must be lists (a scalar
`scoring_periods: full_game` or `provider_mappings: {dabble: points}` is a
defect, not a shorthand). Directly constructed `StatisticCatalog` values are
held to the same version.
`StatisticCatalog.load`
validates the document before constructing immutable `CanonicalStatistic`,
`StatisticMapping`, and `StatisticMatch` objects. Application dependency
assembly loads it before provider construction, so malformed schema, duplicate
canonical identities, duplicate/conflicting provider labels, invalid periods or
units, and inconsistent ordered components fail startup clearly. Route imports
never load or mutate the catalog. A market with absent statistic evidence is
given an immutable `UNMAPPED` match with reason `missing_statistic_evidence`
during snapshot resolution, so every market on `board.snapshots` carries an
explicit match; the provider's coverage evidence and observation remain
unchanged.

The catalog maps full-game points, rebounds, assists, three-pointers made,
steals, blocks, turnovers, PRA, PA, PR, RA, STKS, and field-goal,
three-pointer, and two-pointer attempts. Composite identities use
the reviewed component order (`PRA` is points, rebounds, assists) regardless of
the source label order. Dabble, PrizePicks, and Underdog labels are accepted
only when explicitly listed in the definition; provider evidence is matched
case-insensitively and preserves presentation evidence but does not infer
meaning. Scoring periods are resolved in one place — the provider adapter seam
(`resolve_scoring_period` in `app/providers/dfs.py`, applied by
`PlayerProjectionMarket`) — where a per-provider normalization rule governs the
period without rewriting the payload: an **absent** period label resolves to
`ScoringPeriod.FULL_GAME`, because PrizePicks and Underdog send no period field
on their standard full-game props (Dabble derives the same default from a
full-game stat composition); a **present** label maps to its specific period
(e.g. a first half stays first half, never silently full game); and a present
but **unrecognized** label stays `ScoringPeriod.UNKNOWN`. Only absence defaults
to full game — an unrecognized label is never promoted to it. The raw
`scoring_period_label` (including `None` for an absent label) is retained
verbatim as immutable evidence and is never synthesized from the resolved
period, so a resolved market is stable under `replace()` and the archive
round-trip. The catalog never guesses on top of this: a market that arrives as
`ScoringPeriod.UNKNOWN` stays `MatchState.UNMAPPED` with
`MatchReason.UNKNOWN_SCORING_PERIOD` and is not targetable. Unknown labels,
period-specific scoring periods, unit mismatches, and provider
fantasy labels return `MatchState.UNMAPPED` with a closed `MatchReason`, retain
the original provider evidence, and are not included in the board's
canonical-market view. This slice does not create
comparison groups or a public route.

### Live Player Pool

> Retired request-time path. As of the #110 database-only cutover this
> `PlayerPoolService`/`StoredPlayerPoolReader` request-time reader and its
> `player_pool_snapshots` writer are no longer wired into `build_dependencies`.
> The database-only projection reader below is the sole Slate/Matchup/Selection
> source; because nothing constructs the writer, no request refreshes the
> `player_pool_snapshots` table (an injected `LegacyWriteFence` adds defense in
> depth). The description here is retained for history until the table is
> dropped in #111.

`PlayerPoolService` consumes one `DFSBoardService` result for the current
season and the exact canonical game IDs on an ET Slate. It admits only
available, standard, full-game markets whose Statistic Catalog match belongs
to the closed Market Category vocabulary. Governed athlete and event mapping
outcomes join provider identities to canonical NBA player and game IDs; a
market without both joins cannot affect a slate count. Pool membership is the
union by canonical player ID, with deterministic Market Categories and
per-provider provenance retained on each internal `PoolPlayer`.

The Player Pool never synthesizes players. It persists the governed canonical
pool result for each season and exact Slate game set in the application
database; raw provider labels and identities do not enter this store. A
snapshot at most 15 minutes old is reused with its original provider
`retrieved_at` values. This is an inclusive reuse maximum age, not the
exclusive `fresh` window used by the lower provider cache. The first request
past that maximum refreshes lazily. Refresh replacement is atomic, and a
database refresh lease makes callers in different application workers for the
same scope share one refresh. The lease is normalized by the shared time-window
authority and is longer than the board's bounded collection/mapping operation;
publication is still fenced by ownership and expiry. Followers use bounded,
read-only exponential-backoff polling while a lease is healthy and attempt a
write takeover only after expiry. A partial refresh replaces the stored union with only its
usable providers and reports failed providers as `missing`. Only total failure
may stale-serve the preceding snapshot, through an inclusive six-hour maximum,
marking each contributing provider `stale-served`; cached provider outcomes
already marked stale retain that truth. After six hours the pool is
honestly empty and `unavailable`. A usable empty provider snapshot is `fresh`;
a failed provider is `missing`.
Only unanimously fresh observations make the aggregate `fresh`; unanimously
stale-served usable observations make it `stale-served`. Any mix of fresh,
stale-served, and missing observations omits aggregate status for the
frontend's partial/degraded derivation, and total failure is `unavailable`.
Unknown provider stat market
occurrences and unjoined athlete
or event occurrences, plus canonical athletes whose team is not one of the
governed game's teams, are counted in a bounded Player Pool telemetry event,
without
logging names, labels, or provider IDs.
Drop telemetry is slate-scoped: an event must first join to the requested Slate
before unknown-stat and athlete/team counters apply; an unjoined event is
counted separately, while a governed event on another Slate is irrelevant and
not counted. Aggregate pool `retrieved_at` is the oldest usable contributor
snapshot.

#### Projection archive expansion path

Migration 040 adds the durable projection evidence path without replacing the
legacy Player Pool reader. Migration 041 upgrades already-migrated databases
with poll promotion/failure state and per-offering confirmation time; it
backfills poll promotion only when the referenced v40 generation was a winner,
including unchanged confirmations that reference that winning generation and
are not older than its retrieval fence. Older/late unchanged polls and older or
equal-time losing changed generations remain non-promoted, while Latest
confirmation advances only to the newest temporally valid winning poll
retrieval time.
`ProjectionRecordingService.record_snapshot()` and
`record_failed_poll()` are the application recording boundaries and delegate
durable work to `ProjectionArchive`. They accept an already retrieved Complete
or Partial normalized `ProviderSnapshot`, or one bounded failure, and its
canonical season query. They write only when the provider and canonical query
key match the configured `ProjectionArchiveReadScope` set shared with the
request reader; a mismatch is
rejected before persistence instead of creating invisible evidence. The
lower-level archive retains multi-scope capability for internal use. It then
applies the independent `PROJECTION_ARCHIVE_MAX_MARKETS` pre-persistence bound;
the Comparison Board's `DFS_COMPARISON_MAX_MARKETS` never governs archival
evidence. It writes one Provider Poll for every distinct accepted provider
observation. A changed observation writes
one checksummed source-evidence document, immutable market observations, and
one materialization generation atomically. The evidence checksum covers the
entire canonical document, including `retrieved_at`, for later verification. A
normalized snapshot's markets are ordered first by governed provider market
reference and then by complete retained source evidence before serialization,
checksumming, ordinal assignment, or materialization. Provider array order
therefore cannot split one observation identity; supplied IDs, ID-less
references, and distinct variants remain separate deterministic offerings. A
separate query-scoped content checksum excludes only that retrieval timestamp,
so a later poll confirming identical markets and coverage records an unchanged
poll without duplicating the snapshot, observations, or generation when its
governed mapping inputs are also unchanged. Each generation additionally stores
a deterministic materialization checksum over resolved canonical identities,
category authority, targetability, and the other governed fields used by the
read model. If provider content is unchanged but that checksum changes, the
poll outcome is `rematerialized`: no Provider Snapshot is duplicated, while a
new immutable mapped-observation set and generation atomically replace Latest.
The generation and every mapped observation reference the exact accepted poll
that supplied their observation time. Their representative Provider Snapshot
may therefore have an older retrieval time when identical provider content was
deduplicated; `load_source_snapshot()` verifies that representative content,
while the poll link is the authority for the rematerialization event.
A caller
may supply the actual poll
start; otherwise `started_at` stays null rather than inventing a poll window,
and the acceptance time is the completion time. Poll outcomes are `changed`,
`partial`, `rematerialized`, `unchanged`, or `failed`. Failed polls carry a
bounded reason but no snapshot or generation; unchanged evidence points at the
existing immutable snapshot and generation.
Each poll also records whether it promoted materialized state. Valid late polls
remain immutable health evidence, but they cannot lower an offering's
confirmation time or mask an intervening provider failure. The temporal
promotion fence includes the newest completed failed attempt in the same
provider/query scope, using its actual poll start when supplied and otherwise
its honest completion time. The archive reads this chronology only after it
owns the durable scope fence; it does not discard a failure merely because that
failure committed after the waiting ingestion captured its acceptance time.
Evidence retrieved before that attempt remains
auditable as `older_not_promoted`, even when it arrives later, and cannot retire
Latest rows or clear the six-hour failure fallback. Evidence genuinely retrieved
after the failed attempt may promote and restore successful health. Read health
uses that same attempt chronology rather than failure completion order, so a
success retrieved after a failed attempt began remains the recovery even when
the older attempt finishes and is recorded later. Exact failed-attempt replay
retains its original poll identity and cannot change that ordering.
Accepted snapshot identity is provider, governed query, provider retrieval
instant, and exact evidence checksum. Delayed delivery of that same evidence
returns its persisted result without adding a poll, generation, observation,
or Latest mutation, regardless of a different start or acceptance time. Replay
lookup also joins that identity through the persisted snapshot, so poll IDs
created before the transition migration remain idempotent after upgrade. A
newer provider retrieval instant with unchanged content is a distinct health
confirmation. Failed-attempt identity remains separately tied to its actual
attempt timing. Query status filters are sorted exactly as the
Provider Snapshot codec sorts them, so caller order cannot split one archive
scope. `observation_count` always means the number of normalized observations
present in that accepted snapshot, including unchanged attempts; it is not the
number of newly inserted rows.

Provider polling is coordinated by `ProjectionCollectionCoordinator`, which is
constructed once in application dependencies for the dedicated collector
process. The long-lived Railway loop builds settings, Redis/cache clients, and
provider executors once, then invokes that same coordinator on each five-minute
wake; the one-shot command builds one dependency graph for its single attempt.
It reads the canonical Event Catalog before taking the collector lease, so
offseason/no-due runs do not call a provider. The default board-wide
policy is every 30 minutes beginning 24 hours before the earliest non-postponed,
non-started event and every five minutes in the final two hours. Both intervals
and the horizon are configurable. A past scheduled timestamp does not stop the
poll during the bounded delayed-game window: governed event status
(live/final/postponed), rather than tip-off time, closes an active event's
collection window. A still-scheduled row more than the configured horizon past
tip-off is ignored as stale so an abandoned catalog row cannot pin offseason
collection forever. Each wake derives dueness from the last poll plus the
*current* interval, so crossing into the final two hours immediately adopts the
five-minute cadence instead of waiting out an earlier 30-minute deadline. The
one-shot `scripts/collect_projections.py` command uses this coordinator, not a
second ingestion path or an admin refresh route, and exits nonzero when board
collection fails for every due provider. Code-less Event Catalog rows use
recognized live clock text such as `Q3 5:22` as started while unknown clock text
remains conservatively pregame.

Migration 043 adds one singleton `projection_collection_leases` row and
per-provider `projection_collection_provider_states`. PostgreSQL locks and
fences the lease row using database time, so process-clock skew cannot steal a
live lease or write future poll/backoff state that suppresses the scheduled
collector. Dueness is derived from database-timed `last_poll_at` and
`backoff_until`; no separate poll-deadline column is stored. The owner renews
that lease around board and per-provider work; an expiry or takeover returns
the bounded `busy/lease_lost` outcome. SQLite uses
`BEGIN IMMEDIATE` for local tests but is not evidence for the production
locking contract. A busy or overlapping run is a safe no-op. Provider outcomes
are persisted independently through the existing archive recorder, with
bounded exponential backoff/circuit-open state for failures and a bounded
Provider Poll duration. Stale-if-error cache fallback is failure health rather
than a successful poll, board-wide defects and omitted outcomes create bounded
failure rows, and no failed provider is retried faster than the current healthy
cadence. A failure from one provider cannot suppress another provider's
attempt. The coordinator's adaptive policy is intended to be woken by a
dedicated Railway service every five minutes.
Admin diagnostics expose only provider-safe last-poll/last-changed timestamps,
bounded freshness, failure/backoff state, active/unresolved counts, and lease
state; they never expose raw payloads or source identifiers. Request-time
readers remain database-only and never invoke the coordinator or a provider.
Each newer changed Complete snapshot replaces that provider/query's eligible
set in `latest_player_projections`, so suspended, unresolved, omitted, and
content-reidentified markets cannot leave an older latest pointer behind. An
accepted Partial snapshot replaces only explicitly observed market references;
omitted offerings keep their prior immutable observation and are carried into
the new atomic state generation. Failed polls change no Latest pointers. An
older snapshot remains immutable evidence but cannot move Latest backward. An
identical snapshot at the same observation time is idempotent. Conflicting
documents at the same provider/query observation time use one auditable fence:
the first snapshot accepted while holding the durable scope lock is the sole
promoted generation for that instant. Every later equal-time conflict is
retained as `same_time_not_promoted` evidence and cannot replace Latest, even
when its provider content checksum matches another conflicting evidence
document. An exact-evidence retry cannot rematerialize under changed governed
statistic/category inputs; mapping replay and advancement is a separate
workflow, not an ingestion-idempotency exception.
`older_not_promoted` records the corresponding older-snapshot decision.
Every write first enters a provider/season/query scope transaction. PostgreSQL
serializes both the initial unique scope-lock row insertion race and subsequent
decisions with the durable row selected `FOR UPDATE`. SQLite ignores that
clause, so the archive explicitly begins an `IMMEDIATE` write transaction
before inserting or reading the lock row; this makes
separate engine instances still serialize writers; SQLite therefore has
database-wide rather than per-scope writer concurrency. The in-process lock is
an additional same-engine optimization, not the durable guarantee. The
change decision, evidence inserts, full Latest replacement, and poll commit
therefore serialize as one transaction, and Latest can contain rows from only
one generation for the scope.
Thresholds, selections, modifiers, prices, provider labels, and source
identity round-trip through the existing strict Provider Snapshot codec; raw
upstream payloads are never stored. Governed identities and targetability are
relational fields beside that immutable evidence. Valid unresolved and
non-targetable markets stay in the archive but do not enter the live read
model. Provider market IDs use the established deterministic market-reference
authority, including its content-derived fallback when an ID is absent.
Conflicting repeated provider IDs are already rejected by the normalized
`ProviderSnapshot` contract, while equivalent repeats are collapsed there. If
ID-less markets nevertheless produce the same content-derived reference, every
occurrence stays in immutable observations and the first targetable occurrence
by source ordinal is the sole Latest pointer for that reference.

`LatestProjectionPlayerPoolReader` is the database-only read interface. Its
constructor requires one or more provider scopes sharing one canonical query,
and every read filters by those scopes. Within them it unions current Latest Player
Projections by canonical player, category, and provider without holding a DFS
Board or calling a provider registry. Each offering has a separate confirmation
time. Complete and unchanged Complete polls confirm the whole scope; Partial
polls confirm only included references. Confirmation is live through the
inclusive 15-minute window. After a failed poll it may be stale-served only
through the inclusive six-hour fallback. A disabled provider receives no new
confirmations and expires without deleting Latest or immutable history.
Both the 15-minute live maximum and six-hour failure-fallback maximum enter
through `app.domain.freshness`, including direct reader-constructor overrides.
Populated Latest rows and successful Complete-empty evidence share one
`within_max_age` classification, so both inclusive endpoints are identical and
out-of-domain windows are rejected during construction.
Without an injected test clock the reader uses `app.domain.utc.utc_now`; stored
confirmation timestamps never substitute for the present and therefore cannot
freeze freshness.
`DFS_ENABLED_PROVIDERS` is the sole enablement authority. Dependency assembly
always retains read scopes for every registered archive provider,
independently of the enabled set, so rebuilding
the graph cannot hide a just-disabled provider. Removing the final provider
leaves those historical read scopes intact for expiry and audit, but the
application recording service owns an empty authorization set: every snapshot
and failure submission is rejected before persistence. With a partial enabled
set it authorizes only those provider/query scopes. The deprecated
`PROJECTION_ARCHIVE_READ_PROVIDER` supplies a compatibility/default recorder
identity only and never adds write authority. No disabled provider is required
or receives the failure fallback beyond the 15-minute live window. A targetable
row requires the canonical athlete's name as well as its governed IDs; an ID is
never displayed as a fabricated name. A game with current evidence reports
`state: live` and its oldest included `observed_at`; a game without current
evidence reports `state: missing` and `observed_at: null`. Latest rows and their
promotion-aware poll health are read in one database snapshot. PostgreSQL uses
`REPEATABLE READ`; SQLite uses one explicit read transaction. A writer
committing between those queries therefore cannot pair old Latest state with
new poll health. Every required provider is seeded into the public freshness
document. If it has no eligible row, its entry is `missing` with a null
retrieval time, except that a promoted Complete-empty poll is fresh successful
evidence with its actual retrieval time. A pool backed only by current
Complete-empty evidence is therefore live/fresh with zero players; its
game-specific state is also live with the successful evidence time when every
required provider is currently Complete-empty, so Slate and Matchup describe
the same empty board. A non-required provider's empty evidence remains explicit
at provider level but cannot lift missing required coverage to aggregate live.
With no required providers, eligible empty evidence may remain live only
through the same inclusive 15-minute disabled-provider window. Mixed or
incomplete provider coverage remains missing.
Mixed provider states omit aggregate `status`; `partial` remains reserved for
a multi-game live read containing both live and missing game states. For a
multi-game read spanning live, closing, and missing phases, live evidence
controls aggregate and provider freshness. Closing timestamps never age the
live aggregate, and aggregate `status` is omitted because it does not describe
one uniform phase. If no live evidence exists, a non-empty closing pool takes
precedence over missing games.
`PROJECTION_ARCHIVE_READ_ENABLED` is the operator-controlled activation switch
for the sole reader; it defaults off and an operator flips it on at cutover (see
the rollout step in `docs/SETTINGS.md`). When it is enabled, dependency assembly
gives the same archive reader to Slate and Matchup. Matchup Selection uses a
thin adapter over that reader; scheduled missing state retains its unavailable
contract, while a started game's explicit empty closing set is a valid empty
pool and an outside player is a resource-not-found selection. It does not select
or call a legacy source. One request never combines archive and legacy facts.
The gate is refused when the configured database is the read-only demo fixture,
which cannot contain the archive schema. While the gate is off, an application
database still boots and every read route is honest: Slate and Matchup return
zero targetable players with no `projection_state`, and Matchup Selection
returns `503 provider_unavailable` — never a legacy fallback.

The #110 cutover removed the legacy request-time reader/writer wiring entirely:
`build_dependencies` no longer constructs `PlayerPoolService`,
`StoredPlayerPoolReader`, or `PlayerPoolSnapshotRepository`, so no request can
build a Player Pool or call a projection provider. That construction-removal is
the primary guarantee that no production path writes the empty legacy
`player_pool_snapshots` table. As defense in depth, `PlayerPoolSnapshotRepository`
also accepts an injected `LegacyWriteFence` and, when one is wired, refuses every
persistence method once the `dfs_boards` publication stream is activated. The
table itself is dropped later in #111.

`ClosingProjectionSet` and `ClosingProjectionMembership` are the post-start
read seam. `EventCatalogRepository.publish` persists
`first_observed_started_at` exactly once when governed status first becomes
in-progress or final. `ProjectionCollectionCoordinator.run` is the sole normal
closing-set writer: after a poll, and also when a newly started slate leaves no
provider poll due, it asks `ProjectionRecordingService` to close every enabled
provider scope. The durable observed transition is the start fence. Legacy
started rows without that value fall back to `scheduled_at`; request time is
never used as a fence.

A closing set is unique by provider, season, exact projection `query_key`, and
canonical game, including an explicit empty set. The writer acquires the same
provider/query scope fence used by materialization. Membership rows contain
only foreign-key pointers to immutable `ProjectionObservation` rows; they
never copy, delete, or replace source snapshots. Closing materialization starts
at the newest promoted Complete generation whose provider poll completed no
later than the start fence, then replays its promoted suffix with observations
joined by canonical game ID. Thus replay is bounded in the normal case, never
builds a season-sized generation-ID parameter list, and a delayed provider
delivery can remain immutable archive evidence without entering a set after
the game-start boundary. Repeating a close returns the original set and cannot
move its start time.
Mapping replay generations are explicit deltas even when they reuse a Complete
source snapshot and poll. They are never eligible as the Complete baseline;
the closing fold applies their affected references like a Partial generation,
so a pre-start mapping decision cannot truncate an otherwise complete frozen
board. A Partial poll between the Complete baseline and a replay delta is
folded in the same ordering.
Enabling a provider after a game has closed does not backfill its closing set;
that provider reads `missing` for the game permanently.

The archive reader performs a game-ID-scoped Event Catalog lookup before
choosing a pool; it neither scans a season nor writes archive state. Scheduled
games use the existing live Latest reader and its 15-minute/six-hour
eligibility windows. Started or final games use only immutable closing
pointers, report `state: closing` and provider status `closing`, and do not age
or refresh those pointers. A started game whose collector has not created a
set yet, or whose explicit set has no members, reports `state: missing` with
zero targetable players. Slate and Matchup still return 200, while Selection
keeps scheduled missing-pool behavior and returns `resource_not_found` for a
player outside a started game's derived empty pool. No request path calls a
projection provider, takes an ingestion scope write lock, or creates a closing
set.
Projection Observations retain typed provider identities and an explicit
'resolved'/'unresolved' state in migration 045. Each rematerialized row also
retains its original observation ID and source ordinal, so later athlete, event,
or statistic decisions start from the same immutable provider market while
preserving earlier decisions. Provider-plus-identity indexes bound the history
lookup to observations affected by the decision. A valid normalized market is
archived even when its athlete, event, or statistic identity is unresolved; only
fully resolved, targetable rows enter Latest or Player Pool. An approved athlete
or event mapping invokes the archive's database-only replay seam after the
mapping transaction commits. Statistic Catalog corrections use the same seam.
Replay acquires the existing provider/season/query scope fence, creates or
reactivates the deterministic generation for the resulting materialization, and
batch-replaces only affected Latest references in that transaction. Dense replay
ordinals are generation-local. A provider-reported athlete name remains the
observation name when present; a missing provider name falls back to the
approved canonical name. Canonical IDs and the content-derived reference of an
ID-less market are recomputed from the immutable source market. It reuses the
accepted poll and immutable source snapshot, so source observations, checksums,
and retrieval timestamps are not rewritten. A replayed Latest pointer is
activated at mapping replay time, while its eligibility confirmation remains
the source observation time so the provider board's live window is not reset.
The scope lock records both the active generation, the replay wall clock, and
the source retrieval fence;
an in-flight snapshot retrieved before the mapping decision remains auditable as
non-promoted evidence and cannot erase the recovery when its observations still
carry the pre-decision unresolved identity; evidence with a newer source
retrieval identity may advance normally. Replay preserves the source
`observed_at` for public freshness and does not extend the provider board's
15-minute live window; `confirmed_at` remains the internal eligibility clock.
Ordinary newer promoted polls may advance the Latest read-model `observed_at`,
but replay itself never does so beyond its source observation.
Repeated replay is a no-op. A replay failure after an athlete or event mapping
commit is logged without changing the durable operator decision, and neither
request-time reads nor mapping decisions call a provider. Replay scope
enumeration is provider-wide and has no game filter. Therefore, if a mapping is
approved after a game's closing set is frozen by sibling #108, replay rewrites
the live Latest rows for that provider scope while the frozen membership keeps
the pre-decision observations; this is the intended rule.

### Matchup injury snapshots

`MatchupInjuryService` owns injury collection and Player Pool overrides. The
route and DFS collector do not call RotoWire directly. Before tip, a stored
game snapshot through five minutes old is reused; the first later matchup read
reuses a league observation from the same window or refreshes lazily. Concurrent
lazy refreshes are single-flight within one application worker: a waiter
rechecks durable game and league evidence after acquiring the worker-local
lock. This does not claim cross-worker suppression; another worker relies on
the same durable source recheck. A failed refresh may serve the prior snapshot through an
inclusive 30-minute maximum with `status: stale`. At tip or when the Event
Catalog marks the game final, refreshing stops and the last snapshot is retained.
Its status continues to age: after 30 minutes it is unavailable and cannot
apply badges or overrides, while no post-tip fetch occurs.

`InjurySnapshotRepository` appends each league-wide raw provider list once in
`injury_source_snapshots`; per-game rows store matchup-filtered entries and a
source-snapshot reference. All game reads inside five minutes reuse that
durable source, avoiding N feed requests and N raw copies while each game still
retains the exact evidence it observed at stop time. Ordinary matchup and Slate
reads select only the per-game normalized entries, retrieval time, and unresolved
team count; raw league evidence is loaded only through the repository's explicit
evidence seam. After every transactional reference update, the repository keeps
the 12 newest unreferenced source observations per provider and prunes older
unreferenced rows in that same transaction. A source referenced by any game is
never pruned, so stopped-game historical evidence is retained without allowing
an unused full-feed tail to grow without bound. Normalization retains
RotoWire identity, name, status, reason, and URL.
Reconciliation uses exact normalized athlete name plus canonical team and
season against the active Athlete Catalog; ambiguous or unmatched players stay
visible with a null canonical player ID. The centrally governed RotoWire team
dialects are `GS→GSW`, `NY→NYK`, `SA→SAS`, `PHO→PHX`, and `NO→NOP`; an unknown
team row stays in raw evidence, never receives a guessed matchup team, and is
counted in scalar telemetry. Only a canonical `Out` entry from a usable fresh
or permitted-stale snapshot removes a stored pool
player. Other statuses only supply `injury_badge_ref`; no score, Diet Share, or
role input is modified. Scalar-only injury telemetry counts unmatched entries,
unresolved teams, and board/Out conflicts without retaining player or game identities.
Provider-controlled player links are exposed only when they resolve to HTTPS on
`rotowire.com` or one of its subdomains; every other value falls back to the
fixed injury-report source URL.

`SlateService` uses its existing stored-snapshot injury read to apply the same
Out override to targetable counts. It never refreshes injuries; opening a
Slate cannot fan out league-feed requests across its games. This does not
change the separate Matchup Injury Reports live/snapshot contract.

DFS provider requests use connection/read caps of 3/8 seconds (or the
remaining absolute budget), and safe GET transport retries at most once for a
timeout or one of HTTP 429, 500, 502, 503, or 504. Access-denied, ordinary 4xx, and
malformed responses are not retried. Dabble fixture-detail fan-out remains
bounded at three workers. Late daemon work is ignored after the board deadline
and cannot alter the returned board.

An event records provider, operation, outcome (success/timeout/http_error/
malformed/error), duration, retry count (updated only when a configured
provider retry hook reports a retry), cache status (hit/miss/disabled), HTTP
status, and the request ID. Events are written as one structured log line and
retained in a bounded, thread-safe buffer (capacity 5000); credentials,
authorization headers, URLs, raw bodies, and exception messages are never
captured.

The internal board collector uses a separate typed, bounded aggregate event
collection. Its scalar schema records started-at, optional request correlation,
latency, bounded outcome/reason counters, and coverage counts. Board events do
not enter `recent_provider_events`, `provider_events_total`, or provider
failure counters.

Provider failures are counted once at the seam that owns the failure. The
central error handler in `app.errors` counts only *application* failures
(actual `AppError` codes and HTTP >= 500 responses) and skips
`provider_unavailable`; adapters also avoid emitting a local normalization
failure when an upstream malformed or timeout event already represents the
same terminal retrieval outcome. Operators read the bounded counters and
recent events from the admin-only `GET /api/data/telemetry` endpoint.

## Request flows

Game logs:

```text
GET /api/games/game_logs
  → Firebase auth (or explicit local-only bypass)
  → GameService.get_filtered_logs
  → durable player-game-log publication
  → local/database and request filters
  → serialized logs and averages
```

`GameService.get_player_id` resolves the `player_name` query parameter against
the injected Athlete Catalog (`AthleteCatalogReader.get_catalog`, an
engine-only read bound directly to the database so the full
`AthleteCatalogService`'s provider-backed refresh and NBA Stats adapter stay
unreachable from this read path) for the requested season first -- exact
case-insensitive match, then fuzzy -- because that is the governed identity
source durable game-log ingest already joins on. The catalog carries every
season a player has appeared in, so a display name can repeat across eras (a
retired player and an active one sharing a name); a tie prefers the row
active for the requested season, then the lowest `player_id`, so the name
always resolves the current player rather than a same-named predecessor. It
falls back to the legacy `player_information` table (a nba_api static dump
only the admin `fetch_players` job writes) when no catalog is injected or the
season has no catalog rows, which keeps the read-only demo database and
historical seasons working unchanged; an empty catalog read is never cached,
so a season becomes resolvable as soon as its catalog is published rather
than waiting out the cache TTL.

The request-time player-game-log source is one injected database-only seam
(`app.services.game_logs_source.StoredGameLogsSource`). It captures the active
immutable `player_game_logs` publication generation and reads that generation's
indexed player projection only when the publication is complete and valid. An
unavailable, invalid, or legacy-fallback-eligible publication produces an empty
canonical frame. No request-time provider or Redis player-log cache sits behind
the seam; historical seasons are outside the supported Log Workspace outcome
and may return empty.

`app.services.pbp_game_log_normalization.normalize_pbp_game_logs` is the one
canonical PBP game-log mapping used by durable ingestion.
Provider omissions become zero only for a closed list of additive/counting
box-score fields; identity, game, date, team, opponent, and `MM:SS` minutes
evidence is malformed rather than zero-filled.  The join is fail-closed: any
row that cannot join the governed Event Catalog, contradicts its team tricode,
disagrees with the requested phase, or is missing identity/minutes evidence
aborts the whole ingestion pass, so a durable game publication never tolerates
a dropped row. Canonical
shooting facts derive
`FGM = FG2M + FG3M` and `FGA = FG2A + FG3A`; PBP reports free-throw points
rather than made free throws, and a made free throw is exactly one point, so
the endpoint's canonical `FTM` reads `FtPoints` after that semantic
equivalence; composite and fantasy values are
computed centrally by `app.services.game_log_frame.derive_game_log_frame`.
The stored source rebuilds the request frame from `PlayerGameLogRecord` facts
through that same derivation.

### Team Filter Season Rankings

`teams_against[]` filters rank the 30 opponents from the durable window-aware
team matchup publications, always at the Season window
(`app.services.team_filter_rankings`).  One map names, per Team Filter, the
publication base and the per-48 metric keys it ranks:
`traditional_opponent_season` covers the opponent box set (`OPP_STOCKS` sums
the published blocks and steals), `grouped_shot_types_opponent_season` covers
the catch-and-shoot, pull-up, and under-10-feet filters (their points columns
are derived as `3 * FG3M + 2 * FG2M` from the made-shot counts),
`synergy_play_types_opponent_season` ranks points per possession, and
`assist_locations_season` ranks the published location counters.

There is no governed-window parameter and no request-time provider call: the
game service holds no NBA Stats adapter, so the previous dated
`fetch_opponent_team_stats`/`fetch_opponent_shot_chart` branch and its daily
Redis key are gone along with the legacy `general_opponent_stats`,
`catch_and_shoot`, `pullups`, `less_than_10_ft`, `team_play_types`, and
`processed_team_assists` reads.  Every Team Filter in one request is answered
from a single publication snapshot, so two filters can never intersect two
generations that never coexisted, and two filters sharing a base cost one read.
The read is not cached in Redis: an activation, a rollback, or a season
rollover must never be shadowed by a previous generation.

Those six nightly ranking tables are now **retired and dropped**.
`RETIRED_LEGACY_RANKING_TABLES` in `app.services.table_publisher` refuses them
at the publication boundary, so neither the `update_database` refresh operation
nor a compatibility writer can produce them again, whatever the activation
state of a stream; `DataService._refuse_activation_fenced_frames` refuses them
one step earlier, with the reason `retired_table` and ahead of any activation
check, so a revived collector never reaches the publisher at all.  The
collectors that built them (`_collect_opp_shooting`, `_collect_team_play_types`,
the opponent half of the assist frames, and the
`_fetch_opponent_data`/`_fetch_opp_shooting_data`/`_fetch_team_play_type_data`
provider calls behind them) are deleted rather than left unwired.  Migration
`048_drop_legacy_ranking_tables` then drops the storage: with
`GET /api/teams/stats` cut over to the publications, the tables had no reader
left, so there was nothing to keep them for.  `opp_shooting_zone` is
deliberately not part of that drop -- it is fenced, not retired.
`tests/services/test_legacy_ranking_tables.py` pins the fence, and its
allow-list is the repository-wide search proving no reader survives: every
remaining mention is the fence, the migration, or shared vocabulary.

The rankings are read for the request's own `season_filter`.  A publication
stream carries one pointer, so only the published season can rank: a request
for any other season ranks nothing rather than borrowing the published season's
rankings and attributing them to the wrong year.  A Team Filter on a historical
season therefore resolves to an empty opponent set until that season is
published.

`date_filter` trims the player's own game logs and never reshapes a ranking,
so a date-plus-Team-Filter request stays valid and season-ranked.  The
publication is all thirty opponents or nothing: NBA-owned streams prove the
canonical league and its tricodes at their decode boundary, and the
ledger-owned traditional and assist-location streams are proved here, so a
partial or mislabelled publication refuses rather than ranking a plausible but
wrong top-N.  Contradictory or unbounded evidence -- a non-numeric cell, a
derived column that overflows, or points recorded across no possessions --
refuses the surface the same way.

The ranking read applies the same governance the matchup window read does: a
publication whose coverage cutoff runs past today is refused, and an NBA-owned
publication must match the exact per-team game set resolved at its own manifest
and Event Catalog authority, so a restored, hand-seeded, or corrupted
publication claiming games that authority never held cannot rank.

One team may still be absent from one filter's ranking, and only in the single
legitimate case of a rate with no denominator at all: a team that faced zero
possessions of a play type has no points-per-possession to rank.  Such a team
is excluded from both ends, because it is neither a strongest nor a weakest
opponent against a play type it never faced, so a `rank_filter` of `-30`
returns the twenty-nine teams that have evidence.  A stale newest publication still serves its
last-good ranking; an unavailable, partial, or unscoreable publication ranks
nothing, which resolves to an empty opponent set rather than a new error case.

`players_on[]` and `players_off[]` are game-level appearance filters, not PBP
lineup-stint filters. For every named player, the same game-log source supplies
season rows. `players_on[]` intersects `(game_id, team)` pairs across the
primary player and every named teammate; `players_off[]` removes the union of
same-team pairs where any named teammate has a row. A player appearing for the
opponent in the same game is therefore never treated as the primary player's
teammate. Every named player's rows come from an active durable publication;
unavailable coverage produces no matching games.

Explicit contract amendment (#66): plus/minus is removed from the game-log
contract.  `PLUS_MINUS` is not a supported `self_filters[STAT]`, the averages
carry no `PLUS_MINUS` cell, response rows carry no `+/-` value, and the
canonical PBP frame and durable `player_game_logs` schema store no plus/minus.
PBP's per-game boxscore seam does not expose the field, and the amendment
removes it from the public contract rather than inventing or relabeling
evidence.  Because the durable path therefore covers every public primitive,
an ingestion-complete, valid publication is database-first with no separate
cutover gate.

### Team Profile categories

`GET /api/teams/stats` (`app.services.team_service.TeamService`) serves the
Log Workspace opponent and player panels.  Every category is one projection of
the same Season publications the Team Filters read: `Traditional` from
`traditional_opponent_season`, `Playtypes` from
`synergy_play_types_opponent_season`, `Assists` from `assist_locations_season`,
`Zone Shooting` from `exact_shot_zones_opponent_season`, and `Shooting Type`
from `grouped_shot_types_opponent_season`.  The service holds the same
publication read seam the game service injects, so no provider client is
reachable from it by construction and the legacy `general_opponent_stats`,
`team_play_types`, `processed_team_assists`, `opp_shooting_zone`,
`catch_and_shoot`, `pullups`, and `less_than_10_ft` reads are gone with their
dated `fetch_opponent_team_stats`/`fetch_opponent_shot_chart` branches.

Ranks and percentages come from `publication_league_table` in
`app.services.team_matchup_query`, the same league computation the Matchups
Defense Sheet applies -- mean, population sigma, ascending competition rank
with shared ties, and percent versus the league average over the published
thirty.  The Defense Sheet projects a curated subset of that table; the panel
projects the rest, and the panel derives `OPP_STL+BLK`, the two shooting
percentages, `AssistPoints`, and each shot type's `PTS` as further columns
ranked the same way.  Values are per-48 on nominal minutes, so opponent points
allowed is one number across both surfaces.  `Playtypes` and `Assists` carry a
ratio to the league average because those panel charts are centred on `1.0`;
their ranks stay on the published column, where a division can neither create
nor break a tie.

A requested `date` is accepted and ignored: the rankings are whole-season, the
#39 decision applied here.  An unknown category is a `400`, a stale
publication serves its last-good values silently, and a season with no
published generation -- including the demo database, which carries no
publication tables -- serves nothing rather than a partial league.  Fields the
publications do not carry (`OPP_OREB`, `OPP_DREB`) are omitted rather than
synthesized, and so is a rate one team has no denominator for: a team that
faced zero possessions of a play type is left out of that column exactly as a
Team Filter leaves it out of that ranking, so it neither ranks as the
stingiest defense nor moves the league average the other teams' ratios are
measured against.

### NBA Stats game-log adapter

`app.providers.nba_stats.NBAStatsProvider` is the injectable interface for
live game logs:

```python
class NBAStatsProvider(Protocol):
    def get_player_game_logs(
        self, *, player_id: int, season: str,
        season_type: str = "Regular Season",
    ) -> pandas.DataFrame: ...

    def get_archetype_game_logs(
        self, *, player_ids: Sequence[int], opponent_team_id: int,
        season: str, season_type: str = "Regular Season",
    ) -> pandas.DataFrame: ...

    def get_season_player_game_logs(
        self, *, season: str,
        season_type: str = "Regular Season",
    ) -> pandas.DataFrame: ...
```

The production adapter owns endpoint construction, timeout, concurrency,
telemetry, response normalization, and provider error translation. Tests inject
the protocol into `PlayerService` rather than patching `nba_api`. `GameService`
no longer takes the adapter at all: after the #198 cutover its player logs come
from the injected game-log source and its Team Filters from Season
publications, so no request-time NBA Stats call is reachable from it.

`get_season_player_game_logs` is the legacy season-wide NBA durable-log seam.
Each call fetches one explicit phase for the whole season, retains canonical
`PLAYER_ID`, and keeps the provider's exact minutes rather than the
request-time game-log route's historical whole-minute presentation. It remains
available as a tested backfill utility; the PBP-based incremental
`PlayerGameLogIngestService` is now the Nightly Refresh durable-log step, and
request-time game-log reads use the PBP-backed source described above.

`NBAStatsAdapter.fetch_whole_season_schedule(season=...)` is the provider seam
for the canonical event catalog. It accepts only an explicit canonical
`YYYY-YY` season, calls `ScheduleLeagueV2` through the shared NBA Stats
concurrency/timeout bound, and emits the closed `schedule_whole_season`
telemetry operation. Its normalized frame retains the NBA game ID, explicit
home/away team IDs and identities, UTC scheduled time, status text,
postponement evidence, and provider classification. Recorded fixtures use
`parse_recorded_schedule` and never make a network request.

Schedule normalization separates canonical event kind from display
classification. A recognized 10-digit game-ID prefix is authoritative for the
kind (preseason, regular season, All-Star, or playoffs), which drives slate
exclusion and the preseason flag even when provider branding conflicts. The
catalog's display classification prefers provider classification, subtype, or
label, then a meaningful sublabel such as `Emirates NBA Cup` or
`NBA Mexico City Series`; generic series-state/record text such as
`LAL leads 2-1` or `LAL wins series 4-2`, game-number text, and postponement
sublabels remain status/evidence rather than badges. When no display evidence
remains, the canonical kind supplies the classification.

On a slate read, `resolve_stored_event_classification` returns that canonical
kind and display label together exactly once. Recognized `001` through `004`
prefixes remain authoritative; only an unknown prefix falls back to the stored
display classification for kind, so a badge cannot reclassify a known game.
`is_postponed_event` likewise owns postponement truth across catalog
serialization, event resolution, and slate shaping: an explicit normalized
flag, a postponement status, or non-empty structured evidence is sufficient.

Authenticated slate read:

```text
GET /api/games/slate?date=YYYY-MM-DD
  → require_auth
  → SlateService parses/defaults one US-Eastern Slate Date
  → EventCatalogService reads the last successful refresh and only the ET day's
    half-open UTC event window from the configured season
  → SlateService filters All-Star exhibitions
  → response orders UTC tips and reports schedule/pool freshness independently
```

`SlateService` is assembled once in `ApplicationDependencies` beside the game
service and reads no provider at request time. The window query uses ET
midnights converted to UTC, so spring and fall DST days remain correct without
reading or decoding the whole season. Its schedule status uses the
surface-specific `SLATE_SCHEDULE_MAX_AGE_HOURS` window (30 hours by default),
while Event Catalog matching continues to use `EVENT_CATALOG_MAX_AGE_HOURS`.
An absent runtime Event Catalog dependency or a catalog with zero stored events
is unavailable. Stored catalog rows remain servable when successful-refresh
metadata is missing, with schedule freshness reported as `missing`. A populated
catalog can return an empty date, and populated but
older schedule data remains servable and is marked stale. The player-pool
surface remains an explicit unavailable aggregate until its owning service is
implemented. Availability uses an efficient count of actual season rows; the
refresh record's `event_count` remains metadata and does not gate the read.

Natural-language query:

```text
POST /api/nl-query
  → deterministic BaseQueryParser
  → confidence decision
  → optional OpenAI fallback
  → frontend-compatible structured filters
```

Data refresh:

```text
POST /api/data/update_database (or a PBP/PUT refresh)
  → schedule durable DataRefreshJob (HTTP 202, job_id)
  → DataRefreshJobService runs the refresh in the background
  → AtomicTablePublisher stages frames → single transaction name swap
  → GET /api/data/jobs/<job_id> reports queued/running/succeeded/failed
```

Every mutating refresh is recorded in the application-owned
`data_refresh_jobs` table before the request returns `202 Accepted`.

Successful `update_database` publication also upserts the governed
`stats_tables` row in `stats_refreshes` inside the same transaction as the
complete stats table swap. Failed or fenced publication therefore cannot
advance stats freshness. A surface-specific `StatsFreshnessRepository.get()`
returns the frozen stored fact
`StatsFreshness(last_successful_completion=...)`; a null completion explicitly
distinguishes the before-first-run state. A later presentation seam owns its
translation into API `retrieved_at` and freshness status. Railway runs
`scripts/nightly_refresh.py --hosted-only`. This mode constructs only the
PBP-backed player-game-log ingestion path, reads the governed Event and Athlete
Catalogs from Postgres, and never constructs or calls the NBA Stats adapter.
A failure is retried once and preserves the prior complete game-log
publication. NBA-owned catalogs and statistical surfaces remain residential
collector inputs; their governed team-window publications are later consumed
by the database-first ledger materialization seam described below.

The legacy operator mode without `--hosted-only` runs that same stats service, the
current-season Event Catalog refresh, the current-season Athlete Catalog
refresh, the current-season player-game-log refresh, Season player Diets, and
then team matchup facts. The two catalogs
precede game logs so every stored log joins against current canonical
player/game/team identity. `DataService.update_all_data` publishes the legacy
`player_information` table, not the season-owned Athlete Catalog or its
freshness, so the separate `AthleteCatalogService` step is required rather
than a duplicate stats fetch. Schedule deliberately precedes it: an Athlete
Catalog failure skips player logs but cannot suppress the required Event
Catalog refresh. The prior log publication remains readable, and the command
reports the named `athlete catalog` failed step. The command retries the
complete ordered unit exactly once after any step fails and returns a nonzero
process status when both attempts fail. It is not the Railway cron command.

### Durable player game logs

The legacy `PlayerGameLogService.refresh(season)` consumed the season-wide NBA
Stats seam once for `Regular Season` and once for `Playoffs` during Nightly
Refresh, stamping retrieval only after both provider calls return.  It remains
available as a tested backfill seam; the PBP incremental
`PlayerGameLogIngestService` (described below) is now the Nightly Refresh
durable-log step.  Both paths share the same canonical safeguards: reads go
through `AthleteCatalogService` and `EventCatalogService`; the player-log
module does not query their tables directly. Every snapshot, including an empty preseason observation, requires a
present, fresh, nonempty, internally complete Event Catalog for the same season
observation; its freshness metadata must agree with the actual governed
event-row count. A nonempty union snapshot also requires a present, fresh,
nonempty Athlete Catalog before canonicalization. Each provider `PLAYER_ID`
must join exactly to the requested season's Athlete Catalog, and each `GAME_ID`
plus per-game `TEAM_ID` must join exactly to the Event Catalog. Team and
opponent tricodes and home/away identity come from that canonical event.
Individual well-formed rows that do not join are excluded and counted in
bounded scalar-only telemetry; no name or matchup-text guess is accepted. A
row whose governed catalog event is outside the explicit `Regular Season` and
`Playoffs` durable phase set is likewise excluded with an
`unsupported_phase_count`; it never becomes a stored fact. Stable snapshots
and new source growth containing only governed unsupported-phase exclusions
may republish and advance freshness; those rows are outside canonical identity
coverage rather than failed joins, even when a previously observed unjoined
identity remains stable. A
nonempty snapshot that yields no canonical rows fails the refresh before
publication. For every non-postponed governed `Regular Season` or `Playoffs`
event that is final and scheduled no later than the source observation time,
that exact phase's canonical snapshot must cover both exact event teams with at
least the configured number of distinct players recording positive minutes per
team (five by default). The positive whole-number minimum is injected from
`PLAYER_GAME_LOG_MIN_ACTIVE_PLAYERS_PER_TEAM_GAME`; it is named configuration,
not an implicit sport constant. This exact completed-game invariant makes a
truncated first publication fail closed without guessing an expected season
total or requiring rows for future games or DNPs. Canonical player identity
comes only from the fresh Athlete Catalog owner. If that catalog drops a
previously known player, the source rows become bounded unjoined-athlete
telemetry and the incomplete replacement fails while prior facts remain
served; no stale name or player identity is recovered from player logs.
Structurally, numerically, or logically malformed rows abort the
publication while retaining already observed bounded coverage counts. If a
new cumulative source row cannot join an athlete, event, or team and canonical
publication would otherwise remain at or below its prior size, the apparent
growth exposes incomplete canonical identity coverage and fails closed instead
of stamping the unchanged snapshot fresh. The season sidecar stores the raw
provider count and the identity-relevant count after governed unsupported-phase
rows are removed, so prior and current publications use comparable denominators.
An unchanged partially unjoined snapshot can therefore republish idempotently,
while prior Play-In rows cannot mask new unjoined identity growth.

Migration 011 creates `player_game_logs`, keyed by season, canonical player ID,
and NBA game ID, with an explicit governed `season_type` constrained to
`Regular Season` or `Playoffs`, plus `player_game_log_refreshes`, keyed by
season. Both phase observations form one season replacement; its `nba_stats`
source, timezone-aware retrieval time, and union row count commit in the same
transaction. The sidecar also records the bounded,
nonnegative raw `source_row_count` and `identity_source_row_count`; the latter
excludes only governed unsupported-phase rows. Both can exceed the canonical
row count after exact duplicates or identity exclusions but never be smaller.
Exact duplicate provider rows collapse to one fact and increment bounded
duplicate telemetry;
conflicting duplicates fail closed at canonicalization, and the repository
repeats that invariant for direct persistence callers. Any validation or
database failure leaves both the prior rows and prior successful freshness
unchanged and emits bounded rejection telemetry. Every replacement compares
the complete prior and candidate `(player_id, game_id)` identity sets, even
when additions make the candidate row count equal or larger. Removed facts are
accepted only when every removed key belongs to a game that the fresh Event
Catalog has removed or explicitly made ineligible by phase/postponement;
removing a fact for an eligible game always fails. Recovery telemetry records
the actual admitted removed-key count, never a net row-count delta or a game or
athlete identifier. One service-boundary SQLAlchemy handler covers prerequisite
catalog, identity, and
freshness reads plus publication after repository rollback, without swallowing
the error or counting it twice. An empty phase response is publishable only
when the governed Event Catalog contains no completed event in that exact
phase. This permits an empty `Playoffs` response before the postseason and an
empty `Regular Season` response before opening night, but not either response
after a completed event in its phase. A postponed event does not become
completed evidence merely because its feed status code or text says final.
Preseason, exhibition, All-Star, Play-In, and other unsupported phases are
never stored; their provider rows are counted separately from malformed or
unjoined identity rows. The canonical NBA game-ID prefix `005` is
authoritative Play-In evidence even when a provider classification incorrectly
claims `Regular Season`; Slate retains the unusual `Play-In` label while the
player-log surface excludes it.
An empty union snapshot can never replace a prior nonempty publication. A
missing Event Catalog or an invalid empty phase within the source observation
boundary fails closed and preserves the last valid facts and freshness. Because
a season-wide log snapshot is cumulative, any unexplained removal of an eligible canonical
identity fails closed even when additions produce net growth. Corrections that
retain all identities, pure growth, and the exact governed recovery above
remain publishable. Every rejected refresh records
bounded scalar rejection and accumulated coverage counts without identities.
Only raw box-score inputs are stored: minutes, PTS/REB/AST, FGM/FGA, FG3M/FG3A,
TOV/STL/BLK, and canonical game/team/opponent identity. PRA, PA, PR, RA, STKS,
FG2A, season rates, and rolling selections are derived at read time rather
than persisted in redundant tables.

`PlayerGameLogRepository` is the internal query seam used by later matchup
services. Season rates default to `Regular Season` only; callers may explicitly
request `Playoffs` or all phases. Rates use the reviewed Market Category
spellings and component definitions from `statistic_catalog.yaml`. Last-ten
minutes use the combined Regular Season-plus-Playoffs chronology in
oldest-to-newest sparkline order, while H2H and deterministic multi-player
archetype rows also include both phases. `get_player_summaries` accepts
canonical player IDs and returns every player's default Regular Season rate plus combined-phase
last-ten minutes with one player-log rows query and in-memory grouping; the
single-player rate and last-ten APIs are thin wrappers over that batch seam.
Publication writes the season sidecar for every season. When that season is the
configured current season, the same transaction also advances the named
`player_game_logs` row in `stats_refreshes`. Season reads require their own sidecar completeness; the
configured current season additionally requires that named stats observation
to exist and remain within `PLAYER_GAME_LOG_MAX_AGE_HOURS` (30 hours by
default), otherwise reads fail closed. That global observation never gates or
hides historical backfills, which remain season-sidecar based. Callers consume
the same governed `StatsFreshnessRepository` fact rather than synthesizing
freshness from rows. Neither migration 011
nor its refresh service calls a provider from a public request.

### PBP incremental durable ingestion

The staged PBP migration (#66) expands the durable model to every primitive the
legacy endpoint needs and makes the durable refresh PBP-based.  Migration 016
adds `free_throws_made`, `free_throws_attempted`, `offensive_rebounds`,
`defensive_rebounds`, and `personal_fouls` to
`player_game_logs`, adds an explicit `publication_status`
(`complete`/`in_progress`) to the season sidecar, and creates the
`player_game_log_sync` per-game synchronization evidence table.  The #66
contract amendment removes plus/minus from the public game-log contract, so the
durable facts carry no `plus_minus` either.

`PlayerGameLogIngestService` is the Stage 2 refresh seam used by Nightly
Refresh.  It requires the same fresh, nonempty Event and Athlete Catalog
prerequisites, discovers governed completed `Regular Season`/`Playoffs` games
through the observation time, and requests one PBP per-game player observation
(`GET /get-game-stats` with `Type=Player`, parsed by
`PBPGameLogAdapter.parse_game_stats`) per missing game.  The parser flattens
the nested `stats.Home/Away.FullGame` player arrays, excludes the provider's
team-summary row (`EntityId == 0`), and attaches game identity from the
envelope.  Each game's rows flow through the same canonical normalization
contract, join the fresh Athlete Catalog for player identity (an unjoined
athlete fails the game, never a dropped row), and must cover both exact event
teams with at least the configured
`PLAYER_GAME_LOG_MIN_ACTIVE_PLAYERS_PER_TEAM_GAME` positive-minute players.
Every target game is fetched, normalized, and validated before anything is
written; only then does `PlayerGameLogRepository.publish_refresh` replace all
staged games' rows, upsert their per-game sync evidence, recompute the season
sidecar, and advance the configured current season's stats-surface observation
in one transaction.  A game that fails at any earlier point preserves the exact
prior fact rows and the last complete publication — a correction staged for one
game never leaks into published facts when a later game fails.  The sidecar
`publication_status` is `complete` only when every governed completed event
carries complete PBP sync evidence; legacy NBA-derived publications stay
`in_progress` until a real PBP backfill verifies them, so a database-first read
never mistakes unverified data for a complete publication.

A bounded recent-game reconciliation window
(`PLAYER_GAME_LOG_RECONCILIATION_DAYS`, default three days) re-fetches recent
completed games and atomically replaces their rows when a stat correction
changes the game's normalized checksum; an unchanged game is idempotently
skipped.  Per-game sync evidence records status, a checksum over the game's
normalized facts, row count, source provider, and retrieval time; full raw PBP
documents are never stored.  A malformed, incomplete, identity-removing, or
provider-failed game preserves prior facts and fails the refresh, so Nightly's
whole-unit retry never erases working game logs.  The legacy season-wide NBA
refresh remains available as a tested backfill seam but is no longer wired
into Nightly Refresh.

### Canonical Game Ledger and derived Regular Season streams (#86)

The inactive Canonical Game Ledger is the governed source for the next
generation of Regular Season Matchups facts. It is deliberately separate from
the public `player_game_logs` reader and from the Railway control-plane
routes. `app.services.canonical_game_ledger` accepts one normalized PBP
`FullGame` observation and requires one canonical game identity, exactly two
team fact sets, and all participating player facts. The stored superset is
count primitives only (shooting, rebounds, assists, turnovers, steals,
blocks, fouls, free throws, and minutes, plus optional assist-location
counters); plus/minus and permanent period rows are not part of the ledger.

`CanonicalGameLedgerRepository.replace_game` and
`replace_games_atomic` validate the complete-game participant and count
invariants before deleting anything. A repeated checksum is idempotent; a
new observation with the same game identity replaces the game, team facts,
and player facts in one transaction. A failed or incomplete candidate leaves
the prior correction and its checksum untouched. Provider participant evidence
must exactly equal the retained player set (including zero-minute participants).
Migration `024_canonical_game_ledger` remains the schema owner for the typed
ledger tables, migration `032_ledger_raw_row_evidence` owns the raw archive,
and migration `033_ledger_observation_evidence` owns the durable observation
reference for indefinite retention; repository construction fails clearly
naming migration 032 (and the latest ledger migrations) when any owned table is
missing, and the read-only demo fixture is never eligible.

#### Complete PBP row evidence archive (#113)

Every accepted game also durably archives the complete provider `/get-game-stats`
Home and Away `FullGame` row sets as immutable raw JSON evidence. Migration
`032_ledger_raw_row_evidence` creates `canonical_game_ledger_raw_rows` and adds a
`raw_checksum` column to the canonical game row; the raw archive is written in
the same transaction as the typed game, team, and player facts, so acceptance,
replacement, and correction remain one atomic operation and readers never see a
mixed raw/typed version. Games accepted before migration 032 were archived only
as typed facts: they carry `raw_checksum` NULL and no raw rows. The backfill
treats those games as incomplete (`game_ids_without_raw_evidence`) and re-fetches
them regardless of age, and the season never reports complete until every
governed game retains both team-summary and every player-row evidence.

Retention is governed and indefinite (#25): raw evidence is never pruned, so
the complete archived row set for every accepted game remains recoverable. A
correction replaces the typed facts and the archived raw rows for that game
atomically, and the game row reflects only the latest accepted observation;
nothing is versioned per game inside the ledger tables. What survives every
correction is the observation store: each accepted `CollectionObservation`
payload, checksum, retrieval time, and its cryptographically bound raw evidence
remain in `collection_observations` indefinitely, so a superseded observation
can always be audited and an identical replay is provably a no-op. `schema_drift`
reconciliation items accumulate beside that evidence and are never pruned
either.

Indefinite observation retention is enforced by a durable, queryable reference
rather than by searching rendered JSON. Migration `033_ledger_observation_evidence`
creates `canonical_game_ledger_observation_evidence`, one row per
`collection_observations.observation_id` (a real foreign key) plus the game it
supplied. Every acceptance and every correction records the observation ID in
the same transaction as the game, so a superseded correction observation stays
referenced even after its raw rows are replaced. The generic observation
retention job (`gc_observations`) joins exactly that reference table and exempts
every referenced observation from its window regardless of age, so canonical
ledger evidence is never pruned; unrelated old observations still expire. The
migration backfills existing accepted games so historical evidence is protected
immediately.

`canonical_game_from_pbp` archives both the provider team-summary rows
(`EntityId == 0` / `Name == Team`) and every participating player row from each
side, preserving every provider key and value verbatim. Unknown additive fields
survive schema growth instead of being projected away. Each archived row records
the canonical game identity, side, row type, canonical team identity, provider
entity identity (for player rows), the source observation ID, the timezone-aware
retrieval time, a deterministic row checksum, the exact observed field set
(`observed_fields`), and the typed extractor version (`LEDGER_SCHEMA_VERSION`).
`observed_fields` is stored separately from the payload so additive schema drift
is visible and non-destructive: a corrected observation that adds a provider
field changes the field-set metadata and the raw checksum without touching the
typed primitive set. A replacement whose corresponding archived rows change
their observed field sets also records an operator alert: the repository emits
a bounded `schema_drift` reconciliation item (`record_schema_drift`) inside the
same correction transaction, so drift is recorded and alerted while the valid
correction still lands. A first raw archive is judged against the governed
baseline field set (`LEDGER_GOVERNED_FULLGAME_FIELDS`, versioned in lockstep
with `LEDGER_SCHEMA_VERSION`) instead of an empty prior evidence set: the
baseline is the complete documented PBP Stats `BoxscoreItem` vocabulary (`PBP_BOXSCORE_ITEM_FIELDS`)
plus the normalized aliases the extractor tolerates, so a normal provider row
carrying shot context, rebound opportunities, turnover/foul types,
second-chance, penalty, or pace inputs never alerts. A brand-new game, or a
pre-032 game receiving its row evidence for the first time, that carries a
provider field outside that baseline records an `unknown_field` alert, while a
normal first observation inside the baseline stays silent. This is what makes
schema growth on brand-new games — the normal way a provider field first
appears — recorded and alerted rather than only ever detected later when an
older game is corrected. At the repository boundary each archived row's row type
and entity metadata must agree with its payload identity: a team-summary row
must be payload-identified as a team aggregate (`EntityId` `0`/`None` or
`Name` `Team`) and carry no player entity metadata, and a player row's metadata
identity and name must equal the payload's provider identity and name (the
typed-fact reconciliation then proves the retained player fact matches). The
production backfill consumes the complete raw
`/get-game-stats` document through a dedicated adapter seam
(`PBPGameLogAdapter.fetch_game_stats`) rather than the projected player-only
DataFrame, so team-summary rows and unknown additive keys always reach the
archive. The wire's explicit game, season, and team identities are fenced to
the governed event; the single tolerated disagreement is the exactly swapped
team pair (the wire labels the governed away team `Home`), which is read in
governed orientation — `stats` and `team_results` envelopes, identities, and
abbreviations swapped — with the provider's document retained unchanged as the
observation. An accepted raw observation carries at most one team-summary row
per governed Home/Away side, and may omit one only where the side's diagnostics
prove a zero team-only residual (see below); the team row is the sparse
residual authority and is never a fallback.

Raw JSON canonicalization is deterministic and lossless: each payload is
serialized with sorted keys and compact separators, and the game-level
`raw_checksum` hashes the complete row set together with its identity, row
type, observed fields, and payloads in one explicit canonical order — Home
side rows first, then Away side rows, each in provider row-index order.
Provider `FullGame` array positions are unique per side, so an accepted game
stores exactly one archived row per `(side, row_index)` regardless of row
type; that uniqueness makes the canonical order and checksum deterministic
rather than dependent on arrival order. Each `row_index` must be a
non-boolean non-negative integer and each side's positions must be the exact
contiguous range `0..n-1`, mirroring the complete provider `FullGame` arrays
(negative, boolean, non-integer, or gapped positions are rejected at the
repository boundary). The order is used consistently for
hashing, persistence, and reload, so a game loaded from storage and replaced
unchanged is an idempotent replay that changes no persisted evidence.
Semantically identical replays produce identical checksums, so replaying an
accepted observation is idempotent and changes no persisted evidence: the
repository detects the checksum no-op before inserting the accepted observation,
so an identical replay persists no new `CollectionObservation` row and the
per-attempt observation identity never collides. Because
the raw checksum is independent of
the typed `checksum`, a raw-only correction (a provider field that does not
change any typed primitive) is still recognized as a replacement rather than an
idempotent replay, and the complete raw and typed evidence is replaced
atomically. The PBP `FullGame` wire is sparse: the provider omits observed-zero
additive counters on both player rows and the team-summary row, so an omitted or
null count primitive is a governed zero rather than missing evidence. Identity,
minutes, row presence, and malformed values stay strict and always reject the
candidate atomically. Every count primitive is such an additive counter (points,
two- and three-point makes/attempts, free-throw points/attempts, offensive and
defensive rebounds, assists, turnovers, steals, blocks, and fouls);
`FGM`/`FGA`/`Rebounds` are derived from the two- and three-point components and
from offensive plus defensive rebounds when absent. A governed zero is only
accepted when independently proven: a `Points` omission must reconcile
arithmetically with the retained scoring components (`Points` equals
`2*FG2M + 3*FG3M + FtPoints`), and every other omitted player or team count must
stay consistent with the complete `team_results` diagnostic reconciliation, so a
missing nonzero count rejects the candidate atomically rather than fabricating a
zero while a proven zero passes. Because that proof depends on the diagnostics,
an accepted raw observation must carry the `team_results` Home and Away
`FullGame` envelopes and every governed diagnostic count inside them — `Points`,
`FG2M`/`FG2A`, `FG3M`/`FG3A`, `FtPoints`/`FTA`, offensive, defensive, and total
rebounds, `Assists`, `Turnovers`, `Steals`, `Blocks`, and `Fouls`. A missing
envelope, a null or malformed diagnostic field, or a diagnostic count that
does not reconcile with the declared team authority (player sums plus the
`EntityId == 0` team-summary residual) rejects the candidate atomically rather
than letting an unprovable omission pass as a zero. The `team_results`
envelope is as sparse as every other PBP row: a governed diagnostic key that is
omitted is accepted as the observed zero only when the declared authority for
that count is itself zero (a team with no blocks has no `Blocks` key on any of
its rows); an omission against a nonzero authority, and an explicit `null`
under any authority, still reject. The same rule covers a side whose whole
`EntityId == 0` team-summary row is omitted: it is accepted as a zero team-only
residual only when the side's `team_results` envelope exists and every
governed diagnostic count equals that side's player-row sum; otherwise the
missing row still rejects, and at the repository boundary a side without an
archived team row must have typed counts equal to its player sums. Missing optional expanded
fields preserve the game and leave the dependent typed facts (such as assist
locations) null in the ledger. A null assist-location counter becomes a
governed zero at derivation time only when the retained split proves it
(two-point plus three-point assists equal the player's assists, the
rim/short/long split equals the two-point count, the arc/corner split equals
the three-point count); otherwise the location surface is unavailable.

The declared typed authority is per row type. Player-game typed facts come from
the provider player rows; the real provider team-summary row (`EntityId == 0`)
is itself sparse and carries only the team-only residuals (such as team
rebounds) that no participating player row carries, not a complete traditional
box score. Each complete team count is therefore the sum of the authoritative
participating-player rows plus the corresponding sparse team-row residual, where
an omitted team-row counter is a zero residual; `FGM`/`FGA`/`Rebounds` derive
from the same components and are not separately required. Because both the
player rows and the team row are sparse, the repository re-proves that
equivalence for every count primitive — rebounds included — so a team value
cannot disagree with its player primitives and team-only evidence. Optional
team-summary fields such as possessions remain team authority and are never
compared against summed player possessions. The `team_results` envelope is
required diagnostic/parity evidence for every accepted raw game, not an
optional extra: both Home and Away `FullGame` envelopes must exist with every
governed diagnostic count present and well-formed (or omitted only where the
declared authority for that count is zero), each must reconcile with the
declared authority, and a missing envelope, a null/malformed diagnostic field,
an omission against a nonzero authority, or a reconciliation failure rejects
the candidate atomically. It never populates persisted facts.

The repository boundary repeats these invariants for direct callers.
`validate_complete_game` re-checks the strict evidence on every archived row —
identity, minutes (including team-summary minutes, which must be present and in
the accepted format (`00:00` and other valid `MM:SS` values) but is never
treated as a player-additive fact), row presence, and malformed values — while
sparse count primitives are not required per row: each omitted count is
re-extracted and reconciled against the typed authority that intake proved
against the complete `team_results` diagnostic, so a missing count that the
typed evidence proves nonzero rejects the candidate
atomically and a proven zero passes. It then proves the stored
typed version equals extraction from its authoritative raw rows: each typed
player fact must equal extraction from its archived player row and each typed
team fact must equal the participating-player sums plus the sparse team-summary
row's team-only residual, with the same reconcile-on-every-primitive
equivalence re-proven for every count field. A game whose typed
facts disagree with its raw evidence, or whose raw rows are incomplete, is
rejected atomically with no write, so a direct `replace_game` caller can never
persist a mixed or incomplete raw/typed version. The game identity row carries
both the typed and raw checksums, so an operator can always prove that a stored
typed game and its archived raw evidence came from the same observation.
Acceptance through the governed seam is cryptographically bound as well: inside
the manifest-authorized transaction, the repository recomputes the candidate's
archived rows from the `CollectionObservation` payload being stamped and
requires that recomputed set to reproduce the candidate's exact `raw_checksum`,
and it verifies the observation's own checksum against its payload. A caller
can therefore never persist one observation's envelope while archiving or
typing another raw document, so every raw row's source observation is provably
the document that produced it.

`app.services.ledger_backfill.LedgerBackfillService` discovers final,
non-postponed Regular Season Event Catalog games through an explicit cutoff,
fetches newest first behind an injected bounded worker pool, and persists
cursor/completed/failed progress. Missing games have priority, games seven
days old or newer are rechecked daily, games through day 30 are rechecked
weekly, and older games require explicit historical repair; a stored game that
lacks complete raw evidence (a pre-032 game) is re-fetched at missing-game
priority regardless of age. Historical repair skips games whose accepted
observation already belongs to the active manifest, except games accepted
through the NBA LiveData fallback (`nba_live_data` or `pbp+nba_live_data`
provenance): those carry no assist-location evidence and remain repair targets
on every explicit repair run until a complete PBP observation replaces them.
Repair is operator-invoked and bounded per run by `--max-games` and the
provider retry cap; there is no persisted per-game attempt budget, so a game
PBP keeps refusing is re-attempted on each run. Each provider response records its own timezone-aware
retrieval time at the moment it returns, so every staged observation and
archived row carries that response's retrieval time rather than the batch start.
Any failed target keeps the previous valid publication and reports the season as
unavailable; the season reports complete only when every governed game is
stored with complete raw evidence. Unknown player identities can be sent to a
bounded reconciliation sink rather than dropped.

`app.services.ledger_derivations` owns all derived semantics: traditional
opponent facts read the opposing team fact, assist locations require a
complete location observation (explicit counters, or sparse omissions proven
zero by the retained split), and per-36 values aggregate count primitives by
canonical player while retaining every team-at-game identity. Season and
exact L15 materialization selects governed game IDs only and refuses to call a
window complete until all 30 governed teams are present (and L15 has 15
eligible games for each). League averages use the population denominator;
team values are normalized to per-48 from the nominal game length each
retained team-minute value establishes (48 minutes plus 5 per overtime; the
retained player-minutes-over-five drifts from it by seconds of PBP clock
precision, accepted up to 0.05 minutes, and a value outside that band is
unavailable because the ledger has no independent game-duration evidence;
count-per-game fallback remains for hand-built replay facts without
minutes), and competition ranks are deterministic with ties
represented as `1, 1, 3`.
`LedgerMaterializationService` stores the complete derived payloads and creates
inactive control-plane candidate versions with normalized game-observation
provenance; corrections enqueue each affected derived slice in the same
transaction. Historical rehearsal can read an immutable candidate payload but
never enables public Matchups routes. `ledger_parity` records symmetric
ledger-only and legacy-only player-game identities plus traditional-opponent
and per-36 semantic evidence for adjudication; zero or unequal identity sets
are never reported exact. Player-game-log activation is bound to that exact
candidate and parity artifact just like the other ledger-derived streams; an
empty legacy game-log table produces pending evidence that requires an
explicit audited adjudication rather than bypassing parity.

The executable `scripts/ledger_refresh.py` assembles the injected PBP, Event
Catalog, Athlete Catalog, accepted-observation participant, reconciliation,
repository, and composition seams. Every provider response is first stored as
an accepted `CollectionObservation`; its durable ID is the ledger source and
the only provenance allowed on an inactive candidate. Candidate truth is
independent for player game logs Season, traditional opponent Season/L15,
assist locations Season/L15, and player per-36 Regular Season. Missing assist
primitives that the retained split cannot prove zero retain only the assist
last-good candidates.
NBA-owned opponent play-type and shot publications use this same database-first
read seam as independent surfaces: immutable rows are validated and composed
alongside ledger-owned facts, never substituted from PBP or another NBA
surface. Exact taxonomy, 30-team completeness, and governed Last-15 game-set
checks apply before activation and on public reads. Migration
`025_ledger_parity_artifacts` stores mandatory parity evidence, and pending
adjudication blocks ledger stream activation.
Migration `026_repair_publication_provenance_foreign_keys` removes the
transient PublicationVersion self-reference and gives normalized provenance
its publication cascade and accepted-observation restrict links.
Migration `027_bind_ledger_parity_to_publications` binds each new parity
artifact to the exact inactive PublicationVersion and payload checksum it
rehearsed; pre-binding artifacts are retired and cannot authorize cutover.
Migration `038_bind_manifests_to_event_catalog_publications` gives every new
manifest an exact Event Catalog publication ID and checksum. It backfills only
legacy rows whose prior timestamp rule identifies one snapshot and leaves the
rest unbound, so governance fails closed instead of guessing. Season and L15
game sets both come from that checksummed snapshot; later mutable catalog
status changes and same-clock catalog republications cannot reinterpret an
existing cutoff.
Migration `039_bind_publication_versions_to_manifest_authority` also binds each
governed NBA PublicationVersion to the exact manifest and Event Catalog
publication/checksum that authorized it. Compose records that identity and
activation, rehearsal, materialization, and reads verify it, so a later
same-cutoff manifest cannot reinterpret an older candidate. Legacy versions
are backfilled only when one relevant manifest, one complete Event Catalog,
and any normalized observation provenance agree on the same authority;
ambiguous or unbound rows remain unbound and fail closed. Runtime converts
immutable UTC cutoffs to their DST-aware Eastern slate date before ledger
selection and matchup materialization. Governed game sets use the shared NBA
final/non-postponed semantics, including strict boolean completion evidence
and structured postponement fields.
Provider numeric NBA status codes are normalized to canonical status text, and
stored-versus-incoming catalog comparisons use that same strict predicate so
identical non-final or postponed snapshots remain idempotent.
Explicit boolean `completed: true` is persisted as canonical `Final`/status
code 3 only when structured postponement evidence is absent; postponed and
non-final rows remain excluded on both first publication and replay.
Publication
authority additionally requires the manifest's canonical-ledger scope and
schema version 1. Governed activation always resolves the exact game set from
that bound authority; caller-supplied game maps cannot replace a missing
resolver. Collector request bounds and rehearsal dates use the same Eastern
slate-day conversion as materialization.
PBP responses
remain staged until complete-game and identity validation succeeds; acceptance,
ledger replacement, and all shared-cutoff jobs then commit atomically. Runtime
expectations come only from the active manifest and its bound, completed
Regular Season Event Catalog publication. NBA-owned Season/L15 candidates
contain complete 30-team per-48 payloads; league mean, population sigma,
deviation, and ascending rank are always derived in the backend from that
exact raw value set. Legacy derived fields remain readable but are never
authoritative.
Each valid game commits independently, so a later failed target cannot erase
earlier accepted progress. Governance requires an active, unexpired manifest
at the shared cutoff with exact `canonical_game_ledger` scope and envelope
version; final/postponed decisions use canonical Event Catalog helpers.
Historical equality considers only ledger game dates through that cutoff.
Composition jobs finish independently: incomplete assist evidence leaves its
jobs retryable while unrelated candidates advance. Traditional parity has no
legacy diagnostic left to read -- #199 dropped `general_opponent_stats` -- so
both `traditional_opponent` windows record unavailable evidence, never a
fabricated empty comparison; the same rule governs any missing diagnostic.
Runtime refresh resolves the active unexpired manifest and its exact
authorized Event Catalog snapshot before entering the provider-capable
backfill boundary.
Collection authorization and composition governance are intentionally
separate. Provider I/O and new acceptance require an active Season plus an
active manifest before `collect_before`; already accepted observations may be
composed later from their immutable manifest/cutoff even after that manifest
is expired or superseded (`scripts/ledger_refresh.py --compose-only` performs
that operation without entering collection). Every ledger observation stores that manifest ID,
the canonical ledger surface scope, schema version, and manifest cutoff, and a
candidate retains that exact timestamp and refuses mixed-manifest or
mismatched-cutoff provenance; composition never rounds the cutoff to the
calendar day. Provider
documents live only in an exact-ID staging cache during validation: successful
entries are consumed after the atomic ledger commit, while every rejected or
failed entry is discarded without affecting concurrent IDs. Validation and
per-game acceptance happen inside the bounded worker lifecycle, so retained
full provider documents never exceed the configured collection concurrency.
The backfill service has no ungoverned/direct mode: a real manifest identity,
exact canonical scope, accepted schema version, cutoff, deadline, and governed
event snapshot are mandatory before provider I/O.
Immediately before each accepted observation is inserted, the ledger
transaction conditionally locks and revalidates that exact manifest row,
including active status, server environment, season, cutoff, canonical scope,
schema version, and deadline. Manifest supersession/expiry therefore
serializes with acceptance: a provider response that returns after authority
changes is discarded with no observation, ledger facts, or composition jobs.
Every accepted observation is also cryptographically bound to the candidate
game it supplies: the stored payload's checksum must match its payload, the
payload must reproduce the exact raw evidence being persisted, and the
observation's retrieval time must equal the candidate's retrieval time exactly
after UTC normalization, so a governed caller cannot stamp a correct document
while recording a false retrieval time.

### Database-first game-log reads

Postgres is the only request-time read path. `PlayerGameLogRepository`
reports a complete publication only when the season sidecar is present with
`publication_status == complete` and the read remains valid (a historical
season's own sidecar, or a fresh stats-surface observation for the configured
current season). The request-time source serves that season from the durable
facts through `StoredGameLogsSource`, which rebuilds the canonical frame from
`PlayerGameLogRecord` primitives with the central game-log derivations. Active
immutable player-log publications also own a normalized
`publication_player_game_logs` projection keyed by `publication_id`, player,
and game. The source reads pointer/version metadata without selecting
`publication_versions.payload`, then the repository executes one indexed
player query and decodes only those rows. The matchup read takes the same
projection path: its one snapshot names `player_game_logs` as projection-only,
so that stream's payload value is never fetched (the one capture statement
guards the column behind a per-row `CASE` on stream key) while the other
publication streams in the same generation stay hydrated, and the summaries read resolves
the game's player pool from one indexed query. The selection read does the same
for one card: its snapshot is projection-only, and the head-to-head and
archetype tables each resolve their opponent's rows from one query on the
projection's `(publication_id, opponent_team_id, player_id, game_date)` index.
Composition and rollback write the
projection in the publication transaction, while migration 036 backfills
existing valid versions; the projection therefore preserves exact active and
rollback generation semantics without a season-wide cold decode. A season
without a complete, valid publication returns the endpoint's normal empty
result rather than reaching a provider or legacy repository fallback. Because
the #66 contract amendment removed plus/minus from the game-log contract, the
durable path covers every public primitive.

Authenticated matchup-selection reads compose only stored seams:

```text
GET /api/games/matchup/selection?game_id&player_id
  → strict query parsing + Firebase auth
  → current-season Event Catalog game identity by NBA game ID (one row)
  → newest reusable persisted slate Player Pool containing the game
  → selected player and posted Market Categories
  → player_clusters peer IDs
  → PlayerGameLogRepository Regular Season rates + combined-phase rows
  → newest-first game rows + one final AVG row per nonempty table
```

`MatchupSelectionService` has no NBA Stats or DFS board/provider dependency.
Its dedicated read-only Player Pool seam scans stored canonical slate scopes,
prefers the newest fresh scope containing the requested game, and uses the
existing bounded stale-serve contract only when no fresh scope is available.
It never constructs a one-game pool scope, acquires a refresh lease, or starts
provider collection. The game is read by its NBA game ID; when that row is
absent, an empty Event Catalog (checked only on the miss) or unavailable
stored pool surface is `503`, while an unknown game within a populated catalog
or a player absent from a usable pool is `404`. It resolves the
opponent from the canonical event and the selected pool player's team, excludes
the selected player from the archetype peer query, and asks the durable log
repository for H2H/archetype rows. Rows without positive minutes or a usable
stored Regular Season rate are omitted rather than assigned a synthetic
baseline. Posted Market Categories are derived from the reviewed Statistic
Catalog components, including combinations and FG2A (`FGA - FG3A`). Each game
delta is `stat / minutes - own Regular Season rate`. An AVG row reports
per-game mean stats/minutes and a minutes-weighted rate delta; this preserves
each archetype sample player's own baseline instead of inventing one aggregate
player. `MATCHUP_SELECTION_H2H_MIN_GAMES` and
`MATCHUP_SELECTION_ARCHETYPE_MIN_GAMES` own the delivered table thinness.
Empty tables stay `200`, carry `thin: true`, and have no AVG row.
The response carries the stored Player Pool freshness document plus an explicit
`fresh|stale|missing` durable-log read status and timezone-aware publication
time. Stale and missing log surfaces therefore remain degraded `200` responses
without becoming indistinguishable from a fresh publication with no applicable
history. Game rows identify the sampled canonical player and name; AVG identity
is null. Repository rate calculation and selection-row calculation share one
validated component-value authority, so catalog-sanctioned derived components
cannot diverge between the two paths. A stored category removed from the
current catalog fails explicitly as `503` before row calculation.

Authenticated full-matchup reads likewise compose stored seams only:

```text
GET /api/games/matchup?game_id
  → strict query parsing + Firebase auth
  → current-season Event Catalog game identity by NBA game ID (one row), schedule freshness, and the newest completed-game tip time
  → newest reusable persisted Slate Player Pool containing the game
  → PlayerGameLogRepository bulk Season rates + combined-phase last ten
  → PlayerDietService bulk Season facts
  → TeamMatchupQueryService newest Season + exact team Last-15 windows
  → gated MatchupInjuryService lazy pre-tip observation or retained final snapshot
  → backend-shaped league/team metrics, availability, and freshness
  → additive Historical Matchup declaration and section-owned evidence
```

### Historical Matchup composition (#208, #209)

`MatchupService._experience` shapes the additive `experience` block, and
`app/domain/matchup_experience.py` owns the one eligibility rule and the wire
vocabulary both Matchup responses declare. A game is historical when the
resolved Event Catalog event is completed and not postponed and the composed
Player Pool contributes no player for either team; the Regular Season
restriction is already enforced by the event read, which refuses every other
kind with `404`. A closing projection
set with memberships always contributes players, so that single condition is
exactly "no archived closing projections", and it also keeps a legacy
deployment's still-servable stored pool on the current experience instead of
silently discarding real pool players.

In historical mode the rail is composed from stored evidence rather than a
Player Pool. `PlayerGameLogRepository.get_sync_status` is the completeness
authority, and `list_game_rows` returns the exact game's canonical rows; both
follow the same publication-projection, publication-payload, then legacy read
order as every other player-log seam. Each row becomes one `_Participant`
carrying the identity recorded for that game, so a later trade cannot rewrite
which side a player was on.

crf04/statsplus#47 supersedes #42's focal-free scoring rule: a Historical
Matchup now scores uniformly from the completed-season evidence the page
already displays, focal game included, with hindsight disclosed by label
rather than by withholding an input.
`MatchupService` reads one shared player season-summary once, in both modes,
and that one read feeds `season_scoring`, `last_10_minutes`, and the Matchup
Score inputs alike; there is no second, focal-row-excluding read. That summary
is season-to-date evidence in current mode and completed-season evidence,
focal game included, in historical mode specifically. A historical Matchup
Score consumes the same season Defense Sheet window
`TeamMatchupQueryService.get_latest_window` returns for both modes — the
completed-season read that honors the #41 completed-season exemption — and the
same stored Player Diet facts the Diet Shares display reads.
`MatchupService._focal_safe_team_window`,
which called the strict, pre-focal `get_focal_safe_window` read, is retired
along with the `score_windows`/`score_diets` split it fed; `windows`,
`metric_indexes`, and `availability` now drive both display and scoring in
every mode. `TeamMatchupQueryService.get_focal_safe_window` itself stays: it
is still exercised directly at the query-service level (see
`tests/services/test_nba_publication_matchup_materialization.py`),
independent of `MatchupService`. Player Diet is no longer excluded from
scoring either — `player_diet_facts` is the one completed-season aggregate
per `(season, player_id, base, slice_key)` the Diet Shares display already
reads, and a historical score now consumes it exactly as a current-mode pool
player's Diet would. Last-15 still has no point-in-time snapshot for a
completed season, so its `team_defense:<base>` inputs still name themselves in
`missing_inputs` and Last-15 Matchup Scores stay unavailable, unchanged from
before.

Score formulas and thresholds are unchanged; each window gains a
`missing_inputs` list naming the score-contract inputs it could not consume. In historical mode `MatchupService._presented_window` then withholds
the Blend of any window with a nonempty `missing_inputs`, so an unavailable
cell explains itself instead of presenting a mean of the surviving Bases as a
complete blended score. Withholding happens on the presented copy, not the
memoized one, so a combo still composes from its parts exactly as it did and
then withholds its own Blend under the same rule. Current-mode windows are
byte-identical to what they were.

Every section reports from its own evidence. A Defense Sheet section is
available whenever any governed Base is available for that window, so a missing
Player Pool, a missing legacy `stats_tables` marker, an unavailable Last-15
window, missing participants, and unavailable injuries can none of them
suppress an available Season Defense Sheet.
`league.surface_availability` stays the per-Base authority. Completed-season
Schedule evidence is immutable, so the Schedule section reports the Event
Catalog collection time as `collected_at` provenance rather than an age-only
stale warning; the separate `freshness.schedule` surface keeps its existing
age-based status. The two surfaces answer different questions and neither is
derived from the other.

`MatchupSelectionService` mirrors the declaration. Both services ask
`app/domain/matchup_experience.py` the same eligibility question and use its
wire vocabulary, so the two responses cannot drift apart on what a Historical
Matchup is. When the pool names nobody for a completed game, selection first
requires the focal game's `get_sync_status` to be complete — the same
completeness gate the Matchup route applies to Participants — and fails closed
with `provider_unavailable` otherwise, rather than resolving a participant from
rows that may still be partial. It then resolves the participant from the focal
game's canonical rows, scores the governed Statistic Catalog categories,
restricts `h2h` and `archetype` samples to games strictly before the focal
date, and excludes the focal game from the delta baseline. The declaration adds no
provider call and no schema change: the Event Catalog, completed-season
publications, canonical player game logs, and game-time identities already
exist.

Both assemblies serve the reads they compose on one pooled connection.
`MatchupService` and `MatchupSelectionService` take the application engine,
open one connection for the request, bind the publication snapshot's session to
it, and pass it to each read seam as an optional `connection=`. Every stored
read seam these two routes touch keeps its previous per-call
`engine.connect()` as the default, so a repository called without a connection
is unchanged and wirings without an engine (the demo database and injected test
collaborators) keep today's behavior. Two seams stay on the default path
deliberately: `LatestProjectionPlayerPoolReader` owns its connection's
`REPEATABLE READ` isolation and explicit transactions, and
`MatchupInjuryService` writes a refreshed snapshot inside the same call.
Sharing one connection changes no read semantics — on READ COMMITTED each
statement still takes its own snapshot — it removes the checkout and reset
`ROLLBACK` round trip that each repository call previously paid.

`MatchupService` has no NBA Stats, PBP Stats, DFS provider, or live Player Pool
dependency for statistical reads. Injury Reports retain the existing
`MatchupInjuryService` live/snapshot contract in both assemblies; activating
database-first statistical streams does not change that contract. Missing stored
pool, player, Diet, or team-window facts degrade the response without starting
collection. The team query's latest observation is the sole window-availability
authority; when a Base/window is unavailable or missing, the response emits a
null row window even if an older fact-bearing scope remains stored. This is
especially important for provider-unsupported play-types Last-15: Season is
never relabeled as rolling data. League and team row identities are constructed
from the same `(Base, slice, stat)` taxonomy, so every team key has an exact
league denominator. Player Diets remain raw and Season-only, while Matchup
Scores cross that Season evidence with each independently stored team window.
The score implementation remains inside `MatchupService`, beside the stored
inputs it serializes; it has no provider boundary and performs no request-time
fallback. The play-types crossing is the one exception: it lives in
`app.domain.play_type_matchup` so the Log Workspace rating scores the same
definition instead of a second copy of it. Request-local indexes traverse each stored window's league/team
metrics once, and a per-player/window memo shares primitive scores across a
posted primitive row and every combo that consumes it. Components unavailable
from the stored Diet/sheet taxonomy are omitted instead of estimated. The Diet
score applies each raw observed share to the slice's fractional matchup
difference, so the unobserved residual in an admitted rounded partition has a
neutral baseline without share normalization or fabricated evidence. Every
Base except play types must arrive as a whole partition; Synergy publishes a
player's play-type row only where the sample is large enough to rate, so a
play-type Diet is complete when every observed slice is governed, and its
cell is marked thin when the observed shares cover less than 0.85 of the
player's possessions. A slice
with exact league/opponent `0/0` is likewise a neutral structural zero; nonzero
opponent evidence against a non-positive league denominator fails closed, and
an all-structural-zero component remains absent. A
blendable offensive window emits a score-cell Blend exactly when at least one
component computes; zero computable components remain `components: {}` with
`blend: null`. Newly collected traditional surfaces include `OPP_REB` for REB
and rebound-containing combos in addition to the three defensive score columns;
legacy windows missing only OPP_REB retain the defensive surface and degrade
REB locally. When the other window supplies the OPP_REB row identity, its
league/team value is null only in the legacy window even though traditional
availability remains available; OPP_TOV/OPP_STL/OPP_BLK rows and defensive
scores remain populated. OPP_REB is the sole row-level exception: every other
metric identity divergence marks the affected Base/window
`unavailable/legacy_surface_incomplete` and nulls all of its rows. Normalization
therefore validates the cross-window traditional identity union plus the three
required defensive columns, excluding only OPP_REB for that carveout. The REB
primitive's implicit-share-one traditional cell consumes no Player Diet or
player Season sample evidence, so player game count alone never makes it thin;
combo-level thinness remains unchanged. Combo components and Blends divide
their available weighted
numerators by the fixed total positive Season volume of every required part, so
an unavailable part is neutral zero rather than a reason to renormalize.
Injury reconciliation can remove a canonical Out player or attach a badge
reference; it does not change Matchup Scores, Diet Shares, scoring history, or
projected roles.

### Database-first Matchups activation (#87)

`DatabaseFirstPublicationReader` is the read-side authority for the first
activation. It follows one active `PublicationPointer` per stream, decodes
only its immutable Publication payload, and returns bounded provenance
(`publication_id`, version, Coverage Cutoff, age, and freshness). Freshness is
computed independently for every stream; an active stale Publication remains
the last-good value and is never replaced by a partial refresh or a provider
fallback. Missing or corrupt payloads degrade only that contributor. The
reader reports additive `mixed_cutoff` and `mixed_freshness` flags without
collapsing the source clocks.

`MatchupService(database_only=True)` is the production assembly used by the
authenticated Matchup route for governed statistical facts. Active streams
are decoded from immutable PublicationVersion payloads; an explicitly
inactive stream is the only state that permits its legacy repository fallback,
while a missing or malformed active payload degrades closed. The existing
Injury Reports seam is unchanged: it may use the live/snapshot behavior
already provided by `MatchupInjuryService`; statistical activation does not
change that injury contract. `DatabaseOnlyProviderGuard` is available to
tests and raises on any forbidden statistical provider attribute access. The
route composes durable Regular Season facts and retains the existing
success/degraded/missing shape. Event classification rejects Playoffs and
Play-In during this first activation, and the registry keeps `synergy:l15` as
`never_schedule`/`provider_window_unsupported`.

`PublicationService.activate_stream` is additive and auditable through
`PublicationActivation`; `rollback` and composition use the existing per-stream
fence. `LegacyWriteFence` is injected into the legacy Event/Athlete Catalog,
 player-log, Player Diet, and team-matchup writers, so activated streams reject
 old writes while the tables remain readable for rollback and validation.
Migrations 029 and 030 create and then bind the activation evidence table to
an immutable PublicationVersion with a unique stream/candidate constraint;
no legacy table is removed.

`HistoricalRehearsalRunner` requires seven ordered dates, a concrete isolated
collection/composition callback that returns raw governed facts, and a
completed-season Synergy callback with a candidate publication and raw facts.
It derives exact/normalized parity from isolated Publication payloads; a
difference requires a persisted approved adjudication. Operator evidence also
requires an explicit separate production snapshot database; unit evidence does
not claim production immutability.
`FailureDrillRunner` exercises a migrated temporary control plane, including
publication, control-plane receipt idempotency, residential Outbox replay,
alert recovery, and backup/restore seams. SQLite backup/restore is an explicit
local unit adapter; a production gate refuses to claim evidence without a
configured Postgres URL and a completed Postgres backup/restore artifact. The
benchmark invokes distinct complete MatchupService callables, requires zero
provider calls, and retains bounded SQLite or PostgreSQL query-plan evidence
alongside p95 values; it records but does not claim a recovery-time or
recovery-point SLA.

If independently published windows have asymmetric identities, response-local
availability normalization marks only the incomplete Base/window
`unavailable/legacy_surface_incomplete` and nulls that window's rows. An event
team absent from the governed franchise facts similarly becomes
`missing/team_not_in_governed_roster`. Neither condition starts collection or
turns a known game into a whole-request error. Shot-zone market membership is
derived from `(Base, slice, stat)`, keeping two-point zones out of FG3A and
three-point zones out of FG2A. Response composition also projects stored
shot-zone facts onto the same five nonoverlapping slices as Player Diets,
excluding Left/Right Corner 3, Backcourt, and unknown duplicates without
aggregation. Missing governed slices degrade only that Base/window. Stored
shot-type lookup keys remain unchanged, while response keys use the exact
Player Diet vocabulary `Catch and Shoot`, `Pullups`, and `Less Than 10 ft`;
missing or unknown shot types likewise degrade locally.

The response freshness document does not collapse independent publication
clocks. It retains schedule and stored-pool freshness, the legacy stats-table
completion, player-log read freshness, each Player Diet observation, each team
Base/window observation, and injuries. This preserves the landed modules'
truth instead of presenting one Nightly timestamp as if every source succeeded
together. The stats-table status is stale when its successful completion
predates the newest completed, non-postponed Event Catalog game. That instant
is one narrow catalog read (`latest_final_scheduled_at`, a `max(scheduled_at)`
over final, non-postponed rows of the season); the full season catalog is
never loaded for a matchup. Started and past games bound both team
windows to their Eastern Slate Date; future tips query the current latest
stored scopes with no future `as_of` value.

### Durable refresh jobs

`DataRefreshJobService` writes queued/running/succeeded/failed transitions and
a sanitized `failure_summary` (exception/provider text is never stored), and
its partial unique index enforces at most one queued or running job per
operation. Migration 003 adds the request ID plus an owner, expiry, heartbeat,
and attempt count. A process claims queued or expired rows with a conditional
SQL update, renews its lease while the registered handler runs, and clears the
lease on completion/failure. The app factory creates one coordinator per app
with the closed registry for `update_database`, both PBP refreshes,
`fetch_players_with_teams`, and `fetch_players`; operation names, not callbacks,
are persisted. A bounded dispatcher wakes immediately after enqueue and polls
every 15 seconds (`poll_interval`) per process for restart and expired-lease
recovery; the 60-second lease is renewed by a separate 5-second heartbeat
while a handler runs, so the poll cadence bounds only recovery latency, not
lease health. The service takes an injectable executor and clock;
tests use a `SynchronousExecutor` and `dispatch_once()` so job completion is
deterministic.

Execution is at-least-once: a process crash or an expired lease can cause a
refresh handler to run again, so handlers must tolerate repeated provider
work. Each claim increments `attempt_count`, which is also the fencing token.
Before a handler swaps its staged tables, `AtomicTablePublisher` renews the
claim through a conditional update on the *same* `engine.begin()` connection.
That update and all table swaps commit atomically; a stale attempt therefore
fails before changing a live table and cannot overwrite a newer attempt. This
protects publication and durable job state, but it cannot cancel provider work
that was already in flight before a lease expired.

The `DataService` refresh callables first collect every provider frame in
memory (failures stop the job before any write), then hand the whole related
set to `AtomicTablePublisher`, which stages each `DataFrame` under a unique
name and swaps every name inside one `engine.begin()` transaction. Readers
never observe a mixed old/new set, and a failed swap rolls back to the
previous tables.

`update_all_data` publishes the still-refreshed subset, not everything it
collected. Before publishing it partitions the collected frames against the
injected `LegacyWriteFence`: a table whose database-first stream is activated
is refused with a logged `legacy_write_fenced` refusal and skipped, and the
remaining tables are published as one atomic set with the stats-freshness
completion. An activated stream therefore fences only its own table instead of
aborting the publication of tables that have no database-first replacement,
and the refresh succeeds. The authoritative fence check remains inside the
publication transaction, so a stream activated between the partition and the
swap still fails that publication closed. A fence that cannot be read still
fails the whole refresh closed. When every collected table is fenced the
refresh publishes nothing and succeeds without advancing stats freshness; the
per-table inventory is in
[DATABASE_FIRST_ACTIVATION.md](DATABASE_FIRST_ACTIVATION.md).

### Durable Season player Diet facts

`PlayerDietService` is the Nightly Refresh's fifth ordered step, after the
stats-table, Event Catalog, Athlete Catalog, and durable player-game-log
refreshes and before team matchup facts. It exposes only
`refresh(season)` and `get_for_players(season, player_ids)`. The query is a
stored bulk read and never contacts a provider. Player Diets are Season-only:
there is no player Last-15 window and no traditional Diet Base.

One refresh has a fixed 16-call plan: the 11 offensive `PLAY_TYPES` through
player Synergy with `POSS_PCT`, `POSS`, and `GP`; the three `SHOOTING_TYPES`
through league-wide `LeagueDashPlayerPtShot` GeneralRange calls with
`FGA_FREQUENCY`, `FGA`, and `GP`; one league-wide
`LeagueDashPlayerShotLocations` call; and one PBP player-totals call. Shot-zone
games played come from the three fixed player-shot observations; the joined
union must cover every shot-location player and may not disagree on `GP`.
When the shot-type Base is malformed or cannot join, the shot-zone response is
still validated independently but cannot become a fact without authoritative
games played. Prior valid zone facts remain stored, and the newer shot-zone
observation is therefore
`unavailable/missing_games_played_evidence`, not
`unavailable/provider_invalid_response`.
There are no per-player shot calls and no request-time fallback.

Canonical player identity comes only from the fresh, nonempty Season Athlete
Catalog. NBA rows retain `PLAYER_ID`; PBP rows retain `EntityId`. Names are
evidence only and are never a join. An unjoined provider identity makes its
whole Base unavailable rather than publishing a partial canonical Base.
Available facts require unique `(season, player, Base, slice)` identities,
finite shares in `[0,1]`, finite nonnegative volumes, and positive whole-number
games played. Provider-origin finite domain violations and duplicate fact
identities degrade only their Base as
`unavailable/provider_invalid_response`; validation is repeated at repository
publication as a direct-caller guard. Play types store provider possession
share and possession volume directly; they never reuse the legacy
percentage-of-points transform. Synergy repeats a traded player once per team
stint, so the collector combines a player's stints for a play type into the one
fact its identity allows: volume and games played add, and the share is total
possessions over the team possessions each stint's own share recovers. A stint
whose share is not positive has no recoverable denominator and degrades the
Base rather than being dropped. Shot
types store provider FGA frequency and FGA. Shot zones store five nonoverlapping
display slices, using the provider's aggregate `Corner 3` and excluding its
duplicating left/right children and Backcourt. Assist-location volume uses the
five nonoverlapping PBP location counters divided by total assists. PBP's wire
rows are sparse: canonical identity, games played, and total assists are strict;
an omitted named location counter stays absent and never becomes a synthetic
zero fact.

Migration 013 creates `player_diet_facts` and
`player_diet_surface_observations` after landed migrations 011 and 012. Facts
carry raw share, raw volume, games played, volume unit, provider, and a
timezone-aware retrieval time. Observations are the single per-Base authority
for `available`, `unavailable`, or `missing` plus a bounded reason. All four
observations and every available Base are validated before one transaction.
The transaction replaces available Bases and the observation set together;
degraded Bases retain their last valid facts with their older retrieval time,
while the newer observation states the degradation. A transport or database
failure publishes nothing, so Nightly's existing whole-unit retry starts again
from stats. Bulk reads return even very small raw shares; display thresholds
are deliberately outside this module, and absent requested players or slices
never receive synthetic zero facts.

### Window-aware team matchup facts

`TeamMatchupRefreshService` is the Nightly Refresh's sixth ordered step, after
the stats-table, canonical Event Catalog, Athlete Catalog, durable player
game-log, and Season player-Diet refreshes. It publishes two
internal team windows for the current product: Season and exact rolling 15
games. There is deliberately no public matchup route in this layer; the narrow
consumer seams are `TeamMatchupQueryService.get_window(scope)` and
`get_latest_window(season, window_games, as_of)`. The latter deterministically
selects the greatest observation date no later than an optional Slate Date,
while resolving each surface's most recent fact-bearing scope independently.
It therefore returns the last usable facts alongside the newest persisted
failure or freshness observation instead of letting a fact-free failed run
hide yesterday's valid values. Each surface reports both the scope and
`retrieved_at` of those facts separately from the latest observation's
`retrieved_at`, status, and reason, including when a later failure happened on
the same Slate Date. Consumers can therefore label stale-served metrics
without overstating their freshness. Future cutoffs are rejected and the default
cutoff is the injected clock's current ET date, so future-dated rows cannot
shadow current data. Publication instants are compared with the exclusive end
of that America/New_York Slate Date, including DST transitions; an evening UTC
date rollover that remains on the requested Eastern day is not future data.

Rolling boundaries come only from completed, governed `event_catalog` games.
The resolver excludes postponed, preseason, All-Star, non-final, and
post-as-of events, then resolves each team's own 15th-most-recent game. The
30-team roster is derived from governed regular-season/playoff catalog events,
so preseason and exhibition participants cannot contaminate it. A homogeneous
window carries its governed `Regular Season` or `Playoffs` provider phase. A
window crossing those phases is retained as canonical evidence but published
unavailable because the approved aggregate providers cannot represent that
exact mixed game set in one request.

The legacy provider refresh path queries NBA Stats surfaces per team with `LastNGames=15`, `TeamID`, the
team-specific `DateFrom`, common `DateTo`, and matching phase. NBA dates use
the provider's `MM/DD/YYYY` format. The traditional and shot-type aggregate
responses must identify the requested team and report exactly 15 games; the
shot-zone aggregate must identify the team (that endpoint exposes no game
count). For parity-bearing traditional and assist surfaces, the provider
response must additionally return exact game IDs that match the immutable
Event Catalog authority; a missing-ID aggregate is unavailable even when its
GP count matches, and catalog IDs are never copied into the provider row. A
surface that cannot prove its requested aggregate is discarded and
observed as `unavailable/provider_window_unverified`, never mislabeled
Last-15. PBP Stats opponent totals use `TeamId`, matching phase, that team's
ISO `FromDate`, and the common ISO `ToDate`; its response must identify the
team, expose positive `SecondsPlayed`, and report an aggregate count of exactly
15 games. Those rolling-window evidence fields are enforced by the matchup
refresh rather than the shared PBP Season parser, whose pre-existing contract
requires only the opponent assist fields it consumes. A date-bounded PBP
response without that proof is `unavailable/provider_window_unverified`; one
league-wide cutoff is never used. The recorded BOS `2025-03-01` through
`2025-04-15` response demonstrates why: it reports 525 assists but also
`GamesPlayed=22`, so it is valid provider data and explicitly not a Last-15
aggregate. Synergy exposes neither Last-N nor date
bounds, so its fact-free Last-15 play-type observation is
`unavailable/provider_window_unsupported`; no Season value is relabeled as Last-15.
Season defensive play-type facts persist the raw `PTS` and `POSS` pair for
each governed slice; `GP` is provider evidence rather than a display metric.
The traditional NBA surface persists `OPP_REB`, `OPP_TOV`, `OPP_STL`, and
`OPP_BLK` with the same authoritative minutes denominator for both windows.

Before every team has 15 governed completions, the same transaction publishes
the usable Season snapshot plus a fact-free Last-15 snapshot whose surfaces
are `missing/insufficient_governed_games`. Nightly refresh therefore remains
successful early in the season. Season NBA and PBP aggregates are bounded by
the snapshot date. Season Synergy is collected only for a current-date
snapshot; a backdated as-of cannot bound Synergy and records
`unavailable/provider_unbounded_as_of` instead of combining mismatched scopes.
If the governed catalog does not yet identify exactly the NBA's 30 teams, the
refresh also succeeds with fact-free
`missing/governed_team_roster_incomplete` observations. Neither case deletes
an earlier valid fact snapshot.

Migration 012 creates `team_matchup_facts` and
`team_matchup_surface_observations`. It follows the authoritative player-log
migration 011, so fresh databases apply 011 and then 012.
A fact is identified by season, as-of date, window kind plus rolling game
count, team, Base, slice, and stat. The fact table contains available facts
only; availability and reasons belong solely to the observation table. Facts retain the provider's raw
numerator and its raw minutes or seconds denominator; they do not
persist a previously normalized ratio or rank. Surface observations retain a
timezone-aware collection time and an `available`, `unavailable`, or `missing`
status plus explicit reason per window.

Both windows are fully collected before one repository transaction replaces
observations and each newly valid surface. Repeating a publication is
idempotent. Available surfaces are written only when every metric has the same
30 distinct teams, those team IDs exactly match the governed catalog roster,
and every row has finite raw numerators plus positive finite minutes or
seconds. A substituted off-roster team becomes
`unavailable/provider_roster_mismatch`; partial, non-finite, or mislabeled data
becomes a fact-free unavailable observation and leaves prior valid facts
intact. A provider transport failure degrades only that provider-owned surface as
`unavailable/provider_unavailable`, preserves its prior valid facts, and
publishes independently successful surfaces. The team-matchup step returns a
failed disposition after that atomic partial-success write, so Nightly Refresh
still retries the complete stats → schedule → athlete catalog → player game
logs → player diets → team matchups unit once and alerts if the retry also
fails. A transaction failure leaves both prior snapshots intact. A provider
response that reaches its adapter but is malformed instead degrades only that surface as
`unavailable/provider_malformed_response`, preserves its prior valid facts,
and allows other surfaces to publish. The query service defensively degrades only an
affected incomplete legacy surface and derives allowed-per-48 from valid raw
facts. It then computes the 30-team mean, population sigma, percent versus
average, sigma deviation, and defensive rank. Player scoring and Diet Shares
are outside this team-window store and remain Season-only. If the 30-team mean
is zero, percent-versus-average is null because that ratio is undefined; a
zero population sigma yields a conventional zero sigma deviation.

One fully available run makes 77 Season provider calls: 46 NBA calls (the 16
aggregate/shot/Synergy calls plus 30 independent TeamGameLog membership
calls) and 31 PBP calls (one aggregate plus 30 independent team-game-log
membership calls). It makes 240 rolling calls: six NBA plus two PBP calls for
each of 30 teams, including one independent membership call per provider and
team. The calls stay
sequential: each NBA call already uses the shared configured timeout,
concurrency bound, and provider telemetry, while each PBP call uses the shared
pooled session, connect/read timeouts, retry accounting, and telemetry. The
Nightly Refresh supplies the one whole-unit retry. Adding another concurrency
layer here would multiply load against rate-sensitive upstreams, so the exact
request plan is preferred over speculative parallelism.

### Ledger-owned Season and exact L15 matchup materialization (#114)

`LedgerMatchupMaterializationService` is the high-level seam that turns stored
Canonical Game Ledger evidence into the disposable `team_matchup_facts` read
model without any provider call. It accepts one season and a shared cutoff
(`materialize(season, as_of=...)`), loads the governed Regular Season ledger
games through that cutoff, selects the full governed game set for the Season
window and each team's exact 15 most recent governed games for the L15 window,
and records the exact selected game IDs plus a deterministic ledger checksum
(SHA-256 over the sorted `(game_id, checksum)` pairs of the selected set) on
both the fact rows and the surface observations. The ledger command path
(`scripts/ledger_refresh.py --compose` / `--compose-only`, via
`LedgerRuntime.compose_queued`) publishes this read model at the exact
composition cutoff before composing the inactive publication streams, so an
incomplete Season or pre-15 L15 publishes explicit unavailable observations
instead of approximating a league window.

Every contracted PBP-owned non-shot opponent fact is aggregated exclusively
from typed ledger counts and denominators: the four traditional opponent
surfaces (`OPP_REB`, `OPP_TOV`, `OPP_STL`, `OPP_BLK`) come from the opposing
team's raw counts over the selected window with the nominal game-length
denominator — `OPP_TOV`/`OPP_STL`/`OPP_BLK` from the opposing team
fact, `OPP_REB` from the opposing players' rows because the legacy
`LeagueDashTeamStats` contract excludes team-only rebounds — and the six assist surfaces (`Assists` plus the five
location counters) come from the opposing players' counts over the same
denominator. No PBP or NBA traditional/assist aggregate endpoint is called;
the service has no provider collaborators at all, so it cannot trigger one.
Player and team authority follow the approved #113 model unchanged: team facts
from team-summary rows, player facts from player rows. NBA-owned shot and play
surfaces are composed through the same injected database-first seam as
independent publications. Their refresh and validation are separate, so a
failed or unavailable NBA-owned surface cannot prevent valid ledger-owned
surfaces from materializing, and ledger facts cannot substitute for it.

The two windows share one cutoff. A Season that is not league complete (fewer
than 30 governed teams) publishes fact-free
`missing/governed_team_roster_incomplete` observations for both windows.
Before every governed team has 15 eligible games the league L15 is explicitly
`missing/insufficient_governed_games`, never approximated from partial
evidence. A complete 30-team window carries deterministic competition ranks
(`1, 1, 3` ties) derived only after the governed window is selected, and an
incomplete window publishes no league ranking. Missing assist-location
evidence degrades only the `assist_locations` surface
(`unavailable/assist_location_evidence_incomplete`) while the traditional
surface still publishes. Migration 034
(`034_team_matchup_ledger_lineage`) adds the nullable `game_ids` (JSON) and
`ledger_checksum` columns to `team_matchup_facts` and
`team_matchup_surface_observations`; provider-collected legacy rows keep their
ledger-only checksum columns NULL. Migration 042 adds immutable legacy
`manifest_id`, Event Catalog publication ID/checksum, and
`provider_window_identity` evidence. A fresh parity-bearing provider write
must verify each aggregate's returned window/game count against that exact
authority before storing its game IDs; old nullable/date-only rows are not
backfilled and are rejected by `StoredLegacyMatchupSource`. The existing
authenticated Matchups and player-game-log HTTP contracts remain provider-free
at request time.
Correction propagation adds nullable source-observation lineage, exact
game-set checksum, cutoff, and recomposition reason to those read-model rows.
Composition jobs retain the correction game, affected teams, source lineage,
and corrected ledger checksum so targeted Season/L15 recomposition can be
retried without reconstructing the original acceptance request. The additive
compatibility upgrade runs with the existing migration head; no public route
contract changes.
Migration 037 adds nullable publication ID, version, cutoff, and
freshness lineage to those same rows. The existing authenticated Matchups and
player-game-log HTTP contracts are unchanged and remain provider-free at
request time.

### Governed NBA team-window publications (#115)

The same materialization seam may receive a request-scoped
`DatabaseFirstPublicationReader` generation for the NBA-owned team-window
streams: exact shot zones, grouped shot types, and Season Synergy play types.
Each available publication row is projected into the existing
`team_matchup_facts` read model with its registered taxonomy and the source
publication ID, version, coverage cutoff, freshness label, and publication
game IDs. Ledger-owned traditional and assist facts retain their own ledger
lineage; the two authorities are never blended.

Publication reads are independent per surface. A missing, stale-invalid, or
unsupported publication records only that surface's unavailable/missing
observation and never falls back to a PBP fact. Synergy Last-15 remains
`unavailable/provider_window_unsupported`, and Season values are never copied
into that window. The database-first Matchups query keeps this fence even for
legacy-fallback-shaped readers, while preserving the existing compatibility
fallback for ledger-owned traditional and assist surfaces. Publication
provenance and mixed freshness/cutoff metadata remain additive and request
time provider-free.

A publication cut after the requested slate day is normally withheld as
`publication_cutoff_after_as_of`, because a later snapshot of a moving window
would put games the requested date had not seen into that date's numbers. One
case is exempt. When the Event Catalog bound to the read's own manifest is
complete and every Regular Season event it governs is already final, the
season is over and its season aggregate can no longer move, so the season
snapshot holds exactly the same games for every date in that season: the
refusal would be about timestamps rather than content. This holds for every
governed base — the ledger-derived traditional and assist-location seasons as
much as the NBA-owned play-type and shot seasons — because all of them are one
aggregate over the same finished set of games. The Matchups read then
serves the `season` window and names `season_complete_snapshot` in
the team-window provenance alongside the publication ID and cutoff, so a
reader can see the window came from a post-season snapshot. The exemption is
window-specific: an L15 aggregate keeps moving with the calendar even after the
season ends, so an August last-15 snapshot is not January's last fifteen games
and stays refused. Completeness is proved by the governance the publication
itself is bound to, and a resolver that cannot prove it leaves the refusal in
place. The reason describes one read at one `as_of`, not the immutable
publication row, so it is reported rather than stored and needs no migration.

### Matchup materializer parity and legacy writer fencing (#117)

`app.services.matchup_parity` owns the bounded dual-run that proves the legacy
provider-aggregate writer and the ledger materializer selected the same
governed teams and games and produced the same contracted facts before the
legacy writer is fenced. The two materializers write the same
`team_matchup_facts` surface rows, so their outputs never coexist in one
stored snapshot and a fact's identity has no provider dimension; each side
must be produced into its own isolated store or captured in memory and then
handed to the comparator as an independent `MatchupMaterialization` (season,
window, exact aware cutoff, facts, observations, and per-team game sets).
`compare_matchup_materializations(legacy, ledger, *, surface,
expected_team_ids, expected_game_ids_by_team, tolerance)` compares one surface
per call — `traditional` or `assist_locations`, the two ledger-owned non-shot
surfaces — and returns an immutable `MatchupParityReport`. NBA-owned shot
zones, grouped shot types, and Synergy play types are composed from governed
publications and have no legacy-vs-ledger dual-run.

`MatchupParityRunner` is the bounded dual-run seam: it resolves the governed
30-team roster and exact Season/L15 game sets from the injected governance
reader — the checksummed immutable Event Catalog publication bound to the
active manifest, never the mutable stored event table — and compares the two
sides without reading or advancing a `PublicationPointer`. Each side's exact
cutoff must be the same aware immutable manifest cutoff; the runner rejects
mismatched cutoffs, and the persisted artifact is bound to that exact cutoff.
`LedgerParityArtifactRepository.record_matchup_parity` rejects a `stream_key`
that does not name the report's own surface and window and a cutoff that is
not aware and equal to the report's, so an L15 artifact can never authorize a
Season stream. The runner records one artifact per ledger-owned stream
(`traditional_opponent_season`/`_l15`, `assist_locations_season`/`_l15`) bound
to its exact Publication and payload checksum. Both `assist_locations_*`
streams are parity-required for activation, exactly like the traditional and
per-36 streams.
The legacy writer proves membership independently for both parity surfaces:
NBA Stats TeamGameLog supplies traditional IDs and the PBP Stats team
game-log detail endpoint supplies assist IDs, each bounded to the governed
window. Aggregate game counts cannot stand in for membership, and a
same-count or missing-ID response leaves that surface unavailable. The
persisted provider-window identity retains both exact source memberships and
the immutable Event Catalog authority rather than relabeling catalog IDs after
collection.
Activation requires the complete aligned four-stream Season+L15 cohort at one
exact cutoff; activation selects the newest fully valid artifact per stream,
ignores rejected/superseded historical reruns, and verifies that the supplied
candidate/artifact is the selected member. All four selected artifacts must
share one exact manifest/Event Catalog/cutoff authority, so a one-window CLI
run or one-stream artifact cannot activate by itself.

The comparison rules match the parent's parity contract exactly. Team identity
sets must be exactly equal and League Complete (the governed 30-team roster),
with no duplicate or extra metric/game-set keys.
Every team's exact Season/L15 game set must match the governed authority and
each other, proven by byte-identical game-set checksums; a missing surface or
a single missing metric fails. Integer counts — the four traditional opponent
counts and the six assist surfaces — compare exactly; the single documented
tolerance `MATCHUP_PARITY_TOLERANCE` (`1e-9`) applies only to floating
denominators (the nominal game length derived from retained effective team
minutes; the legacy window value from either aggregate provider, normalized
from seconds, is likewise read as the nominal length it establishes when
within the separate 0.05-minute evidence band) and
to the per-48 rates recomputed from counts and denominators. The ledger
publication's served per-48 and competition-rank fields are also bound to
those recomputed values; a missing or incorrect served value is a hard
`served_rate_mismatch` or `served_rank_mismatch` failure. Deterministic
competition ranks (`1, 1, 3` ties) are re-derived per metric from each side's
per-48 values and compared, so a sub-tolerance near-tie flip that would change
a ranking still fails. Independent per-surface availability is compared. An
unavailable or missing observation is a real difference unless legacy facts
from the same immutable authority and cutoff are explicitly captured as
retained last-good evidence; the failed observation remains recorded beside
that marker.
Every produced difference carries exactly one classification from the closed
vocabulary (`league_incomplete`, `missing_legacy_team`, `missing_ledger_team`,
`game_set_mismatch`, `integer_count_difference`, `non_integer_count`,
`denominator_tolerance_exceeded`, `derived_rate_difference`,
`ranking_difference`, `availability_difference`, `cutoff_mismatch`,
`missing_surface`, `missing_metric`, `extra_metric`, `duplicate_metric`,
`l15_game_count_mismatch`, `scope_mismatch`, `authority_mismatch`, and
`invalid_denominator`, `served_rate_mismatch`, and `served_rank_mismatch`).
Hard classifications produce a `failed` report inside a
`pending_adjudication` artifact. They may be audited as rejected but cannot be
approved. Difference-free artifacts become `exact` automatically; every
difference artifact remains pending until an audited decision. Floating denominator/rate differences in the
required public fields are hard failures; provider rounding is diagnostic
context only and never activation authority.

`app.services.matchup_parity_operation.MatchupParityOperation` is the deep
application module that owns authority resolution, candidate composition and
verification, capture validation, comparison, transaction outcomes, and
sanitized output. `scripts/matchup_parity.py` only parses command-line input
and selects that interface.

The supported sequence is `prepare`, residential-host `collect-per36`, audited
`capture-per36`, then `compare`. The immutable authority must describe the
completed 2025-26 Regular Season (30 teams, 82 completed non-postponed games
per team, 1,230 unique games); a partial catalog marked complete is rejected.
`scripts/matchup_parity.py compare` requires an explicit database URL, Season,
manifest ID, actor, sanitized output path, `isolated|candidate` target, and a
scoped per-36 diagnostic-capture ID. One
invocation runs both Season and exact-L15 matchup windows plus the player
Season per-36 comparison. It resolves the exact manifest cutoff before work,
preflights migrations, Active Season/phase, and stream/pointer state, then
composes all five candidates from the governed
canonical ledger before comparison. It never reads the unscoped legacy
`player_per36_stats` table. The command prints a bounded human table followed
by a clearly labeled protected-operator section containing the required exact
Season/L15 game IDs. It persists only tracker-safe sanitized summary fields,
records the per-stream artifacts atomically,
and exits distinctly for exact, pending adjudication, and invalid evidence;
invalid summaries include pointer/stream nonmutation proof when state capture
succeeded. Required denominator/rate differences are never approvable.
`scripts/matchup_parity.py adjudicate` records the operator decision. The
reports are the evidence backend #87 consumes for database-first activation.
See
[MATCHUP_PARITY_OPERATIONS.md](MATCHUP_PARITY_OPERATIONS.md) for the operator
runbook.

Fencing is per stream and per surface. The legacy
`TeamMatchupRefreshService` writes through
`TeamMatchupRepository.replace_snapshots`, which enforces the injected
`LegacyWriteFence` against the exact ledger-owned stream keys
(`traditional_opponent_season`, `traditional_opponent_l15`,
`assist_locations_season`, `assist_locations_l15`) for only the changed
surfaces, so activating a Season stream fences its own writer without fencing
L15 and a traditional write never fences assist locations. Once a stream is
activated the legacy provider-aggregate writer for that surface raises
`legacy_write_fenced` and fails closed rather than competing with the ledger.
NBA shot zones, grouped shot types, and Synergy play types remain governed and
operational: their facts and observations are written through the separate
`replace_governed_publication_snapshots` path, which verifies the active
publication capability and deliberately bypasses the legacy fence, so the
legacy NBA-owned writers are not fenced by ledger activation. The request-time
read fence is symmetric: an activated ledger-owned stream serves only its
immutable Publication, an inactive stream is the only state that permits the
legacy repository fallback, and an NBA-owned stream never falls back to a PBP
or ledger fact. Activation and rollback both move the fenced pointer atomically
and preserve the last-good Publication, so no competing source of truth is ever
silently restored.

### Canonical athlete catalog

`AthleteCatalogService` owns the application tables `athlete_catalog` and
`athlete_catalog_freshness`. An operator invokes
`scripts/refresh_athlete_catalog.py` with one or more explicit `YYYY-YY`
seasons; there is no wall-clock season default and no background timer. The
NBA Stats `player_roster` seam requests `CommonAllPlayers` for each season and
normalizes stable NBA player IDs, official display names, roster status,
season, and team identity. A `(season, player_id)` key lets a later official
display-name update replace that season's label without rewriting prior
history. Active, inactive, and historical rows remain distinguishable.

Provider collection happens before publication. Each requested season's
catalog rows and success metadata are written in one transaction, so an
interrupted publication rolls back to the prior catalog. Failure metadata is
recorded independently afterward; it never clears the last successful
catalog and is not coupled to a future Event Catalog. A multi-season refresh
returns and prints one sanitized success/failure result per season; the
operator command exits nonzero if any season fails while retaining successful
seasons. `get_catalog()` and
`get_freshness()` read the persisted rows, with freshness controlled by
`ATHLETE_CATALOG_FRESHNESS_DAYS` (default seven days). The tracked demo
database is rejected as a migration or catalog target.

### Provider athlete mappings

`AthleteResolver.resolve(provider, evidence, season)` accepts one typed
provider `AthleteEvidence` value and an explicit requested season;
`resolve_market(market, season)` is the board-facing spelling that lifts a
market's athlete and team evidence, and `for_board(season, identities)`
returns the same two calls served from one read of the catalog and mapping
state for a whole board. There is no other call shape. Resolution
compares only the accent/case/punctuation-normalized official name among
active `is_active_for_season` catalog rows. Normalization strips combining
marks and folds the documented non-decomposing Latin letters (`ø`→`o`,
`œ`→`oe`, `æ`→`ae`, `ł`→`l`, `đ`→`d`, `ð`→`d`, `þ`→`th`, `ħ`→`h`, `ŧ`→`t`,
`ı`→`i`) to ASCII; it applies no aliases, nicknames, or fuzzy similarity.
Exactly one candidate with non-conflicting canonical team evidence is
automatically qualifying; missing team evidence is allowed. Duplicate names,
inactive-only rows, aliases, fuzzy matches, and team conflicts remain typed
non-matches. `AthleteMappingRepository` persists one current
`provider_athlete_mappings` row per provider identity, an append-only
`athlete_mapping_decisions` audit log, and durable
`athlete_mapping_rejections` suppressions. Provider names, provider team IDs,
canonical team IDs, names, and abbreviations are retained as typed evidence, so
an ID-only team conflict keeps both the provider and the candidate side.

Ambiguous, inactive-only, unmatched, and team-conflict evidence never
establishes a canonical identity — it only ever withdraws or promotes a claim
an earlier decision established — but each is retained as one durable typed observation
in the decision log under an idempotency key, so repeated board reads
add nothing and `scripts/athlete_mappings.py list` can show what an operator
still has to decide. Suppression is per transition, not for the lifetime of an
identity: the key is compared only with the latest decision for that identity,
so an `auto` → `ambiguous` → `auto` sequence records all three, and an
identity that is unmatched, rejected, cleared, and then unmatched again
reappears in the queue. Only a repeated equivalent consecutive observation is
dropped, and the log stays append-only. The canonical athletes such an
observation could not choose between are stored as typed
`athlete_mapping_decision_candidates` rows keyed by decision, not as an opaque
blob, and changed candidate evidence — a different player, name, team ID, team
name, abbreviation, or season-active flag — is a new observation rather than a
suppressed repeat. A `mapping_conflict` decision keeps its candidates the same
way: its canonical side is exactly what is disputed, so the athletes the
conflicting evidence named stay actionable in `history` and in the conflict
review queue instead of being dropped as a decided identity's would be.

Inactive-only, ambiguous, and claim-withdrawing unmatched evidence do change an
existing mapping, because the identity cannot stay comparable while the board
cannot say which canonical athlete it is: the catalog lists its athlete as
inactive for the requested season, it names two equally exact athletes and the
observation chose neither, or it no longer lists the claimed athlete at all.
The row becomes `inactive_only`, `ambiguous`, or `unmatched` and inactive, so
no board comparison can reach it, while keeping the `canonical_player_id` it
was mapped to: the claim is withdrawn, not retracted, and the observation with
its candidates stays in the unresolved queue for the operator. Withdrawal is a
state machine rather than a one-way latch: an already withdrawn row moves to
the state the current evidence names — `auto` → `inactive_only` → `ambiguous`
leaves the row `ambiguous`, the inverse leaves it `inactive_only`, and a
claimed athlete disappearing from the catalog leaves it `unmatched` — because
an operator reads the current row to decide why the identity is out of
comparisons *now*. The canonical claim, the provider evidence that established
it, and the conflict-free columns are preserved through every step, the queue
always shows the latest observation, and repeating the state a row already
holds writes nothing beyond the identity's observation clock. A later
unambiguous observation of the same athlete maps the identity again with no
conflict fields left behind; one naming a *different* athlete under the same
provider ID is still a `mapping_conflict`, because a suspended claim is still a
claim. A `mapping_conflict` row is not withdrawn onto another state: it already
awaits an operator. Catalog inactivity, ambiguity, or absence alone never
withdraws a manual mapping —
an operator's decision is unseated only by the fail-closed conflict detection
below, and approving or overriding the identity resolves the withdrawal and
empties the queue.

Identity is the canonical player ID, not a label. When an established
automatic mapping's canonical player is still active for the season, an
official display-name change on either the catalog or the provider side keeps
that mapping (the canonical facts are re-read from the catalog row); a
relabeled player never becomes unmatched or conflicting on the strength of the
label alone. When no active catalog row matches the incoming label, an
established claim is therefore resolved in one order: an incoming label that
exactly matches a *different* canonical athlete is a `mapping_conflict` naming
that candidate, however inactive it is; a provider name that no longer agrees
with the one observed under this identity is a `mapping_conflict` too, so
retention covers catalog renames only while the provider still reports the
athlete we saw; otherwise the claimed row is looked up by canonical player ID
and either retained (active for the season), withdrawn as `inactive_only`
(listed but inactive), or withdrawn as `unmatched` (no longer listed at all),
whatever the catalog now calls it. A claim withdrawn as `unmatched` records the
athlete that disappeared as the observation's candidate, marked as not active
for the season, so the operator's queue distinguishes a withdrawn claim from an
identity that simply matched nothing. Only an identity with
no claim at all falls back to judging the label alone, and its unmatched
observation names no candidate. A retained identity still
validates team evidence: provider team
evidence that disagrees with the requested season's canonical row is a
`mapping_conflict` that deactivates the mapping, not an automatic one. Team
evidence is always compared with the candidate for the *requested* season
rather than the team an earlier season's mapping recorded, so a player who
legitimately changes teams between seasons keeps an active mapping for the new
season while genuinely inconsistent current evidence still conflicts.
`list` reports the latest decision per provider identity and includes
it only while that decision is still unresolved, so a later automatic, manual,
or rejection decision removes the identity from the queue. A `mapping_conflict`
is neither of those: it is inactive, so it is absent from the active-only
mapping listing, and it is not an unresolved observation state, so it is absent
from that queue — yet it is the one state that always needs an operator. It
therefore has its own review queue (`AthleteMappingRepository.list_conflicts`,
reported as `conflicts`), pairing the current conflicting row with the
`mapping_conflict` decision that recorded it. The row names the provider
identity, its observed evidence, the established or approved canonical side,
and the conflicting candidate; the decision adds the reason and time. The queue
elaborates the conflicting row rather than repeating it as a mapping, and the
default listing stays active-only. `--all` widens the listing to inactive
history without naming a conflicting identity twice: `list_mappings` omits
current `mapping_conflict` rows at every visibility, while every other inactive
row stays visible. Approving or overriding the identity is what empties the
queue. Repository reads and
operator writes (approve, override, reject, and clear) translate
`SQLAlchemyError` to `AthleteMappingPersistenceError` (defined in
`app.services.athlete_mapping_errors`), and the resolver translates the same
failure from a catalog read. That single type is what the DFS Board isolates;
it never catches broad exceptions and still returns usable markets.

An injected DFS board read may transactionally record the first qualifying
automatic decision. The repository is idempotent under repeated and concurrent
reads, never replaces manual approvals or overrides, and isolates persistence
failures from the normalized market result. The board asks the resolver for
its view of one board read (`AthleteResolver.for_board(season, identities)`):
the catalog is read and indexed by normalized name once, the mapping and
rejection state of every named identity is read in one statement each per
provider (`get_mappings`/`get_rejections`), and every market is resolved
from those reads, so the read count no longer grows with the market count.
One board read resolves every
market before it writes anything, because a snapshot is temporally coherent:
markets sharing one `(provider, provider_athlete_id)` are one observation of
one athlete, not a sequence of observations that supersede each other. When
they disagree about the athlete's name, canonical ID, or team evidence — after
the same accent/case/punctuation normalization resolution uses, so equivalent
spellings are not a disagreement — the identity fails closed on the
contradiction itself as one `mapping_conflict` reasoned
`contradictory_provider_evidence`, carrying every athlete the markets named as
a candidate and every typed evidence they named it on. Disagreement is judged
by comparing the observations themselves — every market against every other
market for that identity, fact by fact and at the same team tiers resolution
compares: a market that omits the canonical ID or the team, or names the team
at a weaker tier than another does, describes the same athlete more sparsely
rather than a different one, so only a fact whose value changed contradicts.
Comparing each market against a running merge instead would answer for evidence
no market reported and let a weaker fact overwrite a stronger one, so with
three or more markets the provider's listing order would decide whether a
disagreement stayed visible. Which evidence the conflict is *recorded on*, and
the order of its candidates and retained evidence, come from sorting the
observations rather than from the provider's listing order, and the whole
contradicting set is rechecked against a governing manual decision — one market
reporting exactly the approved athlete cannot answer for a read that also
reports another. Markets that do agree about the athlete are combined into one
durable observation carrying every provider and canonical ID, name, and
abbreviation any of them reported, so the row keeps the whole read rather than
whichever market was written last. That combined evidence is then resolved as a
whole rather than inheriting any one market's own pre-merge result: a market
that reported no name said less than its sibling, it did not object to the
athlete the sibling named, so reusing its `unmatched` result would leave the
identity unmapped while the durable row recorded the name that maps it.
Resolving the merged evidence re-raises every objection the catalog or an
operator genuinely holds against evidence that only grew — a rejection, a
manual decision the merged evidence falls outside of, a team the catalog
contradicts, a conflicting established claim — so where the markets disagree
about whether the identity may be claimed — one reporting no team where another
reports one the catalog contradicts — the objection is what the combined
evidence supports and what stands, and the identity is never claimed and
disputed by one read. The answer depends on the combined evidence alone, so it
is the same in every order the provider could have listed the markets in.
Persisting those markets in turn would instead let their arrival order decide
the identity, because each was judged against what the previous one stored, and
would append one observation per market, so an unchanged repeat kept growing
the audit. Contradicted evidence therefore never enters a canonical comparison
in any market order, and repeated markets, repeated board reads, and a repeat
that lists the same markets in another order all append no further decision and
leave the same durable row. Later evidence that disagrees with
an active automatic mapping deactivates it as `mapping_conflict` while keeping
the conflicting evidence in the current row and audit history. Operator
approve/override/reject/clear actions require an identity and reason; approve
and override also accept and retain provider name and team evidence, keeping
the previously observed evidence when none is supplied. Repeating a conflict
read an identity already records changes nothing: the retained conflict
candidate and evidence stay, and no duplicate decision is appended. Automatic
mapping still requires a catalog row that is active for the season, while a
governed approve or override may select an inactive-only row; that choice is
recorded explicitly in the audit as a decision candidate marked not active for
the season. Active rejections suppress future automatic mappings until
explicitly cleared, and a manual decision still wins over any later automatic
read. That precedence protects a governed mapping from automatic *overwrite*,
not from fail-closed conflict detection. When the provider later reports a
clearly different athlete under the same identity — a changed provider name, or
team evidence that contradicts what was approved, such as Nikola/DEN becoming
LeBron/LAL — the resolver reports `mapping_conflict` instead of lending the
operator's decision to an athlete nobody reviewed. Only provider-side facts are
compared, and only when both sides carry one, so an approval recorded without
evidence is never second-guessed and a deliberate override to a differently
named canonical athlete is not a conflict. A promoted manual mapping keeps the
schema's single current state: the row becomes `mapping_conflict` and inactive
(so it is not used in comparisons) while retaining the approved
`canonical_player_id` beside the new conflicting provider evidence, and the
append-only log preserves the original `manual_approved`/`manual_override`
decision with its operator, reason, and approved evidence followed by a
`mapping_conflict` decision reasoned `manual_mapping_conflict`. An operator
resolves it by approving or overriding again, which restores the manual state
and clears the conflict fields. Approve and override with no supplied evidence
fall back to the mapping row's observed evidence, or — for an identity that
only ever produced unresolved observations — to the latest durable decision
read inside the same identity transaction, so a queued observation resolved by
an operator keeps its provider name and team evidence.
Because the resolver reads outside the serialized identity transaction,
its result may already be stale; the current mapping and rejection are re-read
inside that transaction immediately before every automatic, unresolved, or
conflict write, so a manual approve/override or an active rejection recorded in
between wins: a stale unmatched observation cannot requeue a decided identity
and a stale conflict cannot replace a rejection. Whether a stale conflict is
still live is decided by the provider-side evidence alone, compared with the
governing manual decision read inside that transaction. The canonical choice is
not compared: an operator who accepts the provider's observation may still keep
or pick a canonical athlete the automatic side would not have chosen — that
disagreement is the decision — so a stale automatic candidate cannot undo it.
Observation order cannot stand in for the comparison either: a read taken after
a decision may still carry evidence the operator never reviewed. Evidence the
governed decision does contradict is evidence nobody reviewed, and still
promotes a conflict — and a conflict asserts every evidence it contradicts, so
all of it is compared and any single uncovered market unseats the approval.
Ordering fences the other case — a read from *before* the governing decision.
Each `ProviderSnapshot` is temporally coherent, so the board carries its
`retrieved_at` through the resolver onto the typed resolution and into the
`observed_at` column of the decision it appends. That is when the provider was
observed, never when the observation was persisted; the persistence-time
override is named `recorded_at` so the two cannot be confused. Inside the
identity transaction an automatic, unresolved, or conflict write is compared
against the newest governing instant for that identity — an operator
decision's `created_at`, and the identity's observation clock, all UTC. A read
the operator's own mapping already covers runs through that transaction too: it
appends no duplicate audit row and changes no mapping, but the provider did
report the approved identity at that instant, so it raises the clock and a
conflicting read taken earlier is fenced instead of unseating the mapping. That
clock is a durable high-water mark on the identity's lock row, raised inside
the same transaction whenever a read passes the fence. It cannot be derived
from the decision log, because the log suppresses a repeated equivalent
observation on purpose: two identical reads append one row, and a mark taken
from that row would leave a read from between them looking like news. The lock
row is already selected for update by every mutation, so the mark only ever
moves forward and concurrent repeats raise it exactly once.
A strictly earlier read changes nothing, so a slow read
retrieved before a reject/clear cannot deactivate the mapping a newer read
established or queue a conflict between two canonical athletes that were never
claimed at the same time. A read contemporaneous with the governing instant is
not from before it, so replaying one stays idempotent, and a caller that
reports no observation instant is never fenced.
The per-identity lock row is inserted inside a savepoint that is always left
before a duplicate `IntegrityError` is handled, so PostgreSQL rolls back the
failed savepoint instead of leaving the surrounding transaction aborted;
`tests/integration/test_postgres.py` covers that concurrency path against a
real database when `TEST_DATABASE_URL` is set. The process-local identity lock
is per engine and only orders writers inside one process, so the concurrency
test gives each worker its own engine and repository and releases them from a
barrier — the overlap is resolved by the database, not by a shared lock.
Migration 006 also creates a per-identity lock table — which carries that
identity's observation clock — and database checks for
closed mapping states (`ambiguous`, `auto`, `inactive_only`, `manual_approved`,
`manual_override`, `mapping_conflict`, `rejected`, `unmatched`), the closed
decision-state set, active-state coherence — only `auto`, `manual_approved`, and
`manual_override` may be active, and `ambiguous`, `inactive_only`,
`mapping_conflict`, `rejected`, and `unmatched` may not —
cleared-rejection coherence — an active rejection carries no `cleared_at`,
`cleared_by`, or `clear_reason`, and a cleared one carries all three — and
conflict-column coherence: only a current `mapping_conflict` row may name a
`conflict_canonical_player_id`/`conflict_canonical_name`, so an identity that
is reactivated automatically, remapped by an operator, or rejected keeps no
conflict an operator has already left behind. Those
checks compare booleans with
`true`/`false` rather than `1`/`0`, so they are valid on PostgreSQL as well as
SQLite. Migration 006 remains the single definition of those checks rather than
being followed by a constraint-rebuilding migration, because it is unreleased:
no deployed database carries an earlier version of the mapping schema, so
widening the closed state set (as `unmatched` did) is a change to 006 itself
and `scripts/migrate.py` creates the current checks on any target. The operator
CLI never runs migrations implicitly; run `scripts/migrate.py` explicitly
first.

### Provider event mappings

`EventResolver.resolve(provider, evidence, season)` accepts one typed provider
`EventEvidence` value and an explicit requested season;
`resolve_market(market, season)` is the board-facing spelling, and
`for_board(season, identities)` returns the same two calls served from one
read of the catalog and mapping state for a whole board. Resolution asks
one question — which single scheduled NBA game does this evidence identify? —
and answers it only from canonical home and away teams plus schedule proximity.
Both sides must resolve to a canonical NBA team, from the provider's canonical
team ID or from a tricode or team name that unambiguously names one team in the
season's schedule; a label two canonical teams answer to is not identifying
evidence. The matchup label is retained evidence and is never parsed into
teams, so a provider that reports no team evidence resolves no event rather
than a guessed one. Home and away are compared in the orientation the provider
reported, so a reversed matchup is `unmatched` reasoned `home_away_mismatch`
rather than the same game described another way. The provider's start time must
sit within `EVENT_MAPPING_MATCH_WINDOW_HOURS` (default six) of the scheduled
tip-off, and the boundary is inside the window: exactly six hours matches, one
second more does not. Exactly one candidate is automatically qualifying; two
equally admissible games are `ambiguous` and none is `unmatched`. Provider event
IDs, canonical claims, matchup labels, start times, status labels, and both
sides' team IDs, names, and abbreviations are retained as typed evidence beside
the canonical result, and each candidate records its signed distance from the
provider's start time.

`EventMappingRepository` persists one current `provider_event_mappings` row per
provider identity, an append-only `event_mapping_decisions` audit log, typed
`event_mapping_decision_candidates`, durable `event_mapping_rejections`
suppressions, and a per-identity lock row carrying that identity's observation
clock. Governance is the one established for athletes and behaves identically:
active-mapping precedence, append-only decisions suppressed only for a repeated
*consecutive* observation, per-identity serialization with the lock row inserted
inside a savepoint that is left before a duplicate `IntegrityError` is handled,
`observed_at` fencing of a read taken before the newest governing instant, and
one documented failure type — `EventMappingPersistenceError`, defined in
`app.services.event_mapping_errors` — for every repository read and operator
write plus the resolver's catalog read. That single type is what the DFS Board
isolates; it never catches broad exceptions and still returns usable markets.
The board contradiction machinery is shared where it is genuinely identical:
`_contradicts` and `_contradictory_identities` in `app.services.dfs_board` take
the fact vocabulary and tier chains as parameters, so athletes compare a name,
a canonical ID, and one team while events compare a canonical claim, a start
time, and two independent home/away sides. An event conflict records both
sides of what it disputes: the canonical games the markets named as typed
`event_mapping_decision_candidates`, and every provider evidence the
observation asserted as typed `event_mapping_decision_contradictions`, because
the decision row itself carries only the representative evidence.

The provider's own canonical event claim (`EventEvidence.canonical_id`) decides
whether two markets contradict each other, so it is durable evidence like the
rest: `provider_canonical_event_id` is written onto the mapping row, the
decision row, and each contradiction row, is part of the idempotency
fingerprint, and is operable from the CLI. The repository is composed with the
same configured `EVENT_MAPPING_MATCH_WINDOW_HOURS` the resolver uses, and every
in-lock recheck of a governed decision compares start times with it; re-reading
settings inside the transaction would let a configuration change land
mid-decision, and falling back to the reviewed default would accept evidence a
narrower configuration has already called a different fixture.

Two rules are specific to events. A durable mapping is created only when a
stable upstream event identity exists: a market with teams and a start time but
no provider event ID resolves to a canonical game for the current board — the
resolution reports it and `BoardEventMappingOutcome.canonical_event_id` answers
with it — while `EventResolution.is_durable` is false, so no mapping row, audit
row, rejection, or lock row is keyed on a fabricated identity and the next read
reevaluates the evidence from scratch. And a replaced NBA game ID never inherits
a mapping. When the season's schedule no longer lists the game a claim names,
the identity becomes `replacement_pending` and inactive while keeping the game
it was mapped to, whether the new evidence points at exactly one replacement
(`replacement_event_identity`), at several (`ambiguous_replacement_event`), or at
none (`replaced_event_absent`). The queue names the withdrawn claim as a
candidate marked no longer scheduled beside every nearby replacement, and only
an operator approve or override resolves it.

`ambiguous`, `unmatched`, and `replacement_pending` withdraw a claim from board
comparisons without retracting it: the row keeps its `canonical_event_id`,
becomes inactive, and records why the identity is out of comparisons now, so a
later unambiguous observation maps it again with no conflict left behind.
Evidence that resolves to a *different* scheduled game under an established
claim is a `mapping_conflict` that deactivates the mapping and stops its use
pending review, as is provider evidence that contradicts a governed manual
decision — a changed home or away team at the strongest tier both sides carry,
a changed provider canonical event claim, or a start time no longer within the
reviewed window of the approved one. Each of those is compared only when both
the mapping and the new evidence assert it, so a claim, team identity, or start
time one side simply omits is sparse evidence rather than a contradiction and
stays compatible. A reschedule inside the window is the same game and not a
conflict. Operator precedence protects a governed mapping from automatic
*overwrite*, not from fail-closed conflict detection, and approving or
overriding again restores the manual state and clears the conflict column.
Missing or older-than-allowed Event Catalog data yields
`event_catalog_unavailable`: the normalized markets stay visible on the board
with no event comparison identity, and nothing is recorded, because there is
nothing to compare against yet. Freshness gates a governed identity too — an
active `manual_approved` or `manual_override` is withheld and left untouched
while the catalog is unusable, rather than lending an operator's decision to
evidence no schedule can place.

`DFSBoardService` receives the resolver and repository by injection and reports
`board.event_mapping_outcomes`. The board asks the resolver for its view of
one board read (`EventResolver.for_board(season, identities)`): the catalog,
its freshness, and the mapping and rejection state of every named identity
(`get_mappings`/`get_rejections`, one statement each per provider) are read
once and every market is resolved from those reads, so resolution stays a
pure function of the same catalog and mapping state while the read count no
longer grows with the market count. One board read resolves every market before it
writes anything: markets sharing one `(provider, provider_event_id)` are one
observation of one fixture, so compatible markets are combined into a single
durable observation carrying every fact any of them reported and resolved as a
whole, while markets that disagree about the fixture fail closed as one
`mapping_conflict` reasoned `contradictory_provider_evidence`. Repeated reads,
repeated markets, and a reordered repeat of one snapshot all leave the same
durable row and append no further decision. Both the merge and the conflict
order the observations by every field of the evidence they carry, not only by
the facts that identify the fixture, so markets that agree about the game while
spelling its label, status, end time, or update instant differently still yield
one combined observation that does not depend on the provider's listing order.
Catalog freshness gates the board read as it gates a single resolution: when the
season's catalog is missing or over-age, the whole group stays
`event_catalog_unavailable` with no canonical identity — neither merged nor
promoted to a conflict — so a disagreement between markets queues nothing
against an operator's standing decision, the mapping row and its history are
untouched, and every normalized market stays on the board. The disagreement is
withheld rather than resolved away, and fails closed as a conflict again on the
next read with a usable catalog.

### Comparison Groups

`ComparisonBoardService.get_comparisons(query, context, filters=...)` is the
seam above the collector. It turns one board read into deterministic Comparison
Groups, explicit Comparison Availability, and visible unresolved evidence. The
immutable value types live in `app.domain.comparisons`, which imports no
service and states no opinion.

A Comparison Group requires the same Canonical Event, Canonical Athlete,
Canonical Statistic, and scoring period; those four facts are its key. The
canonical athlete and event come only from the governed mapping outcomes the
collector reports, so a suppressed, disputed, or withdrawn identity never
reaches a group. Legitimate multiple thresholds, variants, statuses, and
same-provider markets stay distinct members of one group, and an available and
a suspended offering of the same line are two offerings rather than one market
contradicting itself. Identity is decided once, over whole normalized markets,
before any market is reduced to a member, so two distinct offerings can never
merge because the few facts a member happens to state agree. Repeated source
identities are already collapsed inside `ProviderSnapshot` and the shared
adapter collector, where a repeat that changed what the market says is
malformed rather than a second market; the board applies the same rule to the
references it derives, so a repeated reference collapses only when every
normalized fact and its observation agree.

All three seams read a repeat through one authority, `app.domain.market_content`,
which imports no provider or service module and so cannot drift from either
side. It separates what a market *says* — exact numbers whatever scale they
were written at, selections in an order derived from their own content — from
the audit spellings a board retains beyond that. A provider that relists one
market with its selections the other way round, or rewrites `25.5` as `25.50`,
has therefore restated one offering at every seam, and the repeat that survives
is the least by complete retained evidence rather than the first to arrive. A
repeat that changes a stated fact — another threshold, status, variant, or
selection — remains `conflicting_source_identity` at the provider boundary and
`conflicting_market_identity` on the board. The comparability the catalog
resolved the statistic to is one of those stated facts, because it decides
whether a resolved market may enter a group at all: two readings of one
identity that agree on everything else but disagree there are a content
conflict at every seam, never one offering restated.

A repeat that disagrees is evidence, not a duplicate to discard. Every distinct
contradicting observation of one reference is retained as its own
`BoardMarket`, ordered by its own complete normalized content and observation
rather than by arrival, and each states `conflict_ordinal` and `conflict_count`
so an audit reads exactly what contradicted what. All of them are excluded as
`conflicting_market_identity` and none enters a group; the reference itself is
reported once in `unresolved`, while `market_count` counts every retained
observation. `ComparisonBoard.conflicting_markets` lists them, and
`markets_by_reference` — which can hold only one market per reference — keeps
the first in board order. The whole result is independent of the order the
providers, snapshots, and markets were read in.

The board keeps every normalized market it read, not only the facts a
comparison needs. `ComparisonBoard.markets` holds one `BoardMarket` per
retained market: typed athlete, event, team, league, competition, sport,
appearance, and statistic evidence, the catalog's statistic resolution, the
exact threshold, the status, variant, and scoring period with the provider's
own original labels, every selection with its stable reference, modifiers, and
prices, and the snapshot observation it came from — provider, snapshot status,
retrieval instant, exact decimal age, and freshness. Exactly one of
`comparison_reference` and `exclusion` is set on each, so `markets_for(...)`
reads a group's evidence and `markets_by_reference` reads the whole market
behind any member or unresolved entry. Unresolved, stale, unmapped, and
catalog-blocked markets are therefore auditable in full rather than named by an
opaque reference.

Every market the read retains but cannot compare stays visible as an
`UnresolvedMarket` with one closed `ComparisonExclusion` reason and the
governed state as its detail: ambiguous, unmatched, mapping-conflict,
stale-catalog, and unmapped-statistic markets are all reported rather than
dropped, and none of them enters a group. The checks run in a fixed order —
conflicting identity, future observation, availability, freshness, statistic,
athlete, event, threshold — so a market that fails several always reports the
same reason.

Comparison Availability is decided before any group is built. A missing or
over-age Athlete or Event Catalog makes the whole board unavailable, and each
`CatalogAvailability` carries the catalog's identity (name and season), its
last successful refresh, its exact decimal age, and the configured maximum age.
Both catalogs report that maximum as `max_age_seconds`: the exact seconds of the
very `timedelta` they gated on, counted from its whole microseconds. A TTL
rewritten as floating-point hours is not that duration — a third of an hour
gates at exactly 1200 seconds and was reported as 1199.99999999999988 — so an
age and the ceiling it was compared against can no longer disagree at the
boundary.
The normalized markets are retained throughout; only their comparability is
withheld until a refresh.

One timezone-aware observation timestamp, read from
`ComparisonBoardService.clock` after the collector returns, is the instant the
whole board is measured against: it is the board's `generated_at`, it ages
every provider snapshot and retained market, and it is passed as `now=` to both
canonical catalogs, so a reported age and the availability derived from it can
never disagree at a TTL boundary and a slow collection can never report a
market as fresher than the board that states it.

Freshness uses the reviewed provider cache windows, read through the one shared
predicate in `app.domain.freshness` and compared as exact decimals, so the cache
and the board can never classify one observation two ways. A snapshot inside its
provider's fresh window is contemporaneous; one past it enters comparisons only
while it is inside the permitted stale window and says so; beyond that window
its markets stay visible as `stale_snapshot`. A snapshot the provider timestamped after the board
observed it cannot be aged, so it fails closed: no negative age is ever
reported, the observation carries no freshness, and its markets stay visible as
`future_snapshot`. A group whose members are not contemporaneous — different
retrieval instants, or a mix of fresh and stale observations — is an explicit
Mixed-Freshness Comparison.

A summary states only exact decimal minimum, maximum, and Threshold Spread,
the provider and market counts, the freshness, and the sorted market
references. Thresholds are `Decimal` throughout, so a serialized value is the
provider's own exact number. The Threshold Spread is the exact difference of
the stated minimum and maximum, and a summary validates its spread against
that same exact difference, so neither a stored nor an accepted spread can be
a rounded one. That difference inherits nothing from the ambient decimal
context: it is computed by a fresh `decimal.Context` that states its own
precision, exponent range, rounding, capitals, clamping, and a trap for every
signal, using that context's own method rather than an operator, so no
thread-local precision, clamp, or trap can change or refuse it. Its precision
is `MAX_EXACT_DIFFERENCE_SPAN`, the widest exact difference the normalized
numeric domain admits, so every threshold the provider contract accepted
subtracts exactly.

A summary is derived evidence, never independent evidence. A `ComparisonGroup`
validates its summary against `ComparisonSummary.of(members)` after ordering its
members, so it can carry only the derivation its own members produce: a false
provider count, market count, minimum, maximum, spread, freshness, or market
reference list is refused where the group is built rather than published beside
the markets that contradict it. The comparison is at the scale a threshold is
written in, not merely at the value it compares equal to, because a board
publishes `Decimal("25.50")` and `Decimal("25.5")` as different strings even
though Python compares them equal. The rule is independent of member order,
since nothing the derivation states depends on which member sorts first.

How old an observation or a catalog is inherits nothing either. `exact_seconds`
counts a duration in whole microseconds and writes the result straight from
those digits, so no decimal operation and therefore no ambient precision takes
part; `exact_scaled_seconds` converts a configured window stated in hours,
days, or seconds through the same fully stated context, after the configured
quantity enters the normalized numeric domain. A caller's narrow precision or
trapping context can no longer age one snapshot two ways, or refuse to age it
at all. No probability, expected value, recommendation,
average, preferred market, entry payout, or cross-provider fantasy assumption
is produced anywhere in this seam.

Market, selection, and comparison references are versioned and deterministic
(`mkt_2_…`, `sel_2_…`, `cmp_2_…`). Each is a digest over a canonical injective
encoding: every value is tagged by type and framed by its byte length, and
every sequence carries its element count, so a field containing a separator can
never be read as two fields and two distinct structures can never encode alike.
Decimals take part in one canonical form read straight off the value's own
digits, so `25.5` and `25.50` are the same number and the same reference. That
form performs no arithmetic and no normalization, because both round under the
ambient decimal context: a value carrying more digits than the context permits
keeps every one of them, and a reference never depends on the context a caller
happened to be inside.

A market reference is defined by the provider's own market ID, or — when the
provider publishes none — by every fact it did report: the athlete evidence,
the complete event evidence including provider and canonical IDs, team IDs,
names and abbreviations, start, end, and update times, label and status, the
statistic evidence, the exact threshold, the market status, variant, scoring
period, source labels, times, appearance, and the offered selections with their
modifiers and prices. A market ID is the market's own source identity, so a
market that is suspended and available again keeps that reference; for a market
with no ID, status is the only fact separating an offering the provider is
taking from an identically named suspended one, so it takes part. A threshold's
written spelling never does: `original_value` is retained on the board for
audit, but `25.50` and `25.5` are one line and one identity. Selections take
part in an order derived from their own complete retained content — their
normalized facts first, then the audit spellings kept beyond them — so the
order a provider happened to list two equivalent selections in changes neither
the market reference nor the selections the board returns, even when the only
thing separating them is the scale one exact price was written at, and two
selections differing in any retained fact stay two selections. The spelling
tiebreak applies only inside a semantic tie, so a reference stays
scale-independent. A selection reference is
defined by
its market reference and every fact that defines the offering — identity,
labels, direction, status, modifiers, and prices — so two distinctly priced or
distinctly modified selections are never one reference. A comparison reference
is defined by its canonical identity alone. Each is stable exactly while its
defining identity is unchanged; the version is bumped whenever those facts
change.

Each `ProviderReport` states one provider's whole contribution and the
provenance a reader needs to judge it: the retrieval status and its bounded
reason, the observation's retrieval instant, exact decimal age, freshness, and
snapshot status, the market count, and the closed coverage and skip codes. Its
`BoardCoverage` carries the collector's own fetched, eligible, normalized, and
skipped counts, the pagination and fanout completion evidence, the expected
total, and whether the coverage is complete — counts only, never a rate or an
inference. Its `BoardCacheState` carries the cache status the observation was
served from, the cached retrieval instant, its exact decimal age, and, when a
stale observation was served because a refresh failed, that failure's bounded
reason and instant. Every one of those reasons is the collector's own
classification, so no provider exception text, credential, or upstream detail
reaches a reader through a report.

Ordering is a property of the observations, never of completion order:
provider reports and warnings sort by provider and code, groups by their key,
members by provider, threshold, variant, status, and reference, unresolved
markets by reason, provider, and reference, retained markets by provider,
reference, and contradiction ordinal, and selection references
lexicographically.

Filters are central and exact: enabled providers, Canonical Athlete IDs,
Canonical Event IDs, Canonical Statistic IDs, and Market Status. A provider
filter is answered before retrieval, so an excluded provider is never called
and is reported as disabled for that board. There is deliberately no fuzzy or
partial name filter. The post-filter market ceiling is
`DFS_COMPARISON_MAX_MARKETS` (default 10000); a larger read raises
`ComparisonBoardTooLargeError` (`board_too_large`) carrying the observed count
and the supported narrowing filters, and nothing is ever truncated. The refusal
also retains the completed read's `BoardReadEvidence` — provider reports,
disabled providers, comparison availability, and the group, market, and
unresolved counts — which is observed rather than published, so telemetry
describes the read that actually happened while the caller's details stay as
bounded as before.

Readability outranks the ceiling, and only the ceiling. When a read *is* over
the ceiling, whether any provider could be read from decides which refusal it
is: an unreadable over-ceiling read states nothing at any size, so refusing it
as too large would tell a caller to narrow filters that cannot make an outage
readable. That read alone builds no board. It raises its own result variant,
`UnreadableComparisonBoardError`, carrying only that read's `BoardReadEvidence`
— comparison availability, provider reports, disabled providers, and the
observed group, market, and unresolved counts — and no serializable board. The
response seam catches it, contributes the evidence to the request's
observation, and reports it as the same sanitized 503 a readable outage is; the
evidence itself is never published, and the serializer refuses anything that is
not a `ComparisonBoard`. Nothing publishable is dropped, because every
observation on such a read is beyond its provider's permitted maximum age or
ahead of the board's own clock and so entered no group.

An unreadable read *under* the ceiling is not refused in the domain at all. It
returns an ordinary `ComparisonBoard` that retains every market it observed —
each one unresolved as `stale_snapshot` or `future_snapshot`, with no group and
no readable provider report — so the whole read stays auditable as a board. The
publication seam is what answers it: `has_readable_provider` over that board's
own provider reports turns it into the same sanitized 503, from the same
evidence, one layer later.

The variant exists so that no board can state a count its own collections
contradict. A `ComparisonBoard` retains exactly what it counted — `market_count`
equals its retained markets, `unresolved_count` its retained unresolved markets,
each an exact non-negative integer rather than anything Python merely compares
equal to one, and `is_empty` is read from groups, unresolved markets, and
retained markets together — so an over-ceiling outage, which has counts but
retains nothing, cannot be expressed as a board at all.
`UnreadableComparisonBoardError` is a `ProviderUnavailableError` rather than a
sibling of `ComparisonBoardTooLargeError`, so should it ever escape the response
seam the central handler already answers it as the safe 503 an outage is, with
no evidence in its public details and no 400 telling a caller to narrow filters.

Agreeing counts are not yet a coherent board, so a `ComparisonBoard` also
validates that its three collections are one partition of the evidence it
retained. Every market reference a group's members cite, and every reference
stated as unresolved, is backed by a retained `BoardMarket` on the same board;
each reference lands on exactly one side, entering at most one comparison and
being stated unresolved at most once; and every retained market names where it
went — a compared one names the comparison whose members cite it, an excluded
one names a reference the board reports as unresolved. Backing is not one market
per reference: contradicting observations of one source identity are all
retained, so one unresolved reference may stand on several retained
observations. An empty board satisfies this trivially, and it is why
`group_count + unresolved_count` can never exceed `market_count`.

A reference is not evidence either. Every published projection must be
factually derived from the retained observation it cites, so the board also
validates what each one *says*. A `ComparisonMember` is published only where a
retained, compared `BoardMarket` of that reference, assigned to that same
comparison, projects to exactly it — same provider, same threshold at the same
written scale and unit, same status, variant, retrieval instant, freshness, and
selection references, every field of the value taking part. An
`UnresolvedMarket` is published only where the *complete* same-reference
excluded cluster states one exclusion and the entry states that one, so a
single observation's reason, detail, and provider project exactly, and a
contradiction is described by the whole cluster rather than by whichever
observation happens to be first.

Both projections are derived by one domain authority,
`comparison_member_of` and `unresolved_market_of` in `app.domain.comparisons`.
The assembly builds each published value with it and the board invariant
re-derives one with it, from the retained market alone, so the seam that states
a fact and the seam that judges it cannot drift; neither reads a provider,
catalog, or service module, so the shared authority creates no cycle.

Contradiction evidence is checked for completeness before either projection is
read, because a partial contradiction would let a reader conclude the missing
observation agreed. Every retained observation of one contradicted reference
states the same `conflict_count`, there are exactly that many of them, their
ordinals are exactly `range(count)` with none missing or repeated, and none of
them may stay unmarked; each is excluded as `conflicting_market_identity`, so
the one unresolved entry the cluster produces states that reason and the
cluster's own detail. Conversely, two retained observations of one reference
that state no contradiction are refused: a reference is either one observation
or a complete contradiction.

The exclusion and the evidence for it are required of each other in both
directions. A `BoardMarket` that states `conflict_ordinal` and `conflict_count`
is excluded as `conflicting_market_identity`, and a market excluded as
`conflicting_market_identity` states them, so the reason can never be published
for a lone observation with nothing to disagree with.

Structure is not disagreement. A structurally complete cluster whose retained
observations state the same evidence is a repeat that failed to collapse, so
the board requires the cluster's `count` observations to state `count` distinct
facts. Distinctness is read in exactly the semantics the retention seam
collapses repeats by, and by the same authorities: `market_content_key` from
`app.domain.market_content`, which a `BoardMarket` answers by the same
attribute names a normalized market does, paired with `observation_evidence_key`
over the snapshot observation it was read in. There is no second list of what
counts as a difference, so a fact proves a disagreement here exactly when it
proves one upstream. A differing market ID, threshold value or unit, status,
variant, name, team, event, statistic, resolved comparability, direction, exact
price, or modifier is a contradiction; a differing retrieval instant, snapshot
status, age, or freshness is one too, because the observation is part of what
the retention seam keeps apart.

Audit content proves nothing here. The scale an exact decimal was written at,
the provider's own `original_value` text, and the order equivalent selections
were listed in are all retained, published exactly, and still order the
evidence — but `Decimal("25.50")` and `Decimal("25.5")` are one line, so a
cluster separated only by them is a repeat the board refuses rather than a
disagreement it invents. An exact semantic repeat has already collapsed at the
retention seam, so a cluster the board assembles always satisfies this. Where an
observation sits in the cluster takes no part either: an ordinal is a
statement about the cluster, never evidence the cluster is one.

`BoardReadEvidence` carries counts without the collections behind them, so it
enforces that same relation directly: a read cannot have established more
comparisons and unresolved markets together than the observations it made. The
count-only evidence a refusal carries satisfies it — an unreadable read states
no group and one unresolved reference per observation, and an over-ceiling
readable read states groups and unresolved references drawn from disjoint
subsets of what it observed.

Both seams judge readability through one domain authority,
`ProviderReport.is_readable` and `has_readable_provider`, so the seam that
refuses an over-ceiling read and the seam that reports an under-ceiling outage
cannot disagree about what readable means.

### Published DFS Board

```text
GET /api/dfs/board
  → require_auth (Firebase bearer token)
  → PendingBoardObservation opens                     ← the observation starts here
  → DFSBoardResponseService.respond_to_query(request.args, observation=...)
      → publication gate (feature flag + provider registry)
      → parse_board_request(...) → NBAMarketQuery + ComparisonFilters
      → ComparisonBoardService.get_comparisons(...)   → BoardReadEvidence observed
      → HTTP outcome, version 1 payload, weak ETag
  → private, revalidatable JSON (200), or 304 / 400 / 404 / 503 / 500
  → blueprint after_request: private caching and security headers, every status
                             and the one event, finalized from that status
```

The route in `app.routes.dfs_routes` decides nothing. It authenticates and
formats; `app.services.dfs_board_response` decides whether the board is
published, what the query string means, whether the board is usable, what the
payload is, what the entity tag is, and whether the caller already holds it.
Both the route module and the response service create no provider, Redis, or
database client: the application factory composes `dfs_board_response_service`
from the comparison board alone, which itself wraps the already-composed
collector.

The order of the first two steps is a contract, not an implementation detail.
Publication is settled before a parameter is read, so an authenticated request
to an unpublished board is 404 whatever its query says, and reaches no parser,
provider, database, or cache. Authentication remains above the service, so an
unauthenticated request is 401 and is recorded as no board request at all.

**Publication.** The board is published only when `DFS_BOARD_ENABLED=true`
*and* `DFS_ENABLED_PROVIDERS` names at least one provider. Both are off by
default in every environment, so development and tests opt in explicitly.
Enabling the flag without a registry fails startup with `ConfigurationError`
rather than exposing a route that can never call a provider. Production always
requires `DFS_ENABLED_PROVIDERS` to be present; an explicit empty value is the
supported all-disabled state, while omission is invalid. An unpublished board
answers an authenticated request with 404
`dfs_board_disabled` and calls no provider.

**Outcomes.** A read is 200 when at least one provider produced a *readable*
snapshot — complete, partial, permitted-stale, or empty-complete. An empty
complete snapshot is a valid empty board, not an outage. Readability is judged
from the provider report's own typed evidence, the derived `MarketFreshness`
and the future-observation flag, never from exclusion text: a retrieval that
succeeded but is past its stale-if-error ceiling, or timestamped ahead of the
board's clock, carries no freshness, enters no comparison, and leaves every one
of its markets unresolved. A board of only those states nothing, so it is the
503 it is rather than an empty 200. The ceiling is inclusive, so a snapshot
exactly at it is still readable — and being readable, such a read is subject to
the market ceiling again, so an over-large board built from it is the 400 it
was before. The 503 carries the same bounded Provider
Outcome vocabulary the board reports on success — provider name, status, stable
failure reason, freshness, future-observation flag, coverage warning codes, and
cache state. No upstream text, URL, payload, or credential can reach a caller
through it.

**Filters.** Every supplied filter is read as a narrowing the caller meant. An
empty value or an empty comma-separated member — `providers=`, `providers=,`,
`providers=dabble,`, a blank canonical identity, `season=` — is
`400 invalid_input`, never a silent widening to the unfiltered board or the
default season, and it reaches no provider. A repeated identity is accepted and
collapsed. Omitting a parameter is the only way to accept a default.

**Conditional requests.** The response carries a weak `ETag` computed over the
board's stated facts with the instant of observation and every age derived from
it excluded, so an unchanged board revalidates as 304 instead of resending a
board that differs only in how old it says it is. The tag identifies a board, so
it is set on 200 and 304 only and never on a failure.

`Cache-Control: private, no-cache, max-age=0, must-revalidate`,
`Vary: Authorization`, `X-Content-Type-Options: nosniff`, and `X-Request-ID` are
stated once, by a blueprint `after_request` scoped to this route, so every
status carries them — including the 401, the parser's 400, the gate's 404, the
503, and a centrally handled 500, each produced by a different layer. `Vary` is
added rather than assigned, so a CORS `Origin` survives beside it. No other
blueprint's caching is affected.

**Observability.** Exactly one `BoardRequestEvent` per authenticated request,
and its lifecycle is deliberately split across the two layers that each know
half of it.

A `PendingBoardObservation` opens in the route *before* the dependency graph is
read, before the publication gate, and before a parameter is parsed, so a
request that fails before it reaches a board is still one request that
happened. It is passed into the response service rather than reached for
through a Flask global, so the service stays free of request state. The service
contributes only what it knows: the typed `BoardReadEvidence` a completed read
established, and the typed failure it raised.

The event is finalized once, by the blueprint `after_request`, from the status
of the response the caller actually received. That is the only place the status
is settled — a dependency that never resolved, a serialization that raised
after the board was assembled, and a centrally handled `AppError` all decide it
after the service has stopped speaking — so the event and the response can
never describe two different requests. `after_request` runs for every status,
including a centrally handled 500, so a served board that failed to render is
recorded as the `error` it was rather than the `served` it intended. Finalizing
again does nothing: one request is one event.

Because a refusal after retrieval has already learned everything a served board
would have shown, `ComparisonBoardTooLargeError` carries that read's
`BoardReadEvidence`, and the observation absorbs it. A board refused at the
ceiling therefore reports its real observed market count, provider statuses,
failure reasons, freshness, cache states, disabled providers, and comparison
availability — the same facts its public `observed_market_count` already
states — instead of an empty read that looks like it never happened.

The event records latency, the HTTP outcome and status, comparison
availability, provider status and failure-reason counts, freshness and
cache-state counts, and group/market/unresolved/disabled counts. Outcome and
status are one closed pairing (`served`/200, `not_modified`/304, `invalid`/400,
`too_large`/400, `disabled`/404, `error`/500, `unavailable`/503) enforced where
the event is built, alongside the finite non-negative duration and boolean-free
counts. Two outcomes share the 400, so the refusal the service raised chooses
between them and every other status names its outcome alone; a status the
vocabulary cannot state is a defect, logged and counted as `error`. Every label
comes from a closed vocabulary in `app.utils.telemetry`; no athlete, event,
market, selection, or provider-source ID and no upstream text can become a
metric dimension. An unauthenticated request opens no observation and records
nothing: telemetry begins where the caller's identity does. Operators read the
most recent 50 as `recent_board_request_events` on `GET /api/data/telemetry`.
The collector's own `BoardTelemetryEvent` remains the record of one retrieval.

**Operations.** Catalog freshness gates comparisons but never retrieval, so a
stale catalog yields a 200 board with `comparison_availability.available:
false` and the catalog identity and age that explain it. Athlete and event
catalogs are refreshed by deployment-owned scheduling with
`scripts/refresh_athlete_catalog.py` and `scripts/refresh_event_catalog.py`
(daily is the reviewed cadence; API workers run no scheduler). Redis fails open:
with `ENABLE_CACHE=false` or an unreachable Redis, snapshots are retrieved
directly and each provider report states its `cache` state. Ambiguous
identities are governed offline with `scripts/athlete_mappings.py` and
`scripts/event_mappings.py`; version 1 exposes no mapping mutation route.
Version 1 reports facts only — minimum, maximum, Threshold Spread, counts,
freshness, and references — and never a probability, expected value,
recommendation, or entry payout.

PBP Stats:

```text
GET /api/health/pbp-stats or PUT /api/data/*_PBP
  → injected PBPStatsProvider
  → shared requests.Session with PBP-specific connect/read timeouts and retries
  → validate multi_row_table_data and normalize to a dataframe
  → ProviderUnavailableError on timeout, unavailable, or malformed responses
```

`DataService` and `ProviderHealthService` receive the same app-owned provider
instances from `ApplicationDependencies`; they do not create duplicate
providers or perform provider calls in route modules.

### Collection control plane

Issue #84's Railway control plane is an additive seam beside the legacy data
refresh queue. `app.services.collection_control` owns the Active Season,
catalog bootstrap/publication, immutable cutoff-specific Collection Manifest,
collector identity/token, atomic Observation ingestion, and fenced Publication
pointer services. Collectors receive short-lived HMAC-signed tokens bound to an
environment, audience, operation, owner, provider, and explicit surface scopes;
generic `poll`/`ingest` capabilities never widen that binding. Token replay can be rejected by
consuming its ID, and rotated machine secrets overlap only until their bounded
expiry. A repeated collector/client observation ID with the same checksum is
an idempotent receipt; a conflicting checksum is rejected. Publication
advancement increments a per-stream database fence, preserving the prior
active version for rollback and rejecting stale composition workers. Accepted
observations enqueue a deduplicated composition job immediately, while
`reconcile_pending` is the scheduled backstop. Composition derives its gate
from registered required observations plus league/Base completeness evidence;
a caller-provided `complete` flag alone cannot advance a pointer. A manifest
may additionally declare one immutable atomic repair group: the set of streams
whose replacement must land together because their existing rollback targets
are the defect being repaired. The declaration is bound into the manifest
checksum, is stored as normalized group/member rows, and holds its members'
composition jobs `queued` instead of promoting them independently -- the
worker skips them and `compose_from_observations` refuses them with
`grouped_repair_pending`. Collector reads see the group filtered to the
surfaces they already hold and never its pointer guards. See
[PUBLICATION_REPAIR_GROUPS.md](PUBLICATION_REPAIR_GROUPS.md). Production
requires `COLLECTOR_SIGNING_SECRET`; only non-production credential-free runs
may use a process-local key.

The source for the standalone residential writer lives in `app.collector`, but
the wheel maps only that directory to the installed `statsplus_collector`
package. Flask routes, models, services, SQLAlchemy, and Postgres drivers are
absent from the distribution and runtime import graph. A one-shot invocation uses an injected NBA provider
compatibility seam, a bounded WAL SQLite Outbox, and `RailwayClient`; the
Outbox stores only compressed normalized envelopes and deletes them only after
an exact durable receipt checksum. It orders newest cutoffs first, refuses a
hard-limit write based on the actual database/WAL footprint, and removes unsent
work only when an explicit governed cutoff makes it obsolete. Cached Bootstrap Requests and Manifests
are routing metadata only and are executable during a Railway outage only
until their server-issued expiry/deadline. The Windows wrapper retries only
transient/pending exit codes for the bounded recovery window; release
directories are immutable; install, upgrade, and rollback leave the named task
disabled until the explicit credential/config/checksum/rehearsal promotion gate.

Migration 017 creates these records without changing existing public readers.
The collector routes are narrow HTTP adapters under `/api/collector`, while
reasoned Firebase-admin mutations live under `/api/admin/collection`; raw
observations and credentials are never returned. Machine discovery/polling is
environment-, owner-, provider-, and surface-bound, deterministic, and bounded;
bootstrap status and
catalog publication complete the executable Request -> Catalog -> Manifest
handshake.
Manifest discovery additively expands frozen authorized surfaces into bounded
`scope_descriptors`; each descriptor fixes the team, category, Season/L15
window, and cutoff-derived `date_to`. Collector status reports contain only
the release version/checksum; Railway owns the persisted `last_seen_at`.
Every operator mutation is coordinated by one service transaction that writes
the state change, a durable `OperatorJob`, and its audit event together; a
failed mutation cannot leave a succeeded job or audit trail. Publication
composition locks its per-stream pointer row on PostgreSQL and checks the
worker's expected fence before advancing it. Completeness filters accepted
observations by manifest, provider, and registered scope, then applies the
canonical 30-team or registered Base evidence gate. Catalog publication enters
through the same bounded, gzip Observation Envelope path as other collector
evidence; the accepted catalog observation and its governed publication are one
transaction, so a direct complete flag cannot bypass row validation. Event
Catalog validation requires unique canonical game IDs, canonical home/away
teams, phase/status/date evidence, and configurable whole-season volume/team
bounds. Athlete Catalog validation requires unique identity/team rows with
season-coverage evidence and the identities derived from accepted Event/Railway
evidence; an optional caller list cannot assert completeness and an empty Event
Catalog cannot establish a no-game cycle.

Collector ingestion is serialized by a database-backed identity lease rather
than a process-local semaphore. PostgreSQL acquires the lease row with
`SELECT ... FOR UPDATE`, increments its fence, and expires it after a bounded
interval for crash recovery; a busy worker receives explicit retry timing. The
owner/fence pair is rechecked under the row lock immediately before accepted
observation and composition-enqueue commit, so an expired worker that was
taken over fails closed with `stale_lease`.
Usage counters reset locked rows in place at the 24-hour boundary and use the
same row-lock discipline. Event/Athlete Catalog completeness is proven by
exact equality with governed Active Season/Event schedule and roster evidence;
only Regular Season rows qualify, and provider/env floors never assert a whole
season. Lifecycle audits are
append-only and include token issue/use, rotation/revocation, and rejected
same-ID/different-checksum observations. Maintenance emits deterministic
first-failure, stale-threshold, six-hour attention, and recovery alerts while
suppressing false failure/stale alerts when work is queued or running.

Publication versions retain normalized references to exact accepted
Observation IDs in `publication_observations`. Garbage collection follows
active, previous, and rollback pointer relations instead of searching payload
JSON, and pruning removes old rendered facts while preserving immutable
provenance/audit metadata. Identity-unresolved validation writes one bounded,
deduplicated Reconciliation Item before rejecting the input.

Catalog publication and keyed reconciliation share one transaction. Additions
and corrections update governed EventCatalog/AthleteCatalog rows for the next
complete snapshot; missing rows remain untouched unless an explicit complete
snapshot supplies an exact tombstone set. Incomplete attempts are retained as
incomplete evidence without destructive reconciliation. A changed event ID or
completed-game set supersedes affected frozen manifests/cycles. Catalog reads
select the newest complete Event publication and the newest fresh complete
Athlete publication that covers every Event-derived identity, skipping newer
incomplete attempts. Maintenance runs publication pruning after reconciliation
and observation GC, while active/previous/rollback provenance remains
protected.

A successful complete reconciliation also advances the canonical
`event_catalog_refreshes` or `athlete_catalog_freshness` sidecar in that same
transaction. This keeps governed publications compatible with downstream
services that enforce the canonical catalog freshness contract. Migration 035
backfills those sidecars from the newest complete governed publication for
each catalog type and season, including catalogs published before this bridge
was introduced.

Migration 036 creates `publication_player_game_logs`, an immutable normalized
query projection with `ON DELETE CASCADE` provenance to
`publication_versions`. It backfills every valid existing player-log
publication and leaves malformed historical versions unprojected so their
existing fail-closed unavailable behavior is preserved. New composition and
rollback transactions insert projection rows before advancing or returning a
publication.

Collector release health crosses a separate machine-authenticated status seam.
It persists only a validated 64-character release identifier/checksum pair and
the report time on `collector_identities`; arbitrary fields, secrets, payloads,
and player data do not cross that seam. Migration 023 adds the checksum column
for existing deployments.

Admin diagnostics join publication streams to their active pointer/version and
join usage to the database lease. Stream availability is registry-derived:
`never_schedule` is always `unavailable` and cannot be activated. Otherwise a
missing active version is `missing`; active versions use the closed freshness
rule thresholds (`cutoff_current` one hour, `daily_recheck` 24 hours,
`seven_day` seven days) against the injected clock. Age and retry values are
clamped to finite non-negative integers. Usage reports the configured 24-hour
poll/envelope/byte ceilings, one active database lease as concurrency, the
counter reset instant, and lease retry timing. Diagnostics expose identifiers
and operational metadata only.

Credential rotation returns only a durable status to the admin console. A
short-lived machine token plus the old secret during the configured overlap
window claims an encrypted one-time delivery; the admin metadata endpoint
cannot decrypt or expose the replacement.

## Schema maintenance

Application-owned tables are versioned by `app.migrations.run_migrations` and
the `scripts/migrate.py` command. Migration 004 adds the canonical
`athlete_catalog` and `athlete_catalog_freshness` tables. A fresh or existing
application database can
be created or upgraded with an explicit `--database-url` argument or
`DATABASE_URL`; the CLI has no database-file fallback and fails if neither is
provided. Rerunning the command is idempotent because applied versions are
recorded in `schema_migrations`. PostgreSQL migration runs acquire one
transaction-scoped advisory lock before inspecting or changing the migration
table. Status output masks database passwords.

Production Gunicorn workers never run migrations from `create_app()`. Railway
owns the deployment boundary: `railway.json` runs `python scripts/migrate.py`
as a pre-deploy command, and a nonzero result prevents the new application
workers from starting. Local and test app factories retain automatic schema
initialization for their disposable databases. Migration 031 idempotently
recreates the five Canonical Game Ledger tables from migration 024, repairing
the production drift caused by the former concurrent worker-startup path.

The pre-deploy command must run in the built deploy image so bare `python`
resolves to the interpreter that already has SQLAlchemy and psycopg2 installed,
and `DATABASE_URL` must be present in the pre-deploy context; otherwise
`scripts/migrate.py` cannot import `app.migrations`' dependencies or resolve a
target and exits non-zero. `scripts/migrate.py` is fail-closed by construction:
`argparse` exits 2 when no target is supplied, and any exception from
`run_migrations` propagates out of `main` (never a zero status), so a failed
migration exits non-zero and Railway does not promote the release. Its output
is the retrievable deploy-log record of the outcome — either
`Applied N migration(s) to <url>: ...` or `Database is already up to date at
version N: <url>` with the password redacted.

Because a pre-deploy step can still be skipped, misconfigured, or edited after
a release (the exact 45-vs-046 drift that motivated issue #189, whose root
cause is not confirmable from the repository and remains the operator's to
read from a real pre-deploy log), `create_app()` runs a boot-time schema-drift
guard (`app.startup_schema_guard.verify_schema_is_current`) as a fail-closed
backstop. Before serving, it compares the live schema head (max `version` in
`schema_migrations`) with the code's expected head
(`max(m.version for m in MIGRATIONS)`): if the database is strictly behind, it
raises `SchemaBehindError` so the worker fails to boot, the Railway healthcheck
fails, and the behind-schema release is not promoted. A database at or ahead of
the expected head boots normally, so a mid-rollout worker running older code
against a newer schema is never blocked. The guard is deliberately narrow — it
is inert for the local/testing environments, the read-only demo fixture, and
any database that records no migration head (the offline test suite's
mock-engine app factories) — so it engages only for a real, non-demo,
non-testing deployment database. The emergency override `ALLOW_SCHEMA_DRIFT=true`
downgrades the fatal error to a loud warning; it defaults to enforcing and
should be removed once the schema is migrated.

Recovery from drift: run `python scripts/migrate.py` with `DATABASE_URL` set to
the affected database (or pass `--database-url`); it applies the pending
migrations idempotently and prints the resulting head. Then redeploy so the
boot guard passes. The Railway service settings cannot be changed from this
repository; the operator must confirm the `statsplus-backend` production deploy
manifest keeps `preDeployCommand: "python scripts/migrate.py"` (asserted by
`tests/test_railway_config.py`) running fail-closed in the built environment.

Migration 005 creates the writable `event_catalog` and
`event_catalog_refreshes` tables. Migrations are applied in order. Event
refreshes upsert by NBA game ID in one transaction without replacing the
table; omitted historical rows remain available and replacement IDs remain
distinct. Provider event mapping state is owned by migration 008 below, and
provider-athlete mapping state by migration 006. Event freshness is
independent from Athlete Catalog freshness and defaults to 72 hours through
`EVENT_CATALOG_MAX_AGE_HOURS`. Operators use
`scripts/refresh_event_catalog.py` with one or more explicit seasons; each
season is independent and the command exits nonzero if any season fails.

Migration 006 creates the provider athlete mapping, append-only decision,
decision candidate, and durable rejection tables. Migration 007 adds
`athlete_mapping_decision_contradictions`, the typed evidence a fail-closed
observation contradicted itself over, keyed by decision and ordered by the same
deterministic evidence order the conflict was recorded in; the decision itself
carries only the representative evidence, so without those rows the rest of the
contradiction would be missing from history and the conflict queue. Those rows
are `ON DELETE CASCADE` children of the decision, and SQLite ignores declared
foreign keys unless `PRAGMA foreign_keys` is set per connection, so
`app.utils.db` registers one SQLAlchemy `connect` listener on the `Engine`
class that sets the pragma on every SQLite DBAPI connection in the process —
including engines scripts and tests build directly, because referential
integrity is a property of the schema rather than of one caller's engine. The
listener recognizes the SQLite driver connection by type and does nothing for
PostgreSQL, which enforces its own constraints. Operators
use
`scripts/athlete_mappings.py` for
read-only listing, dry runs, audited approve/override/reject/clear actions,
and history. `list` reports current mappings, active rejections, every
identity whose latest decision is still unresolved, with the candidates an
operator has to choose between, and the `conflicts` review queue of identities
whose current state is a mapping conflict. `history` and the conflict queue
report each decision's `contradictory_evidence` alongside its candidates, so an
operator reviews everything the markets asserted rather than the one evidence
the conflict happened to be recorded on. These commands require an
explicit writable database URL and never contact a provider.

Migration 008 creates the provider event mapping, append-only decision,
decision candidate, decision contradiction, durable rejection, and per-identity
lock tables (`provider_event_mappings`, `event_mapping_decisions`,
`event_mapping_decision_candidates`,
`event_mapping_decision_contradictions`, `event_mapping_rejections`, and
`event_mapping_locks`). Its database checks mirror migration 006's for the event
vocabulary: the closed mapping-state set (`ambiguous`, `auto`,
`manual_approved`, `manual_override`, `mapping_conflict`, `rejected`,
`replacement_pending`, `unmatched`), the closed decision-state set, active-state
coherence — only `auto`, `manual_approved`, and `manual_override` may be active
— cleared-rejection coherence, and conflict-column coherence, so a row that has
left `mapping_conflict` may not keep naming a conflicting game. The boolean
literals are `true`/`false`, so the checks are valid on PostgreSQL as well as
SQLite, and the candidate and contradiction rows are `ON DELETE CASCADE`
children of their decision, each keyed by it and ordered by the deterministic
evidence order the observation was recorded in. Migration 008 is applied by
model-driven creates, so re-running it is a no-op on a database that already
has the tables. `tests/integration/test_postgres.py` exercises the schema, the
cascade, the savepoint/row-lock concurrency, and the idempotency and manual
precedence rules against a real PostgreSQL database when `TEST_DATABASE_URL` is
set, and is skipped otherwise so the default suite stays offline.
Operators use `scripts/event_mappings.py` for read-only listing, dry
runs, audited approve/override/reject/clear actions, and history, with the same
writable-URL requirement and no provider contact; the command builds the
catalog read seam with a provider that refuses every call, so it is offline by
construction.

Migration 012 creates the raw window-aware team matchup tables described
above after migration 011's canonical player game logs. Migration tests pin
that order and exercise a fresh apply plus an idempotent rerun.
Migration 013 then creates the Season player Diet fact and observation tables.

The tracked `nba_play_types.db` file is a public read-only fixture. Run
`scripts/validate_demo_db.py` to check its required tables and columns without
opening it for writes. Migration 009 creates the surface-keyed
`stats_refreshes` completion records; migration 010 creates Player Pool
snapshots; migration 011 creates normalized phase-aware player-game-log facts
and their season freshness record.
Migration tests must use a temporary database, and the validator must not be
used to repair the fixture.

## Test seams

- App and route behavior: use the `app` and `client` fixtures in `tests/conftest.py`.
- Route/service interaction: replace methods on the dependency graph supplied
  through the `DEPENDENCIES` app-factory override.
- Provider failures: raise the relevant `requests` timeout/error from a patched service or endpoint constructor.
- Provider response contracts: run the recorded fixtures in `tests/fixtures/nba_stats` and `tests/fixtures/pbp_stats` through the production parse seams (`parse_recorded_game_logs`, `parse_recorded_player_roster`, `parse_recorded_schedule`, `PBPTotalsAdapter.parse_totals`, `PBPGameLogAdapter.parse_game_logs`, `PBPGameLogAdapter.parse_game_stats`) with no network.
- Player Diet provider contracts: run `tests/fixtures/player_diets/` through
  `parse_recorded_player_diet` and
  `PBPTotalsAdapter.parse_player_diet_totals`; these fixtures pin canonical
  player IDs and raw Season share, volume, and game fields without network.
- DFS provider contracts: run each Dabble, PrizePicks, and Underdog adapter
  against its recorded fixtures through `get_snapshot`; the shared compliance
  suite verifies the same immutable `ProviderSnapshot` boundary for all three.
- `PBPTotalsAdapter.parse_totals` validates the operation-specific columns
  consumed by the existing PBP Season publication/assist transforms. The #57
  rolling matchup path separately requires `TeamId`, `SecondsPlayed`, and
  `GamesPlayed`, so exact Last-15 publication fails closed on absent identity,
  denominator, or game-count evidence without widening the shared Season
  parser contract. A nonempty row set missing a Season-required column is a
  malformed provider response; an empty result is materialized with that
  declared schema so refresh publication cannot replace a valid table with a
  schema-less frame.
- Live provider contracts: `tests/live/test_provider_contracts.py` hits the real providers and is excluded from the default gate by the registered `live` marker (`addopts = -m "not live"`). Opt in with `LIVE_CONTRACT_TESTS=true` plus `-m live`.
- Parser behavior: use the bundled SQLite data and patch static NBA lookups when the parser needs a deterministic team list.
- LLM behavior: inject or mock the OpenAI client; the default suite must not require an API key.

The authoritative local and CI gate is `./scripts/check.sh`.

## Known seams to improve incrementally

- The app-scoped dependency graph keeps app-factory isolation explicit while
  avoiding database, Redis, and parser initialization during route imports.
- The game-log request path is fully synchronous and bounded: the route
  parses query parameters into one typed `GameLogQuery`, and the service runs
  under Flask's threaded gunicorn model (`--workers 4 --threads 2`). That path
  reaches no provider client: player logs and Team Filters come from durable
  publications, and a structural test walks the dependency graph and asserts
  an empty provider path list. Diet facts arrive
  through a read-only reader bound to the Diet repository, not the
  refresh-capable service that owns the adapters.  Wherever an NBA Stats call
  is made, it goes through
  `NBAStatsAdapter`, which applies a
  process-shared `threading.BoundedSemaphore` sized by
  `NBA_STATS_MAX_CONCURRENCY` (default 10) and shares the provider timeout from
  `NBA_STATS_TIMEOUT_SECONDS`.  All adapter instances using the same configured
  limit in one worker reuse that gate.
- The `NBA_STATS_MAX_CONCURRENCY` bound is per worker **process**, not global:
  all adapters in each worker share one gate, so worst-case provider
  concurrency is `workers × NBA_STATS_MAX_CONCURRENCY` (4 × 10 = 40 with the
  Procfile defaults). A true cross-process bound would need shared locking
  (e.g. Redis) and is intentionally out of scope; operators scale the bound and
  worker count together.
- `app.utils.db.get_engine()` applies `DatabaseSettings` pool configuration
  (`pool_size`, `max_overflow`, `pool_recycle_seconds`,
  `connect_timeout_seconds`) only to Postgres engines, so the worst-case
  Postgres connection count is `processes × (pool_size + max_overflow)` — with
  the Procfile's 4 gunicorn workers and the documented defaults, `4 × (3 + 4)
  = 28`. Scripts that call `create_engine` directly
  (`scripts/migrate.py`, `scripts/nightly_refresh.py`, and similar
  one-shot/operator scripts) are not governed by these settings.
- Several services catch broad exceptions and return sentinel values, which can hide provider-specific failures.
- The bundled provider-generated tables are validated as a public fixture; they
  are not application migration targets.

Keep these constraints visible when changing nearby code. Improve them behind tests in small slices instead of combining them with unrelated feature work.
