import type { AcquisitionLifecycle, ActionItem, ChampionshipBacktest, DraftRoom, FantasyProsQuota, ManagerNotification, ManagerPlan, ProjectionBacktest, Readiness, TrashTalkStatus } from "./types";

const API = process.env.API_BASE_URL ?? "http://127.0.0.1:8000";
const API_TIMEOUT_MS = Number(process.env.API_FETCH_TIMEOUT_MS ?? 1500);

async function get<T>(path: string, fallback: T): Promise<T> {
  try {
    const response = await fetch(`${API}${path}`, {
      cache: "no-store",
      signal: AbortSignal.timeout(API_TIMEOUT_MS),
    });
    if (!response.ok) return fallback;
    return (await response.json()) as T;
  } catch {
    return fallback;
  }
}

export const api = {
  readiness: () => get<Readiness>("/api/readiness", { ready: false, checks: [], discrepancies: [] }),
  actions: () => get<ActionItem[]>("/api/actions", []),
  draft: () => get<DraftRoom>("/api/draft", { recommendation: null, live: { pick_count: 0, picks: [] }, simulations: [] }),
  calendar: () => get<{ events: Array<Record<string, unknown>> }>("/api/calendar", { events: [] }),
  sources: () => get<Array<Record<string, unknown>>>("/api/source-health", []),
  sourceDatasets: () => get<Array<Record<string, unknown>>>("/api/source-dataset-health", []),
  incidents: () => get<Array<Record<string, unknown>>>("/api/operational-incidents", []),
  hostHealth: () => get<Record<string, unknown>>("/api/host-health", { status: "unavailable", checks: {} }),
  sourceCatalog: () => get<{ objective?: string; strategy?: string; sources: Array<Record<string, unknown>> }>("/api/source-catalog", { sources: [] }),
  manager: () => get<ManagerPlan>("/api/manager", { manager_state: "unavailable", execution_state: "analysis_only", decision: null, blockers: ["Manager API unavailable"] }),
  fantasyProsQuota: () => get<FantasyProsQuota>("/api/fantasypros-quota", { configured: false, status: "unavailable" }),
  projectionBacktest: () => get<ProjectionBacktest>("/api/projection-backtest", { status: "not_run", qualification_status: "blocked" }),
  championshipBacktest: () => get<ChampionshipBacktest>("/api/championship-backtest", { status: "not_run", qualification_status: "blocked" }),
  acquisitionLifecycle: () => get<AcquisitionLifecycle>("/api/acquisition-lifecycle", { status: "not_run", active_count: 0, completed_count: 0, failed_count: 0, requires_manager_refresh: false, claims: [] }),
  notifications: () => get<ManagerNotification[]>("/api/notifications?channel=dashboard&limit=5", []),
  trashTalk: () => get<TrashTalkStatus>("/api/trash-talk", { configured: false, enabled: false, opted_out_roster_ids: [], weekly_target: 6, weekly_limit: 10, weekly_count: 0, image_upload_configured: false, posts: [] }),
};
