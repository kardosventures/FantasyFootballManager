import { ACTION_TYPES } from "./config.mjs";

const ACTION_SET = new Set(ACTION_TYPES);
const FORBIDDEN_KEYS = new Set([
  "trade",
  "trade_id",
  "commissioner",
  "commissioner_setting",
  "settings",
]);

function requiredText(parameters, key) {
  const value = parameters[key];
  if (typeof value !== "string" || !value.trim()) throw new Error(`${key} is required`);
  return value.trim();
}

function requiredId(parameters, key) {
  const value = parameters[key];
  if ((typeof value !== "string" && typeof value !== "number") || String(value).trim() === "") {
    throw new Error(`${key} is required`);
  }
  return String(value);
}

function requiredPositiveInteger(parameters, key) {
  const value = Number(parameters[key]);
  if (!Number.isInteger(value) || value <= 0) throw new Error(`${key} must be a positive integer`);
  return value;
}

function requiredNonNegativeInteger(parameters, key) {
  const value = Number(parameters[key]);
  if (!Number.isInteger(value) || value < 0) {
    throw new Error(`${key} must be a non-negative integer`);
  }
  return value;
}

function requiredIdArray(parameters, key) {
  const value = parameters[key];
  if (!Array.isArray(value) || value.length < 1 || value.length > 20) {
    throw new Error(`${key} must contain 1-20 player ids`);
  }
  const ids = value.map((item) => String(item));
  if (ids.some((item) => !item.trim()) || new Set(ids).size !== ids.length) {
    throw new Error(`${key} must contain unique non-empty player ids`);
  }
  return ids;
}

function reserveIdArray(parameters, key, players) {
  const value = parameters[key];
  if (!Array.isArray(value) || value.length > 20) {
    throw new Error(`${key} must contain 0-20 player ids`);
  }
  const ids = value.map((item) => String(item));
  if (
    ids.some((item) => !item.trim() || !players.includes(item))
    || new Set(ids).size !== ids.length
  ) {
    throw new Error(`${key} must contain unique rostered player ids`);
  }
  return ids;
}

function validateIr(actionType, parameters) {
  const playerId = requiredId(parameters, "player_id");
  requiredText(parameters, "player_name");
  requiredText(parameters, "player_display_name");
  requiredText(parameters, "player_position");
  requiredText(parameters, "player_team");
  const status = requiredText(parameters, "observed_status").toUpperCase();
  const players = requiredIdArray(parameters, "expected_players");
  const before = reserveIdArray(parameters, "expected_reserve_before", players);
  const after = reserveIdArray(parameters, "expected_reserve_after", players);
  if (!players.includes(playerId)) throw new Error(`${actionType} player must remain rostered`);
  if (actionType === "MOVE_TO_IR") {
    if (
      before.includes(playerId)
      || after.length !== before.length + 1
      || after.at(-1) !== playerId
      || !["IR", "PUP", "NFI", "OUT"].includes(status)
    ) {
      throw new Error("MOVE_TO_IR must add exactly one eligible player to reserve");
    }
  } else if (
    !before.includes(playerId)
    || after.length !== before.length - 1
    || after.some((item, index) => item !== before.filter((id) => id !== playerId)[index])
  ) {
    throw new Error("REMOVE_FROM_IR must remove exactly one player from reserve");
  }
}

function pendingClaims(parameters, key) {
  const rows = parameters[key];
  if (!Array.isArray(rows) || rows.length > 8) {
    throw new Error(`${key} must contain 0-8 exact pending claims`);
  }
  const identities = new Set();
  return rows.map((row) => {
    const add = requiredId(row ?? {}, "add_player_id");
    requiredText(row, "add_player_name");
    requiredText(row, "add_player_position");
    requiredText(row, "add_player_team");
    const drop = row.drop_player_id ? requiredId(row, "drop_player_id") : "";
    if (drop) {
      requiredText(row, "drop_player_name");
      requiredText(row, "drop_player_position");
      requiredText(row, "drop_player_team");
    }
    const identity = `${add}:${drop}`;
    if (identities.has(identity)) throw new Error(`${key} contains duplicate pending claims`);
    identities.add(identity);
    return identity;
  });
}

function validatePendingWaiverAction(actionType, parameters) {
  requiredText(parameters, "ui_contract_version");
  requiredText(parameters, "management_reason");
  const before = pendingClaims(parameters, "expected_claims_before");
  const after = pendingClaims(parameters, "expected_claims_after");
  if (actionType === "CANCEL_WAIVER_CLAIM") {
    const claim = parameters.claim;
    const target = `${requiredId(claim ?? {}, "add_player_id")}:${claim?.drop_player_id ? requiredId(claim, "drop_player_id") : ""}`;
    if (
      !before.includes(target)
      || JSON.stringify(after) !== JSON.stringify(before.filter((identity) => identity !== target))
    ) {
      throw new Error("CANCEL_WAIVER_CLAIM must remove exactly one pending claim");
    }
  } else if (
    before.length < 2
    || before.length !== after.length
    || before.every((identity, index) => identity === after[index])
    || [...before].sort().some((identity, index) => identity !== [...after].sort()[index])
  ) {
    throw new Error("REORDER_WAIVER_CLAIMS must reorder the same pending claim set");
  }
}

function validateLineupSwap(parameters) {
  const from = requiredId(parameters, "from_player_id");
  const to = requiredId(parameters, "to_player_id");
  requiredText(parameters, "from_player_name");
  requiredText(parameters, "to_player_name");
  requiredText(parameters, "from_slot");
  requiredText(parameters, "to_slot");
  const targetIndex = requiredNonNegativeInteger(parameters, "target_slot_index");
  const before = requiredIdArray(parameters, "expected_starters_before");
  const after = requiredIdArray(parameters, "expected_starters_after");

  if (from === to) throw new Error("SET_LINEUP players must be different");
  if (before.length !== after.length || targetIndex >= before.length) {
    throw new Error("SET_LINEUP expected starter arrays must have the same valid length");
  }
  if (before[targetIndex] !== from || after[targetIndex] !== to) {
    throw new Error("SET_LINEUP target slot does not match the exact before/after arrays");
  }
  const sourceIndex = before.indexOf(to);
  const changed = before
    .map((playerId, index) => (playerId === after[index] ? null : index))
    .filter((index) => index !== null);
  if (sourceIndex === -1) {
    if (changed.length !== 1 || after.includes(from)) {
      throw new Error("SET_LINEUP bench swap must change exactly one starter slot");
    }
  } else if (
    changed.length !== 2
    || !changed.includes(targetIndex)
    || !changed.includes(sourceIndex)
    || after[sourceIndex] !== from
  ) {
    throw new Error("SET_LINEUP starter swap must exchange exactly two starter slots");
  }
}

function validateAcquisition(actionType, parameters) {
  const add = requiredId(parameters, "add_player_id");
  requiredText(parameters, "add_player_name");
  requiredText(parameters, "add_player_search_name");
  requiredText(parameters, "add_player_position");
  requiredText(parameters, "add_player_team");
  requiredText(parameters, "contingency");
  requiredPositiveInteger(parameters, "claim_priority");
  const faab = requiredNonNegativeInteger(parameters, "faab_percent");
  if (faab > 100) throw new Error("faab_percent cannot exceed 100");
  const expectedType = actionType === "ADD_FREE_AGENT" ? "free_agent" : "waiver";
  if (requiredText(parameters, "observed_acquisition_type") !== expectedType) {
    throw new Error(`${actionType} disagrees with the observed Sleeper action type`);
  }
  if (actionType === "ADD_FREE_AGENT" && faab !== 0) {
    throw new Error("ADD_FREE_AGENT cannot carry a FAAB bid");
  }
  if (actionType === "WAIVER_CLAIM") {
    requiredPositiveInteger(parameters, "transaction_week");
    if (parameters.claim_group !== undefined) {
      if (
        !Array.isArray(parameters.claim_group)
        || parameters.claim_group.length < 1
        || parameters.claim_group.length > 8
      ) {
        throw new Error("WAIVER_CLAIM claim_group must contain 1-8 exact claims");
      }
      const keys = new Set();
      const priorities = new Set();
      for (const row of parameters.claim_group) {
        const groupAdd = requiredId(row ?? {}, "add_player_id");
        const groupDrop = row?.drop_player_id ? requiredId(row, "drop_player_id") : "";
        const groupPriority = requiredPositiveInteger(row ?? {}, "claim_priority");
        const key = `${groupAdd}:${groupDrop}`;
        if (keys.has(key) || priorities.has(groupPriority)) {
          throw new Error("WAIVER_CLAIM claim_group identities and priorities must be unique");
        }
        keys.add(key);
        priorities.add(groupPriority);
      }
      if (!keys.has(`${add}:${parameters.drop_player_id ? String(parameters.drop_player_id) : ""}`)) {
        throw new Error("WAIVER_CLAIM is not present in its exact claim_group");
      }
    }
  }

  const drop = parameters.drop_player_id ? requiredId(parameters, "drop_player_id") : "";
  if (drop) {
    requiredText(parameters, "drop_player_name");
    requiredText(parameters, "drop_player_search_name");
    requiredText(parameters, "drop_player_position");
    requiredText(parameters, "drop_player_team");
  }
  const before = requiredIdArray(parameters, "expected_roster_before");
  const after = requiredIdArray(parameters, "expected_roster_after");
  if (before.includes(add) || !after.includes(add)) {
    throw new Error(`${actionType} add player conflicts with the exact roster states`);
  }
  if (drop && (!before.includes(drop) || after.includes(drop))) {
    throw new Error(`${actionType} drop player conflicts with the exact roster states`);
  }
  const transformed = before.filter((playerId) => playerId !== drop);
  transformed.push(add);
  if (
    transformed.length !== after.length
    || transformed.some((playerId, index) => playerId !== after[index])
  ) {
    throw new Error(`${actionType} must encode exactly one roster acquisition`);
  }
}

function tradeAssets(parameters, key) {
  const rows = parameters[key];
  if (!Array.isArray(rows) || rows.length < 1 || rows.length > 3) {
    throw new Error(`${key} must contain 1-3 exact player assets`);
  }
  const ids = new Set();
  return rows.map((row) => {
    const playerId = requiredId(row ?? {}, "player_id");
    requiredText(row, "player_name");
    requiredText(row, "player_display_name");
    requiredText(row, "position");
    requiredText(row, "team");
    if (ids.has(playerId)) throw new Error(`${key} contains duplicate player identities`);
    ids.add(playerId);
    return playerId;
  });
}

function validateTrade(actionType, parameters) {
  const counterpartyRosterId = requiredPositiveInteger(parameters, "counterparty_roster_id");
  if (!counterpartyRosterId) throw new Error("counterparty_roster_id is required");
  requiredText(parameters, "counterparty_account_label");
  requiredText(parameters, "decision_reason");
  requiredText(parameters, "championship_case");
  const upsideTier = requiredText(parameters, "upside_tier");
  if (
    ["PROPOSE_TRADE", "ACCEPT_TRADE"].includes(actionType)
    && upsideTier !== "high"
  ) {
    throw new Error(`${actionType} requires a high-upside classification`);
  }
  const confidence = Number(parameters.decision_confidence);
  if (!Number.isFinite(confidence) || confidence < 0 || confidence > 1) {
    throw new Error(`${actionType} requires decision_confidence between 0 and 1`);
  }
  if (["PROPOSE_TRADE", "ACCEPT_TRADE"].includes(actionType) && confidence < 0.92) {
    throw new Error(`${actionType} requires at least 0.92 decision confidence`);
  }
  const sends = tradeAssets(parameters, "send_assets");
  const receives = tradeAssets(parameters, "receive_assets");
  if (sends.some((id) => receives.includes(id))) {
    throw new Error(`${actionType} cannot send and receive the same player`);
  }
  const ours = requiredIdArray(parameters, "expected_our_roster_before");
  const theirs = requiredIdArray(parameters, "expected_counterparty_roster_before");
  if (sends.some((id) => !ours.includes(id)) || receives.some((id) => !theirs.includes(id))) {
    throw new Error(`${actionType} assets conflict with the exact before rosters`);
  }
  if (["ACCEPT_TRADE", "DECLINE_TRADE"].includes(actionType)) {
    requiredText(parameters, "offer_fingerprint");
  }
  if (actionType === "PROPOSE_TRADE") {
    const expiration = requiredText(parameters, "expiration_label");
    if (!["1 Hour", "END OF TODAY", "24 Hours"].includes(expiration)) {
      throw new Error("PROPOSE_TRADE requires a bounded Sleeper expiration");
    }
  }
  if (actionType === "ACCEPT_TRADE") {
    const expectedAfter = requiredIdArray(parameters, "expected_our_roster_after");
    const transformed = ours.filter((id) => !sends.includes(id)).concat(receives).sort();
    if (
      transformed.length !== expectedAfter.length
      || transformed.some((id, index) => id !== [...expectedAfter].sort()[index])
    ) {
      throw new Error("ACCEPT_TRADE expected roster does not match the exact exchange");
    }
  }
}

export function validateCommand(command, config, now = new Date()) {
  if (!command || !ACTION_SET.has(command.action_type)) {
    throw new Error("Command action type is not allowlisted");
  }
  if (String(command.league_id) !== config.leagueId) throw new Error("Wrong Sleeper league");
  if (Number(command.roster_id) !== config.rosterId) throw new Error("Wrong Sleeper roster");
  if (!command.idempotency_key) throw new Error("Missing idempotency key");
  if (typeof command.expected_state_hash !== "string" || !/^[a-f0-9]{64}$/i.test(command.expected_state_hash)) {
    throw new Error("Missing or invalid expected state hash");
  }
  if (new Date(command.expires_at) <= now) throw new Error("Command expired before preflight");
  for (const key of Object.keys(command.parameters ?? {})) {
    if (FORBIDDEN_KEYS.has(key.toLowerCase())) throw new Error(`Forbidden parameter: ${key}`);
  }

  const parameters = command.parameters ?? {};
  switch (command.action_type) {
    case "SET_LINEUP":
      validateLineupSwap(parameters);
      break;
    case "MOVE_TO_IR":
    case "REMOVE_FROM_IR":
      validateIr(command.action_type, parameters);
      break;
    case "DROP_PLAYER":
      requiredId(parameters, "drop_player_id");
      requiredText(parameters, "player_name");
      break;
    case "WAIVER_CLAIM":
    case "ADD_FREE_AGENT":
      validateAcquisition(command.action_type, parameters);
      break;
    case "CANCEL_WAIVER_CLAIM":
    case "REORDER_WAIVER_CLAIMS":
      validatePendingWaiverAction(command.action_type, parameters);
      break;
    case "PROPOSE_TRADE":
    case "ACCEPT_TRADE":
    case "DECLINE_TRADE":
      validateTrade(command.action_type, parameters);
      break;
    case "SET_AUTOSUB":
      requiredId(parameters, "starter_player_id");
      requiredId(parameters, "substitute_player_id");
      requiredText(parameters, "starter_player_name");
      requiredText(parameters, "substitute_player_name");
      break;
    case "DRAFT_PLAYER": {
      const draftId = requiredId(parameters, "draft_id");
      if (!/^\d+$/.test(draftId)) throw new Error("draft_id must be numeric");
      requiredId(parameters, "player_id");
      requiredText(parameters, "player_name");
      requiredText(parameters, "player_position");
      requiredPositiveInteger(parameters, "expected_pick_no");
      requiredPositiveInteger(parameters, "owner_slot");
      if (!config.ownerUserId) throw new Error("SLEEPER_OWNER_USER_ID is required");
      break;
    }
  }
  return command;
}
