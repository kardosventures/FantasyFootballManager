import { createHash } from "node:crypto";

function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`)
      .join(",")}}`;
  }
  return JSON.stringify(value);
}

export function contentHash(value) {
  return createHash("sha256").update(canonicalJson(value)).digest("hex");
}

export function snakeSlot(pickNo, teams) {
  const roundIndex = Math.floor((pickNo - 1) / teams);
  const index = (pickNo - 1) % teams;
  return roundIndex % 2 === 0 ? index + 1 : teams - index;
}

async function sleeperJson(config, route, fetchImpl) {
  const endpoint = new URL(route, config.sleeperApiUrl);
  endpoint.searchParams.set("fresh", `${Date.now()}-${Math.random()}`);
  const response = await fetchImpl(endpoint, {
    method: "GET",
    headers: { "cache-control": "no-cache" },
    signal: AbortSignal.timeout(10_000),
  });
  if (!response.ok) throw new Error(`Sleeper draft preflight returned HTTP ${response.status}`);
  return response.json();
}

function ownedPick(pick, config, ownerSlot) {
  return (
    String(pick.picked_by ?? "") === String(config.ownerUserId) &&
    Number(pick.draft_slot) === ownerSlot
  );
}

export async function preflightDraftPick(command, config, fetchImpl = fetch) {
  const parameters = command.parameters;
  const draftId = String(parameters.draft_id);
  const draft = await sleeperJson(config, `draft/${draftId}`, fetchImpl);
  const picks = await sleeperJson(config, `draft/${draftId}/picks`, fetchImpl);
  if (!draft || !Array.isArray(picks)) throw new Error("Sleeper returned malformed draft state");
  const isConfiguredLeagueDraft =
    String(draft.league_id ?? "") === String(config.leagueId);
  const isExplicitStandaloneMock =
    draft.league_id == null &&
    String(config.draftId ?? "") !== "" &&
    draftId === String(config.draftId);
  if (!isConfiguredLeagueDraft && !isExplicitStandaloneMock) {
    throw new Error("Draft is not attached to the configured Sleeper league");
  }
  if (String(draft.type ?? "snake") !== "snake") {
    throw new Error("Only snake drafts are qualified for automatic picks");
  }

  const expectedPickNo = Number(parameters.expected_pick_no);
  const ownerSlot = Number(parameters.owner_slot);
  const existing = picks.find((pick) => Number(pick.pick_no) === expectedPickNo);
  if (existing) {
    if (
      String(existing.player_id) === String(parameters.player_id) &&
      ownedPick(existing, config, ownerSlot)
    ) {
      return {
        alreadyCommitted: true,
        draftStatus: draft.status,
        pickCount: picks.length,
        picksHash: contentHash(picks),
      };
    }
    throw new Error(`Expected pick #${expectedPickNo} has already been used`);
  }

  if (draft.status !== "drafting") throw new Error("Draft is not actively drafting");
  const teams = Number(draft.settings?.teams ?? 0);
  if (!Number.isInteger(teams) || teams < 2) throw new Error("Draft team count is invalid");
  const sequential = picks.every((pick, index) => Number(pick.pick_no) === index + 1);
  if (!sequential) throw new Error("Draft pick stream is not sequential");
  if (picks.length + 1 !== expectedPickNo) throw new Error("Draft advanced after recommendation");
  if (contentHash(picks) !== command.expected_state_hash) {
    throw new Error("Draft pick stream changed after recommendation");
  }

  const configuredSlot = Number(draft.draft_order?.[config.ownerUserId]);
  if (configuredSlot !== ownerSlot) throw new Error("Owner draft slot no longer matches");
  if (snakeSlot(expectedPickNo, teams) !== ownerSlot) throw new Error("Your team is not on the clock");
  const rosterId = draft.slot_to_roster_id?.[String(ownerSlot)];
  if (
    !isExplicitStandaloneMock &&
    rosterId !== undefined &&
    Number(rosterId) !== Number(config.rosterId)
  ) {
    throw new Error("Draft slot is not attached to the configured roster");
  }
  if (picks.some((pick) => String(pick.player_id) === String(parameters.player_id))) {
    throw new Error("Recommended player has already been drafted");
  }
  return {
    alreadyCommitted: false,
    draftStatus: draft.status,
    pickCount: picks.length,
    picksHash: command.expected_state_hash,
  };
}
