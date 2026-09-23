import test from "node:test";
import assert from "node:assert/strict";
import {
  matchFreeAgentTransaction,
  matchWaiverTransaction,
  preflightAcquisition,
  preflightLineup,
  preflightTrade,
  verifyCommand,
} from "../src/verify.mjs";

const config = {
  leagueId: "league-1",
  ownerUserId: "owner-1",
  rosterId: 8,
  sleeperApiUrl: new URL("https://api.sleeper.app/v1/"),
};

test("verifies a lineup swap against the public roster", async () => {
  const result = await verifyCommand(
    {
      action_type: "SET_LINEUP",
      parameters: {
        from_player_id: "old",
        to_player_id: "new",
        expected_starters_after: ["new", "third"],
      },
    },
    config,
    async () =>
      new Response(
        JSON.stringify([
          { roster_id: 8, players: ["old", "new", "third"], starters: ["new", "third"] },
        ]),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
  );
  assert.equal(result.verified, true);
});

test("verifies starter-to-starter slot order, not just membership", async () => {
  const command = {
    action_type: "SET_LINEUP",
    parameters: {
      from_player_id: "first",
      to_player_id: "second",
      expected_starters_after: ["second", "first", "third"],
    },
  };
  const wrong = await verifyCommand(command, config, async () =>
    new Response(
      JSON.stringify([
        { roster_id: 8, players: ["first", "second", "third"], starters: ["first", "second", "third"] },
      ]),
      { status: 200, headers: { "content-type": "application/json" } },
    ));
  assert.equal(wrong.verified, false);
});

test("lineup preflight detects already-applied and changed states", async () => {
  const command = {
    action_type: "SET_LINEUP",
    parameters: {
      from_player_id: "old",
      to_player_id: "new",
      expected_starters_before: ["old", "third"],
      expected_starters_after: ["new", "third"],
    },
  };
  const already = await preflightLineup(command, config, async () =>
    new Response(
      JSON.stringify([
        { roster_id: 8, players: ["old", "new", "third"], starters: ["new", "third"] },
      ]),
      { status: 200, headers: { "content-type": "application/json" } },
    ));
  assert.equal(already.alreadyCommitted, true);

  await assert.rejects(
    () => preflightLineup(command, config, async () =>
      new Response(
        JSON.stringify([
          { roster_id: 8, players: ["old", "new", "third"], starters: ["third", "old"] },
        ]),
        { status: 200, headers: { "content-type": "application/json" } },
      )),
    /starters changed/,
  );
});

test("verifies the exact player, owner, slot, and pick number for a draft", async () => {
  const result = await verifyCommand(
    {
      action_type: "DRAFT_PLAYER",
      parameters: {
        draft_id: "123",
        player_id: "5859",
        player_name: "A.J. Brown",
        expected_pick_no: 27,
        owner_slot: 7,
      },
    },
    config,
    async () =>
      new Response(
        JSON.stringify([
          { pick_no: 27, player_id: "5859", picked_by: "owner-1", draft_slot: 7 },
        ]),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
  );
  assert.equal(result.verified, true);
});

test("verifies add/drop as one exact roster-state change", async () => {
  const commandTime = "2026-09-23T14:45:00.000Z";
  const result = await verifyCommand(
    {
      action_type: "ADD_FREE_AGENT",
      not_before: commandTime,
      parameters: {
        add_player_id: "new",
        drop_player_id: "old",
        transaction_week: 3,
        expected_roster_after: ["keep", "new"],
      },
    },
    config,
    async (url) => new Response(
      JSON.stringify(String(url).includes("/rosters")
        ? [{ roster_id: 8, players: ["new", "keep"], starters: [] }]
        : [{
          transaction_id: "fa-tx-1",
          type: "free_agent",
          status: "complete",
          creator: "owner-1",
          roster_ids: [8],
          adds: { new: 8 },
          drops: { old: 8 },
          created: Date.parse(commandTime) + 1_000,
        }]), {
        status: 200,
        headers: { "content-type": "application/json" },
      },
    ),
  );
  assert.equal(result.verified, true);
  assert.equal(result.transaction_id, "fa-tx-1");
  assert.equal(result.transaction_status, "complete");
});

test("free-agent matching requires an exact completed transaction after the command", () => {
  const command = {
    action_type: "ADD_FREE_AGENT",
    not_before: "2026-09-23T14:45:00.000Z",
    parameters: { add_player_id: "new", drop_player_id: "old" },
  };
  const transaction = {
    type: "free_agent",
    status: "complete",
    creator: "owner-1",
    roster_ids: [8],
    adds: { new: 8 },
    drops: { old: 8 },
    created: Date.parse(command.not_before) + 1_000,
  };
  assert.equal(matchFreeAgentTransaction(command, config, transaction), true);
  assert.equal(matchFreeAgentTransaction(command, config, {
    ...transaction,
    created: Date.parse(command.not_before) - 60_000,
  }), false);
  assert.equal(matchFreeAgentTransaction(command, config, {
    ...transaction,
    drops: { another: 8 },
  }), false);
});

test("acquisition preflight detects already-applied and changed rosters", async () => {
  const command = {
    action_type: "ADD_FREE_AGENT",
    not_before: "2026-09-23T14:45:00.000Z",
    parameters: {
      add_player_id: "new",
      drop_player_id: "old",
      transaction_week: 3,
      expected_roster_before: ["keep", "old"],
      expected_roster_after: ["keep", "new"],
    },
  };
  const already = await preflightAcquisition(command, config, async (url) => new Response(
    JSON.stringify(String(url).includes("/rosters")
      ? [{ roster_id: 8, players: ["new", "keep"], starters: [] }]
      : [{
        transaction_id: "fa-tx-2",
        type: "free_agent",
        status: "complete",
        creator: "owner-1",
        roster_ids: [8],
        adds: { new: 8 },
        drops: { old: 8 },
        created: Date.parse(command.not_before) + 1_000,
      }]),
    { status: 200, headers: { "content-type": "application/json" } },
  ));
  assert.equal(already.alreadyCommitted, true);

  await assert.rejects(
    () => preflightAcquisition(command, config, async () => new Response(
      JSON.stringify([{ roster_id: 8, players: ["keep", "other"], starters: [] }]),
      { status: 200, headers: { "content-type": "application/json" } },
    )),
    /roster changed/,
  );
});

const waiverCommand = {
  action_type: "WAIVER_CLAIM",
  parameters: {
    add_player_id: "new",
    drop_player_id: "old",
    transaction_week: 2,
    expected_roster_before: ["keep", "old"],
    expected_roster_after: ["keep", "new"],
  },
};

const pendingWaiver = {
  transaction_id: "tx-1",
  status: "pending",
  type: "waiver",
  creator: "owner-1",
  roster_ids: [8],
  adds: { new: 8 },
  drops: { old: 8 },
  created: 1_789_000_000_000,
};

test("matches only the exact owner waiver transaction", () => {
  assert.equal(matchWaiverTransaction(waiverCommand, config, pendingWaiver), true);
  assert.equal(
    matchWaiverTransaction(waiverCommand, config, { ...pendingWaiver, creator: "another-owner" }),
    false,
  );
  assert.equal(
    matchWaiverTransaction(waiverCommand, config, { ...pendingWaiver, drops: { other: 8 } }),
    false,
  );
  assert.equal(
    matchWaiverTransaction(waiverCommand, config, { ...pendingWaiver, status: "failed" }),
    false,
  );
});

test("verifies the exact pending waiver through Sleeper transactions", async () => {
  const result = await verifyCommand(
    waiverCommand,
    config,
    async (url) => {
      assert.match(String(url), /transactions\/2/);
      return new Response(JSON.stringify([pendingWaiver]), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    },
  );
  assert.equal(result.verified, true);
  assert.equal(result.transaction_id, "tx-1");
  assert.equal(result.transaction_status, "pending");
});

test("waiver preflight treats the exact pending transaction as idempotently committed", async () => {
  const result = await preflightAcquisition(waiverCommand, config, async (url) => {
    if (String(url).includes("/rosters")) {
      return new Response(
        JSON.stringify([{ roster_id: 8, players: ["keep", "old"], starters: [] }]),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }
    return new Response(JSON.stringify([pendingWaiver]), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  });
  assert.equal(result.alreadyCommitted, true);
  assert.equal(result.transaction_id, "tx-1");
});

test("trade preflight binds both complete rosters and detects an accepted exchange", async () => {
  const command = {
    action_type: "ACCEPT_TRADE",
    parameters: {
      counterparty_roster_id: 10,
      send_assets: [{ player_id: "send" }],
      receive_assets: [{ player_id: "receive" }],
      expected_our_roster_before: ["keep", "send"],
      expected_counterparty_roster_before: ["receive", "their-keep"],
      expected_our_roster_after: ["keep", "receive"],
    },
  };
  const ready = await preflightTrade(command, config, async () => new Response(
    JSON.stringify([
      { roster_id: 8, players: ["keep", "send"] },
      { roster_id: 10, players: ["receive", "their-keep"] },
    ]),
    { status: 200, headers: { "content-type": "application/json" } },
  ));
  assert.equal(ready.alreadyCommitted, false);

  const accepted = await preflightTrade(command, config, async () => new Response(
    JSON.stringify([
      { roster_id: 8, players: ["receive", "keep"] },
      { roster_id: 10, players: ["send", "their-keep"] },
    ]),
    { status: 200, headers: { "content-type": "application/json" } },
  ));
  assert.equal(accepted.alreadyCommitted, true);

  await assert.rejects(
    () => preflightTrade(command, config, async () => new Response(
      JSON.stringify([
        { roster_id: 8, players: ["keep", "other"] },
        { roster_id: 10, players: ["receive", "their-keep"] },
      ]),
      { status: 200, headers: { "content-type": "application/json" } },
    )),
    /roster changed/,
  );
});
