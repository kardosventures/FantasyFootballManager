import assert from "node:assert/strict";
import test from "node:test";

import { _runProcess } from "../src/runner.mjs";

test("a process that exits cleanly after SIGTERM is still reported as timed out", async () => {
  const source = [
    'process.on("SIGTERM", () => process.exit(0));',
    "setInterval(() => {}, 1000);",
  ].join("\n");

  await assert.rejects(
    _runProcess(process.execPath, ["-e", source], { timeoutMs: 50 }),
    /Codex timed out/,
  );
});
