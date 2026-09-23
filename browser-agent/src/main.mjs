import { setTimeout as delay } from "node:timers/promises";
import { AgentApi } from "./api.mjs";
import { ACTION_TYPES, loadConfig } from "./config.mjs";
import { executeCommand } from "./executor.mjs";
import {
  ensureLeaguePage,
  fetchCurrentNflPeriod,
  inspectFreeAgentSnapshot,
  inspectLineupSnapshot,
  inspectMatchupSnapshot,
  inspectPendingWaiverSnapshot,
  inspectSleeperSession,
  inspectTargetedFreeAgentSnapshot,
  inspectTradeSnapshot,
  launchSleeperSession,
  leaguePath,
  mergeFreeAgentSnapshots,
} from "./session.mjs";

const config = loadConfig();
const api = new AgentApi(config);
const { context, close, page } = await launchSleeperSession(config);
let playersPage = context
  .pages()
  .find(
    (candidate) =>
      candidate !== page && candidate.url().includes(`/leagues/${config.leagueId}/players`),
  );
let matchupPage = context
  .pages()
  .find(
    (candidate) =>
      candidate !== page && candidate.url().includes(`/leagues/${config.leagueId}/matchup`),
  );
let tradePage = context
  .pages()
  .find(
    (candidate) =>
      candidate !== page
      && candidate.url().includes(`/leagues/${config.leagueId}/team`),
  );
let targetedFreeAgents = null;
let targetedFreeAgentSignature = "";
let targetedFreeAgentRefreshAt = 0;
let playersPageMustReload = true;
const TARGETED_FREE_AGENT_REFRESH_MS = 2 * 60 * 1000;
let nflPeriod = null;
let nflPeriodRefreshAt = 0;
const NFL_PERIOD_REFRESH_MS = 60 * 1000;
let tradeSnapshot = null;
let tradeSnapshotRefreshAt = 0;
const TRADE_SNAPSHOT_REFRESH_MS = 60 * 1000;

async function currentTradeSnapshot() {
  if (tradeSnapshot && Date.now() < tradeSnapshotRefreshAt) return tradeSnapshot;
  if (!tradePage || tradePage.isClosed()) tradePage = await context.newPage();
  tradeSnapshot = await inspectTradeSnapshot(tradePage, config);
  tradeSnapshotRefreshAt = Date.now() + TRADE_SNAPSHOT_REFRESH_MS;
  return tradeSnapshot;
}

async function currentNflPeriod() {
  if (nflPeriod && Date.now() < nflPeriodRefreshAt) return nflPeriod;
  nflPeriod = await fetchCurrentNflPeriod(config);
  nflPeriodRefreshAt = Date.now() + NFL_PERIOD_REFRESH_MS;
  return nflPeriod;
}

async function freeAgentSnapshot(period) {
  if (!playersPage || playersPage.isClosed()) {
    playersPage = await context.newPage();
    playersPageMustReload = true;
  }
  const target = leaguePath(config, "/players");
  if (playersPage.url() !== target) {
    await playersPage.goto(target, { waitUntil: "domcontentloaded" });
  } else if (playersPageMustReload) {
    await playersPage.reload({ waitUntil: "domcontentloaded" });
  }
  playersPageMustReload = false;
  const base = await inspectFreeAgentSnapshot(playersPage, config, period);
  const targetReport = await api.freeAgentTargets().catch((error) => {
    console.error(`browser-agent targets: ${error.message}`);
    return { targets: [] };
  });
  const targets = Array.isArray(targetReport?.targets) ? targetReport.targets : [];
  const signature = JSON.stringify(targets.map((targetPlayer) => targetPlayer.player_id));
  const refreshDue = Date.now() >= targetedFreeAgentRefreshAt;
  if (targets.length && (refreshDue || signature !== targetedFreeAgentSignature)) {
    try {
      targetedFreeAgents = await inspectTargetedFreeAgentSnapshot(
        playersPage,
        config,
        targets,
        period,
      );
      targetedFreeAgentSignature = signature;
      targetedFreeAgentRefreshAt = Date.now() + TARGETED_FREE_AGENT_REFRESH_MS;
    } catch (error) {
      console.error(`browser-agent targeted free agents: ${error.message}`);
      targetedFreeAgentRefreshAt = Date.now() + 30_000;
    }
  } else if (!targets.length) {
    targetedFreeAgents = null;
    targetedFreeAgentSignature = "";
  }
  return mergeFreeAgentSnapshots(base, targetedFreeAgents);
}

async function matchupSnapshot(period) {
  if (!matchupPage || matchupPage.isClosed()) matchupPage = await context.newPage();
  const target = leaguePath(config, "/matchup");
  if (matchupPage.url() !== target) {
    await matchupPage.goto(target, { waitUntil: "domcontentloaded" });
  }
  return inspectMatchupSnapshot(matchupPage, config, period);
}

async function report(command, result) {
  await api.report(command.id, {
    agent_id: config.agentId,
    status: result.status,
    message: result.message,
    evidence: result.evidence,
  });
}

async function attachFailureScreenshot(actionPage, command, result) {
  if (!["blocked", "failed", "unverified"].includes(result.status)) return result;
  try {
    const screenshot = await actionPage.screenshot({ type: "jpeg", quality: 60, fullPage: false });
    return {
      ...result,
      evidence: {
        ...(result.evidence ?? {}),
        failure_screenshot: {
          filename: `${command.action_type.toLowerCase()}-${command.id}.jpg`,
          content_type: "image/jpeg",
          data_base64: screenshot.toString("base64"),
          captured_at: new Date().toISOString(),
          url: actionPage.url(),
        },
      },
    };
  } catch (error) {
    return {
      ...result,
      evidence: {
        ...(result.evidence ?? {}),
        screenshot_error: error.message,
      },
    };
  }
}

async function loop() {
  while (true) {
    try {
      await ensureLeaguePage(page, config);
      const session = await inspectSleeperSession(page, config);
      const period = session.sessionAvailable
        ? await currentNflPeriod().catch((error) => {
          console.error(`browser-agent NFL period: ${error.message}`);
          return null;
        })
        : null;
      const lineupSnapshot = session.sessionAvailable && period
        ? await inspectLineupSnapshot(page, config, period).catch(() => null)
        : null;
      const pendingWaiverSnapshot = session.sessionAvailable
        ? await inspectPendingWaiverSnapshot(page, config).catch(() => null)
        : null;
      const availablePlayers = session.sessionAvailable && period
        ? await freeAgentSnapshot(period).catch(() => null)
        : null;
      const liveMatchup = session.sessionAvailable && period
        ? await matchupSnapshot(period).catch(() => null)
        : null;
      const liveTrades = session.sessionAvailable
        ? await currentTradeSnapshot().catch((error) => {
          console.error(`browser-agent trades: ${error.message}`);
          tradeSnapshotRefreshAt = Date.now() + 15_000;
          return null;
        })
        : null;
      await api.heartbeat({
        agent_id: config.agentId,
        app_version: config.uiContractVersion,
        app_running: session.browserRunning,
        session_available: session.sessionAvailable,
        execution_mode: config.mode,
        capabilities: ACTION_TYPES.filter(
          (action) => config.mode !== "browser" || config.liveActions.has(action),
        ),
        details: {
          browser: config.browserChannel,
          headless: config.headless,
          correct_league: session.correctLeague,
          correct_origin: session.correctOrigin,
          current_url: session.url,
          live_actions: [...config.liveActions],
          observation_period: period,
          lineup_snapshot: lineupSnapshot,
          pending_waiver_snapshot: pendingWaiverSnapshot,
          free_agent_snapshot: availablePlayers,
          matchup_snapshot: liveMatchup,
          trade_snapshot: liveTrades,
        },
      });
      const command = await api.lease(config.agentId);
      if (command) {
        let actionPage = page;
        if (["ADD_FREE_AGENT", "WAIVER_CLAIM"].includes(command.action_type)) {
          if (!playersPage || playersPage.isClosed()) playersPage = await context.newPage();
          actionPage = playersPage;
        } else if (["PROPOSE_TRADE", "ACCEPT_TRADE", "DECLINE_TRADE"].includes(
          command.action_type,
        )) {
          if (!tradePage || tradePage.isClosed()) tradePage = await context.newPage();
          actionPage = tradePage;
        }
        await report(command, {
          status: "preflight",
          message: "Playwright agent started semantic command preflight",
          evidence: { ui_contract_version: config.uiContractVersion },
        });
        let result = await executeCommand(actionPage, config, command, {
          authenticatedSession: session,
          onStatus: (status, message, evidence) =>
            report(command, { status, message, evidence }),
          beforeWrite: (uiObservation) =>
            api.preflight(command.id, config.agentId, uiObservation),
        });
        result = await attachFailureScreenshot(actionPage, command, result);
        await report(command, result);
        if (
          result.status === "verified"
          && [
            "WAIVER_CLAIM",
            "ADD_FREE_AGENT",
            "CANCEL_WAIVER_CLAIM",
            "REORDER_WAIVER_CLAIMS",
          ].includes(command.action_type)
        ) {
          // A transaction can leave another Sleeper tab's local availability
          // cache stale. Reload the dedicated observation/execution tab before
          // leasing the next acquisition command.
          playersPageMustReload = true;
          targetedFreeAgentRefreshAt = 0;
        }
        // Start the next loop immediately so the post-command navigation and
        // heartbeat happen before the backend can mistake a busy agent for a
        // stale one. The following empty lease still observes the normal delay.
        continue;
      }
    } catch (error) {
      console.error(`browser-agent: ${error.message}`);
      if (page.isClosed() || /browser has been closed|target page.*closed|disconnected/i.test(error.message)) {
        await close().catch(() => {});
        process.exit(1);
      }
    }
    await delay(config.pollSeconds * 1000);
  }
}

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.once(signal, async () => {
    await close();
    process.exit(0);
  });
}

await loop();
