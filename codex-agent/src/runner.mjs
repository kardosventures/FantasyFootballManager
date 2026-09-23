import { spawn } from "node:child_process";
import fs from "node:fs/promises";
import path from "node:path";

function codexEnvironment() {
  const environment = { ...process.env };
  environment.PATH = ["/usr/local/bin", environment.PATH].filter(Boolean).join(":");
  for (const key of Object.keys(environment)) {
    if (key.startsWith("OPENAI_")) delete environment[key];
  }
  return environment;
}

function runProcess(command, args, options = {}) {
  const { input, timeoutMs = 15_000 } = options;
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, {
      env: codexEnvironment(),
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    let timedOut = false;
    const collect = (current, chunk) => (current + chunk.toString("utf8")).slice(-16_000);
    child.stdout.on("data", (chunk) => { stdout = collect(stdout, chunk); });
    child.stderr.on("data", (chunk) => { stderr = collect(stderr, chunk); });
    child.on("error", reject);
    const timer = setTimeout(() => {
      timedOut = true;
      child.kill("SIGTERM");
      setTimeout(() => child.kill("SIGKILL"), 1000).unref();
    }, timeoutMs);
    child.on("close", (code, signal) => {
      clearTimeout(timer);
      if (timedOut) return reject(new Error("Codex timed out"));
      if (code === 0) return resolve({ stdout, stderr });
      const detail = (stderr || stdout).replaceAll(/sk-[A-Za-z0-9_-]+/g, "[redacted]").slice(-1000);
      reject(new Error(signal ? `Codex timed out (${signal})` : `Codex exited ${code}: ${detail}`));
    });
    if (input) child.stdin.end(input);
    else child.stdin.end();
  });
}

export { runProcess as _runProcess };

export async function assertChatGptLogin(config) {
  const result = await runProcess(config.codexBin, ["login", "status"]);
  const status = `${result.stdout}\n${result.stderr}`;
  if (!status.includes("Logged in using ChatGPT")) {
    throw new Error("Codex is not signed in with the ChatGPT subscription");
  }
  if (status.toLowerCase().includes("api key")) {
    throw new Error("Refusing to run Codex with API-key authentication");
  }
}

export async function runCodexDecision(config, request) {
  const workDir = path.join(config.runtimeDir, request.request_id);
  const schemaPath = path.join(workDir, "decision-schema.json");
  const outputPath = path.join(workDir, "decision.json");
  await fs.rm(workDir, { recursive: true, force: true });
  await fs.mkdir(workDir, { recursive: true, mode: 0o700 });
  await fs.writeFile(schemaPath, `${JSON.stringify(request.schema)}\n`, { mode: 0o600 });

  const args = [];
  if (request.web_search_enabled) args.push("--search");
  args.push(
    "exec",
    "--ephemeral",
    "--ignore-user-config",
    "--skip-git-repo-check",
    "--sandbox", "read-only",
    "--color", "never",
    "--model", request.model,
    "-c", `model_reasoning_effort=${JSON.stringify(request.reasoning_effort)}`,
    "--output-schema", schemaPath,
    "--output-last-message", outputPath,
    "-C", workDir,
    "-",
  );
  const timeoutMs = Math.max(1000, Math.floor((request.expires_at - Date.now() / 1000) * 1000));
  const started = performance.now();
  await runProcess(config.codexBin, args, { input: request.prompt, timeoutMs });
  let raw;
  try {
    raw = await fs.readFile(outputPath, "utf8");
  } catch (error) {
    if (error?.code === "ENOENT") {
      throw new Error("Codex completed without writing a decision");
    }
    throw error;
  }
  const decision = JSON.parse(raw);
  return {
    decision,
    latencyMs: Math.round((performance.now() - started) * 10) / 10,
    usedWebSearch: request.web_search_enabled,
  };
}
