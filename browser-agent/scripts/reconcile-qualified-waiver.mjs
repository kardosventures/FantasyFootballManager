import { AgentApi } from "../src/api.mjs";
import { loadConfig } from "../src/config.mjs";
import { launchSleeperSession } from "../src/session.mjs";
import { verifyPendingWaiverUi } from "../src/ui.mjs";

const config = loadConfig();
const commandId = process.env.CONTROLLED_WAIVER_COMMAND_ID;
if (!commandId) throw new Error("CONTROLLED_WAIVER_COMMAND_ID is required");

const api = new AgentApi(config);
const actions = await api.get("/api/actions");
const action = actions.find((item) => item.execution?.id === commandId);
if (!action || action.action_type !== "WAIVER_CLAIM") {
  throw new Error("The exact controlled waiver action was not found");
}
const { page } = await launchSleeperSession(config);
const verification = await verifyPendingWaiverUi(page, config, action.exact_action);
if (!verification.verified) throw new Error(verification.message);

await api.report(commandId, {
  agent_id: config.agentId,
  status: "verified",
  message: verification.message,
  evidence: {
    ui_contract_version: config.uiContractVersion,
    qualification_run: true,
    reconciled_from_unverified: true,
    write_attempted: true,
    public_api_verified: false,
    authenticated_ui_verified: true,
    verification,
  },
});
process.stdout.write(`${JSON.stringify({ command_id: commandId, ...verification })}\n`);
process.exit(0);
