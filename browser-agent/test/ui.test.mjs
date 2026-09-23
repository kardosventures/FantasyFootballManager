import test from "node:test";
import assert from "node:assert/strict";
import {
  lineupSlotLabel,
  matchesPendingWaiverRow,
  sleeperAcquisitionSearchName,
  sleeperPlayerSearchName,
} from "../src/ui.mjs";
import {
  attachTargetIdentity,
  fetchCurrentNflPeriod,
  mergeFreeAgentSnapshots,
  parseFreeAgentRow,
  parseLineupRow,
  parseMatchupPlayer,
} from "../src/session.mjs";

test("uses Sleeper's authoritative current week instead of display_week", async () => {
  const period = await fetchCurrentNflPeriod(
    { sleeperApiUrl: new URL("https://api.sleeper.app/v1/") },
    async () => ({
      ok: true,
      json: async () => ({ season: "2026", week: 2, display_week: 1 }),
    }),
  );
  assert.deepEqual(period, { season: "2026", week: 2, display_week: 1 });
});

test("rejects an unprovable Sleeper observation period", async () => {
  await assert.rejects(
    fetchCurrentNflPeriod(
      { sleeperApiUrl: new URL("https://api.sleeper.app/v1/") },
      async () => ({ ok: true, json: async () => ({ season: "2026", display_week: 1 }) }),
    ),
    /valid season or week/,
  );
});

test("uses Sleeper's suffix-free player search names", () => {
  assert.equal(sleeperPlayerSearchName("Chris Godwin Jr."), "Chris Godwin");
  assert.equal(sleeperPlayerSearchName("Marvin Harrison Jr"), "Marvin Harrison");
  assert.equal(sleeperPlayerSearchName("Brian Robinson Sr."), "Brian Robinson");
  assert.equal(sleeperPlayerSearchName("Odell Beckham III"), "Odell Beckham");
  assert.equal(sleeperPlayerSearchName("A.J. Brown"), "A.J. Brown");
});

test("builds the exact accessible lineup destination label", () => {
  assert.equal(lineupSlotLabel("flex", "Jalen Coker"), "Slot FLEX - Jalen Coker");
});

test("uses Sleeper's city label to search for a defense", () => {
  assert.equal(sleeperAcquisitionSearchName("Las Vegas. Raiders", "DEF"), "Las Vegas");
  assert.equal(sleeperAcquisitionSearchName("Devaughn Vele", "WR"), "Devaughn Vele");
});

test("matches only the exact authenticated pending waiver row", () => {
  const parameters = {
    add_player_name: "C. Douglas",
    add_player_position: "WR",
    add_player_team: "MIA",
    drop_player_id: "9508",
    drop_player_name: "T Spears",
    drop_player_position: "RB",
    drop_player_team: "TEN",
  };
  const config = { teamLabel: "Jim.ai", accountLabel: "jimkardos" };
  const row = {
    teamName: "Jim.ai",
    ownerName: "jimkardos",
    addName: "C. Douglas",
    addPosition: "WR - MIA",
    dropName: "T. Spears",
    dropPosition: "RB - TEN",
  };
  assert.equal(matchesPendingWaiverRow(row, parameters, config), true);
  assert.equal(
    matchesPendingWaiverRow({ ...row, dropPosition: "RB - JAX" }, parameters, config),
    false,
  );
  assert.equal(
    matchesPendingWaiverRow({ ...row, teamName: "Another Team" }, parameters, config),
    false,
  );
});

test("parses a semantic Sleeper lineup row and its projection", () => {
  assert.deepEqual(
    parseLineupRow({
      label: "Slot FLEX - Michael Pittman",
      playerAlt: "nfl Player 6819",
      text: "FLEX M Pittman WR - PIT Sun 11:00 AM 84% 16% - 8.67",
    }),
    {
      slot: "FLEX",
      player_name: "Michael Pittman",
      player_id: "6819",
      projected_points: 8.67,
      visible_text: "FLEX M Pittman WR - PIT Sun 11:00 AM 84% 16% - 8.67",
    },
  );
  assert.equal(parseLineupRow({ label: "N/A", playerAlt: "nfl Player 1", text: "" }), null);
});

test("parses a locked in-game lineup row without an editable Slot label", () => {
  assert.deepEqual(
    parseLineupRow({
      label: "N/A",
      playerAlt: "nfl Player 11564",
      playerName: "D Maye",
      slot: "QB",
      text: "QB D Maye 2.04 16.38",
    }),
    {
      slot: "QB",
      player_name: "D Maye",
      player_id: "11564",
      projected_points: 16.38,
      visible_text: "QB D Maye 2.04 16.38",
    },
  );
});

test("parses a Sleeper free-agent projection row without inventing an id", () => {
  assert.deepEqual(
    parseFreeAgentRow({
      name: "D. Boston",
      positionText: "WR - CLE(11)",
      status: "Q",
      actionClass: "link-button player-action-button waiver",
      cells: ["6", "0", "1", "-", "3", "5", "38", "0"],
    }),
    {
      display_name: "D. Boston",
      position: "WR",
      team: "CLE",
      injury_status: "Q",
      acquisition_type: "waiver",
      projected_points: 6,
      projected_stat_cells: ["0", "1", "-", "3", "5", "38", "0"],
    },
  );
});

test("attaches a backend player id only after name, position, and team match", () => {
  const row = {
    display_name: "D. Boston",
    position: "WR",
    team: "CLE",
    acquisition_type: "waiver",
    projected_points: 11.2,
  };
  assert.equal(
    attachTargetIdentity(
      row,
      { player_id: "wr-1", name: "Denzel Boston", position: "WR", team: "CLE" },
      "2026-09-14T12:00:00Z",
    ).player_id,
    "wr-1",
  );
  assert.equal(
    attachTargetIdentity(
      row,
      { player_id: "wr-2", name: "Denzel Boston", position: "WR", team: "JAX" },
      "2026-09-14T12:00:00Z",
    ),
    null,
  );
});

test("targeted free-agent observations replace anonymous list rows", () => {
  const anonymous = {
    display_name: "D. Boston",
    position: "WR",
    team: "CLE",
    projected_points: 10,
  };
  const targeted = { ...anonymous, player_id: "wr-1", projected_points: 11.2 };
  const merged = mergeFreeAgentSnapshots(
    { observed_at: "base", observed_row_count: 1, players: [anonymous] },
    { target_count: 1, players: [targeted] },
  );
  assert.equal(merged.players.length, 1);
  assert.equal(merged.players[0].player_id, "wr-1");
  assert.equal(merged.players[0].projected_points, 11.2);
  assert.equal(merged.targeted_player_count, 1);
});

test("parses matchup player and defense ids from Sleeper image URLs", () => {
  assert.deepEqual(
    parseMatchupPlayer({
      imageUrl: "https://sleepercdn.com/content/nfl/players/11564.jpg",
      name: "D. Maye",
      positionText: "QB - NE",
      projection: "19.67",
    }),
    {
      player_id: "11564",
      display_name: "D. Maye",
      position: "QB",
      team: "NE",
      projected_points: 19.67,
    },
  );
  assert.equal(
    parseMatchupPlayer({
      imageUrl: "https://sleepercdn.com/images/team_logos/nfl/phi.png",
      name: "PHI",
      positionText: "DEF - PHI",
      projection: "8.71",
    }).player_id,
    "PHI",
  );
});
