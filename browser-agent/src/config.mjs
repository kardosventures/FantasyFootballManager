import path from "node:path";

export const ACTION_TYPES = Object.freeze([
  "SET_LINEUP",
  "MOVE_TO_IR",
  "REMOVE_FROM_IR",
  "SET_AUTOSUB",
  "WAIVER_CLAIM",
  "CANCEL_WAIVER_CLAIM",
  "REORDER_WAIVER_CLAIMS",
  "ADD_FREE_AGENT",
  "DROP_PLAYER",
  "PROPOSE_TRADE",
  "ACCEPT_TRADE",
  "DECLINE_TRADE",
  "DRAFT_PLAYER",
]);

const EXECUTION_MODES = new Set(["disabled", "manual", "dry_run", "fake", "browser"]);

function booleanValue(value, fallback = false) {
  if (value === undefined) return fallback;
  return value.toLowerCase() === "true";
}

function integerValue(value, fallback) {
  const parsed = Number.parseInt(value ?? "", 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function actionSet(value = "") {
  const actions = value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
  const unknown = actions.filter((item) => !ACTION_TYPES.includes(item));
  if (unknown.length) throw new Error(`Unsupported BROWSER_LIVE_ACTIONS: ${unknown.join(", ")}`);
  return new Set(actions);
}

export function loadConfig(environment = process.env) {
  const mode = environment.EXECUTION_MODE ?? "dry_run";
  if (!EXECUTION_MODES.has(mode)) throw new Error(`Unsupported EXECUTION_MODE: ${mode}`);

  const leagueId = environment.SLEEPER_LEAGUE_ID ?? "1395499060898586624";
  const ownerUserId = environment.SLEEPER_OWNER_USER_ID ?? "1398337982238343168";
  const rosterId = integerValue(environment.SLEEPER_ROSTER_ID, 8);
  const projectRoot = path.resolve(import.meta.dirname, "../..");
  const profileDir = path.resolve(
    environment.BROWSER_PROFILE_DIR ?? path.join(projectRoot, ".browser-profile"),
  );
  const sharedSecret = environment.SHIM_SHARED_SECRET ?? "";
  if (sharedSecret.length < 20) {
    throw new Error("SHIM_SHARED_SECRET must be configured with at least 20 characters");
  }

  const sleeperApiBase = environment.SLEEPER_BASE_URL ?? "https://api.sleeper.app/v1/";
  const browserCdpUrl = new URL(environment.BROWSER_CDP_URL ?? "http://127.0.0.1:9227/");
  if (
    browserCdpUrl.protocol !== "http:" ||
    !["127.0.0.1", "localhost", "[::1]"].includes(browserCdpUrl.hostname) ||
    !browserCdpUrl.port
  ) {
    throw new Error("BROWSER_CDP_URL must be an explicit localhost HTTP port");
  }

  return {
    apiUrl: new URL(environment.FANTASY_API_URL ?? "http://127.0.0.1:8000"),
    sharedSecret,
    agentId: environment.FANTASY_AGENT_ID ?? "playwright-agent-primary",
    mode,
    leagueId,
    draftId: environment.SLEEPER_DRAFT_ID?.trim() || null,
    ownerUserId,
    rosterId,
    leagueUrl:
      environment.SLEEPER_LEAGUE_WEB_URL ?? `https://sleeper.com/leagues/${leagueId}`,
    sleeperApiUrl: new URL(sleeperApiBase.endsWith("/") ? sleeperApiBase : `${sleeperApiBase}/`),
    accountLabel: environment.SLEEPER_ACCOUNT_LABEL ?? "jimkardos",
    teamLabel: environment.SLEEPER_TEAM_LABEL ?? "Jim.ai",
    profileDir,
    browserCdpUrl,
    browserApplication: environment.BROWSER_APPLICATION ?? "Google Chrome",
    headless: booleanValue(environment.BROWSER_HEADLESS, false),
    browserChannel: environment.BROWSER_CHANNEL ?? "chrome",
    pollSeconds: integerValue(environment.AGENT_POLL_SECONDS, 10),
    liveActions: actionSet(environment.BROWSER_LIVE_ACTIONS),
    uiContractVersion:
      environment.SLEEPER_UI_CONTRACT_VERSION ?? "sleeper-web-2026-09-09",
  };
}
