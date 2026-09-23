import test from "node:test";
import assert from "node:assert/strict";
import { loadConfig } from "../src/config.mjs";
import { validateCommand } from "../src/contract.mjs";

const config = loadConfig({
  SHIM_SHARED_SECRET: "test-secret-that-is-long-enough",
  SLEEPER_LEAGUE_ID: "league-1",
  SLEEPER_ROSTER_ID: "8",
});

function command(overrides = {}) {
  return {
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
    ...overrides,
  };
}

test("accepts a scoped in-season command", () => {
  assert.equal(validateCommand(command(), config).action_type, "SET_LINEUP");
});

test("accepts a fully scoped automatic draft command", () => {
  const draft = command({
    action_type: "DRAFT_PLAYER",
    parameters: {
      draft_id: "123456789",
      player_id: "5859",
      player_name: "A.J. Brown",
      player_position: "WR",
      player_team: "NE",
      expected_pick_no: 27,
      owner_slot: 7,
    },
  });
  assert.equal(validateCommand(draft, config).action_type, "DRAFT_PLAYER");
});

test("rejects unsafe draft ids and incomplete draft targets", () => {
  assert.throws(
    () =>
      validateCommand(
        command({
          action_type: "DRAFT_PLAYER",
          parameters: {
            draft_id: "../other",
            player_id: "5859",
            player_name: "A.J. Brown",
            player_position: "WR",
            expected_pick_no: 27,
            owner_slot: 7,
          },
        }),
        config,
      ),
    /numeric/,
  );
});

test("rejects the wrong league or roster", () => {
  assert.throws(() => validateCommand(command({ league_id: "other" }), config), /league/);
  assert.throws(() => validateCommand(command({ roster_id: 7 }), config), /roster/);
});

test("rejects trades and commissioner parameters", () => {
  assert.throws(() => validateCommand(command({ action_type: "TRADE" }), config), /allowlisted/);
  assert.throws(
    () =>
      validateCommand(
        command({
          parameters: {
            from_player_id: "1",
            from_player_name: "A",
            to_player_id: "2",
            to_player_name: "B",
            settings: {},
          },
        }),
        config,
      ),
    /Forbidden parameter/,
  );
});

test("requires UI-safe player names in addition to ids", () => {
  assert.throws(
    () =>
      validateCommand(
        command({ parameters: { from_player_id: "1", to_player_id: "2" } }),
        config,
      ),
    /from_player_name/,
  );
});

test("accepts an exact swap between two existing starter slots", () => {
  const value = command({
    parameters: {
      from_player_id: "p1",
      from_player_name: "Starter One",
      from_slot: "RB",
      to_player_id: "p2",
      to_player_name: "Starter Two",
      to_slot: "FLEX",
      target_slot_index: 0,
      expected_starters_before: ["p1", "p2", "p3"],
      expected_starters_after: ["p2", "p1", "p3"],
    },
  });
  assert.equal(validateCommand(value, config).action_type, "SET_LINEUP");
});

test("rejects a lineup command that changes more than its exact swap", () => {
  const value = command({
    parameters: {
      ...command().parameters,
      expected_starters_after: ["p2", "p4"],
    },
  });
  assert.throws(() => validateCommand(value, config), /exactly one starter slot/);
});

test("accepts only an exact observed free-agent roster mutation", () => {
  const acquisition = command({
    action_type: "ADD_FREE_AGENT",
    parameters: {
      add_player_id: "LV",
      add_player_name: "Las Vegas. Raiders",
      add_player_search_name: "Las Vegas",
      add_player_position: "DEF",
      add_player_team: "LV",
      drop_player_id: "MIN",
      drop_player_name: "Minnesota Vikings",
      drop_player_search_name: "Minnesota Vikings",
      drop_player_position: "DEF",
      drop_player_team: "MIN",
      claim_priority: 1,
      faab_percent: 0,
      contingency: "Only while Sleeper shows an immediate add",
      observed_acquisition_type: "free_agent",
      expected_roster_before: ["p1", "MIN"],
      expected_roster_after: ["p1", "LV"],
    },
  });
  assert.equal(validateCommand(acquisition, config).action_type, "ADD_FREE_AGENT");
  assert.throws(
    () => validateCommand({
      ...acquisition,
      parameters: { ...acquisition.parameters, observed_acquisition_type: "waiver" },
    }, config),
    /observed Sleeper action type/,
  );
  assert.throws(
    () => validateCommand({
      ...acquisition,
      parameters: { ...acquisition.parameters, expected_roster_after: ["p1", "other", "LV"] },
    }, config),
    /exactly one roster acquisition/,
  );
  const waiver = {
    ...acquisition,
    action_type: "WAIVER_CLAIM",
    parameters: {
      ...acquisition.parameters,
      observed_acquisition_type: "waiver",
      transaction_week: 2,
    },
  };
  assert.equal(validateCommand(waiver, config).action_type, "WAIVER_CLAIM");
  assert.throws(
    () => validateCommand({
      ...waiver,
      parameters: { ...waiver.parameters, transaction_week: undefined },
    }, config),
    /transaction_week/,
  );
});

test("accepts exact IR state transitions and rejects ineligible moves", () => {
  const move = command({
    action_type: "MOVE_TO_IR",
    parameters: {
      player_id: "injured",
      player_name: "Injured Player",
      player_display_name: "I. Player",
      player_position: "WR",
      player_team: "MIA",
      observed_status: "OUT",
      expected_players: ["healthy", "injured"],
      expected_reserve_before: [],
      expected_reserve_after: ["injured"],
    },
  });
  assert.equal(validateCommand(move, config).action_type, "MOVE_TO_IR");
  assert.throws(
    () => validateCommand({ ...move, parameters: { ...move.parameters, observed_status: "Q" } }, config),
    /eligible/,
  );
});

test("accepts exact pending-waiver cancellation and reordering", () => {
  const first = {
    add_player_id: "a",
    add_player_name: "Add A",
    add_player_position: "WR",
    add_player_team: "MIA",
    drop_player_id: "x",
    drop_player_name: "Drop X",
    drop_player_position: "RB",
    drop_player_team: "TEN",
  };
  const second = {
    add_player_id: "b",
    add_player_name: "Add B",
    add_player_position: "RB",
    add_player_team: "DEN",
    drop_player_id: "y",
    drop_player_name: "Drop Y",
    drop_player_position: "WR",
    drop_player_team: "NYJ",
  };
  const cancel = command({
    action_type: "CANCEL_WAIVER_CLAIM",
    parameters: {
      ui_contract_version: "pending-waiver-browser-test",
      claim: second,
      expected_claims_before: [first, second],
      expected_claims_after: [first],
      management_reason: "The second claim no longer improves the roster.",
    },
  });
  assert.equal(validateCommand(cancel, config).action_type, "CANCEL_WAIVER_CLAIM");
  const reorder = command({
    action_type: "REORDER_WAIVER_CLAIMS",
    parameters: {
      ui_contract_version: "pending-waiver-browser-test",
      expected_claims_before: [first, second],
      expected_claims_after: [second, first],
      management_reason: "The second claim now has greater championship value.",
    },
  });
  assert.equal(validateCommand(reorder, config).action_type, "REORDER_WAIVER_CLAIMS");
  assert.throws(
    () => validateCommand({
      ...reorder,
      parameters: { ...reorder.parameters, expected_claims_after: [first, second] },
    }, config),
    /reorder/,
  );
});

test("requires a waiver command to belong to its exact fallback claim group", () => {
  const base = {
    add_player_id: "new",
    add_player_name: "New Player",
    add_player_search_name: "New Player",
    add_player_position: "WR",
    add_player_team: "MIA",
    drop_player_id: "old",
    drop_player_name: "Old Player",
    drop_player_search_name: "Old Player",
    drop_player_position: "RB",
    drop_player_team: "TEN",
    claim_priority: 1,
    faab_percent: 0,
    contingency: "Role remains confirmed",
    observed_acquisition_type: "waiver",
    transaction_week: 2,
    expected_roster_before: ["old"],
    expected_roster_after: ["new"],
  };
  const waiver = command({
    action_type: "WAIVER_CLAIM",
    parameters: {
      ...base,
      claim_group: [
        { add_player_id: "new", drop_player_id: "old", claim_priority: 1 },
        { add_player_id: "fallback", drop_player_id: "old", claim_priority: 2 },
      ],
    },
  });
  assert.equal(validateCommand(waiver, config).action_type, "WAIVER_CLAIM");
  assert.throws(
    () => validateCommand({
      ...waiver,
      parameters: {
        ...waiver.parameters,
        claim_group: [{ add_player_id: "other", drop_player_id: "old", claim_priority: 1 }],
      },
    }, config),
    /claim_group/,
  );
});

function tradeParameters(overrides = {}) {
  return {
    counterparty_roster_id: 10,
    counterparty_account_label: "other-manager",
    send_assets: [{
      player_id: "send-1",
      player_name: "Send Player",
      player_display_name: "S. Player",
      position: "WR",
      team: "IND",
    }],
    receive_assets: [{
      player_id: "receive-1",
      player_name: "Receive Player",
      player_display_name: "R. Player",
      position: "RB",
      team: "JAX",
    }],
    expected_our_roster_before: ["keep-1", "send-1"],
    expected_counterparty_roster_before: ["receive-1", "their-keep"],
    decision_reason: "Improves championship ceiling without creating a starter hole.",
    championship_case: "Adds a scarce upside role while preserving lineup depth.",
    upside_tier: "high",
    decision_confidence: 0.94,
    expiration_label: "24 Hours",
    ...overrides,
  };
}

test("accepts only exact, high-confidence trade proposals", () => {
  const proposal = command({
    action_type: "PROPOSE_TRADE",
    parameters: tradeParameters(),
  });
  assert.equal(validateCommand(proposal, config).action_type, "PROPOSE_TRADE");
  assert.throws(
    () => validateCommand({
      ...proposal,
      parameters: tradeParameters({ decision_confidence: 0.91 }),
    }, config),
    /0\.92 decision confidence/,
  );
  assert.throws(
    () => validateCommand({
      ...proposal,
      parameters: tradeParameters({ receive_assets: [{
        player_id: "not-theirs",
        player_name: "Wrong Player",
        player_display_name: "W. Player",
        position: "RB",
        team: "JAX",
      }] }),
    }, config),
    /conflict with the exact before rosters/,
  );
});

test("accepts an exact incoming trade response and validates the resulting roster", () => {
  const acceptance = command({
    action_type: "ACCEPT_TRADE",
    parameters: tradeParameters({
      offer_fingerprint: "other-manager:receive-1:send-1",
      expected_our_roster_after: ["keep-1", "receive-1"],
      expiration_label: undefined,
    }),
  });
  assert.equal(validateCommand(acceptance, config).action_type, "ACCEPT_TRADE");
  assert.throws(
    () => validateCommand({
      ...acceptance,
      parameters: { ...acceptance.parameters, expected_our_roster_after: ["keep-1"] },
    }, config),
    /exact exchange/,
  );
});
