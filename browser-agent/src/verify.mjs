import { setTimeout as delay } from "node:timers/promises";

const VERIFY_ATTEMPTS = 5;
const VERIFY_INTERVAL_MS = 2_000;

function includes(items, value) {
  return Array.isArray(items) && items.map(String).includes(String(value));
}

function excludes(items, value) {
  return Array.isArray(items) && !items.map(String).includes(String(value));
}

function exactIds(actual, expected) {
  return (
    Array.isArray(actual)
    && Array.isArray(expected)
    && actual.length === expected.length
    && actual.every((item, index) => String(item) === String(expected[index]))
  );
}

function exactIdSet(actual, expected) {
  if (!Array.isArray(actual) || !Array.isArray(expected) || actual.length !== expected.length) {
    return false;
  }
  const actualIds = actual.map(String).sort();
  const expectedIds = expected.map(String).sort();
  return actualIds.every((playerId, index) => playerId === expectedIds[index]);
}

function mappedToRoster(mapping, playerId, rosterId) {
  return (
    mapping != null
    && typeof mapping === "object"
    && String(mapping[String(playerId)]) === String(rosterId)
  );
}

export function matchWaiverTransaction(command, config, transaction) {
  const parameters = command.parameters ?? {};
  if (
    transaction?.type !== "waiver"
    || !["pending", "complete"].includes(String(transaction.status))
    || String(transaction.creator ?? "") !== String(config.ownerUserId)
    || !includes(transaction.roster_ids, config.rosterId)
    || !mappedToRoster(transaction.adds, parameters.add_player_id, config.rosterId)
  ) {
    return false;
  }
  if (parameters.drop_player_id) {
    return mappedToRoster(transaction.drops, parameters.drop_player_id, config.rosterId);
  }
  return !Object.values(transaction.drops ?? {}).some(
    (rosterId) => String(rosterId) === String(config.rosterId),
  );
}

export function matchFreeAgentTransaction(command, config, transaction) {
  const parameters = command.parameters ?? {};
  if (
    transaction?.type !== "free_agent"
    || String(transaction.status) !== "complete"
    || String(transaction.creator ?? "") !== String(config.ownerUserId)
    || !includes(transaction.roster_ids, config.rosterId)
    || !mappedToRoster(transaction.adds, parameters.add_player_id, config.rosterId)
  ) {
    return false;
  }
  if (parameters.drop_player_id) {
    if (!mappedToRoster(transaction.drops, parameters.drop_player_id, config.rosterId)) {
      return false;
    }
  } else if (Object.values(transaction.drops ?? {}).some(
    (rosterId) => String(rosterId) === String(config.rosterId),
  )) {
    return false;
  }
  const boundary = Date.parse(command.not_before ?? command.created_at ?? "");
  const observed = Number(transaction.created);
  return !Number.isFinite(boundary) || !Number.isFinite(observed) || observed + 5_000 >= boundary;
}

function pendingWaiverConflict(command, config, transaction) {
  if (
    transaction?.type !== "waiver"
    || transaction?.status !== "pending"
    || String(transaction.creator ?? "") !== String(config.ownerUserId)
    || !includes(transaction.roster_ids, config.rosterId)
  ) {
    return false;
  }
  const parameters = command.parameters ?? {};
  return (
    mappedToRoster(transaction.adds, parameters.add_player_id, config.rosterId)
    || (
      parameters.drop_player_id
      && mappedToRoster(transaction.drops, parameters.drop_player_id, config.rosterId)
    )
  );
}

function evaluateRoster(command, roster) {
  const parameters = command.parameters;
  const players = roster.players ?? [];
  const starters = roster.starters ?? [];
  const reserve = roster.reserve ?? [];
  switch (command.action_type) {
    case "SET_LINEUP":
      return exactIds(starters, parameters.expected_starters_after);
    case "MOVE_TO_IR":
      return (
        exactIdSet(players, parameters.expected_players)
        && exactIdSet(reserve, parameters.expected_reserve_after)
      );
    case "REMOVE_FROM_IR":
      return (
        exactIdSet(players, parameters.expected_players)
        && exactIdSet(reserve, parameters.expected_reserve_after)
      );
    case "ADD_FREE_AGENT":
      return exactIdSet(players, parameters.expected_roster_after);
    case "WAIVER_CLAIM":
      return false;
    case "DROP_PLAYER":
      return excludes(players, parameters.drop_player_id);
    case "ACCEPT_TRADE":
      return exactIdSet(players, parameters.expected_our_roster_after);
    default:
      return false;
  }
}

async function fetchLeagueRosters(config, fetchImpl, cacheKey) {
  const endpoint = new URL(`league/${config.leagueId}/rosters`, config.sleeperApiUrl);
  endpoint.searchParams.set("fresh", cacheKey);
  const response = await fetchImpl(endpoint, {
    method: "GET",
    headers: { "cache-control": "no-cache" },
    signal: AbortSignal.timeout(10_000),
  });
  if (!response.ok) throw new Error(`Sleeper verification returned HTTP ${response.status}`);
  const rosters = await response.json();
  if (!Array.isArray(rosters)) throw new Error("Sleeper roster verification returned invalid data");
  return rosters;
}

async function fetchConfiguredRoster(config, fetchImpl, cacheKey) {
  const endpoint = new URL(`league/${config.leagueId}/rosters`, config.sleeperApiUrl);
  endpoint.searchParams.set("fresh", cacheKey);
  const response = await fetchImpl(endpoint, {
    method: "GET",
    headers: { "cache-control": "no-cache" },
    signal: AbortSignal.timeout(10_000),
  });
  if (!response.ok) throw new Error(`Sleeper verification returned HTTP ${response.status}`);
  const rosters = await response.json();
  const roster = rosters.find((item) => Number(item.roster_id) === config.rosterId);
  if (!roster) throw new Error("Configured roster was not present in verification data");
  return roster;
}

async function fetchTransactions(command, config, fetchImpl, cacheKey) {
  const week = Number(command.parameters?.transaction_week);
  if (!Number.isInteger(week) || week <= 0) {
    throw new Error(`${command.action_type} requires an exact positive transaction_week`);
  }
  const endpoint = new URL(
    `league/${config.leagueId}/transactions/${week}`,
    config.sleeperApiUrl,
  );
  endpoint.searchParams.set("fresh", cacheKey);
  const response = await fetchImpl(endpoint, {
    method: "GET",
    headers: { "cache-control": "no-cache" },
    signal: AbortSignal.timeout(10_000),
  });
  if (!response.ok) throw new Error(`Sleeper verification returned HTTP ${response.status}`);
  const transactions = await response.json();
  if (!Array.isArray(transactions)) {
    throw new Error("Sleeper transaction verification returned an invalid payload");
  }
  return transactions;
}

export async function preflightLineup(command, config, fetchImpl = fetch) {
  const roster = await fetchConfiguredRoster(config, fetchImpl, `lineup-preflight-${Date.now()}`);
  const current = roster.starters ?? [];
  if (exactIds(current, command.parameters.expected_starters_after)) {
    return { alreadyCommitted: true, starters: current.map(String) };
  }
  if (!exactIds(current, command.parameters.expected_starters_before)) {
    throw new Error("Sleeper starters changed after the lineup decision; refusing the write");
  }
  if (
    !includes(roster.players, command.parameters.from_player_id)
    || !includes(roster.players, command.parameters.to_player_id)
  ) {
    throw new Error("A lineup-swap player is no longer on the configured roster");
  }
  return { alreadyCommitted: false, starters: current.map(String) };
}

export async function preflightAcquisition(command, config, fetchImpl = fetch) {
  const roster = await fetchConfiguredRoster(
    config,
    fetchImpl,
    `acquisition-preflight-${Date.now()}`,
  );
  const current = roster.players ?? [];
  const parameters = command.parameters;
  if (command.action_type === "WAIVER_CLAIM") {
    const transactions = await fetchTransactions(
      command,
      config,
      fetchImpl,
      `waiver-preflight-${Date.now()}`,
    );
    const existing = transactions.find((transaction) =>
      matchWaiverTransaction(command, config, transaction));
    if (existing) {
      return {
        alreadyCommitted: true,
        players: current.map(String),
        transaction_id: String(existing.transaction_id),
        transaction_status: String(existing.status),
      };
    }
    if (transactions.some((transaction) => pendingWaiverConflict(command, config, transaction))) {
      throw new Error(
        "A different pending waiver uses the exact add or drop player; refusing the write",
      );
    }
  }
  if (command.action_type === "ADD_FREE_AGENT" && exactIdSet(
    current,
    parameters.expected_roster_after,
  )) {
    const transactions = await fetchTransactions(
      command,
      config,
      fetchImpl,
      `free-agent-preflight-${Date.now()}`,
    );
    const existing = transactions.find((transaction) =>
      matchFreeAgentTransaction(command, config, transaction));
    if (existing) {
      return {
        alreadyCommitted: true,
        players: current.map(String),
        transaction_id: String(existing.transaction_id),
        transaction_status: "complete",
      };
    }
    throw new Error(
      "Expected roster state exists without a transaction bound to this command; refusing the write",
    );
  }
  if (exactIdSet(current, parameters.expected_roster_after)) {
    return { alreadyCommitted: true, players: current.map(String) };
  }
  if (!exactIdSet(current, parameters.expected_roster_before)) {
    throw new Error("Sleeper roster changed after the acquisition decision; refusing the write");
  }
  if (includes(current, parameters.add_player_id)) {
    throw new Error("The acquisition target is already on the configured roster");
  }
  if (parameters.drop_player_id && excludes(current, parameters.drop_player_id)) {
    throw new Error("The exact drop player is no longer on the configured roster");
  }
  return { alreadyCommitted: false, players: current.map(String) };
}

export async function preflightIr(command, config, fetchImpl = fetch) {
  const roster = await fetchConfiguredRoster(config, fetchImpl, `ir-preflight-${Date.now()}`);
  const players = (roster.players ?? []).map(String);
  const reserve = (roster.reserve ?? []).map(String);
  const parameters = command.parameters;
  if (!exactIdSet(players, parameters.expected_players)) {
    throw new Error("Sleeper roster changed after the IR decision; refusing the write");
  }
  if (exactIdSet(reserve, parameters.expected_reserve_after)) {
    return { alreadyCommitted: true, players, reserve };
  }
  if (!exactIdSet(reserve, parameters.expected_reserve_before)) {
    throw new Error("Sleeper reserve changed after the IR decision; refusing the write");
  }
  return { alreadyCommitted: false, players, reserve };
}

export async function preflightTrade(command, config, fetchImpl = fetch) {
  const rosters = await fetchLeagueRosters(config, fetchImpl, `trade-preflight-${Date.now()}`);
  const parameters = command.parameters;
  const ours = rosters.find((row) => Number(row.roster_id) === Number(config.rosterId));
  const theirs = rosters.find(
    (row) => Number(row.roster_id) === Number(parameters.counterparty_roster_id),
  );
  if (!ours || !theirs) throw new Error("Trade preflight did not find both exact rosters");
  const ourPlayers = (ours.players ?? []).map(String);
  const theirPlayers = (theirs.players ?? []).map(String);
  if (
    command.action_type === "ACCEPT_TRADE"
    && exactIdSet(ourPlayers, parameters.expected_our_roster_after)
  ) {
    return { alreadyCommitted: true, our_players: ourPlayers, their_players: theirPlayers };
  }
  if (
    !exactIdSet(ourPlayers, parameters.expected_our_roster_before)
    || !exactIdSet(theirPlayers, parameters.expected_counterparty_roster_before)
  ) {
    throw new Error("A trade roster changed after the manager decision; refusing the write");
  }
  if (
    parameters.send_assets.some((asset) => !ourPlayers.includes(String(asset.player_id)))
    || parameters.receive_assets.some((asset) => !theirPlayers.includes(String(asset.player_id)))
  ) {
    throw new Error("A trade asset is no longer on the exact expected roster");
  }
  return { alreadyCommitted: false, our_players: ourPlayers, their_players: theirPlayers };
}

export async function verifyCommand(command, config, fetchImpl = fetch) {
  if (command.action_type === "DRAFT_PLAYER") {
    const parameters = command.parameters;
    const endpoint = new URL(`draft/${parameters.draft_id}/picks`, config.sleeperApiUrl);
    let lastError = "Expected draft pick was not observed";
    for (let attempt = 1; attempt <= VERIFY_ATTEMPTS; attempt += 1) {
      try {
        endpoint.searchParams.set("fresh", `${Date.now()}-${attempt}`);
        const response = await fetchImpl(endpoint, {
          method: "GET",
          headers: { "cache-control": "no-cache" },
          signal: AbortSignal.timeout(10_000),
        });
        if (!response.ok) throw new Error(`Sleeper verification returned HTTP ${response.status}`);
        const picks = await response.json();
        const pick = picks.find(
          (item) => Number(item.pick_no) === Number(parameters.expected_pick_no),
        );
        if (
          pick &&
          String(pick.player_id) === String(parameters.player_id) &&
          String(pick.picked_by ?? "") === String(config.ownerUserId) &&
          Number(pick.draft_slot) === Number(parameters.owner_slot)
        ) {
          return {
            verified: true,
            message: `Sleeper public API confirmed ${parameters.player_name} at pick #${parameters.expected_pick_no}`,
          };
        }
        if (pick) lastError = `Pick #${parameters.expected_pick_no} does not match the command`;
      } catch (error) {
        lastError = error.message;
      }
      if (attempt < VERIFY_ATTEMPTS) await delay(VERIFY_INTERVAL_MS);
    }
    return { verified: false, message: lastError };
  }
  if (command.action_type === "ADD_FREE_AGENT") {
    let lastError = "Expected completed free-agent transaction was not observed";
    for (let attempt = 1; attempt <= VERIFY_ATTEMPTS; attempt += 1) {
      try {
        const roster = await fetchConfiguredRoster(
          config,
          fetchImpl,
          `free-agent-roster-verify-${Date.now()}-${attempt}`,
        );
        const transactions = await fetchTransactions(
          command,
          config,
          fetchImpl,
          `free-agent-transaction-verify-${Date.now()}-${attempt}`,
        );
        const transaction = transactions.find((item) =>
          matchFreeAgentTransaction(command, config, item));
        if (evaluateRoster(command, roster) && transaction) {
          return {
            verified: true,
            message: `Sleeper public API confirmed completed free-agent transaction ${transaction.transaction_id}`,
            verification_channel: "public_api",
            transaction_id: String(transaction.transaction_id),
            transaction_status: "complete",
            transaction_created: transaction.created ?? null,
          };
        }
      } catch (error) {
        lastError = error.message;
      }
      if (attempt < VERIFY_ATTEMPTS) await delay(VERIFY_INTERVAL_MS);
    }
    return { verified: false, message: lastError };
  }
  if (command.action_type === "SET_AUTOSUB") {
    return { verified: false, message: "SET_AUTOSUB has no qualified public-API verifier" };
  }
  if (command.action_type === "WAIVER_CLAIM") {
    let lastError = "Expected pending waiver transaction was not observed";
    for (let attempt = 1; attempt <= VERIFY_ATTEMPTS; attempt += 1) {
      try {
        const transactions = await fetchTransactions(
          command,
          config,
          fetchImpl,
          `waiver-verify-${Date.now()}-${attempt}`,
        );
        const transaction = transactions.find((item) =>
          matchWaiverTransaction(command, config, item));
        if (transaction) {
          return {
            verified: true,
            message: `Sleeper public API confirmed ${transaction.status} waiver transaction ${transaction.transaction_id}`,
            verification_channel: "public_api",
            transaction_id: String(transaction.transaction_id),
            transaction_status: String(transaction.status),
            transaction_created: transaction.created ?? null,
          };
        }
        const failed = transactions.find((item) => (
          item?.type === "waiver"
          && item?.status === "failed"
          && String(item.creator ?? "") === String(config.ownerUserId)
          && mappedToRoster(item.adds, command.parameters.add_player_id, config.rosterId)
        ));
        if (failed) {
          return {
            verified: false,
            message: `Sleeper rejected the waiver: ${failed.metadata?.notes ?? "unknown reason"}`,
            transaction_id: String(failed.transaction_id),
            transaction_status: "failed",
          };
        }
      } catch (error) {
        lastError = error.message;
      }
      if (attempt < VERIFY_ATTEMPTS) await delay(VERIFY_INTERVAL_MS);
    }
    return { verified: false, message: lastError };
  }
  let lastError = "Expected roster state was not observed";
  for (let attempt = 1; attempt <= VERIFY_ATTEMPTS; attempt += 1) {
    try {
      const roster = await fetchConfiguredRoster(
        config,
        fetchImpl,
        `verify-${Date.now()}-${attempt}`,
      );
      if (evaluateRoster(command, roster)) {
        return { verified: true, message: "Sleeper public API confirmed the expected roster state" };
      }
    } catch (error) {
      lastError = error.message;
    }
    if (attempt < VERIFY_ATTEMPTS) await delay(VERIFY_INTERVAL_MS);
  }
  return { verified: false, message: lastError };
}
