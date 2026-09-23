# Playwright browser write adapter

The write path runs outside Docker in the logged-in macOS session. It launches a normal installed Google Chrome instance with a dedicated persistent profile and a localhost-only debugging port, then attaches Playwright after login. This lets CAPTCHA/login run before Playwright attaches. The agent leases semantic commands from the manager API and navigates only to the configured `https://sleeper.com/leagues/<league-id>` origin and league.

The same authenticated session also provides read-only lineup, free-agent, matchup, and trade-inbox
observations. Dedicated league Team, Players, and Matchup tabs publish visible projections,
team totals, and win probabilities in the signed heartbeat. Roster and matchup players use
Sleeper player IDs; free-agent rows are conservatively identity-joined by the backend and
ambiguous rows are excluded. The free-agent reader also records whether Sleeper exposes its
`add` or `waiver` control, without clicking it. The API rejects partial snapshots and the expert
rejects stale ones. These read capabilities do not enable a roster write.

Every projection observation is explicitly bound to Sleeper's authoritative `state/nfl` season
and `week`. The agent selects that exact week in the Team, Players, and Matchup controls and marks
the snapshot `weekly_projection`; the API and expert independently reject missing, prior-week, or
wrong-mode observations. This is intentionally based on `week`, not `display_week`, because Sleeper
can keep `display_week` on the completed scoring period while the next lineup week is active. The
matchup side is additionally checked against the public current opponent roster. If the opponent
has not populated a lineup and the matchup projection panel is unavailable, matchup totals and win
probability are omitted with a warning; roster-aware reasoning and lineup management continue from
the public opponent roster and player-level projections.

The agent always binds its primary page to the exact `/team` route and maintains separate
Players and Matchup pages. This avoids restart-dependent tab ordering from silently replacing
the roster observation with another valid league page.

## One-time setup

```sh
cd browser-agent
corepack pnpm install
cd ..
make browser-login
```

Sign in to Sleeper in the dedicated Chrome window and open the configured league. The helper detects the successful navigation, attaches only then to verify the account/team, and closes the setup window automatically. The profile is stored in `BROWSER_PROFILE_DIR`. Do not point Playwright at a normal Chrome profile; Chrome may lock it and it would unnecessarily expose unrelated browsing data.

`make install-launchd` copies only the browser-agent runtime, its local configuration, and (on first install) the isolated profile into `~/Library/Application Support/JimAiFantasy`. This avoids macOS background-process restrictions on `Documents` while leaving the project source in place.

## Modes

- `dry_run`: launches the browser, confirms the signed-in account/team and exact league, and leases commands without clicking a write control.
- `fake`: contract testing only; no Sleeper write.
- `browser`: allows only actions named in `BROWSER_LIVE_ACTIONS` after their checklist is completed in `docs/qualification.md`.
- `disabled` and `manual`: no browser write.

Example staged activation:

```env
EXECUTION_MODE=browser
BROWSER_LIVE_ACTIONS=DRAFT_PLAYER
DRAFT_AUTO_PICK_ENABLED=true
```

Never enable an action based only on a successful dry run. Qualify its player row, modal, confirmation, cancellation, lock-state, wrong-league, logout, ambiguous-state, and public-API verification paths first.

## Safety behavior

- The agent rejects another league, roster, expired command, unknown action, missing idempotency key, generic trade instructions, and any commissioner-setting parameter.
- A draft command is queued only while the authenticated agent heartbeat is fresh and reports `DRAFT_PLAYER` capability.
- Immediately before clicking, the agent re-fetches the draft and pick stream, verifies the league, owner, roster, snake slot, expected next pick, player availability, and canonical pick-stream hash.
- Player IDs are used for public-API verification; human-readable player names are required for UI targeting.
- The browser driver uses semantic player-row and dialog locators, never raw screen coordinates.
- After a submission, the agent checks the documented public roster endpoint or, for private pending
  waivers, the authenticated exact queue. A missing or ambiguous result becomes `unverified` and is
  never automatically resubmitted.
- Pending cancellations and reorders require an exact full queue match in the browser and freshly
  rebuilt backend context before writing, then require the exact target order afterward.
- IR moves require immutable roster/reserve arrays, eligible injury state, an unlocked player, and
  exact public roster verification.
- Trade proposals and responses require exact full rosters, counterparty, player IDs, UI display
  names, positions, teams, and offer fingerprint. Proposal and acceptance contracts additionally
  require high-upside classification and at least 92% confidence. A proposal stops at the observed
  `SEND TRADE` boundary until the backend rebuilds all evidence; accepts and declines use the exact
  active offer. Proposal/decline outcomes are verified in the authenticated trade UI, while accepted
  roster state is verified through the public roster endpoint.
- Protected drops continue to require the backend's signed, single-use approval before a command can be leased.

## Current qualification status

On 2026-09-08, `DRAFT_PLAYER` was rehearsed from owner slot 7 in user-created mock draft `1403103641488887808`. The roster-aware engine chose A.J. Brown at pick 27; the exact visible player row and its draft button were clicked once, and Sleeper's public picks endpoint confirmed player `5859` at the expected owner slot and pick number. This qualification is intentionally limited to the configured league's snake-draft flow. Lineup changes were qualified separately on 2026-09-09. Acquisition and IR actions remain disabled by configuration even though waiver submission is now qualified.

On 2026-09-09, the configured league's team page was qualified for read-only semantic
observation: all 16 roster player IDs, slots, names, and weekly projections were captured.
During lineup qualification, selecting Jalen Coker and then Kenny Gainwell's FLEX slot
committed immediately—Sleeper did not wait for the visible `Ok` control. Sleeper's public
roster endpoint confirmed Coker entered the starting array and Gainwell left it. This was
the expert's recommended change, but it establishes that the second slot click is the write
boundary and that the operation is not an atomic multi-change form. The agent now enforces
one exact semantic swap per command, full expected starter arrays before and after the click,
idempotent already-applied detection, and exact public-API slot-order verification.
On 2026-09-09, a second controlled same-personnel test completed the lineup qualification:
Rico Dowdle moved from FLEX to RB and De'Von Achane moved from RB to FLEX. The visible page
showed the exact target slots, and a cache-busted Sleeper public roster read confirmed the full
ordered starter array. `SET_LINEUP` is now live-allowlisted for this league with one exact swap
per command, safe timing, changed-state refusal, idempotent already-applied detection, and no
retry after ambiguous writes. Add/drop, waiver, and IR actions remain disabled.

The same 2026-09-09 read qualification captured all 89 visible league free agents and all
32 players in the active matchup. The resulting evidence included Jim.ai's 135.99 projected
points and 67% displayed win probability versus Team curryfury's 112.24 and 33%. A subsequent
read showed 85 immediate-add controls and one waiver control; the set can change with locks and
transactions, so every observation is timestamped and revalidated.

On 2026-09-10, the production LaunchAgent was switched to Chrome's `--headless=new` mode and
restarted from a fully closed dedicated Chrome process. The saved authenticated profile remained
available, the exact league and roster checks passed, and consecutive heartbeats captured all 16
roster players, 72 currently rendered free agents, and 16 players on each matchup side. Chrome's
debug endpoint reported a `HeadlessChrome` user agent and no headed Chrome process remained. A
visible Sleeper window is therefore not required for normal operation; the Mac must still remain
logged in, awake, online, and running the LaunchAgent and Docker services.

The immediate free-agent modal was also inspected without submission. Sleeper renders defenses
with full UI labels rather than their canonical IDs: `LV` appears as `Las Vegas. Raiders`, while
the drop dialog renders `MIN` as `Minnesota Vikings`. Clicking the row's `add` control opens an
`Add Player` alert dialog and does not change the public roster. The dialog contains one semantic
roster row per possible drop; its final `Add Player` button is the write boundary. The agent now
requires exact UI name, position, and team for both sides, revalidates the `add` class immediately
before opening the dialog, checks the full public roster before the write, and verifies the exact
roster set afterward. Sequential free-agent release is independently gated and permits only one
verified move at a time.
On 2026-09-14, one
controlled waiver crossed the final boundary: Caleb Douglas for Tyjae Spears. Sleeper's
authenticated `My Waivers` panel confirmed the exact claim pending for Jim.ai, while the public API
correctly exposed no private pending transaction. The browser heartbeat now ingests that private
claim, an exact match is treated as already satisfied, a different pending claim blocks execution,
and the public API will reconcile the processed outcome.

Ordered fallback, pending-claim edit, immediate free-agent submission, IR, and trade actions now
have strict contracts and verifiers. This personal deployment can explicitly use trust-then-verify
while action-specific qualification is accumulated; any mismatch blocks before clicking or becomes
terminal `unverified` after a possible write. The current Caleb Douglas claim is never altered merely
to manufacture qualification evidence.

When the expert returns multiple immediate free-agent moves, the backend proves each exact roster
transition but queues only step one. A successful write must be visible in the public transaction and
roster state; the lifecycle reconciler then forces a fresh expert decision. Deferred previews are
never executable commands, so a failed, blocked, or ambiguous first move cannot release step two.

On 2026-09-15, the configured league's trade builder was inspected without submitting an offer.
The adapter identified the exact partner columns, selected assets by player identity and
position/team, validated the two-team Review Trade Offer summary, selected a bounded expiration,
and identified `SEND TRADE` as the final write control. Incoming accept/decline controls still depend
on the first naturally occurring exact offer and therefore remain trust-then-verify, with no retry
after an ambiguous click.

A non-committing Matt Gay test exposed two additional UI distinctions. Sleeper searches the
canonical full name (`Matt Gay`) but renders the result row as `M. Gay`, so the command now carries
separate search and exact-display names. The current acquisition container is the unique
`alertdialog`, while its possible drops are accessible roster links; the agent scopes the exact
drop name, position, and team to that dialog. The test selected Tyler Loop, stopped at the enabled
`Add Player` button, and closed the tab without submitting.
