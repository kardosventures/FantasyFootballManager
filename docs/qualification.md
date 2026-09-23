# Browser action qualification

## DRAFT_PLAYER checklist

- [x] User-created Sleeper mock draft ID recorded: `1403103641488887808`
- [x] UI contract recorded as `sleeper-web-2026-09-08`
- [x] Live DOM inspection covers draft room, on-clock, player search, exact player row, draft button, and post-pick state
- [ ] Anchored screenshot fixtures cover every Accessibility fallback
- [x] Configured owner slot 7 replayed successfully
- [x] The selected recommendation is revalidated against a cache-busted latest pick stream
- [x] Duplicate/existing exact picks are recognized without another click
- [x] Network or ambiguous submit results never trigger an automatic click retry
- [x] Logged-out session, wrong league, wrong roster, wrong owner slot, stale pick stream, and ambiguous player row block before clicking
- [x] Public `draft/{draft_id}/picks` verification passed for A.J. Brown, player `5859`, at mock pick `27`
- [ ] Slack receives blocked, failed, and unverified outcomes
- [x] Owner explicitly authorized autonomous draft selection on 2026-09-08

Live activation record: **approved for league `1395499060898586624`, roster 8, owner slot 7, snake drafts only**. Runtime activation remains fail-closed until the dedicated Playwright profile is signed in and all three settings are enabled: `EXECUTION_MODE=browser`, `BROWSER_LIVE_ACTIONS=DRAFT_PLAYER`, and `DRAFT_AUTO_PICK_ENABLED=true`.

## SET_LINEUP checklist

- [x] UI contract recorded as `sleeper-web-2026-09-09.4`
- [x] Live DOM inspection resolved the exact current player and destination slot controls
- [x] Full expected starter order was checked before the write
- [x] The first player click was confirmed non-committing and the second exact slot click was treated as the write boundary
- [x] Same-personnel qualification moved Rico Dowdle from FLEX to RB and De'Von Achane from RB to FLEX
- [x] No player was added, dropped, started, or benched by the qualification
- [x] Sleeper's cache-busted public roster endpoint confirmed the exact target starter order
- [x] Changed state and already-applied state are handled without another click
- [x] Ambiguous verification never triggers an automatic click retry
- [x] Owner authorized lineup qualification and activation on 2026-09-09

Live activation record: **approved for league `1395499060898586624`, roster 8, exact one-swap lineup changes only**. Runtime activation requires `EXECUTION_MODE=browser`, `BROWSER_LIVE_ACTIONS` containing `SET_LINEUP`, `IN_SEASON_LINEUP_ACTIONS_ENABLED=true`, a ready expert plan above the configured confidence threshold, fresh authenticated lineup evidence, and an open safe execution window.

## Remaining in-season action qualification

The contracts and tests are implemented, but these real Sleeper write boundaries are deliberately
not inferred from another action. All switches remain off until a harmless exact state exists:

- [ ] Two-claim fallback: both claims appear once in approved order; conflict and partial success
  fail closed.
- [ ] Immediate free agent: exact add/drop dialog, public post-state, idempotency, and no ambiguous retry.
- [ ] Pending cancellation: exact full queue before/after, unique confirmation, changed-state refusal.
- [ ] Pending reorder: exact full queue before/after, unchanged set, changed-state refusal.
- [ ] Move to IR: exact identity/eligibility/unlocked state, reserve arrays, and public post-state.
- [ ] Remove from IR: exact eligibility transition, reserve arrays, and public post-state.
- [ ] Slack incoming webhook: blocked, failed, and outcome messages received externally.
- [x] Slack failure contract: required `SLEEPER AI AGENT: ` prefix and detailed evidence are
  covered by automated tests; screenshot evidence remains private and local.

## Trade action checklist

- [x] Read-only authenticated trade builder and exact partner/asset columns inspected.
- [x] Proposal is staged through exact Review Trade Offer summary and a bounded expiration.
- [x] `SEND TRADE` is treated as the proposal write boundary.
- [x] Backend and browser independently bind both full rosters and all player assets.
- [x] High-upside classification and 92% minimum confidence enforced for propose/accept.
- [x] One outbound proposal per week, seven-day counterparty cooldown, and no stacked active
  outbound offers enforced.
- [x] Proposal and decline verification use the authenticated exact-offer fingerprint; acceptance
  uses exact public post-roster state.
- [x] Any uncertain write is terminal and is not retried.
- [ ] A naturally occurring incoming offer has exercised Sleeper's accept/decline write controls.
- [ ] Slack webhook is bound to the intended channel and a real failure-shaped message is received.

Runtime posture: explicit trust-then-verify is allowed for this personal league. The first UI drift
or verification ambiguity stops the action, persists the details and screenshot, and alerts the
owner; it never causes an automatic second click.

Sequential immediate moves use a separate runtime switch. Even when enabled, only one exact command
exists at a time; public completion, roster refresh, and a newly compiled expert plan are mandatory
before the next move. Deferred recommendations have no execution lease and cannot run after a failure.

Durable dashboard notifications and minute-by-minute acquisition reconciliation require no external
write qualification and become active with deployment.
