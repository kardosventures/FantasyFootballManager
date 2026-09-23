import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { writeHeartbeat } from "../src/heartbeat.mjs";

test("writes an atomic worker heartbeat with current processing state", async () => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), "fantasy-codex-heartbeat-"));
  const target = path.join(directory, "nested", "heartbeat.json");
  try {
    const payload = await writeHeartbeat(target, {
      status: "processing",
      currentRequestId: "request-123",
    });
    const persisted = JSON.parse(await fs.readFile(target, "utf8"));
    assert.equal(persisted.version, "codex-worker-heartbeat-2026.1");
    assert.equal(persisted.status, "processing");
    assert.equal(persisted.current_request_id, "request-123");
    assert.equal(persisted.pid, process.pid);
    assert.deepEqual(persisted, payload);
  } finally {
    await fs.rm(directory, { recursive: true, force: true });
  }
});
