import { createHash } from "node:crypto";
import { ensureLeaguePage, inspectSleeperSession } from "./session.mjs";
import { validateCommand } from "./contract.mjs";
import {
  executeUiAction,
  prepareAcquisitionAction,
  preparePendingWaiverCancellation,
  prepareTradeProposal,
  prepareTradeResponse,
  verifyPendingWaiverUi,
  verifyPendingWaiverStateUi,
  verifyTradeOfferUi,
} from "./ui.mjs";
import {
  preflightAcquisition,
  preflightIr,
  preflightLineup,
  preflightTrade,
  verifyCommand,
} from "./verify.mjs";
import { preflightDraftPick } from "./draft.mjs";

function fingerprint(value) {
  return createHash("sha256").update(JSON.stringify(value)).digest("hex");
}

function result(status, message, evidence = {}) {
  return { status, message, evidence };
}

export async function executeCommand(page, config, command, options = {}) {
  const onStatus = options.onStatus ?? (async () => {});
  try {
    validateCommand(command, config);
  } catch (error) {
    return result("blocked", error.message, { write_attempted: false });
  }

  if (command.action_type === "DRAFT_PLAYER") await ensureLeaguePage(page, config);
  const before = await inspectSleeperSession(page, config);
  const sharedSession = options.authenticatedSession;
  if (
    !before.sessionAvailable
    && before.correctOrigin
    && before.correctLeague
    && sharedSession?.sessionAvailable
    && sharedSession?.correctOrigin
    && sharedSession?.correctLeague
  ) {
    // Auxiliary tabs share the same authenticated Chrome context but Sleeper's
    // responsive players/trade pages do not always repeat the account/team
    // labels used by the primary team-page session proof.
    before.sessionAvailable = true;
    before.authenticationSource = "shared_authenticated_browser_context";
  }
  const evidence = {
    ui_contract_version: config.uiContractVersion,
    action_type: command.action_type,
    idempotency_key: command.idempotency_key,
    before_url: before.url,
    before_fingerprint: fingerprint(before),
    write_attempted: false,
  };
  let preparedAcquisition = null;
  let preparedPendingWaiver = null;
  let preparedTrade = null;
  if (!before.sessionAvailable || !before.correctOrigin || !before.correctLeague) {
    return result("blocked", "Sleeper login, league, or team preflight failed", evidence);
  }
  if (config.mode !== "browser") {
    return result("verified", "Playwright dry run passed preflight; no Sleeper write attempted", evidence);
  }
  if (!config.liveActions.has(command.action_type)) {
    return result("blocked", `${command.action_type} is not enabled in BROWSER_LIVE_ACTIONS`, evidence);
  }

  try {
    if (command.action_type === "DRAFT_PLAYER") {
      const draftPreflight = await preflightDraftPick(
        command,
        config,
        options.fetchImpl ?? fetch,
      );
      evidence.draft_preflight = draftPreflight;
      if (draftPreflight.alreadyCommitted) {
        const verification = await verifyCommand(command, config, options.fetchImpl ?? fetch);
        return result(
          verification.verified ? "verified" : "blocked",
          verification.message,
          { ...evidence, public_api_verified: verification.verified },
        );
      }
    }
    if (command.action_type === "SET_LINEUP") {
      const lineupPreflight = await preflightLineup(
        command,
        config,
        options.fetchImpl ?? fetch,
      );
      evidence.lineup_preflight = lineupPreflight;
      if (lineupPreflight.alreadyCommitted) {
        const verification = await verifyCommand(command, config, options.fetchImpl ?? fetch);
        return result(
          verification.verified ? "verified" : "blocked",
          verification.message,
          { ...evidence, public_api_verified: verification.verified },
        );
      }
    }
    if (["MOVE_TO_IR", "REMOVE_FROM_IR"].includes(command.action_type)) {
      const irPreflight = await preflightIr(command, config, options.fetchImpl ?? fetch);
      evidence.ir_preflight = irPreflight;
      if (irPreflight.alreadyCommitted) {
        const verification = await verifyCommand(command, config, options.fetchImpl ?? fetch);
        return result(
          verification.verified ? "verified" : "blocked",
          verification.message,
          { ...evidence, public_api_verified: verification.verified },
        );
      }
    }
    if (["CANCEL_WAIVER_CLAIM", "REORDER_WAIVER_CLAIMS"].includes(command.action_type)) {
      const after = await verifyPendingWaiverStateUi(
        page,
        config,
        command.parameters.expected_claims_after,
      );
      evidence.authenticated_pending_after_preflight = after;
      if (after.verified) {
        return result("verified", after.message, {
          ...evidence,
          authenticated_ui_verified: true,
          verification: after,
        });
      }
      const beforeQueue = await verifyPendingWaiverStateUi(
        page,
        config,
        command.parameters.expected_claims_before,
      );
      evidence.authenticated_pending_preflight = beforeQueue;
      if (!beforeQueue.verified) {
        throw new Error("Sleeper My Waivers changed after the pending-claim decision");
      }
      if (command.action_type === "CANCEL_WAIVER_CLAIM") {
        preparedPendingWaiver = await preparePendingWaiverCancellation(
          page,
          config,
          command.parameters,
        );
        evidence.ui_preflight = preparedPendingWaiver.evidence;
      }
    }
    if (["ADD_FREE_AGENT", "WAIVER_CLAIM"].includes(command.action_type)) {
      const acquisitionPreflight = await preflightAcquisition(
        command,
        config,
        options.fetchImpl ?? fetch,
      );
      evidence.acquisition_preflight = acquisitionPreflight;
      if (acquisitionPreflight.alreadyCommitted) {
        const verification = await verifyCommand(command, config, options.fetchImpl ?? fetch);
        return result(
          verification.verified ? "verified" : "blocked",
          verification.message,
          { ...evidence, public_api_verified: verification.verified },
        );
      }
      if (command.action_type === "WAIVER_CLAIM") {
        const authenticatedPending = await verifyPendingWaiverUi(
          page,
          config,
          command.parameters,
        );
        evidence.authenticated_pending_preflight = authenticatedPending;
        if (authenticatedPending.verified) {
          return result("verified", authenticatedPending.message, {
            ...evidence,
            public_api_verified: false,
            authenticated_ui_verified: true,
            verification: authenticatedPending,
          });
        }
      }
      preparedAcquisition = await prepareAcquisitionAction(
        page,
        config,
        command.parameters,
        command.action_type === "ADD_FREE_AGENT" ? "Add" : "Claim",
      );
      evidence.ui_preflight = preparedAcquisition.evidence;
    }
    if (["PROPOSE_TRADE", "ACCEPT_TRADE", "DECLINE_TRADE"].includes(command.action_type)) {
      const tradePreflight = await preflightTrade(
        command,
        config,
        options.fetchImpl ?? fetch,
      );
      evidence.trade_preflight = tradePreflight;
      if (tradePreflight.alreadyCommitted) {
        const verification = await verifyCommand(command, config, options.fetchImpl ?? fetch);
        return result(
          verification.verified ? "verified" : "blocked",
          verification.message,
          { ...evidence, public_api_verified: verification.verified },
        );
      }
      preparedTrade = command.action_type === "PROPOSE_TRADE"
        ? await prepareTradeProposal(page, config, command.parameters)
        : await prepareTradeResponse(
          page,
          config,
          command.parameters,
          command.action_type === "ACCEPT_TRADE" ? "ACCEPT" : "DECLINE",
        );
      evidence.ui_preflight = preparedTrade.evidence;
    }
    if (typeof options.beforeWrite !== "function") {
      throw new Error("Authenticated backend write preflight is required");
    }
    const backendPreflight = await options.beforeWrite(evidence.ui_preflight ?? {});
    evidence.backend_write_preflight = backendPreflight;
    if (backendPreflight?.allowed !== true) {
      throw new Error(
        backendPreflight?.reason ?? "Backend evidence gate blocked the final write boundary",
      );
    }
    await onStatus("executing", "Playwright is applying the semantic action", evidence);
    evidence.write_attempted = true;
    if (preparedAcquisition) await preparedAcquisition.commit();
    else if (preparedPendingWaiver) await preparedPendingWaiver.commit();
    else if (preparedTrade) await preparedTrade.commit();
    else await executeUiAction(page, config, command);
    const submittedEvidence = { ...evidence, after_url: page.url() };
    await onStatus(
      "verifying",
      "Sleeper UI submission completed; checking the public API",
      submittedEvidence,
    );
    let verification;
    if (command.action_type === "PROPOSE_TRADE") {
      verification = await verifyTradeOfferUi(page, config, command.parameters, true);
    } else if (command.action_type === "DECLINE_TRADE") {
      verification = await verifyTradeOfferUi(page, config, command.parameters, false);
    } else if (["CANCEL_WAIVER_CLAIM", "REORDER_WAIVER_CLAIMS"].includes(command.action_type)) {
      verification = await verifyPendingWaiverStateUi(
        page,
        config,
        command.parameters.expected_claims_after,
      );
    } else {
      verification = await verifyCommand(command, config, options.fetchImpl ?? fetch);
      if (command.action_type === "WAIVER_CLAIM" && !verification.verified) {
        verification = await verifyPendingWaiverUi(page, config, command.parameters);
      }
    }
    const authenticatedUiVerified = (
      verification.verified
      && ["authenticated_pending_ui", "authenticated_trade_ui"].includes(
        verification.verification_channel,
      )
    );
    return result(verification.verified ? "verified" : "unverified", verification.message, {
      ...submittedEvidence,
      public_api_verified: verification.verified && !authenticatedUiVerified,
      authenticated_ui_verified: authenticatedUiVerified,
      verification,
    });
  } catch (error) {
    if (!evidence.write_attempted && (preparedAcquisition || preparedPendingWaiver || preparedTrade)) {
      await (preparedAcquisition ?? preparedPendingWaiver ?? preparedTrade).abort().catch(() => {});
    }
    return result(
      evidence.write_attempted ? "unverified" : "blocked",
      error.message,
      evidence,
    );
  }
}
