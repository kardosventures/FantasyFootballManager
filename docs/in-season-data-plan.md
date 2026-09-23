# In-season championship data plan

Status: read/decision foundation and exact lineup swaps live 2026-09-09; one exact waiver selector,
submission, and private verifier are qualified. Fallback claims, pending edits, immediate free-agent
transactions, and IR writes are implemented but independently disabled pending real qualification.

## Objective

Maximize Jim.ai's probability of winning the league championship. Weekly points are an input,
not the objective. Decisions must consider the entire roster, opponent rosters, replacement value,
waiver opportunity cost, injuries, byes, future schedules, playoff weeks, and uncertainty.

The programmatic layer owns facts, timestamps, player identity, eligibility, locks, candidate
allowlists, and safety validation. The expert model reasons over those facts and returns a structured,
multi-action plan. It may never invent an available player or bypass a lock or protected-drop rule.

## Recommended source stack

| Priority | Source | Purpose | Delivery | Production cadence |
|---|---|---|---|---|
| Required | [Sleeper public API](https://docs.sleeper.com/) | League settings, rosters, starters, matchups, transactions, waiver outcomes, player status, trending adds/drops | Adaptive polling | 5 minutes normally; 30 seconds around waiver and kickoff windows |
| Required/read-only | Authenticated Sleeper UI | Current roster, free-agent, opponent, team-total, and win-probability projections not exposed by the public API | Browser observation | Heartbeat cadence; evidence rejected when stale or partial |
| Recommended | [FantasyPros API](https://www.fantasypros.com/api-data/) | Targeted weekly projections for our roster, opponent, and rotating positions | Quota-aware polling | 3 hours normally; 2 hours on game day; hourly maximum near kickoff |
| Required/free | [nflverse data](https://nflreadr.nflverse.com/articles/nflverse_data_schedule.html) | Schedule, injuries, depth charts, snaps, player stats, advanced usage | Batch polling | Every 4 hours; refresh after games and known upstream releases |
| Required/free | [DynastyProcess player IDs](https://github.com/dynastyprocess/data) | Sleeper/FantasyPros/GSIS/ESPN/Sportradar ID crosswalk | Batch polling | Daily |
| Recommended/free | [National Weather Service API](https://www.weather.gov/documentation/services-web-api) | Wind, precipitation, temperature and alerts for outdoor games | Adaptive polling | 6 hours normally; 15 minutes near kickoff when weather matters |
| Contextual | Codex web research | Resolve disputed or late-breaking coach/news context | Event-triggered | Only when material ambiguity remains |
| Optional paid | [SportsDataIO](https://sportsdata.io/developers/workflow-guide/nfl) | Consolidated projections, injuries, inactives, news and depth charts | Polling | Their projections update every 15 minutes until kickoff |
| Optional paid | [Sportradar](https://developer.sportradar.com/football/docs/nfl-ig-push) | Enterprise live game events and statistics | REST + push | Only valuable if live-play modeling becomes necessary |
| Optional paid | [The Odds API](https://the-odds-api.com/liveapi/guides/v4/) | Consensus game totals, spreads and player-prop market signals | Polling | 6 hours normally; 30 minutes near kickoff |

Sleeper documents a read-only API with roster, matchup, transaction, player and trending endpoints,
and asks clients to remain below 1,000 requests per minute. The proposed adaptive polling remains
far below that threshold.

FantasyPros is the preferred independent projection feed, but the configured limited tier returns
only ten players per request. The implementation therefore spends its quota on position-scoped
weekly projections for our team and current opponent instead of duplicating news and injury data
already available from Sleeper and nflverse.

nflverse is supporting evidence, not the only late-news source. Its scheduled releases are excellent
for usage and trend modeling, but batch publication is not sufficient for a last-minute inactive.

## Feed versus polling decision

A true streaming feed is not required for the first production version. Nearly every actionable
fantasy decision locks before kickoff, so fresh pregame information matters more than play-by-play
latency. Adaptive polling provides the required reliability at much lower cost and complexity.

Use a paid push feed only if later evaluation shows that live scoring and the next kickoff wave need
sub-minute event data that Sleeper cannot supply. Even Sportradar states that its push service is an
add-on and must be paired with REST recovery after disconnects.

## Adaptive operating cadence

### Normal weekday

- Sleeper league state, rosters, transactions and matchups: every 5 minutes.
- Targeted FantasyPros projections: every 3 hours.
- nflverse schedule, injuries, depth charts and ID mapping: every 4 hours.
- Usage and snap data: after upstream postgame updates.
- Weather: every 6 hours for outdoor games only.

### Waiver window

- Recalculate the ordered claim tree whenever availability, injuries or competing transactions change.
- Poll Sleeper transactions and rosters every minute.
- Refresh projections from non-quota-constrained sources every 5–15 minutes near the submission
  cutoff; FantasyPros remains subject to its hourly floor and rolling quota gate.
- Revalidate every add/drop pair and the exact claim order immediately before browser submission.

### Kickoff window

- Begin focused monitoring two hours before each game.
- Refresh injury and inactive evidence every 5 minutes around the official inactive window.
- Poll Sleeper roster/matchup state every 30 seconds.
- Refresh material outdoor weather every 15 minutes.
- Execute qualified lineup changes with a multi-minute safety buffer, never at the final second.

## Decision context supplied to the expert

Each decision request must contain:

1. The full roster, lineup slots, locks, injuries, byes and kickoff times.
2. The current opponent, live score distribution, remaining players and win probability.
3. Every available add candidate and every legal drop candidate.
4. Weekly floor/median/ceiling projections and rest-of-season value from multiple signals.
5. Opportunity trends: snaps, routes, targets, carries, high-value touches and depth role.
6. All league rosters, transaction behavior, waiver order/budgets and likely opponent demand.
7. The next four weeks plus fantasy-playoff schedule and roster requirements.
8. Source timestamps, disagreement, missing evidence and confidence.
9. A precomputed baseline lineup/waiver plan and counterfactual roster paths.
10. The exact allowlisted actions and protected players.

Weekly human course corrections live in `reports/in-season-overrides.json`. A protected
player is removed from the model's drop allowlist and the constraint expires automatically
when the recorded season or week no longer matches the live Sleeper state.

The response must explain how the complete roster becomes more likely to win the championship,
not merely why one player has the highest point estimate.

## Current implementation state

`app.in_season_sources` now captures:

- Sleeper NFL state, league, users, every roster, current-week matchups, current-week transactions,
  and 24-hour trending adds/drops.
- nflverse schedule, injury reports, depth charts, player stats and snaps when published.
- DynastyProcess cross-provider player identifiers.
- NWS hourly forecasts and active alerts matched to each exposed game's exact kickoff hour.
  Confirmed domes are skipped, roof-uncertain venues stay eligible, alerts that expire before
  kickoff are excluded, and the stadium-ID coordinate crosswalk is cached locally.
- FantasyPros weekly projections when a key is configured. Four position-scoped requests per sync
  cover our mapped roster first and rotate opponent overflow across refreshes. A rolling 24-hour ledger stops
  scheduled use at 40 of 50 calls and preserves ten. Records join through DynastyProcess IDs or an
  exact name/position/team key; merely configuring a key does not count as fresh grounding.
- The authenticated Sleeper team page's 16 visible roster projections, captured by semantic
  player IDs and rejected unless the observation is fresh and sufficiently complete.
- The authenticated Players page's league-filtered free-agent projections and stat lines,
  conservatively joined to the Sleeper catalog. The browser additionally searches up to 32 exact
  manager targets every two minutes, validates display name, position, and team, and only then
  attaches the stable Sleeper player ID. Ambiguous or unmatched rows are excluded rather than
  guessed. Each accepted row also carries Sleeper's observed `add` or `waiver` control state.
- The authenticated Matchup page's 32 player projections, both team totals, and displayed
  win probabilities, with player IDs read directly from Sleeper asset URLs.

Every response is content-hashed into immutable source observations and updates both provider- and
dataset-level health. A deterministic evidence gate evaluates freshness, completeness, league-wide
projection coverage, and exact authenticated/public roster identity before reasoning and again before
lineup execution. Every decision is bound to its evidence hashes and component versions.
Summary reports are written to `reports/in-season-context.json`, `reports/nflverse-context.json`,
`reports/fantasypros-context.json`, `reports/in-season-sources.json`, and
`reports/weather-context.json`, `reports/stadium-locations.json`,
`reports/browser-lineup.json`, `reports/browser-free-agents.json`, and
`reports/browser-matchup.json`.

`app.in_season_expert` now joins the full 12-team league, current opponent, legal lineup,
kickoff locks, injuries, depth roles, schedule, preseason baselines, free-agent allowlist,
and the live roster, free-agent, and matchup projections. A ChatGPT-authenticated local Codex worker returns a
schema-constrained whole-roster plan. Programmatic validation rejects illegal slots,
duplicate players, locked-player changes, unavailable additions, invalid drops, and
malformed waiver order. The response includes a season-long waiver thesis and classifies each
candidate as a one-week rental, multi-week bridge, rest-of-season hold, or playoff stash. Every
claim must identify its roster role, exact drop cost, immediate case, season case, exit plan, and
re-evaluation week. A full roster cannot produce an executable claim without an exact drop, and a
one-week rental must be re-evaluated no later than the following week. Successful decisions are
cached by exact grounding hash.

For this league, the authenticated commissioner settings confirm rolling waiver priority,
Wednesday 1:00 a.m. America/Denver processing, and a two-day post-drop waiver period.
The manager reads the current rolling priority from live roster state, prices its opportunity cost,
and does not apply FAAB logic.

The manager dashboard is live at `/manager`. It displays the team assessment, exact
11-slot lineup, derived changes, season-long roster thesis, immediate and rest-of-season claim
cases, drop cost, exit plan, current research citations, alerts, evidence gaps, source quality,
and the explicit execution boundary. The fourteen-day calendar includes the weekly six-hour
waiver review window and confirmed processing time.

The lineup compiler reduces the exact target to Sleeper's one-swap-at-a-time semantics.
Every step carries full expected starter arrays before and after the click, refuses changed
state, detects an already-applied move idempotently, and verifies exact public-API slot order.
An eligible plan is targeted for 45 minutes before the earliest affected
kickoff and expires five minutes before that lock; newer plans supersede untouched commands.
The exact lineup path is qualified and enabled for this league. It still fails closed unless
the expert plan is ready, fresh authenticated evidence is complete, confidence meets the
threshold, and the safe pre-kickoff execution window is open.

The `/actions` outbox exposes the exact command status, attempt count, eligibility time, and
expiry time. It renders a live countdown and lets the owner cancel an approval request or waiting
command. Cancellation is durable and suppresses the identical grounded plan; it is refused once
the browser holds an active lease, avoiding a false promise that an in-progress write was stopped.

The expert and evidence cadence now accelerates automatically: 15 minutes around waivers,
10 minutes on game day, and five minutes inside two hours of kickoff for expert reasoning;
FantasyPros refreshes every three hours normally, every two hours on game day, and no faster than
hourly near kickoff. nflverse refreshes hourly in critical windows. NWS weather refreshes every 15 minutes on game day and near kickoff,
hourly in the playoffs, and every six hours otherwise. Unchanged evidence reuses the exact
grounding-hash model result.

The acquisition compiler independently requires the expert's `free_agent` or `waiver` action
to match Sleeper's observed row control. It emits exact canonical and displayed add/drop names and
IDs, transaction week, claim priority, contingency, and complete expected roster before and after
the transaction. These instructions are preview-only by default. A dormant scheduler can queue one
immediate free-agent action or an ordered tree of up to eight exact waiver claims, supersede an untouched stale plan or
retire it when the new decision is hold, use the appropriate kickoff or waiver-processing expiry,
and reapply protected-drop policy at the outbox boundary. The waiver browser contract stages and
checks the exact live dialog before the backend's final evidence gate. Its verifier requires an
exact claim in Sleeper's authenticated `My Waivers` panel while the claim is private and pending,
then uses the public transaction API to verify the result after processing. Existing exact claims
are ingested through the browser heartbeat and are idempotent; conflicting pending claims fail closed. The
read-only selector qualification is durable in `reports/waiver-ui-qualification.json`. A separate
`reports/waiver-submission-qualification.json` is required from a controlled, authenticated and exactly verified
write before execution can report ready. The feature flag and live browser allowlist entry remain disabled.

The expert must explicitly keep, reorder, cancel, replace, or defer every authenticated pending
claim state. Exact cancellations run bottom-up and reorders preserve the same claim set; browser and
backend independently compare the whole ordered queue at the final boundary. The IR planner removes
no-longer-eligible reserve players before filling open slots with unlocked IR/PUP/NFI/OUT players.
Each command binds immutable roster and reserve states. Acquisition outcomes reconcile every minute,
never retry uncertain writes, publish durable local alerts, and force a fresh manager decision.

Trade intelligence reads the authenticated exact offer inbox and every league roster. It identifies
exact targets and offers, explains championship and counterparty value, and states a cost ceiling,
trigger, risk, evidence status, upside tier, and confidence. Propose/accept actions require confirmed
high upside and at least 92% confidence. Outbound offers are limited to one per week, one active at a
time, a seven-day counterparty cooldown, and a 24-hour expiration. The browser and backend bind both
full rosters and exact assets immediately before clicking; proposal/decline state is verified in the
authenticated UI and accepted roster state through the public API. Uncertain writes are terminal.

The nflverse workload layer joins Sleeper identities to official player-week stats and snap counts,
uses only causal regular-season rows, and carries prior-season evidence into Week 1. It produces
versioned recent workload, share, trend, volatility, provenance, and explicit missing-field records.
The projection ensemble exposes every input and weight plus P20/P50/P80, disagreement, and confidence.
Its daily evaluator now selects only the last persisted snapshot before kickoff, waits for a fully
completed week, measures paired source and ensemble accuracy by position, and publishes calibrated
position weights plus an enforceable promotion gate. The dashboard shows the gate, coverage,
disagreement, rolling quota, and a protected emergency-refresh control.
The deterministic seeded league simulator models standings, byes, legal weekly lineups, seeding and
playoff paths and reports Monte Carlo uncertainty. It now fixes completed live points, simulates only
remaining player value, conditions availability on injury/practice evidence, preserves shared NFL
game/team covariance, adjusts future opponents from prior completed results, decays distant weekly
signals, and fills ordinary bye-week holes with explicit projected replacement levels. Fresh observed
usage is permitted to inform Codex because it is factual evidence. Derived projection ensembles and
championship simulations remain shadow-only until their empirical promotion gates pass and are not
permitted to drive clicks.

## Next implementation slices

1. Accumulate two completed pregame shadow weeks; the automated paired-source backtest will keep the
   ensemble locked unless it meets sample, position, MAE, and interval-coverage thresholds.
2. Accumulate completed-week championship snapshots; the automated historical/component and
   Brier/log/reliability report keeps each feature shadowed until its threshold passes.
3. Review and tune any component that the causal replay rejects, then extend the implemented
   common-random-number add/drop analysis across projection sources and uncertainty bands.
4. Run the documented replay, shadow and live promotion gates; only then set
   `IN_SEASON_PLAN_V2_ENABLED=true`.
5. Continue adversarial monitoring of the qualified exact lineup-swap path.
6. Qualify the ordered fallback path with two harmless real claims; never reuse single-claim proof.
7. Qualify immediate free-agent, pending cancellation/reordering, and both IR directions only when
   exact harmless live states exist. Until then their switches remain off.
8. Complete the post-only Slack webhook and external alert testing; durable dashboard alerts and the
   failure-message/attachment contract are already active.
