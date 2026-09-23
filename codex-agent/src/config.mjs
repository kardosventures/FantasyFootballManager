import os from "node:os";
import path from "node:path";

function integer(name, fallback, minimum, maximum) {
  const value = Number.parseInt(process.env[name] ?? String(fallback), 10);
  if (!Number.isInteger(value) || value < minimum || value > maximum) {
    throw new Error(`${name} must be an integer from ${minimum} to ${maximum}`);
  }
  return value;
}

function required(name) {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`${name} is required`);
  return value;
}

export function loadConfig() {
  const root = process.env.CODEX_DECISIONS_HOST_DIR?.trim()
    || path.join(os.homedir(), "Library", "Application Support", "JimAiFantasy", "codex-decisions");
  const sharedSecret = required("SHIM_SHARED_SECRET");
  if (sharedSecret.length < 24) {
    throw new Error("SHIM_SHARED_SECRET must contain at least 24 characters");
  }
  const provider = process.env.DRAFT_EXPERT_PROVIDER?.trim() || "codex_cli";
  if (provider !== "codex_cli") {
    throw new Error("The local Codex worker requires DRAFT_EXPERT_PROVIDER=codex_cli");
  }
  return {
    root,
    requestsDir: path.join(root, "requests"),
    responsesDir: path.join(root, "responses"),
    runtimeDir: path.join(path.dirname(root), "codex-runtime"),
    heartbeatPath: path.join(
      path.dirname(root),
      "codex-runtime",
      "codex-worker-heartbeat.json",
    ),
    heartbeatMs: integer("CODEX_WORKER_HEARTBEAT_MS", 10_000, 1_000, 60_000),
    sharedSecret,
    codexBin: process.env.CODEX_BIN?.trim() || "/usr/local/bin/codex",
    model: process.env.DRAFT_EXPERT_MODEL?.trim() || "gpt-5.6-terra",
    reasoningEffort: process.env.DRAFT_EXPERT_REASONING_EFFORT?.trim() || "medium",
    webSearchEnabled: (process.env.DRAFT_EXPERT_WEB_SEARCH_ENABLED ?? "true") === "true",
    pollMs: integer("CODEX_DECISION_POLL_MS", 500, 100, 5000),
  };
}
