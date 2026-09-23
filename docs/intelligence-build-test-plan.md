# Championship intelligence build and test plan

Status: Releases 1–3 are deployed as operational gates plus shadow intelligence. The new intelligence
cannot affect executable decisions while `IN_SEASON_PLAN_V2_ENABLED=false`.

Promotion-control update on 2026-09-09:

- The daily ensemble evaluator reads persisted projection snapshots, rejects every observation at or
  after the player's kickoff, and waits for the full NFL week to complete.
- Paired MAE/RMSE/bias, interval coverage, position splits, and inverse-error position weights are
  generated automatically. Promotion requires two completed weeks, 40 player-week samples, five
  observations at each of QB/RB/WR/TE, 50–75% P20-P80 coverage, and a 1% MAE improvement over the
  strongest paired source.
- Projection and championship authority are separate fail-closed runtime gates under the manual v2
  master switch. The championship gate additionally requires 90% league coverage, at least 5,000
  simulations, at most 1% standard error, live remaining-player state, injury scenarios,
  correlations, and future-opponent effects.
- The manager dashboard exposes promotion evidence, disagreement, FantasyPros target coverage and
  rolling quota. A local CSRF-protected emergency refresh can use the ten-call reserve and has a
  15-minute cooldown.

Championship-model update on 2026-09-10:

- The current week now fixes completed starter points from Sleeper, classifies players as scheduled,
  in progress, final, or on bye from kickoff/final-score evidence, and simulates only remaining value.
- Status/practice-conditioned availability scenarios use the best roster or projected free-agent
  replacement. Healthy future attrition contributes uncertainty without an expensive per-player draw.
- Shared NFL offense, defense, game, passing-game, and backfield factors produce within-lineup and
  head-to-head covariance. Pairwise Gaussian reduction preserves that covariance while reducing the
  live 10,000-run benchmark from more than a minute to about ten seconds.
- Prior completed nflverse results produce league-scoring-specific opponent factors with an eight-game
  shrinkage prior and a 0.85–1.15 cap. Weekly projections decay toward position anchors as the horizon
  grows instead of being copied unchanged through Week 17.
- Explicit replacement-level starters cover rational bye-week streaming without claiming a missing
  roster is structurally invalid. The live league now has 100% legal team-week coverage, 94% direct
  projection coverage, and 5.9% modeled-replacement reliance.
- All five features are implemented but remain `shadow_unvalidated`. Promotion now requires replay
  qualification for every feature, at least 95% structural coverage, at least 90% direct projection
  coverage, and no more than 10% modeled-replacement reliance.

Championship-qualification update on 2026-09-10:

- A daily historical replay now scores availability with Brier/log loss and reliability buckets,
  opponent adjustments against a causal unadjusted baseline, modeled correlations against empirical
  residual correlations, projection decay against no decay, and replacement levels against historical
  weekly rank cutoffs.
- Persisted simulation snapshots now include current-week win probabilities. Completed weeks feed an
  automatic Brier/log/reliability gate; championship calibration remains pending until season outcomes
  exist, and the independent projection gate still requires two completed live shadow weeks.
- The manager screens position-diverse add/drop pairs whose acquisition state was observed in Sleeper
  and resimulates the three strongest against holding the roster with the same random seed. These are
  analysis-only counterfactuals and never submit a move.
- The playoff implementation now uses Sleeper's documented fixed 2/4/6/8-team winners-bracket paths.
  Unsupported field sizes and nonstandard median, round, seed, or playoff settings fail closed. Tests
  cover every field size from 2 through 12, exact six-team paths, bye counts, tiebreak declaration, and
  insufficient playoff windows.
- Component qualification is evidence-driven: the simulator only marks a feature `qualified` after its
  report threshold passes. Missing history remains visibly blocked instead of being inferred as success.

P0 hardening completed on 2026-09-09:

- Sleeper `fantasy_positions` eligibility is preserved through manager validation and championship
  lineup optimization, including players whose primary catalog position is IDP-only.
- The Playwright agent must obtain a fresh authenticated backend evidence decision after its public
  Sleeper roster preflight and before every live UI write.
- The evidence gate consumes required dataset-health rows, record completeness and unresolved
  operational incidents; missing or critical evidence fails closed.
- Backup health verifies the compressed stream, SHA-256 sidecar, byte count, backup identity,
  successful isolated-restore manifest and restored table count.
- Failure tests cover stale/missing/partial/mismatched evidence, critical incidents, changed roster
  state, corrupt backups and multi-position eligibility. A live restart drill recovered all seven
  services with database state intact.

Still required for full Release 1 qualification: configure and prove external notification delivery,
and retain periodic recovery/disk drills as operational evidence.

Release 2 implementation update on 2026-09-09:

- nflverse offense and kicker player-weeks are now rescored from raw events using this league's
  exact Sleeper settings, including half-PPR and distance-based kicking; the model no longer assumes
  standard scoring plus receptions.
- Unique Sleeper-catalog name/position fallback closes the DynastyProcess kicker-ID gap while still
  rejecting ambiguous identities. Team defenses receive separate rolling histories; points allowed
  is explicitly labeled an estimate because opponent final score can contain points not charged to D/ST.
- A daily, reproducible walk-forward diagnostic now reports MAE, RMSE, bias, P20–P80 coverage and
  position splits. The first 2025 run evaluated 5,316 player-weeks across Weeks 3–18 with zero
  leakage violations.
- The fixed recency blend did not beat the strongest internal baseline (4.2424 versus 4.1330 MAE),
  so it was correctly rejected. This is diagnostic evidence, not a promoted model.
- Projection qualification remains blocked until the configured independent feed accumulates two
  completed pregame shadow weeks and passes the automated paired-source thresholds. Historical
  injury-status calibration remains a separate improvement.

## Release 1 — trustworthy operations

Built:

- Dataset-level health with freshness, completeness, failure counters, recovery, and durable incidents.
- Deterministic capability gates for reasoning and lineup execution, evaluated again immediately before a write.
- Immutable decision manifests binding state, evidence, versions, and results.
- Fifteen-minute host checks for disk and all services; API-side backup freshness verification.
- Daily atomic PostgreSQL backups that must pass checksum, decompression, isolated restore, and table-count verification before publication.
- Degraded scheduler cadence keeps critical sources at one- to five-minute polling instead of going quiet.

Promotion tests:

1. Inject stale, missing, partial, mismatched, and failed source states; confirm fail-closed behavior and a durable incident.
2. Restore a fresh production backup to an isolated database and compare migration/table state.
3. Kill and restart each service; confirm restart policy, health reporting, and source recovery.
4. Fill disk to warning/critical thresholds in a disposable test environment and verify alerts without deleting data.
5. Revalidate exact public/browser roster identity immediately before every lineup command.

Acceptance: no critical stale or mismatched state can reach Codex or the browser; backups have a recent
successful restore manifest; all incidents and recovery events are visible from the dashboard/API.

## Release 2 — decision-grade usage and projections

Built in shadow:

- Canonical Sleeper/GSIS/PFR identity joins with ambiguous fallback rejection.
- Causal regular-season player-week usage, snap, opportunity, trend and volatility features, including prior-year Week 1 grounding.
- Transparent projection ensemble with source values/weights, P20/P50/P80, disagreement, confidence, version and input hash.
- Idempotent feature/projection snapshots for rostered and available players.
- Explicit unavailable status for routes and red-zone opportunities instead of fabricated precision.

Required qualification:

1. Configure at least one independent projection/ROS provider in addition to Sleeper.
2. Walk-forward backtest every completed 2025–2026 week; fit weights without future leakage.
3. Report MAE, RMSE, bias, interval coverage and calibration by position, horizon and injury status.
4. Compare against Sleeper-only and simple consensus baselines; reject promotion unless the ensemble adds material calibrated value.
5. Replay identity changes, trades, team changes, missing IDs, duplicate names, stat corrections and late inactives.
6. Run at least two live shadow weeks and review every material recommendation delta.

Acceptance: no look-ahead leakage; stable identity coverage; calibrated intervals; documented source fallback;
measurable performance at least as good as the strongest available baseline.

## Release 3 — championship probability planning

Built in shadow:

- Seeded, reproducible Monte Carlo league simulation using complete fantasy schedules and legal optimized lineups.
- Standings, points tiebreak, playoff seeding, byes and bracket advancement.
- Championship, playoff and bye probabilities, seed distributions, Monte Carlo standard errors, and common-random-number counterfactual comparisons.
- Live fixed-score/remaining-player decomposition and horizon-widened uncertainty.
- Injury availability and replacement scenarios, cross-player/game correlations, prior-season
  opponent effects, multi-week signal decay, and explicit bye-week replacement modeling.
- Separate structural, direct-projection, inferred-projection, and replacement-reliance diagnostics.

Required qualification:

1. Accumulate enough completed live-week simulation outcomes for the Brier/log/reliability thresholds.
2. Complete the independent projection feed's two-week promotion gate.
3. Review any historical component that fails its automated threshold and recalibrate without leakage.
4. Extend counterfactual sensitivity across projection sources and plausible uncertainty bands.

Acceptance: deterministic replay; correct league rules; calibrated probabilities; stable counterfactual ordering;
no result presented as decision-grade when coverage or current-week state is incomplete.

## Authority gate

Keep collection, persistence, API and dashboard visibility enabled. Keep `IN_SEASON_PLAN_V2_ENABLED=false`
until all Release 2 and 3 acceptance criteria pass. Promotion is one explicit config change followed by a
fresh full-suite, replay, browser dry-run and live evidence-readiness check. Waiver execution remains a
separate later qualification track.
