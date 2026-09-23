# Fantasy Operations Manager

A self-hosted Sleeper fantasy-football operations monitor for the `Jim.ai` team in league `1395499060898586624`. The system builds an immutable digital twin from documented public GET endpoints, evaluates draft and league-readiness obligations, queues durable actions, sends post-only Slack alerts, and exposes a LAN dashboard.

The in-season objective is to maximize Jim.ai's probability of winning the league championship.
See [the in-season data and operating plan](docs/in-season-data-plan.md) for the live source stack,
adaptive polling strategy, and expert-manager roadmap.

The first production slice includes:

- Live Sleeper league, member, roster, draft, NFL-state, player, and draft-pick ingestion.
- Immutable content-hashed observations with last-known-good preservation.
- League normalization, discrepancy reporting, source health, and a fourteen-day event calendar.
- PostgreSQL-backed events, scheduled jobs, actions, execution commands, audit records, and row leasing.
- A programmatic grounding board using the open DynastyProcess/nflverse ranking feed.
- A whole-roster expert model that reasons over league scoring, Jim.ai's full build,
  every opponent build, positional runs, tiers, injuries, and next-turn dynamics.
- A two-second live monitor that allows only the expert model's validated selection
  to enter the browser execution queue; there is no deterministic auto-pick fallback.
- Manual, disabled, fake, dry-run, and Playwright browser execution adapters.
- A persistent-profile Chrome agent with signed-in Sleeper, league, and team preflight checks.
- A responsive Next.js dashboard and post-only Slack notification outbox.
- An opt-in, autonomous Jim.ai trash-talk feed grounded in verified league scores, with
  PG-13 content gates, anti-pile-on limits, original robot scorecards, and an immediate pause control.
- An in-season championship manager that combines the full league state, opponent,
  nflverse context, confirmed waiver rules, and authenticated Sleeper roster, free-agent,
  matchup projections, and kickoff-specific NWS weather in a constrained whole-roster
  Codex decision.
- A season-horizon waiver engine that classifies every target as a one-week rental,
  multi-week bridge, rest-of-season hold, or playoff stash and requires the exact drop,
  lost option value, roster role, and exit/re-evaluation plan.
- Dataset-level freshness/completeness gates, durable incidents and decision manifests, plus
  daily restore-verified backups and host/service readiness monitoring.
- Versioned nflverse usage features, transparent projection ensembles, and reproducible
  championship simulations running in shadow mode until their promotion gates pass.

No component sends direct HTTP writes to Sleeper, stores Sleeper credentials, or changes
commissioner settings. Authorized writes use exact semantic controls in the dedicated signed-in
browser, followed by independent state verification.

## Prerequisites

- macOS on AC power with at least 40 GB of free internal storage.
- Docker Desktop running.
- Google Chrome installed; Sleeper is signed in once through `make browser-login`.
- Codex CLI signed in with ChatGPT (`codex login status` should say
  `Logged in using ChatGPT`). This uses the Codex allowance included with the
  ChatGPT subscription; no OpenAI API key is required by the default setup.
- A Slack incoming webhook bound to one alert channel.

The current machine had less than the required free space during initial inspection. `make doctor` reports this without deleting or moving any user data.

## Start

```bash
cp .env.example .env
# Replace APP_SECRET_KEY and SHIM_SHARED_SECRET, then add SLACK_WEBHOOK_URL.
make doctor
make bootstrap
docker compose up
```

Open `http://localhost:3000`. The backend remains bound to `127.0.0.1:8000`.
The in-season manager is available at `http://localhost:3000/manager`.

For host-side tests and the read-only live probe, create a Python 3.12+ environment once:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e 'backend[dev]'
make probe
make test
```

## Useful commands

```bash
make sync-sleeper
make sync-rankings
make sync-in-season
make historical-backfill
make manager
make draft-room
make draft-monitor
make league-readiness
make event-calendar
make test
make lint
make backup
./scripts/verify-backup.sh "/Volumes/Extreme SSD/FantasyFootballManager/backups/daily/<file>.dump.gz"
make browser-login
make browser-agent
make codex-agent
```

Generated artifacts include `reports/readiness.json`, `reports/next-14-days.json`,
`reports/draft-room.json`, `reports/browser-lineup.json`,
`reports/browser-free-agents.json`, `reports/browser-matchup.json`,
`reports/weather-context.json`, `reports/manager-context.json`, and
`reports/in-season-plan.json`. `make historical-backfill` builds a compressed, resumable
nflverse archive for completed seasons, follows linked or explicitly configured prior Sleeper
league IDs, and checks past-week FantasyPros access without treating undated responses as causal
projection evidence.

## League trash talk

Trash talk is disabled by default. To enable automatic text posts, configure
`TRASH_TALK_SLACK_WEBHOOK_URL` for the entertainment channel, set
`TRASH_TALK_SLACK_CHANNEL_LABEL`, and then set `TRASH_TALK_ENABLED=true` after the Slack webhook
and local Codex worker are healthy. If no dedicated webhook is configured, the operational
`SLACK_WEBHOOK_URL` remains the backward-compatible fallback. The
scheduler checks league events every five minutes during game windows, requires score events to
remain stable, and enforces separate weekly, daily, cooldown, per-team, quiet-hour, and opt-out
limits. It never consumes the operational failure-alert budget.

Original PNG scorecards additionally require `SLACK_BOT_TOKEN` with only `files:write` and the
destination `SLACK_CHANNEL_ID`; invite that bot to the channel. If image delivery fails, the system
falls back once to the already-approved webhook text. The manager dashboard shows every post and
suppression and provides an immediate kill switch. Manager usernames are never used, unsafe team
names fall back to `Team <roster id>`, and injury references are limited to confirmed fantasy
impact or a lineup decision—never the player's pain or recovery.

The draft monitor automatically selects this league's active Sleeper draft. For a
standalone mock rehearsal, set `SLEEPER_DRAFT_ID` to the numeric ID from the open
Sleeper draft URL before running `make draft-monitor`. A standalone draft is accepted
only when its ID exactly matches that setting; unrelated drafts remain blocked. Clear
the setting after the rehearsal to restore league-only selection. Set
`DRAFT_EXPERT_ENABLED=true` to enable executable decisions. The default
`DRAFT_EXPERT_PROVIDER=codex_cli` runs `gpt-5.6-terra` with medium reasoning and current
web search through the local ChatGPT-authenticated Codex session. It deliberately
removes `OPENAI_API_KEY` from the child process and has no paid-API fallback. If the
subscription allowance is unavailable, exhausted, or does not return a decision within
`DRAFT_EXPERT_TIMEOUT_SECONDS` (85 seconds by default), the system revalidates the exact
live board and uses its roster-aware emergency engine. It still fails closed if the board
changed, no verified candidate exists, or the emergency fallback is disabled.
`DRAFT_EXPERT_PROVIDER=openai_api` remains available as an explicitly billed alternative.
When the team is on the clock, the Codex selection appears on the live dashboard
immediately. The durable browser command remains ineligible until the Sleeper clock
reaches `DRAFT_AUTO_SUBMIT_SECONDS_REMAINING` (15 seconds by default). A manual
Sleeper pick made before that release changes the board hash and invalidates the
pending command before it can click.
Attached league drafts use `DRAFT_AUTO_SUBMIT_SECONDS_REMAINING` (15 by default).
Explicit standalone mocks instead use `DRAFT_MOCK_AUTO_SUBMIT_DELAY_SECONDS` (0 by
default), so Playwright submits as soon as the expert decision is validated.
Static rankings and the public player catalog are cached locally for fast restarts;
live picks remain a lightweight public GET every two seconds.

## Execution safety

`EXECUTION_MODE` defaults to `dry_run` in the example configuration. The Playwright
adapter places semantic commands in the execution outbox and validates the browser
session. `DRAFT_PLAYER` is qualified for this league's snake draft and can submit
the whole-roster expert selection automatically when `DRAFT_AUTO_PICK_ENABLED=true`,
the expert model and local Codex worker are available, browser mode is active, and
the agent heartbeat is healthy. The model can select only from the programmatically verified
live allowlist. A missing, timed-out, malformed, or out-of-allowlist model decision may invoke
the explicitly enabled roster-aware emergency fallback; both paths are board-hash checked and
use the same live allowlist. A low-confidence selection or any changed/ambiguous board blocks
the click. Live browser mode remains action-by-action: every
action must pass the qualification checklist and be named in `BROWSER_LIVE_ACTIONS`.
Uncertain submissions are never retried.

Protected-drop approval tokens bind the exact action, evidence hashes, and decision-policy version. Trades and commissioner settings are rejected before they can enter the outbox.

The in-season manager's authenticated Sleeper adapter reads the visible team, free-agent, and
matchup projections. Every projection snapshot must prove the authoritative Sleeper season/week
and projection view; prior-week results and matchup sides that do not match the public current
opponent are rejected. A temporarily unavailable matchup panel removes its totals and win
probability without disabling roster-aware reasoning. Exact one-swap lineup writes are qualified
and enabled for this league.
The single-claim waiver selector, submission, and authenticated pending-claim verifier are
qualified, but new waiver submissions, fallback trees, pending-queue edits, immediate free-agent,
and IR writes remain disabled by independent gates. This boundary is shown on the manager dashboard. The
backend compiles a target lineup into ordered, single-swap
commands with exact before/after starter arrays, a 45-minute execution target, and a five-minute
pre-lock expiry. Queued actions expose their exact eligibility/expiry window on `/actions`, with
a live countdown and an owner cancel control that is available until the browser agent acquires
an active execution lease. Immediately before a live click, the browser agent must also obtain a
fresh authenticated backend decision that rebuilds the manager evidence gate from current reports,
required dataset health/completeness, and unresolved operational incidents; any missing, stale,
partial, or critical state blocks the write. Free-agent observations also carry Sleeper's current
`add` versus `waiver` control state. The browser periodically searches the exact manager shortlist,
validates name, position, and team, and attaches stable Sleeper player IDs before the expert can use
an acquisition state. The expert is blocked from choosing the wrong transaction type, and executable
claims compile into exact add/drop roster previews. Immediate multi-move plans are compiled as
chained roster states, but only the first move can enter the execution queue. Its completion must be
reconciled and the expert must build a fresh plan from the new roster before another move can become
eligible. In this home deployment, the acquisition,
fallback, pending-waiver, and IR families can run in explicit `trust_then_verify` mode while their
separate qualification evidence accumulates. Before a waiver write, Playwright stages the
actual claim dialog, validates the target, team/position, full canonical drop-player identity, and
enabled final control, then obtains a second fresh backend evidence decision. After submission it
verifies the exact private pending claim in the authenticated `My Waivers` panel. The public
transaction API verifies the outcome after processing because it does not expose private pending
claims; changed and conflicting states fail closed. Every claim must explain its immediate and
rest-of-season cases, classify its horizon, price the drop, and state when and how the roster spot
will be reconsidered. The acquisition scheduler supports one immediate add, a sequential plan gated
by `IN_SEASON_SEQUENTIAL_FREE_AGENT_ACTIONS_ENABLED`, or an ordered tree of up to eight exact waiver
claims. Every fallback re-runs protected-drop policy and carries
the whole approved claim group. It requires matching UI name/position/team, complete before/after rosters, a safe
pre-kickoff or pre-waiver-processing window, and authenticated verification. A newer recommendation
or a hold decision supersedes an untouched command. Pending claims are ingested from the private UI,
and an already pending exact claim is treated idempotently rather than submitted twice.
`IN_SEASON_ACQUISITION_ACTIONS_ENABLED=false` can still return add/drop and waiver paths to
preview-only mode. Separate switches gate sequential immediate moves, fallback trees, exact pending-queue cancellation
and reordering, and IR transitions. Pending edits compare the complete authenticated ordered queue
before and after a write. IR commands bind exact roster/reserve arrays, eligibility, lock state, and
public post-state. None of these paths retries an uncertain write.

Acquisition outcomes reconcile every minute against private pending state and public processed
transactions. Completion or failure creates a durable dashboard alert and immediately refreshes the
manager. A failed, blocked, or unverified browser command creates a detailed local alert and queues
a Slack message whose title begins `SLEEPER AI AGENT: `. The incoming webhook can post only to its
selected channel and grants no Slack read access. Browser screenshots remain in the private local
execution record; Slack receives detailed text and notes when screenshot evidence is available.

Trade intelligence ingests the exact authenticated offer inbox and every league roster. The expert
may propose, accept, or decline only exact offers. Proposals and acceptances require confirmed
evidence, `high` upside for the whole roster, and at least 92% decision confidence. The outbox allows
at most one outbound proposal per week, applies a seven-day counterparty cooldown, refuses to stack
an offer while another outbound offer is active, and uses a 24-hour expiration. Every action is
rebound to both complete rosters and the exact visible partner/assets at the final write boundary;
ambiguous writes are never retried.

The in-season expert refreshes hourly in normal conditions and around waivers,
every 10 minutes on game day, and every five minutes inside the two-hour kickoff window. Identical
decision state is content-hash cached; refreshed timestamps and small free-agent popularity-order
movements do not trigger another model call. NWS hourly forecasts and active alerts refresh every six hours
normally and every 15 minutes on game day or near kickoff; confirmed domes are skipped and
roof-uncertain games remain covered. When a limited FantasyPros key is configured, position-scoped
projection calls prioritize every mapped player on our roster and rotate opponent overflow across
refreshes. A rolling quota ledger preserves ten of the 50 daily calls. Results join to exact Sleeper
players through stable cross-provider IDs before Codex reasons over them.
Fresh observed usage—snaps, targets, carries, shares, and related opportunity—is factual evidence
available to Codex even while derived estimates remain unqualified. The projection ensemble and
championship layers are collected and displayed in shadow mode by default;
the daily walk-forward evaluator retains only the last snapshot before kickoff, scores completed
weeks by position, recommends inverse-error weights, and requires the blend to beat its strongest
paired source. `IN_SEASON_PLAN_V2_ENABLED=false` remains a manual master switch after every empirical
and structural gate passes. The manager dashboard exposes those gates, cross-source disagreement,
FantasyPros coverage/quota, and a CSRF-protected emergency refresh with a 15-minute cooldown.
The championship model fixes completed live scores, separates remaining players, models explicit
injury/replacement scenarios and shared NFL correlations, applies shrunk future-opponent effects,
decays distant projections, and assumes documented replacement-level streaming for ordinary roster
holes. Its feature evidence, structural/direct coverage, replacement reliance, and simulation
precision are visible on the dashboard; implementation alone does not grant decision authority.
Championship qualification runs daily (or with `make championship-backtest`) and publishes component
replay metrics plus completed-week Brier/log-loss/reliability scoring at
`GET /api/championship-backtest`. The manager also shows analysis-only add/drop counterfactuals ranked
by championship-equity delta; these scenarios never submit transactions. Standard Sleeper 2/4/6/8-team
fixed brackets are modeled, while unsupported playoff settings fail closed.

See [architecture](docs/architecture.md), [security model](docs/security.md), [browser adapter](docs/browser-adapter.md), [qualification checklist](docs/qualification.md), the [intelligence build/test plan](docs/intelligence-build-test-plan.md), and the [operations runbook](docs/runbook.md). The complete supplied requirements are preserved in [product spec](docs/product-spec.md).
