import { readFile, rename, writeFile } from "node:fs/promises";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

import { loadConfig } from "../src/config.mjs";
import {
  inspectSleeperSession,
  launchSleeperSession,
  leaguePath,
} from "../src/session.mjs";
import { prepareAcquisitionAction } from "../src/ui.mjs";

const config = loadConfig();
const defaultPlan = path.resolve(import.meta.dirname, "../../reports/in-season-plan.json");
const planPath = path.resolve(process.argv[2] ?? defaultPlan);
const plan = JSON.parse(await readFile(planPath, "utf8"));
const preview = (plan.waiver_execution_preview ?? []).find(
  (item) => item.action_type === "WAIVER_CLAIM",
);
if (!preview) throw new Error("The current plan has no exact WAIVER_CLAIM preview to qualify");
if (preview.parameters.drop_player_id && !preview.parameters.drop_player_search_name) {
  const drop = (plan.our_team?.all_players ?? []).find(
    (player) => String(player.player_id) === String(preview.parameters.drop_player_id),
  );
  if (!drop?.name) throw new Error("The preview lacks the exact full drop-player name");
  preview.parameters.drop_player_search_name = drop.name;
}

const { context } = await launchSleeperSession(config);
const page = await context.newPage();
await page.goto(leaguePath(config, "/team"), { waitUntil: "domcontentloaded" });
let prepared;
try {
  let session;
  for (let attempt = 1; attempt <= 10; attempt += 1) {
    session = await inspectSleeperSession(page, config);
    if (session.sessionAvailable && session.correctLeague && session.correctOrigin) break;
    await delay(500);
  }
  if (!session.sessionAvailable || !session.correctLeague || !session.correctOrigin) {
    throw new Error("Sleeper login, league, or team qualification preflight failed");
  }
  prepared = await prepareAcquisitionAction(page, config, preview.parameters, "Claim");
  await prepared.abort();
  const qualification = {
    generated_at: new Date().toISOString(),
    status: "selector_qualified",
    ui_contract_version: config.uiContractVersion,
    league_id: config.leagueId,
    roster_id: config.rosterId,
    action_type: preview.action_type,
    add_player_id: preview.parameters.add_player_id,
    drop_player_id: preview.parameters.drop_player_id || null,
    write_attempted: false,
    public_transaction_verified: false,
    dialog_dismissed: true,
    evidence: prepared.evidence,
  };
  const qualificationPath = path.join(path.dirname(planPath), "waiver-ui-qualification.json");
  const temporaryPath = `${qualificationPath}.tmp-${process.pid}`;
  await writeFile(temporaryPath, `${JSON.stringify(qualification)}\n`, "utf8");
  await rename(temporaryPath, qualificationPath);
  await page.close();
  process.stdout.write(`${JSON.stringify(qualification)}\n`);
} catch (error) {
  await prepared?.abort().catch(() => {});
  await page.close().catch(() => {});
  throw error;
}
process.exit(0);
