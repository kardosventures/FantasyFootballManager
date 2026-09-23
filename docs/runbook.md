# Operations runbook

## Prerequisites

1. Mount `Extreme SSD` before starting Docker Desktop. All durable project state lives beneath
   `/Volumes/Extreme SSD/FantasyFootballManager`; Docker Desktop's data folder, including the live
   PostgreSQL volume, is its `docker` subdirectory.
2. Keep enough internal free space for macOS and host tools, then start Docker Desktop and enable
   “Start Docker Desktop when you sign in.”
3. Copy `.env.example` to `.env`, generate both secrets, select a private `DASHBOARD_BIND_ADDRESS`, and configure the Slack incoming webhook.
4. Grant Accessibility and Screen Recording to Terminal/the built mac-agent in System Settings.
5. Keep the Mac on AC, logged in, awake, and online during action windows. The production
   Playwright agent runs Sleeper headlessly, so no visible browser window is required.

Run `make doctor`. Every item must pass before production setup.

## Start and initialize

```sh
make bootstrap
make draft-room
make dev
```

Open `http://<private-mac-address>:3000`. The API remains at loopback only. Run `make install-launchd` only after the stack is healthy; it installs the Playwright browser agent under `~/Library/LaunchAgents`. The Docker services use restart policies and return when Docker Desktop starts.

## Backups

The `backup` Docker service writes one compressed custom-format PostgreSQL dump per day beneath
`/Volumes/Extreme SSD/FantasyFootballManager/backups`. It restores every dump into an isolated temporary
database before publishing it, then writes a checksum and verification manifest. Daily retention is
14 days; Sunday copies retain the eight newest weeklies. `make backup` remains available for an
on-demand backup.

Independently restore-test an on-demand or scheduled backup with:

```sh
./scripts/verify-backup.sh "/Volumes/Extreme SSD/FantasyFootballManager/backups/daily/<file>.dump.gz"
```

Restore is deliberately guarded:

```sh
CONFIRM_RESTORE=restore-fantasy ./scripts/restore.sh "/Volumes/Extreme SSD/FantasyFootballManager/backups/daily/<file>.dump.gz"
```

After restoration, rerun bootstrap and compare readiness, source hashes, and action state before re-enabling the mac-agent.

## Fresh-Mac Docker rehydration

The SSD retains Docker Desktop's `Docker.raw`, but Docker Desktop's pointer to that data folder is a
per-Mac setting. The recovery directory retains a copy of `settings-store.json`. After installing
Docker Desktop on a fresh Mac, keep Docker stopped and run:

```sh
./scripts/rehydrate-docker.sh --apply
```

The script refuses to proceed unless the SSD, saved setting, Docker application, and existing
`Docker.raw` are present. It backs up any fresh settings file, changes only `DataFolder`, starts
Docker Desktop, and waits for the daemon. Do not create or reset Docker data on the internal disk.
If the retained Docker disk cannot be opened by a future Docker version, create a clean data disk
and restore the verified PostgreSQL dump from `backups`.

The canonical SSD layout is:

```text
FantasyFootballManager/
├── source/    Git checkout
├── docker/    Docker Desktop data disk
├── backups/   verified PostgreSQL dumps
├── recovery/  encrypted recovery image and offline Git bundle
└── runtime/   mounted encrypted live runtime
```

The `runtime` mount holds `.env`, reports, browser authentication state, and Codex decision files.
It must be mounted before Docker Desktop or the host LaunchAgents start.

Mount it interactively with:

```sh
make mount-runtime
```

## Operational health

`make install-launchd` installs the browser worker, Codex worker, one-minute Codex heartbeat
watchdog, and a 15-minute host-health
probe. The probe checks internal disk headroom and all seven Docker services. The API separately
checks backup freshness through a read-only external-drive mount. Inspect `/sources`,
`/api/host-health`, `/api/source-dataset-health`, and `/api/operational-incidents` before any
high-impact window. Stale or incomplete critical evidence blocks reasoning or lineup execution.

Keep at least 40 GB free on the internal data volume. A warning starts below 50 GB and becomes a
critical health failure below 40 GB.

## Activation ladder

1. `disabled`: observe only.
2. `manual`: recommendations and owner-executed changes.
3. `dry_run`: semantic commands pass macOS preflight but never click Sleeper.
4. `fake`: local fake UI/verification tests.
5. `browser`: permits only action types separately named in `BROWSER_LIVE_ACTIONS` after qualification.

`DRAFT_PLAYER` was qualified for the configured league and owner slot in a user-created mock. Before the real draft, run `make browser-login`, verify the saved session, then set `EXECUTION_MODE=browser`, `BROWSER_LIVE_ACTIONS=DRAFT_PLAYER`, and `DRAFT_AUTO_PICK_ENABLED=true`. Keep other live actions disabled unless separately qualified.

`SET_LINEUP` is separately qualified and enabled for league `1395499060898586624`, roster 8.
The `/actions` dashboard shows when each click becomes eligible and when its safety window closes.
The owner can cancel a waiting command there; an active browser lease deliberately cannot be
cancelled because the UI write may already be in progress.

In-season writes activate per family, never through browser mode alone:

- Single waiver: acquisition switch plus `WAIVER_CLAIM`.
- Ordered fallbacks: also the multi-claim switch and its qualification report.
- Pending edits: pending-waiver switch plus the exact cancel/reorder action.
- Immediate free agent: acquisition switch plus `ADD_FREE_AGENT` and its report.
- IR: IR switch plus the exact move/remove action and its report.
- Trades: trade switch plus all three exact propose/accept/decline actions. Proposals and accepts
  still require confirmed high upside and 92% confidence; outbound volume is capped at one per week.

For this personal deployment, `IN_SEASON_TRANSACTION_TRUST_THEN_VERIFY=true` allows these families
to operate before every harmless qualification state naturally occurs. It does not relax exact
identity, fresh evidence, protected-drop, safe-window, final preflight, or post-write verification.

Readiness remains false when a report, UI version, league/roster binding, feature switch, or
allowlist entry is missing. Never copy qualification from one action family to another.

## Failure posture

- Empty, malformed, or implausibly partial sources are retained but rejected.
- Missing/disputed/stale ranking inputs lower confidence and block destructive changes.
- Slack delivery failure leaves a durable delivery record for retry.
- Dashboard notifications remain durable when the Slack webhook is not configured.
- Failed, blocked, and unverified browser writes queue a detailed message through
  `SLACK_WEBHOOK_URL`; every title begins `SLEEPER AI AGENT: `. The webhook can only post to its
  selected Slack channel and grants no read access. Screenshot evidence remains in the private
  local execution record. Run `make notification-test`, then
  `docker compose up -d --force-recreate api worker` so both services load the webhook. Historical
  pre-activation email records remain suppressed. The worker enforces `SLACK_DAILY_ALERT_LIMIT`.
- Acquisition success or failure triggers a fresh manager run; uncertain writes are reconciled,
  never retried.
- A Sleeper update, app logout, missing permissions, network loss, sleep, or ambiguous window blocks writes.
- An uncertain write is `unverified`; never resubmit until public state is reconciled.
