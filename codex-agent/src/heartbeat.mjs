import fs from "node:fs/promises";
import path from "node:path";

export async function writeHeartbeat(filePath, state = {}) {
  const payload = {
    version: "codex-worker-heartbeat-2026.1",
    generated_at: new Date().toISOString(),
    pid: process.pid,
    status: state.status ?? "idle",
    current_request_id: state.currentRequestId ?? null,
  };
  await fs.mkdir(path.dirname(filePath), { recursive: true, mode: 0o700 });
  const temporary = `${filePath}.tmp-${process.pid}`;
  await fs.writeFile(temporary, `${JSON.stringify(payload)}\n`, { mode: 0o600 });
  await fs.rename(temporary, filePath);
  return payload;
}
