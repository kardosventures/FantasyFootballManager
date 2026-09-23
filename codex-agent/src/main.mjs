import fs from "node:fs/promises";
import path from "node:path";

import { loadConfig } from "./config.mjs";
import { signedEnvelope, validateRequest, verifyEnvelope } from "./contract.mjs";
import { writeHeartbeat } from "./heartbeat.mjs";
import { assertChatGptLogin, runCodexDecision } from "./runner.mjs";

const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

async function writeAtomic(filePath, value) {
  const temporary = `${filePath}.tmp-${process.pid}`;
  await fs.writeFile(temporary, `${JSON.stringify(value)}\n`, { mode: 0o600 });
  await fs.rename(temporary, filePath);
}

function safeError(error) {
  const message = error instanceof Error ? error.message : String(error);
  return message.replaceAll(/sk-[A-Za-z0-9_-]+/g, "[redacted]").slice(0, 1200);
}

async function processRequest(config, filename) {
  const requestPath = path.join(config.requestsDir, filename);
  const requestId = filename.slice(0, -5);
  const responsePath = path.join(config.responsesDir, filename);
  try {
    await fs.access(responsePath);
    return;
  } catch {
    // A response does not exist yet.
  }

  let request;
  try {
    const envelope = JSON.parse(await fs.readFile(requestPath, "utf8"));
    request = validateRequest(verifyEnvelope(config.sharedSecret, envelope), config);
    if (request.request_id !== requestId) throw new Error("Request filename does not match its id");
    await assertChatGptLogin(config);
    const result = await runCodexDecision(config, request);
    const payload = {
      request_id: requestId,
      completed_at: Date.now() / 1000,
      model: request.model,
      latency_ms: result.latencyMs,
      used_web_search: result.usedWebSearch,
      decision: result.decision,
    };
    await writeAtomic(responsePath, signedEnvelope(config.sharedSecret, payload));
    process.stdout.write(`Completed Codex draft decision ${requestId.slice(0, 12)}\n`);
  } catch (error) {
    const payload = {
      request_id: requestId,
      completed_at: Date.now() / 1000,
      model: request?.model || config.model,
      error: safeError(error),
    };
    await writeAtomic(responsePath, signedEnvelope(config.sharedSecret, payload));
    process.stderr.write(`Codex draft decision ${requestId.slice(0, 12)} failed: ${payload.error}\n`);
  }
}

async function main() {
  const config = loadConfig();
  const heartbeatState = { status: "starting", currentRequestId: null };
  let heartbeatWrites = Promise.resolve();
  const persistHeartbeat = () => {
    const snapshot = { ...heartbeatState };
    heartbeatWrites = heartbeatWrites
      .catch(() => undefined)
      .then(() => writeHeartbeat(config.heartbeatPath, snapshot))
      .catch((error) => {
        process.stderr.write(`Codex worker heartbeat failed: ${safeError(error)}\n`);
      });
    return heartbeatWrites;
  };
  await fs.mkdir(config.requestsDir, { recursive: true, mode: 0o700 });
  await fs.mkdir(config.responsesDir, { recursive: true, mode: 0o700 });
  await fs.mkdir(config.runtimeDir, { recursive: true, mode: 0o700 });
  await assertChatGptLogin(config);
  heartbeatState.status = "idle";
  await persistHeartbeat();
  setInterval(persistHeartbeat, config.heartbeatMs);
  process.stdout.write(`Codex draft worker ready with ChatGPT auth (${config.model})\n`);
  while (true) {
    const filenames = (await fs.readdir(config.requestsDir))
      .filter((name) => /^[a-f0-9]{64}\.json$/.test(name))
      .sort();
    for (const filename of filenames) {
      try {
        await fs.access(path.join(config.responsesDir, filename));
        continue;
      } catch {
        // A response does not exist yet.
      }
      heartbeatState.status = "processing";
      heartbeatState.currentRequestId = filename.slice(0, -5);
      await persistHeartbeat();
      await processRequest(config, filename);
      heartbeatState.status = "idle";
      heartbeatState.currentRequestId = null;
      await persistHeartbeat();
    }
    await sleep(config.pollMs);
  }
}

main().catch((error) => {
  process.stderr.write(`Codex draft worker stopped: ${safeError(error)}\n`);
  process.exit(1);
});
