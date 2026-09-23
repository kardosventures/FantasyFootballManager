import test from "node:test";
import assert from "node:assert/strict";
import { executeCommand } from "../src/executor.mjs";

function fakePage() {
  return {
    url: () => "https://sleeper.com/leagues/league-1/team",
    isClosed: () => false,
    locator: (selector) => {
      if (selector === "body") {
        return { innerText: async () => "jimkardos Jim.ai" };
      }
      return { count: async () => 3 };
    },
  };
}

function fakeAuxiliaryPage() {
  return {
    ...fakePage(),
    locator: (selector) => {
      if (selector === "body") {
        return { innerText: async () => "PLAYERS C. Douglas WR - MIA" };
      }
      return { count: async () => 3 };
    },
  };
}

const command = {
  action_type: "SET_LINEUP",
  league_id: "league-1",
  roster_id: 8,
  idempotency_key: "once",
  expected_state_hash: "a".repeat(64),
  expires_at: "2099-01-01T00:00:00Z",
  parameters: {
    from_player_id: "p1",
    from_player_name: "Starter",
    from_slot: "FLEX",
    to_player_id: "p2",
    to_player_name: "Bench",
    to_slot: "BN",
    target_slot_index: 0,
    expected_starters_before: ["p1", "p3"],
    expected_starters_after: ["p2", "p3"],
  },
};

test("dry run reports no write attempted", async () => {
  const result = await executeCommand(fakePage(), {
    leagueUrl: "https://sleeper.com/leagues/league-1",
    leagueId: "league-1",
    sleeperApiUrl: new URL("https://api.sleeper.app/v1/"),
    rosterId: 8,
    accountLabel: "jimkardos",
    teamLabel: "Jim.ai",
    mode: "dry_run",
    liveActions: new Set(),
    uiContractVersion: "test",
  }, command);
  assert.equal(result.status, "verified");
  assert.equal(result.evidence.write_attempted, false);
});

test("live mode blocks actions that are not explicitly qualified", async () => {
  const result = await executeCommand(fakePage(), {
    leagueUrl: "https://sleeper.com/leagues/league-1",
    leagueId: "league-1",
    sleeperApiUrl: new URL("https://api.sleeper.app/v1/"),
    rosterId: 8,
    accountLabel: "jimkardos",
    teamLabel: "Jim.ai",
    mode: "browser",
    liveActions: new Set(),
    uiContractVersion: "test",
  }, command);
  assert.equal(result.status, "blocked");
  assert.equal(result.evidence.write_attempted, false);
});

test("live mode requires and obeys the final backend evidence gate", async () => {
  let checked = 0;
  const result = await executeCommand(fakePage(), {
    leagueUrl: "https://sleeper.com/leagues/league-1",
    leagueId: "league-1",
    sleeperApiUrl: new URL("https://api.sleeper.app/v1/"),
    rosterId: 8,
    accountLabel: "jimkardos",
    teamLabel: "Jim.ai",
    mode: "browser",
    liveActions: new Set(["SET_LINEUP"]),
    uiContractVersion: "test",
  }, command, {
    fetchImpl: async () => new Response(JSON.stringify([
      { roster_id: 8, players: ["p1", "p2", "p3"], starters: ["p1", "p3"] },
    ]), { status: 200, headers: { "content-type": "application/json" } }),
    beforeWrite: async () => {
      checked += 1;
      return { allowed: false, reason: "Dataset became stale", gate: { status: "blocked" } };
    },
  });
  assert.equal(checked, 1);
  assert.equal(result.status, "blocked");
  assert.equal(result.evidence.write_attempted, false);
  assert.equal(result.evidence.backend_write_preflight.gate.status, "blocked");
  assert.match(result.message, /stale/);
});

test("auxiliary league tabs inherit authentication only from the proven shared context", async () => {
  let checked = 0;
  const result = await executeCommand(fakeAuxiliaryPage(), {
    leagueUrl: "https://sleeper.com/leagues/league-1",
    leagueId: "league-1",
    sleeperApiUrl: new URL("https://api.sleeper.app/v1/"),
    rosterId: 8,
    accountLabel: "jimkardos",
    teamLabel: "Jim.ai",
    mode: "browser",
    liveActions: new Set(["SET_LINEUP"]),
    uiContractVersion: "test",
  }, command, {
    authenticatedSession: {
      sessionAvailable: true,
      correctOrigin: true,
      correctLeague: true,
    },
    fetchImpl: async () => new Response(JSON.stringify([
      { roster_id: 8, players: ["p1", "p2", "p3"], starters: ["p1", "p3"] },
    ]), { status: 200, headers: { "content-type": "application/json" } }),
    beforeWrite: async () => {
      checked += 1;
      return { allowed: false, reason: "Stop after shared-session proof" };
    },
  });

  assert.equal(checked, 1);
  assert.equal(result.status, "blocked");
  assert.equal(result.evidence.write_attempted, false);
  assert.match(result.message, /shared-session proof/);
});
