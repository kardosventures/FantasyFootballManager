import test from "node:test";
import assert from "node:assert/strict";
import { contentHash, preflightDraftPick, snakeSlot } from "../src/draft.mjs";

const config = {
  leagueId: "league-1",
  draftId: null,
  rosterId: 8,
  ownerUserId: "owner-1",
  sleeperApiUrl: new URL("https://api.sleeper.app/v1/"),
};

const draft = {
  draft_id: "123",
  league_id: "league-1",
  status: "drafting",
  type: "snake",
  settings: { teams: 12 },
  draft_order: { "owner-1": 7 },
  slot_to_roster_id: { "7": 8 },
};

function command(picks, overrides = {}) {
  return {
    expected_state_hash: contentHash(picks),
    parameters: {
      draft_id: "123",
      player_id: "5859",
      player_name: "A.J. Brown",
      player_position: "WR",
      player_team: "NE",
      expected_pick_no: picks.length + 1,
      owner_slot: 7,
    },
    ...overrides,
  };
}

function fetchSequence(...payloads) {
  let index = 0;
  return async () =>
    new Response(JSON.stringify(payloads[index++]), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
}

test("computes both directions of a snake draft", () => {
  assert.equal(snakeSlot(7, 12), 7);
  assert.equal(snakeSlot(18, 12), 7);
  assert.equal(snakeSlot(31, 12), 7);
});

test("allows one exact on-clock pick with an unchanged pick stream", async () => {
  const picks = Array.from({ length: 6 }, (_, index) => ({
    pick_no: index + 1,
    player_id: `p${index + 1}`,
  }));
  const result = await preflightDraftPick(
    command(picks),
    config,
    fetchSequence(draft, picks),
  );
  assert.equal(result.alreadyCommitted, false);
  assert.equal(result.pickCount, 6);
});

test("allows only the explicitly configured standalone mock draft", async () => {
  const picks = Array.from({ length: 6 }, (_, index) => ({
    pick_no: index + 1,
    player_id: `p${index + 1}`,
  }));
  const standaloneMock = {
    ...draft,
    league_id: null,
    slot_to_roster_id: { "7": 7 },
  };
  const result = await preflightDraftPick(
    command(picks),
    { ...config, draftId: "123" },
    fetchSequence(standaloneMock, picks),
  );
  assert.equal(result.alreadyCommitted, false);

  await assert.rejects(
    preflightDraftPick(
      command(picks),
      { ...config, draftId: "different-draft" },
      fetchSequence(standaloneMock, picks),
    ),
    /not attached to the configured Sleeper league/,
  );
});

test("blocks if another pick changes the expected state", async () => {
  const recommendedAt = Array.from({ length: 6 }, (_, index) => ({
    pick_no: index + 1,
    player_id: `p${index + 1}`,
  }));
  const now = [
    ...recommendedAt,
    { pick_no: 7, player_id: "someone-else", picked_by: "owner-1", draft_slot: 7 },
  ];
  await assert.rejects(
    preflightDraftPick(command(recommendedAt), config, fetchSequence(draft, now)),
    /already been used/,
  );
});

test("treats the already-recorded exact pick as idempotent", async () => {
  const picks = Array.from({ length: 7 }, (_, index) => ({
    pick_no: index + 1,
    player_id: index === 6 ? "5859" : `p${index + 1}`,
    picked_by: index === 6 ? "owner-1" : "other",
    draft_slot: index === 6 ? 7 : index + 1,
  }));
  const staleCommand = command(picks.slice(0, 6));
  const result = await preflightDraftPick(
    staleCommand,
    config,
    fetchSequence(draft, picks),
  );
  assert.equal(result.alreadyCommitted, true);
});
