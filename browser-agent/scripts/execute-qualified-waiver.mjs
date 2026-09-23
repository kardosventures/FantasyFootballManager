import { readFile } from "node:fs/promises";
import path from "node:path";

import { AgentApi } from "../src/api.mjs";
import { loadConfig } from "../src/config.mjs";
import { executeCommand } from "../src/executor.mjs";
import { launchSleeperSession } from "../src/session.mjs";

const config = loadConfig();
if (config.mode !== "browser" || !config.liveActions.has("WAIVER_CLAIM")) {
  throw new Error("Controlled qualification requires browser mode with WAIVER_CLAIM explicitly enabled");
}
const defaultPlan = path.resolve(import.meta.dirname, "../../reports/in-season-plan.json");
const planPath = path.resolve(process.argv[2] ?? defaultPlan);
const plan = JSON.parse(await readFile(planPath, "utf8"));
const preview = (plan.waiver_execution_preview ?? []).find(
  (item) => item.action_type === "WAIVER_CLAIM",
);
const queued = plan.acquisition_execution?.queued ?? [];
const commandId = queued.length === 1 && queued[0].command_id
  ? queued[0].command_id
  : process.env.CONTROLLED_WAIVER_COMMAND_ID;
const expectedAddPlayerId = preview?.parameters.add_player_id
  ?? process.env.CONTROLLED_WAIVER_ADD_PLAYER_ID;
const expectedDropPlayerId = preview?.parameters.drop_player_id
  ?? process.env.CONTROLLED_WAIVER_DROP_PLAYER_ID
  ?? "";
if (!commandId || !expectedAddPlayerId) {
  throw new Error(
    "Qualification requires one queued plan command or explicit controlled command/add ids",
  );
}

const api = new AgentApi(config);
await api.qualificationRetry(commandId, config.agentId);
const command = await api.lease(config.agentId, {
  command_id: commandId,
  action_types: ["WAIVER_CLAIM"],
});
if (!command) throw new Error("The exact controlled waiver command was not leaseable");
if (
  command.id !== commandId
  || command.action_type !== "WAIVER_CLAIM"
  || String(command.parameters.add_player_id) !== String(expectedAddPlayerId)
  || String(command.parameters.drop_player_id ?? "")
    !== String(expectedDropPlayerId)
) {
  throw new Error("The leased command does not exactly match the controlled waiver target");
}

const { page } = await launchSleeperSession(config);
await api.report(command.id, {
  agent_id: config.agentId,
  status: "preflight",
  message: "Controlled waiver qualification started exact semantic preflight",
  evidence: { ui_contract_version: config.uiContractVersion, qualification_run: true },
});
const result = await executeCommand(page, config, command, {
  onStatus: (status, message, evidence) => api.report(command.id, {
    agent_id: config.agentId,
    status,
    message,
    evidence: { ...evidence, qualification_run: true },
  }),
  beforeWrite: (uiObservation) =>
    api.preflight(command.id, config.agentId, uiObservation),
});
await api.report(command.id, {
  agent_id: config.agentId,
  status: result.status,
  message: result.message,
  evidence: { ...result.evidence, qualification_run: true },
});
process.stdout.write(`${JSON.stringify({
  command_id: command.id,
  action_type: command.action_type,
  add_player_id: command.parameters.add_player_id,
  drop_player_id: command.parameters.drop_player_id || null,
  status: result.status,
  message: result.message,
  write_attempted: result.evidence.write_attempted,
  verification: result.evidence.verification ?? null,
})}\n`);
process.exit(result.status === "verified" ? 0 : 1);
