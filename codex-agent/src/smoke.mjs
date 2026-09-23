import crypto from "node:crypto";
import fs from "node:fs/promises";
import path from "node:path";

import { loadConfig } from "./config.mjs";
import { signedEnvelope, verifyEnvelope } from "./contract.mjs";

const config = loadConfig();
const requestId = crypto.randomBytes(32).toString("hex");
const requestPath = path.join(config.requestsDir, `${requestId}.json`);
const responsePath = path.join(config.responsesDir, `${requestId}.json`);
const now = Date.now() / 1000;
const payload = {
  version: 1,
  request_id: requestId,
  created_at: now,
  expires_at: now + 50,
  model: config.model,
  reasoning_effort: config.reasoningEffort,
  web_search_enabled: config.webSearchEnabled,
  prompt: [
    "This is a connectivity smoke test for an autonomous fantasy-football draft manager.",
    "Do not inspect files or take actions. Return the required structured response only.",
    "Set status to ok and identify authentication_mode as ChatGPT subscription.",
  ].join(" "),
  schema: {
    type: "object",
    additionalProperties: false,
    properties: {
      status: { type: "string", enum: ["ok"] },
      authentication_mode: { type: "string", enum: ["ChatGPT subscription"] },
    },
    required: ["status", "authentication_mode"],
  },
};

await fs.mkdir(config.requestsDir, { recursive: true, mode: 0o700 });
await fs.mkdir(config.responsesDir, { recursive: true, mode: 0o700 });
await fs.writeFile(requestPath, `${JSON.stringify(signedEnvelope(config.sharedSecret, payload))}\n`, {
  mode: 0o600,
  flag: "wx",
});

try {
  const deadline = Date.now() + 55_000;
  while (Date.now() < deadline) {
    try {
      const response = verifyEnvelope(
        config.sharedSecret,
        JSON.parse(await fs.readFile(responsePath, "utf8")),
      );
      if (response.error) throw new Error(response.error);
      if (response.request_id !== requestId) throw new Error("Smoke response id did not match");
      process.stdout.write(`${JSON.stringify(response)}\n`);
      process.exitCode = 0;
      break;
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  if (process.exitCode === undefined) throw new Error("Timed out waiting for the Codex worker");
} finally {
  await fs.rm(requestPath, { force: true });
  await fs.rm(responsePath, { force: true });
}
