# Codex Build Prompt: Sleeper Fantasy Football Operations Monitor

You are Codex acting as a principal engineer. Build a production-quality, personal, noncommercial fantasy-football operations system for a Sleeper league.

Do not stop after writing a design document. Inspect the repository, create a staged implementation plan, and then implement a working vertical slice with tests, migrations, Docker configuration, documentation, a dashboard, scheduled monitoring, and a durable event/action queue.

When information is missing, use conservative defaults, mark the corresponding feature as unresolved, and continue implementing everything else. Do not invent league settings or silently assume undocumented Sleeper enum meanings.

## 1. Product objective

Build a low-attention fantasy-football management system that continuously:

1. Monitors the Sleeper league, draft, rosters, lineups, matchups, waiver order, transactions, player status, NFL schedule, injuries, projections, usage, weather, and bye weeks.
2. Detects upcoming fantasy-management obligations well before deadlines.
3. Produces draft recommendations, lineup recommendations, AutoSub recommendations, waiver claim trees, free-agent targets, IR actions, bye-week plans, and protected-drop warnings.
4. Sends escalating reminders before actions lock.
5. Verifies that completed actions actually appear in Sleeper.
6. Maintains a complete audit trail showing what the system knew, what it recommended, why it recommended it, and whether the action was completed.
7. Submits or responds to trades only when exact, fully grounded, high-upside whole-roster policy
   and low-volume anti-spam controls authorize it.
8. Never drops a protected player.
9. Never leaves an avoidably inactive player in a starting slot.
10. Continues to function when an optional projection, weather, news, or language-model provider is unavailable.

The goal is to maximize the probability of a top-three finish while minimizing required owner attention and preventing catastrophic mistakes.

## 2. Mandatory Sleeper access boundary

Sleeper’s documented public API is read-only, requires no authentication token, and supports league, roster, matchup, transaction, draft, player, and trending-player data.

Use the documented public API for authoritative reads and verification. Authorized roster writes
run only through the dedicated, signed-in local browser session and the semantic action outbox.

Hard constraints:

* Do not use reverse-engineered/private HTTP endpoints, capture mobile-app traffic, or store Sleeper
  credentials. Playwright may attach to the isolated local Chrome profile after the owner completes
  Sleeper login; browser state never enters prompts or leaves the machine.
* Do not implement `POST`, `PUT`, `PATCH`, or `DELETE` requests to Sleeper.
* Do not ask the user for a Sleeper password, token, cookie, or session.
* Do not put Sleeper authentication fields in the database, configuration schema, logs, or environment templates.
* Only call documented public `GET` endpoints under `https://api.sleeper.app/v1/`.
* Keep an `ExecutionAdapter` interface, but ship only:

  * `ManualExecutionAdapter`
  * `DisabledSleeperWriteAdapter`
* A future write adapter may be enabled only when it is based on a documented Sleeper write API or an integration Sleeper has explicitly approved in writing.
* Add an automated test that fails if application code attempts a non-GET request to a Sleeper hostname.
* Add an automated test that fails if Playwright, Selenium, Puppeteer, browser cookies, or Sleeper credential fields are introduced.

Official references:

```text
https://docs.sleeper.com/
https://support.sleeper.com/en/articles/5486620-general-terms-of-use
```

Sleeper documents the API as read-only and recommends staying below 1,000 API requests per minute. The application should remain far below that threshold.

## 3. Known league configuration

Use these values as expected assertions, but fetch the live league object during bootstrap and report every discrepancy.

```yaml
league:
  platform: sleeper
  league_id: "1395499060898586624"
  season: "2026"
  total_teams: 12
  timezone: America/Denver

roster:
  starters:
    QB: 1
    RB: 2
    WR: 3
    TE: 1
    FLEX_RB_WR_TE: 2
    K: 1
    DEF: 1
  bench: 5
  injured_reserve: 1
  draft_rounds: 16

waivers:
  expected_type: rolling_priority
  faab_expected: false
  exact_clear_day: unresolved_fetch_from_sleeper
  exact_clear_time: unresolved_fetch_from_sleeper
  post_drop_waiver_duration: unresolved_fetch_from_sleeper
  custom_daily_waivers: unresolved_fetch_from_sleeper
  after_game_clear_rule: unresolved_fetch_from_sleeper

draft:
  draft_position: unresolved
  draft_date: unresolved
  draft_type: unresolved
  pick_timer: unresolved
  third_round_reversal: unresolved
```

### Expected scoring

```yaml
passing:
  yards: 0.04
  touchdown: 4
  two_point_conversion: 2
  interception: -1

rushing:
  yards: 0.1
  touchdown: 6
  two_point_conversion: 2

receiving:
  reception: 0.5
  yards: 0.1
  touchdown: 6
  two_point_conversion: 2

kicking:
  field_goal_0_19: 3
  field_goal_20_29: 3
  field_goal_30_39: 3
  field_goal_40_49: 4
  field_goal_50_59: 5
  field_goal_60_plus: 6
  extra_point_made: 1
  field_goal_missed: -1
  extra_point_missed: -1

team_defense:
  defensive_touchdown: 6
  points_allowed_0: 10
  points_allowed_1_6: 7
  points_allowed_7_13: 4
  points_allowed_14_20: 1
  points_allowed_21_27: unresolved_fetch_from_sleeper
  points_allowed_28_34: -1
  points_allowed_35_plus: -4
  sack: 1
  interception: 2
  fumble_recovery: 2
  safety: 2
  forced_fumble: 1
  blocked_kick: 2

special_teams_defense:
  touchdown: 6
  forced_fumble: 1
  fumble_recovery: 1

special_teams_player:
  touchdown: 6
  forced_fumble: 1
  fumble_recovery: 1

miscellaneous:
  fumble_lost: -2
  fumble_recovery_touchdown: 6
```

The first bootstrap report must specifically identify and populate the missing `points_allowed_21_27` value.

## 4. Recommended technology stack

Use this stack unless the repository already establishes a compatible alternative:

```text
Backend: Python 3.12, FastAPI, Pydantic, SQLAlchemy 2, Alembic
Database: PostgreSQL 16+
HTTP: httpx
Scheduling: APScheduler or a small dedicated scheduler process
Durable queue: PostgreSQL-backed queue/outbox
Data processing: Polars where useful
NFL data: nflreadpy
Optimization: OR-Tools CP-SAT or another deterministic optimizer
Frontend: Next.js with TypeScript and a responsive PWA interface
Testing: pytest, pytest-asyncio, respx, Playwright only for our own UI tests
Packaging: uv for Python, pnpm for frontend
Deployment: Docker Compose
Observability: structured JSON logs, Prometheus-compatible metrics, health endpoints
CI: GitHub Actions
```

Do not require Redis for the MVP. Use PostgreSQL row leasing with `FOR UPDATE SKIP LOCKED`, advisory locks where appropriate, and transactional outbox records.

The service should be deployable locally, on a small cloud VM, or on a home server without architecture changes.

## 5. Repository structure

Use a structure similar to:

```text
/
  backend/
    app/
      api/
      clients/
      domain/
      engines/
      events/
      jobs/
      models/
      notifications/
      repositories/
      services/
      settings/
      workers/
    migrations/
    tests/
  frontend/
    app/
    components/
    lib/
    tests/
  fixtures/
    sleeper/
    fantasypros/
    nflverse/
    weather/
  docs/
    architecture.md
    data-sources.md
    implementation-plan.md
    league-readiness.md
    operations-runbook.md
    decision-policy.md
    sleeper-setting-mappings.md
  infra/
  docker-compose.yml
  Makefile
  .env.example
  README.md
```

## 6. Configuration

Create a validated environment configuration.

```dotenv
APP_ENV=development
APP_TIMEZONE=America/Denver
DATABASE_URL=postgresql+psycopg://fantasy:fantasy@db:5432/fantasy

SLEEPER_LEAGUE_ID=1395499060898586624
SLEEPER_USERNAME=
SLEEPER_OWNER_USER_ID=
SLEEPER_ROSTER_ID=
SLEEPER_LEAGUE_WEB_URL=

FANTASYPROS_API_KEY=
SPORTRADAR_API_KEY=
OPENAI_API_KEY=

SLACK_WEBHOOK_URL=
SLACK_CHANNEL_LABEL=sleeper-ai-alerts
SLACK_DAILY_ALERT_LIMIT=20

NWS_USER_AGENT=fantasy-operations-monitor/1.0 contact@example.com
```

Rules:

* `SLEEPER_USERNAME`, owner ID, and roster ID may initially be blank.
* First-run setup must load league users and rosters and allow the owner to select the correct team.
* Store the stable Sleeper user ID after resolving a username.
* Optional providers must fail gracefully and show as disabled rather than preventing startup.
* Do not log secret values.
* Do not store an account password or Sleeper session data.
* Store all timestamps in UTC and render both Mountain Time and Eastern Time where operationally useful.
* Use IANA time zones and `zoneinfo`; do not implement fixed UTC offsets.

## 7. Sleeper runtime endpoints

Create a typed `SleeperClient` with retries, timeouts, response validation, content hashes, conditional requests where supported, and per-endpoint metrics.

### Core league endpoints

```text
GET https://api.sleeper.app/v1/league/1395499060898586624
GET https://api.sleeper.app/v1/league/1395499060898586624/users
GET https://api.sleeper.app/v1/league/1395499060898586624/rosters
GET https://api.sleeper.app/v1/league/1395499060898586624/drafts
GET https://api.sleeper.app/v1/league/1395499060898586624/traded_picks
```

### Season-state and weekly endpoints

```text
GET https://api.sleeper.app/v1/state/nfl
GET https://api.sleeper.app/v1/league/1395499060898586624/matchups/{week}
GET https://api.sleeper.app/v1/league/1395499060898586624/transactions/{week}
GET https://api.sleeper.app/v1/league/1395499060898586624/winners_bracket
GET https://api.sleeper.app/v1/league/1395499060898586624/losers_bracket
```

### Draft endpoints

First retrieve the current draft ID from the league draft endpoint, then monitor:

```text
GET https://api.sleeper.app/v1/draft/{draft_id}
GET https://api.sleeper.app/v1/draft/{draft_id}/picks
GET https://api.sleeper.app/v1/draft/{draft_id}/traded_picks
```

### Player endpoints

```text
GET https://api.sleeper.app/v1/players/nfl
GET https://api.sleeper.app/v1/players/nfl?active=true
GET https://api.sleeper.app/v1/players/nfl?position=QB&active=true
GET https://api.sleeper.app/v1/players/nfl?position=RB&active=true
GET https://api.sleeper.app/v1/players/nfl?position=WR&active=true
GET https://api.sleeper.app/v1/players/nfl?position=TE&active=true
GET https://api.sleeper.app/v1/players/nfl?position=K&active=true
GET https://api.sleeper.app/v1/players/nfl/trending/add?lookback_hours=24&limit=100
GET https://api.sleeper.app/v1/players/nfl/trending/drop?lookback_hours=24&limit=100
GET https://api.sleeper.app/v1/players/nfl/trending/add?lookback_hours=6&limit=100
GET https://api.sleeper.app/v1/players/nfl/trending/drop?lookback_hours=6&limit=100
```

The complete player map is large. Retrieve it once per day, while smaller filtered requests may be used when justified. Sleeper’s player objects can provide injury, practice, team, position, and external-ID metadata.

### Optional user endpoint

```text
GET https://api.sleeper.app/v1/user/{username}
GET https://api.sleeper.app/v1/user/{user_id}/leagues/nfl/2026
```

Do not infer the owner’s roster merely from team name. Match the resolved user ID to the roster’s `owner_id`, or require explicit first-run selection.

## 8. Sleeper settings bootstrap

Implement a `LeagueSettingsNormalizer`.

It must:

1. Store the complete raw league response as immutable JSONB.
2. Normalize:

   * season
   * status
   * total rosters
   * roster positions
   * scoring settings
   * playoff settings
   * trade settings
   * IR settings
   * positional limits
   * AutoSub count
   * waiver type
   * waiver clear day
   * waiver processing hour
   * post-drop waiver duration
   * after-game waiver rule
   * custom daily waiver schedule
   * median-matchup setting
   * draft ID
3. Compare normalized values with the known configuration in this prompt.
4. Produce human-readable and machine-readable discrepancy reports.
5. Surface every unknown setting and raw value.
6. Refuse to mark the system “league ready” while any critical waiver, roster, scoring, or lineup setting is unresolved.

Sleeper exposes many settings as numeric or compact values. Do not assign semantics to an undocumented integer purely from its field name.

Candidate fields may include values resembling:

```text
waiver_type
waiver_day_of_week
waiver_clear_days
waiver_hours
daily_waivers
daily_waivers_hour
waiver_budget
reserve_slots
taxi_slots
playoff_week_start
```

Inspect the live response, retain all raw values, document every mapping in `docs/sleeper-setting-mappings.md`, and add a fixture-based test for each interpreted value.

### Waiver timing semantics

Rolling waivers are continuous: a successful claim sends that team to the bottom of the priority order. Custom daily waiver settings can designate days as free agency, waivers, locked, or waivers followed by free agency. When multiple timing rules apply, the later applicable clearing time controls.

Build a `WaiverScheduleResolver` that considers:

* After-game waiver clearing
* Time on waivers after a player is dropped
* Custom daily waiver status
* Custom daily processing hour
* Game start
* Player drop timestamp
* Current league week
* Daylight-saving transitions
* Any league-setting change

The resolver must return:

```yaml
player_id:
eligible_claim_at:
expected_process_at:
expected_free_agent_at:
controlling_rule:
confidence:
unresolved_inputs:
```

Do not guess the exact league waiver timestamp. Resolve it from the live league object. After the first real waiver period, compare the predicted time with completed transaction timestamps and calibrate the schedule model.

If waivers do not process at the expected time:

* Continue polling.
* Raise a high-priority alert after a configurable grace period.
* Do not advise the owner or commissioner to alter waiver settings immediately.
* Show the Sleeper support guidance and recommend preserving the current state for diagnosis. Sleeper warns that changing settings can interfere with diagnosing delayed waivers.

## 9. External data providers

Use provider interfaces so individual vendors can be replaced.

```python
class ProjectionProvider: ...
class RankingProvider: ...
class InjuryProvider: ...
class NewsProvider: ...
class UsageProvider: ...
class WeatherProvider: ...
class ScheduleProvider: ...
```

Every observation must include:

```yaml
provider:
source_url:
observed_at:
source_updated_at:
effective_at:
content_hash:
raw_payload:
normalized_payload:
confidence:
freshness_status:
```

Never overwrite the last known-good snapshot with malformed, empty, truncated, or implausibly partial data.

### 9.1 FantasyPros

Use FantasyPros as the primary structured projection, rankings, injury, and player-news provider for the MVP.

Base URL:

```text
https://api.fantasypros.com/public/v2/json
```

Endpoints:

```text
GET /nfl/players
GET /nfl/news
GET /nfl/injuries
GET /nfl/2026/consensus-rankings
GET /nfl/2026/projections
GET /nfl/2026/player-points
```

Authenticate with:

```text
x-api-key: ${FANTASYPROS_API_KEY}
```

Use the official API documentation for endpoint parameters, pagination, scoring modes, week selection, and rate limits:

```text
https://www.fantasypros.com/api-data/
https://api.fantasypros.com/
```

FantasyPros documents projections with full statistical lines, consensus rankings, news, injuries, player metadata, and cross-provider identifiers. Recalculate fantasy points locally from projected statistics whenever the provider supplies enough detail rather than trusting a generic scoring preset.

### 9.2 nflverse

Use `nflreadpy` for schedule, historical usage, roster, injuries, stats, and snap-count data.

References:

```text
https://github.com/nflverse
https://github.com/nflverse/nflreadpy
https://github.com/nflverse/nflverse-data
https://github.com/nflverse/nflverse-data/releases
```

Functions may include:

```python
load_schedules()
load_players()
load_rosters()
load_rosters_weekly()
load_player_stats()
load_team_stats()
load_snap_counts()
load_injuries()
```

Treat nflverse as an analytical and historical source rather than a second-by-second injury source. Participation data may be historical rather than live.

### 9.3 Official NFL references

Provide direct links in every relevant action card:

```text
https://www.nfl.com/injuries/
https://operations.nfl.com/calendar-events/nfl-important-dates/
https://operations.nfl.com/gameday/pre-game/countdown-to-kickoff/
```

Do not aggressively crawl NFL HTML pages. Use them as official human-verification references and use a structured provider for automated injury ingestion.

Create report-window schedules based on the actual day of each game. The NFL’s 2026 reporting policy includes:

```text
Monday game:
  practice reports Thursday, Friday, Saturday
  final game status Saturday

Wednesday game:
  practice reports Sunday, Monday, Tuesday
  final game status Tuesday

Thursday game:
  practice reports Monday, Tuesday, Wednesday
  final game status Wednesday

Friday game:
  practice reports Tuesday, Wednesday, Thursday
  final game status Thursday

Saturday game:
  practice reports Tuesday, Wednesday, Thursday
  final game status Thursday

Sunday game:
  practice reports Wednesday, Thursday, Friday
  final game status Friday
```

Reports are generally due by 4:00 p.m. Eastern or shortly after practice. Do not hardcode a Thursday–Sunday-only NFL schedule.

The official pregame process includes inactive-list delivery around 90 minutes before kickoff. Begin intensified monitoring before that point rather than waiting until exactly T-90.

### 9.4 Weather

For games in the United States, prefer the National Weather Service.

Reference:

```text
https://www.weather.gov/documentation/services-web-API
```

Runtime endpoints:

```text
GET https://api.weather.gov/points/{latitude},{longitude}
GET {forecast URL returned by /points}
GET {forecastHourly URL returned by /points}
GET https://api.weather.gov/alerts/active?point={latitude},{longitude}
```

Send a descriptive `User-Agent`. The NWS API is open, cache-friendly, and requires a User-Agent.

For international games or as a fallback, implement an Open-Meteo adapter:

```text
GET https://api.open-meteo.com/v1/forecast
```

Suggested parameters:

```text
latitude
longitude
hourly=temperature_2m,precipitation_probability,precipitation,wind_speed_10m,wind_gusts_10m
temperature_unit=fahrenheit
wind_speed_unit=mph
precipitation_unit=inch
timezone=auto
forecast_days=16
```

Documentation:

```text
https://open-meteo.com/en/docs
```

Open-Meteo provides global forecast coverage and allows noncommercial API access within its stated limits.

Maintain a versioned stadium table containing:

```yaml
stadium_id:
name:
team:
latitude:
longitude:
country:
timezone:
roof_type:
retractable:
weather_relevant:
source:
verified_at:
```

Skip weather adjustments for confirmed closed-roof games. Treat retractable-roof state as uncertain until confirmed.

### 9.5 Optional professional provider

Create but do not require a Sportradar adapter for structured injuries, depth charts, transactions, and real-time updates.

```text
https://developer.sportradar.com/football/reference/nfl-overview
```

The application must remain fully functional without this paid provider.

## 10. Canonical player identity

Create a canonical player table and an ID mapping table.

```yaml
canonical_player:
  id:
  full_name:
  nfl_team:
  fantasy_positions:
  active:
  last_verified_at:

player_external_id:
  canonical_player_id:
  provider:
  external_id:
  confidence:
  source:
```

Use available Sleeper and FantasyPros cross-reference IDs before fuzzy matching.

Fuzzy matching may be used only as a fallback and must consider:

* Normalized full name
* Team
* Position
* Suffixes such as Jr., Sr., II, III
* Recent team changes
* Defensive team identifiers
* Duplicate names

Any ambiguous match must enter a manual resolution queue and must not influence a destructive recommendation.

## 11. Database model

At minimum, implement:

```text
league_settings_snapshots
league_members
league_roster_snapshots
league_matchup_snapshots
league_transaction_snapshots
waiver_priority_snapshots
drafts
draft_picks
players
player_external_ids
nfl_games
nfl_teams
stadiums
injury_snapshots
practice_report_snapshots
projection_snapshots
ranking_snapshots
usage_snapshots
weather_snapshots
source_observations
source_health
domain_events
scheduled_jobs
action_items
action_dependencies
action_audit
notification_deliveries
manual_confirmations
```

Important snapshot rules:

* Preserve raw payloads as JSONB.
* Add `observed_at`, `source_updated_at`, and `effective_at`.
* Add a content hash.
* Deduplicate identical observations.
* Never update historical snapshots in place.
* Maintain normalized current-state views separately.
* Store reasons when a record is rejected as malformed or implausible.

## 12. Durable event queue

Build a Postgres-backed event queue.

Event record:

```yaml
id:
event_type:
entity_type:
entity_id:
priority:
payload:
dedupe_key:
correlation_id:
causation_id:
created_at:
effective_at:
not_before:
expires_at:
status:
attempt_count:
lease_owner:
lease_until:
last_error:
completed_at:
```

Required behavior:

* At-least-once delivery
* Idempotent consumers
* Atomic outbox writes with domain-state changes
* Exponential backoff with jitter
* Configurable maximum attempts
* Dead-letter state
* Lease recovery after worker failure
* Dedupe by semantic key
* No duplicate user notifications for unchanged information
* Replay tooling for debugging and tests

Implement these event families:

```text
SOURCE_REFRESH_DUE
SOURCE_REFRESH_COMPLETED
SOURCE_CHANGED
SOURCE_STALE
SOURCE_FAILED
SOURCE_RECOVERED

LEAGUE_SETTINGS_CHANGED
LEAGUE_READINESS_CHANGED
ROSTER_CHANGED
ROSTER_OVER_LIMIT
LINEUP_CHANGED
LINEUP_INVALID
IR_ELIGIBILITY_CHANGED
BYE_CONFLICT_DETECTED

WAIVER_PRIORITY_CHANGED
WAIVER_WINDOW_OPENED
WAIVER_PLAN_DUE
WAIVER_DEADLINE_APPROACHING
WAIVERS_EXPECTED_CLEARED
WAIVERS_VERIFIED
WAIVERS_DELAYED
PLAYER_DROPPED
PLAYER_AVAILABLE_NOW
PLAYER_WAIVER_ELIGIBILITY_CHANGED

DRAFT_CREATED
DRAFT_TIME_CHANGED
DRAFT_SLOT_ASSIGNED
DRAFT_STARTED
DRAFT_PICK_MADE
ON_CLOCK_SOON
ON_CLOCK
DRAFT_COMPLETED

PRACTICE_REPORT_EXPECTED
PRACTICE_REPORT_POSTED
INJURY_STATUS_CHANGED
GAME_STATUS_CHANGED
INACTIVES_DUE
PLAYER_INACTIVE
PLAYER_ACTIVATED
STARTER_AT_RISK

PROJECTION_CHANGED
RANKING_CHANGED
USAGE_CHANGED
WEATHER_RISK_CHANGED
DEPTH_ROLE_CHANGED

ACTION_CREATED
ACTION_UPDATED
ACTION_DUE
ACTION_OVERDUE
ACTION_CONFIRMED
ACTION_VERIFICATION_DUE
ACTION_VERIFIED
ACTION_FAILED
ACTION_EXPIRED

DAILY_DIGEST_DUE
WEEKLY_REVIEW_DUE
PLAYOFF_PREP_DUE
```

## 13. Action queue

The action queue is the center of the product.

State machine:

```text
detected
  -> evaluated
  -> proposed
  -> approval_required
  -> ready
  -> user_confirmed
  -> verifying
  -> verified
```

Terminal or alternate states:

```text
dismissed
cancelled
expired
failed
superseded
```

Action types:

```text
DRAFT_PLAYER
SET_LINEUP
SET_AUTOSUB
MOVE_PLAYER_TO_IR
ACTIVATE_PLAYER_FROM_IR
SUBMIT_WAIVER_CLAIM
CANCEL_WAIVER_CLAIM
ADD_FREE_AGENT
DROP_PLAYER
STREAM_KICKER
STREAM_DEFENSE
RESOLVE_ROSTER_LIMIT
REVIEW_TRADE
VERIFY_LEAGUE_SETTING
```

Trades enter the outbox only as exact `PROPOSE_TRADE`, `ACCEPT_TRADE`, or `DECLINE_TRADE` actions.
Generic trade actions remain forbidden. Proposal/acceptance requires confirmed high upside and at
least 92% confidence; outbound proposals are capped at one per week with one active at a time and a
seven-day counterparty cooldown. Every possible write is exact-state checked and verified afterward.

Every action card must contain:

```yaml
title:
action_type:
exact_action:
primary_reason:
supporting_evidence:
source_urls:
source_freshness:
confidence:
estimated_weekly_gain:
estimated_four_week_gain:
estimated_rest_of_season_gain:
downside:
reversibility:
waiver_priority_cost:
drop_protection_tier:
created_at:
recommended_complete_by:
escalation_at:
hard_lock_at:
verification_due_at:
dependencies:
conflicting_information:
sleeper_manual_steps:
sleeper_web_url:
```

Action cards must support:

* Confirm completed
* Snooze
* Dismiss
* Require re-evaluation
* Protect player
* Mark player as churn slot
* Add a manual note

A user confirmation does not complete the action. After confirmation, the system must retrieve fresh Sleeper state and verify the intended result wherever the public API exposes it.

Pending waiver claims and AutoSub configuration may not be visible through the public API. For those cases:

* Store the owner’s manual confirmation.
* Show the action as `user_confirmed_unverified`.
* Verify the outcome after the relevant waiver processing or game lock.
* Never falsely label an unobservable action as verified.

## 14. Monitoring profiles and schedules

The scheduler must be state-aware rather than relying on one global cron table.

Profiles:

```text
OFFSEASON
PRE_DRAFT
DRAFT_IMMINENT
DRAFTING
IN_SEASON_NORMAL
WAIVER_IMMINENT
WAIVER_PROCESSING
GAME_DAY
KICKOFF_IMMINENT
PLAYOFFS
SOURCE_DEGRADED
```

Use actual league status, NFL state, draft time, waiver schedule, and game kickoffs to activate profiles.

Add small randomized jitter to noncritical polling so multiple jobs do not fire simultaneously.

### 14.1 Sleeper polling

| Data                |                                          Normal cadence |                                                          Intensified cadence |
| ------------------- | ------------------------------------------------------: | ---------------------------------------------------------------------------: |
| NFL state           |     15 minutes preseason/offseason; 5 minutes in season |                                     1 minute during league-state transitions |
| League settings     |              15 minutes pre-draft; 60 minutes in season |          5 minutes in final 24 hours before draft or after a detected change |
| League users        |                                                 6 hours |                          15 minutes around ownership or commissioner changes |
| Rosters             |                                     5 minutes in season | 60 seconds within two hours of a relevant kickoff; 30 seconds around waivers |
| Matchups/starters   | 30 minutes Tuesday–Wednesday; 5 minutes Thursday–Monday |                 60 seconds within two hours of any rostered player’s kickoff |
| Transactions        |                                               5 minutes |                    30 seconds from T-15 to T+15 around expected waiver clear |
| Full player map     |              Once daily at approximately 03:15 Mountain |                                Manual refresh after material player-ID issue |
| Trending adds/drops |                                              30 minutes |            10 minutes on waiver day; 5 minutes for one hour after processing |
| Draft list/object   |                                  15 minutes until T-24h |                                                         1 minute within T-2h |
| Draft picks         |                             Disabled until draft starts |                                             Every 2–3 seconds while drafting |
| Playoff brackets    |                                       Daily late season |                                 15 minutes after relevant matchup completion |

### 14.2 Waiver processing burst

At the calculated waiver-clear time:

```text
T-48h: preliminary candidate discovery
T-24h: initial claim/free-agent plan
T-6h: final ranked claim tree
T-90m: unresolved important action alert
T-30m: recheck injuries, roster, availability and waiver priority
T-15m: freeze recommendations unless material news arrives
T-2m: final source-health check
T: expected processing
T through T+3m: poll transactions and rosters every 10 seconds
T+3m through T+15m: poll every 30 seconds
T+10m: raise delayed-processing alert if no expected processing evidence exists
T+15m: recompute available free agents and produce immediate add-now plan
T+30m: verify roster legality, waiver priority and all expected outcomes
```

Do not exceed upstream limits. Back off on rate-limit responses and use cached last-known-good data.

### 14.3 Draft monitoring

```text
T-7d:
  verify league settings
  build all 12 draft-slot simulations
  verify projections and player mappings

T-24h:
  refresh rankings, projections, injuries and draft metadata
  verify owner notifications
  generate initial queue

T-2h:
  refresh every critical source
  verify draft slot if assigned
  create slot-specific board
  test alert delivery

T-15m:
  freeze a safe fallback queue
  display source health
  identify unavailable or injured players

During draft:
  poll picks every 2–3 seconds
  recalculate after each pick
  alert at three picks away
  alert at one pick away
  send immediate on-clock alert
  display one recommended pick and two fallbacks
```

Do not submit a draft selection. The user performs the final action in Sleeper.

### 14.4 Projection, news and injury monitoring

FantasyPros or equivalent:

```text
Normal in-season:
  projections 4 times daily
  rankings 4 times daily
  injuries every 60 minutes
  news every 30 minutes

Game day:
  projections every 60 minutes
  injuries every 15 minutes
  news every 15 minutes

Within 3 hours of a relevant kickoff:
  projections every 15 minutes when provider quota allows
  injuries every 5 minutes
  news every 5 minutes
```

NFL reporting windows:

```text
From 3:45 p.m. to 6:00 p.m. Eastern on an expected report day:
  refresh structured injuries every 15 minutes

Once a final game-status report is due but not observed:
  refresh every 5 minutes until received or declared stale
```

Inactives window:

```text
T-4h: preliminary injury and replacement check
T-2h: starter-risk check
T-105m: begin inactive monitoring
T-90m: expected official inactive information
T-75m: raise source-stale alert if no updated information was observed
T-45m: final lineup and AutoSub check
T-15m: final hard-lock warning
```

### 14.5 Weather monitoring

Only intensify weather monitoring for outdoor or potentially open-roof games.

```text
T-7d to T-72h: every 12 hours
T-72h to T-24h: every 6 hours
T-24h to T-6h: every 2 hours
T-6h to T-2h: every 30 minutes
T-2h to T-90m: every 15 minutes
T-90m to kickoff: every 10 minutes
```

Track:

* Sustained wind
* Wind gusts
* Precipitation probability
* Precipitation intensity
* Temperature
* Snow or freezing precipitation
* Severe alerts
* Roof status
* Forecast disagreement between providers

Do not meaningfully change rankings for ordinary weather. Apply an adjustment only when defined thresholds and model evidence justify it.

### 14.6 nflverse refresh

```text
Schedules: daily, plus on season-state change
Rosters: daily
Injuries/practice data: daily as a secondary source
Player/team stats: six hours after completed games and next morning
Snap counts: periodically until new data arrives, then stop
Weekly consolidated usage model: Tuesday morning
```

## 15. Weekly readiness cadence

Generate actions dynamically from actual deadlines, but provide this normal operating rhythm.

### Monday

* Review Sunday results and injuries.
* Detect role changes from usage.
* Run two-week bye and depth forecast.
* Identify likely waiver candidates.
* Do not finalize the week until the Monday game is complete.

### Tuesday morning

* Close the previous fantasy week.
* Refresh usage, projections and rest-of-season values.
* Build initial waiver claim tree.
* Identify the current roster’s protected, review, and churn players.
* Flag any Wednesday IR or roster-limit problem.

### Relative to waiver clear

Use the T-48h through T+30m cadence defined above rather than relying only on weekday names.

### Thursday morning

* Set a complete provisional lineup.
* Put early-game players in dedicated RB or WR slots when possible.
* Preserve later players in FLEX.
* Recommend AutoSubs for questionable or high-risk starters.
* Resolve any player involved in an early game before that game’s lock.

### Friday

* Recalculate after official game-status reports.
* Produce the primary weekend lineup.
* Check whether questionable players have viable late-game replacements.
* Verify that every starting slot contains an eligible player.

### Saturday

* Recheck injuries, transactions, weather and unusual Saturday games.
* Confirm Sunday morning contingency plan.

### Each game window

* Execute T-4h, T-2h, T-105m, T-45m and T-15m checks.
* Recompute only affected recommendations.
* Escalate unresolved starter risks.

### Forward planning

Every day, calculate:

* Next seven days of lineup obligations
* Next fourteen days of bye conflicts
* IR activation risks
* Bench shortages
* Upcoming early international or unusual kickoff times
* Future defense and kicker streaming needs

## 16. Notification policy

Implement at least:

* Generic webhook notifications
* Email
* In-app/PWA notifications

Design the webhook adapter so it can target Home Assistant, ntfy, Pushover, Slack, or another notification relay without changing domain logic.

### Priority levels

#### P0: immediate intervention

Examples:

* The team is on the clock.
* A starter is officially inactive and kickoff is within two hours.
* The roster is illegal or over a limit and transactions are blocked.
* A critical waiver action is due within 90 minutes.
* A required source is stale during a waiver or kickoff window.
* A confirmed action failed verification.

Send immediately. Repeat at sensible escalation points until acknowledged or expired.

#### P1: action required soon

Examples:

* Questionable starter lacks an AutoSub or viable replacement.
* Waiver plan is awaiting confirmation.
* IR eligibility changed.
* A bye-week shortage exists in the next two weeks.
* League settings changed.
* A protected-player drop is being considered.

#### P2: daily actionable digest

Send one digest at 6:00 a.m. America/Denver by default. Include only meaningful information.

#### P3: informational

Examples:

* Projection changes below the action threshold
* Source recovered
* Trending player without roster relevance

Keep these in the dashboard unless the user opts in.

Do not send “nothing changed” notifications.

## 17. Source freshness and fail-closed behavior

Define source-specific freshness thresholds.

Suggested defaults:

```yaml
sleeper_league_settings:
  normal_stale_after: 2h
  critical_window_stale_after: 15m

sleeper_rosters:
  normal_stale_after: 15m
  kickoff_window_stale_after: 3m

sleeper_transactions:
  normal_stale_after: 15m
  waiver_window_stale_after: 2m

sleeper_draft_picks:
  drafting_stale_after: 10s

injury_data:
  normal_stale_after: 2h
  kickoff_window_stale_after: 15m

projections:
  normal_stale_after: 8h
  game_day_stale_after: 2h

weather:
  normal_stale_after: 6h
  kickoff_window_stale_after: 30m
```

Critical rules:

* Never recommend a destructive drop when critical sources are stale.
* Never call a player active merely because the latest status is missing.
* Never replace a last-known-good snapshot with an empty response.
* Display the age of every input on every action card.
* When sources disagree, show the disagreement and lower confidence.
* Require an official or high-confidence structured status before treating a player as inactive.
* LLM-generated text cannot override deterministic freshness or safety rules.

## 18. Fantasy scoring engine

Implement the league scoring rules as a versioned deterministic module.

The engine must support:

* Player projected stat-line scoring
* Actual scoring reconciliation
* Quarterback rushing value
* Half-PPR reception scoring
* Kicker distance buckets and misses
* Defense points allowed
* Sacks, interceptions, forced fumbles, recoveries, safeties and blocked kicks
* Team and player special-teams scoring
* Fumbles lost
* Stacked scoring events when multiple categories apply

Add unit tests for every scoring category and combinations such as:

* Strip sack recovered by the defense
* Defensive interception return touchdown
* Individual kick-return touchdown plus team D/ST return touchdown
* Quarterback rushing touchdown versus passing touchdown
* Missed 50-yard field goal
* Fumble recovery touchdown
* Every points-allowed tier

The expected scoring object must come from the live league snapshot after bootstrap. The hard-coded values in this prompt are assertions and test expectations, not a substitute for live configuration.

## 19. Projection ensemble

Build a deterministic projection pipeline.

For each player, retain:

```yaml
weekly_median:
weekly_floor:
weekly_ceiling:
rest_of_season:
projected_stat_line:
provider_count:
provider_dispersion:
confidence:
last_updated:
```

When only one projection provider is available:

* Use it as the baseline.
* Report single-source confidence.
* Do not invent an ensemble.

Where full stat lines are available, score them with the custom league scoring engine.

Incorporate role evidence such as:

* Snap share
* Route participation
* Target share
* Carries
* Goal-line carries
* Red-zone targets
* Team pass rate
* Team pace
* Depth-chart movement
* Injuries to adjacent players

Do not allow one anomalous fantasy score to outweigh a clearly weak role.

An optional LLM may explain the recommendation, but it must not calculate eligibility, deadlines, waiver priority, scoring, or protected-drop status.

## 20. Lineup optimizer

Solve the complete legal lineup globally rather than filling each position independently.

Required slots:

```text
QB
RB
RB
WR
WR
WR
TE
FLEX
FLEX
K
DEF
```

The optimizer must account for:

* Position eligibility
* Already locked players
* Game start times
* Injured reserve
* Official inactive status
* Projection median
* Floor and ceiling
* Source freshness
* Available substitutes
* Two FLEX positions
* Opponent matchup state
* AutoSub compatibility

Primary objective:

```text
maximize expected fantasy points
```

Secondary conservative adjustments:

* When clearly favored, modestly prefer reliable volume and floor.
* When clearly behind, modestly favor ceiling.
* Do not override a material median-projection gap for narrative reasons.

Flexibility rule:

* Put early-game RBs and WRs in dedicated position slots where possible.
* Place the latest-playing eligible players in FLEX.
* Preserve maximum substitution flexibility.

Target metric:

```text
zero avoidable inactive starters
```

## 21. Sleeper AutoSubs

AutoSub recommendations are essential because the owner cannot reliably monitor every weekend kickoff.

Reference:

```text
https://support.sleeper.com/en/articles/9731991-how-does-player-autosubs-work
```

Sleeper AutoSubs must be configured manually in the Sleeper mobile app unless Sleeper changes its supported interfaces. AutoSubs require eligible starter/substitute pairings and lock when either player’s game begins.

For every questionable or game-time-decision starter:

1. Identify eligible bench replacements.
2. Rank substitutes by projection and kickoff flexibility.
3. Verify that neither relevant game has started.
4. Create a `SET_AUTOSUB` action.
5. Set `recommended_complete_by` to two hours before the earlier kickoff.
6. Set `hard_lock_at` to the earlier kickoff minus fifteen minutes.
7. Require the user to confirm manual configuration.
8. Revisit the recommendation after official inactive information.

Do not assume the public API exposes configured AutoSub relationships. Track manual confirmation and verify only what is actually observable.

## 22. Waiver strategy engine

This league uses rolling priority rather than FAAB.

Model two different acquisition modes:

```text
CLAIM:
  consumes waiver priority after a successful claim

ADD_NOW:
  free-agent addition that does not consume rolling priority
```

A successful rolling waiver claim moves the team to the bottom of the priority order.

### Claim value

Use a model similar to:

```text
net_claim_value =
    addition_four_week_value
  + addition_rest_of_season_value
  + immediate_lineup_gain
  + upside_option_value
  + positional_scarcity_value
  - dropped_player_four_week_value
  - dropped_player_rest_of_season_value
  - depth_damage
  - uncertainty_penalty
  - waiver_priority_opportunity_cost
```

### Priority policy

```yaml
priority_1_to_3:
  claim_only_for:
    - clear recurring starter
    - major injury beneficiary
    - critical multiweek need
    - unusually scarce positional upgrade

priority_4_to_6:
  claim_for:
    - substantial roster upgrade
    - probable recurring flex
    - strong role-change candidate

priority_7_to_12:
  claim_more_aggressively_for:
    - credible breakout
    - important short-term starter
    - meaningful depth upgrade
```

Do not burn top-half priority on a kicker or defense by default. Prefer adding those positions after waivers clear.

### Claim tree

Produce ordered fallback claims:

```yaml
claims:
  - priority: 1
    add: Player A
    drop: Player X
    reason: clear recurring starter
  - priority: 2
    condition: Player A unavailable
    add: Player B
    drop: Player X
  - priority: 3
    condition: Players A and B unavailable
    add: Player C
    drop: Player Y

post_clear_free_agents:
  - Player D
  - Player E
```

Ensure that an earlier successful fallback cannot create an undesirable later result.

Pending claims are private and may not be visible through Sleeper’s public API. Represent league competition and claim success probability as uncertainty rather than fabricated knowledge.

## 23. Drop-protection engine

Every rostered player must be assigned one of:

```text
PROTECTED
REVIEW_REQUIRED
CHURN_ELIGIBLE
```

### Protected

Never recommend dropping automatically.

Typical reasons:

* Active weekly starter
* Meaningful value over waiver replacement
* Scarce-position advantage
* High rest-of-season value
* Injured player with meaningful expected return value
* Manual owner lock
* Insufficient evidence
* Important handcuff or contingent-value player
* Player needed for an upcoming bye

### Review required

A drop may be discussed but must be prominently escalated.

Examples:

* Startable bench player
* High-upside rookie
* Player whose role is changing
* Recent meaningful acquisition
* Player with provider disagreement
* Player whose injury prognosis is unsettled

### Churn eligible

Potential normal drop candidates:

* Expired one-week replacement
* Replaceable kicker or defense
* Low-ceiling redundant reserve
* Player clearly below league replacement level
* Player whose role has disappeared
* Explicitly designated churn slot

Hard safeguards:

* All drops require human confirmation in version one.
* Never recommend a drop from stale data.
* Never recommend dropping a starter to solve one bye.
* Never downgrade a player solely because of one bad game.
* Never downgrade a player solely because one provider lowered a projection.
* Require a larger expected improvement as uncertainty increases.
* Provide projected regret and replacement availability.
* Preserve a manual `never_drop` flag that no model may override.

## 24. Roster-construction and draft engine

This is a deep-starting, shallow-bench format:

* Seven RB/WR positions must be filled weekly.
* There are three mandatory WRs.
* There are two FLEX positions.
* There are only five bench spots.

Default drafted roster constructions:

```yaml
preferred:
  - QB: 1
    RB: 5
    WR: 7
    TE: 1
    K: 1
    DEF: 1

  - QB: 1
    RB: 6
    WR: 6
    TE: 1
    K: 1
    DEF: 1
```

Default rules:

* Do not draft a backup QB.
* Do not draft a backup TE.
* Do not draft a second kicker.
* Do not draft a second defense.
* Draft kicker and defense in rounds 15 and 16.
* Through round 14, target one QB, one TE, and twelve RB/WR players.
* By round 8, target six or seven RB/WR players, at least three WRs and at least two RBs.
* Prefer six or seven total WRs because of the three-WR and two-FLEX configuration.
* Treat these as strong defaults rather than absolute rules when a clear tier value appears.

Draft score:

```text
draft_score =
    custom_scoring_value_over_replacement
  + tier_cliff_urgency
  + positional_scarcity
  + probability_player_is_gone_by_next_pick
  + roster_fit
  + upside_option_value
  - injury_risk
  - role_uncertainty
  - reach_cost
```

Before the draft slot is assigned:

* Simulate draft positions 1 through 12.
* Save a separate strategy branch for each slot.
* Update simulations as ADP, injuries and projections change.

When `draft_order` becomes available:

* Detect the owner’s slot.
* Activate the corresponding branch.
* Continue recalculating from live picks.

During the draft, show:

1. Best recommendation
2. First fallback
3. Second fallback
4. Tier cliff
5. Probability each fallback survives to the next pick
6. Current roster construction
7. Positions that should not yet be drafted
8. Warning when a recommended player has new injury or status risk

## 25. Kicker and defense logic

### Kicker

Model:

* Accuracy
* Long-distance conversion history
* Coaching willingness to attempt long field goals
* Team implied scoring environment when available
* Stadium
* Weather
* Miss risk

The league awards additional points at 40, 50 and 60 yards and subtracts one point for a miss.

### Defense

Model:

* Opponent sack susceptibility
* Opponent quarterback turnover rate
* Offensive-line quality
* Backup-quarterback status
* Defensive pressure
* Takeaways
* Expected points allowed
* Weather
* Game script

The league separately rewards sacks, interceptions, fumble recoveries and forced fumbles. Do not rank defenses only by points allowed.

Treat both K and DEF as streaming positions by default. Preserve waiver priority by using post-clear free agency when reasonable.

## 26. IR and roster legality

Perform a mandatory roster-legality audit after:

* Every transaction
* Every waiver processing
* Every injury-status change
* Every IR-eligibility change
* Every lineup confirmation
* Wednesday morning
* Two hours before each relevant kickoff

Detect:

* Too many players
* Position-limit violation
* Ineligible IR player
* Empty required starter
* Locked illegal lineup
* Player on IR who must be activated or dropped
* Transaction blocked by roster state

A roster-legality problem is P0 when it can prevent another required move or lineup change.

## 27. User interface

Build a responsive dashboard with these pages.

### Today

Show:

* Current fantasy week
* Next deadline
* P0/P1 actions
* Current matchup
* Current lineup
* Injury risks
* Waiver position
* Source freshness
* Seven-day calendar

### Action Queue

Group by:

* Overdue
* Due today
* Due this week
* Waiting for verification
* Completed

### Draft Room

Show:

* Draft date and slot
* Live pick stream
* Current roster
* Top recommendation and fallbacks
* Tier board
* Position counts
* Draft construction warnings
* Source health

### Roster

Show:

* Starters, bench and IR
* Protected/review/churn classification
* Weekly and rest-of-season values
* Bye weeks
* Injury status
* Manual never-drop control

### Lineup

Show:

* Current lineup
* Optimized lineup
* Differences
* Projected gain
* Lock times
* AutoSub recommendations
* Flex-position reasoning

### Waivers

Show:

* Current rolling priority
* Expected clear time
* Claim tree
* Free-agent targets
* Add/drop pairs
* Priority cost
* Protected-drop warnings
* Verification after processing

### Players

Search and compare players with:

* Projections
* Rankings
* Usage
* Injury history
* Trending additions/drops
* Availability in this league

### League Rules

Show:

* Raw Sleeper settings
* Normalized settings
* Expected settings
* Discrepancies
* Unresolved mappings

### Source Health

Show:

* Provider status
* Last successful request
* Last changed payload
* Freshness
* Failure count
* Rate-limit state
* Last-known-good snapshot

### Audit

Show:

* Every recommendation
* Evidence snapshot
* User response
* Verification result
* Outcome
* Later retrospective evaluation

## 28. League readiness gate

The system cannot display “Ready” until all critical checks pass.

Checklist:

* [ ] Live league object fetched
* [ ] Correct owner and roster selected
* [ ] Twelve teams verified
* [ ] Roster positions match expected configuration
* [ ] Bench and IR sizes verified
* [ ] All scoring settings match or discrepancies are acknowledged
* [ ] Defense points-allowed 21–27 value resolved
* [ ] Rolling waivers verified
* [ ] No FAAB verified
* [ ] Waiver-clear day resolved
* [ ] Waiver-clear hour resolved
* [ ] Post-drop waiver duration resolved
* [ ] Custom daily waiver behavior resolved
* [ ] After-game waiver behavior resolved
* [ ] AutoSub count resolved
* [ ] Positional limits resolved
* [ ] Draft ID found
* [ ] Draft date/status loaded or explicitly pending
* [ ] Draft order loaded or explicitly pending
* [ ] NFL schedule loaded
* [ ] Player mapping current
* [ ] Projection source healthy
* [ ] Injury source healthy
* [ ] Notification test delivered
* [ ] No Sleeper write automation enabled

Produce:

```text
league_readiness.json
league_readiness.md
next_14_days.json
source_health.json
```

Also expose them through the API and dashboard.

## 29. Reliability and observability

Implement:

* Structured JSON logging
* Request IDs
* Correlation IDs across jobs, events and actions
* `/health/live`
* `/health/ready`
* `/metrics`
* Per-provider latency, error and freshness metrics
* Queue depth and oldest-event metrics
* Notification delivery metrics
* Database backup instructions
* Migration rollback instructions
* Graceful shutdown
* Worker lease recovery
* Retry and dead-letter tooling
* Replay by event ID or time range

Create alerts for:

* Critical source stale
* Queue backlog
* Dead-letter event
* Repeated provider failure
* Scheduler not heartbeating
* Unverified action after deadline
* Unexpected league-setting change

## 30. Testing requirements

### Unit tests

* Every scoring rule
* Every roster slot and eligibility combination
* Two FLEX optimization
* Early-player dedicated-slot placement
* Player ID mapping
* Waiver priority cost
* Rolling-priority movement after successful claims
* Waiver timing with overlapping rules
* Drop protection
* IR eligibility
* Bye conflict detection
* Time-zone and daylight-saving behavior
* Source freshness
* Action lifecycle
* Notification deduplication

### Integration tests

* Sleeper bootstrap from stored fixtures
* League-setting discrepancy report
* Roster and lineup change detection
* Waiver-clear burst and verification
* Draft-order detection
* Draft pick replay
* Provider outage and recovery
* Malformed response preserving last-known-good state
* Database worker lease recovery
* Event idempotency

### Safety tests

* No non-GET Sleeper request can be issued.
* No Sleeper credential field exists.
* No browser-automation package is installed for Sleeper.
* Protected players cannot become normal drop recommendations.
* Stale critical data blocks destructive recommendations.
* A user confirmation cannot mark an action verified without observable evidence.

### End-to-end fixture scenario

Create a synthetic league-week fixture containing:

* One questionable Sunday player
* One Sunday-night replacement
* One player ruled inactive at T-90
* One IR eligibility change
* One rolling waiver claim
* One failed waiver fallback
* One successful waiver claim
* One post-clear free-agent addition
* One roster-limit problem
* One weather-risk game
* One projection-provider outage

Verify that the correct events, actions, deadlines, notifications and audit records are produced.

## 31. Commands and developer experience

Implement these commands through `make`, `just`, or equivalent:

```text
make bootstrap
make dev
make web
make api
make scheduler
make worker
make sync
make sync-sleeper
make sync-projections
make league-readiness
make event-calendar
make replay-draft
make replay-events
make test
make lint
make format
make migrate
```

`docker compose up` should start:

* PostgreSQL
* API
* Scheduler
* Worker
* Frontend

Document local setup in `README.md`.

## 32. Implementation phases

Keep the repository runnable after every phase.

### Phase 1: foundation

Implement:

* Repository structure
* Backend service
* Database
* Migrations
* Configuration
* Health endpoints
* Structured logging
* Docker Compose
* CI
* Sleeper network safety guard

### Phase 2: Sleeper digital twin

Implement:

* Sleeper client
* Raw snapshots
* Normalized league settings
* Users and roster selection
* Rosters
* NFL state
* Draft metadata
* Player map
* Transactions
* Matchups
* League readiness report

### Phase 3: scheduler and durable event queue

Implement:

* Scheduler profiles
* Postgres event queue
* Outbox
* Worker
* Retries
* Dead letter
* Domain change events
* Source health

### Phase 4: dashboard and action queue

Implement:

* Today page
* League Rules
* Source Health
* Action Queue
* Notifications
* Manual confirmation
* Verification flow

### Phase 5: external data

Implement:

* FantasyPros adapter
* nflverse adapter
* Weather adapters
* Player ID mapping
* Injury and projection snapshots

### Phase 6: fantasy engines

Implement:

* Custom scoring
* Projection scoring
* Lineup optimizer
* Bye and IR planner
* Drop protection
* Waiver planner
* Kicker and defense streaming

### Phase 7: draft system

Implement:

* Twelve-slot simulations
* Dynamic tiers
* Live draft monitoring
* Pick recommendations
* Fallbacks
* Draft alerts
* Draft replay

### Phase 8: hardening

Implement:

* End-to-end scenarios
* Failure injection
* Source degradation behavior
* Backup/runbook
* Metrics
* Audit reports
* Retrospective decision evaluation

## 33. Initial deliverable

Do not merely return prose.

For the first implementation pass, complete Phases 1 through 3 and enough of Phase 4 to display the live league-readiness report.

The result must:

1. Start through Docker Compose.
2. Fetch the public Sleeper league data for league `1395499060898586624`.
3. Store immutable raw snapshots.
4. Normalize league, scoring, roster, draft and waiver settings.
5. Resolve or clearly identify unresolved waiver timing fields.
6. Allow selection of the owner’s roster.
7. Produce `league_readiness.json` and `league_readiness.md`.
8. Produce the next fourteen days of scheduled jobs and expected events.
9. Display source health.
10. Run a scheduler and durable worker.
11. Generate an action when a critical league setting is unresolved.
12. Include complete tests for the implemented behavior.
13. Document exactly how to run and inspect the system.

At the end of the implementation pass, provide:

```text
1. Files created or changed
2. Commands to run
3. Tests executed and results
4. Live league settings successfully resolved
5. Remaining unresolved settings
6. Next implementation phase
7. Any assumptions made
```

Do not report a setting as resolved unless it was actually retrieved and interpreted from the live league data.

## 34. Product principles

Use these principles to resolve implementation ambiguity:

1. Fresh, deterministic data beats narrative.
2. League-specific scoring beats generic rankings.
3. Role and opportunity beat one-week fantasy points.
4. Reversible actions need fewer safeguards than irreversible actions.
5. Preserving a strong player is more important than acquiring a marginal one.
6. Deadlines must be anticipated rather than merely detected.
7. Every important action must have a recommended completion time, escalation time and hard lock.
8. Every completed action must be verified.
9. No source should silently fail.
10. No LLM should control scoring, eligibility, timing, protection, or execution.
11. No unauthorized Sleeper automation.
12. When uncertain, fail closed and explain the missing information.
