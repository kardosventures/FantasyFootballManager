import { leaguePath } from "./session.mjs";

const CONFIRM_LABELS = /^(ok|confirm|submit|save|add|drop|claim|move)$/i;

async function navigate(page, config, route) {
  const target = leaguePath(config, route);
  if (page.url() !== target) await page.goto(target, { waitUntil: "domcontentloaded" });
  await page.locator("body").waitFor({ state: "visible", timeout: 15_000 });
  // A read snapshot or an interrupted prior preflight can leave a noncommitting
  // Sleeper dialog open. Clear it before locating a new semantic action control.
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const dialogs = page.locator('[role="alertdialog"]:visible');
    if (await dialogs.count() === 0) return;
    await page.keyboard.press("Escape").catch(() => {});
    await page.waitForTimeout(150);
  }
  if (await page.locator('[role="alertdialog"]:visible').count()) {
    throw new Error("Sleeper left an unrelated dialog open before action preflight");
  }
}

function exactPlayerRow(page, playerName) {
  return page
    .locator(".player-list-item")
    .filter({ has: page.getByText(playerName, { exact: true }) });
}

async function requireUniqueVisible(locator, label) {
  await locator.first().waitFor({ state: "visible", timeout: 10_000 }).catch(() => {});
  const visible = [];
  for (const candidate of await locator.all()) {
    if (await candidate.isVisible()) visible.push(candidate);
  }
  if (visible.length !== 1) {
    throw new Error(`Expected one visible ${label}; found ${visible.length}`);
  }
  return visible[0];
}

export function sleeperPlayerSearchName(playerName) {
  return String(playerName)
    .replace(/\s+(?:Jr\.?|Sr\.?|II|III|IV|V)$/i, "")
    .trim();
}

export function sleeperAcquisitionSearchName(playerName, position) {
  if (String(position).toUpperCase() === "DEF") {
    return String(playerName).split(".")[0].trim();
  }
  return sleeperPlayerSearchName(playerName);
}

export function lineupSlotLabel(slot, playerName) {
  return `Slot ${String(slot).toUpperCase()} - ${String(playerName).trim()}`;
}

function normalizedVisibleName(value) {
  return String(value ?? "").toLowerCase().replaceAll(/[^a-z0-9]/g, "");
}

export function matchesPendingWaiverRow(row, parameters, config) {
  const expectedAddPosition = `${parameters.add_player_position} - ${parameters.add_player_team}`;
  const expectedDropPosition = parameters.drop_player_id
    ? `${parameters.drop_player_position} - ${parameters.drop_player_team}`
    : "";
  return (
    String(row.teamName ?? "").trim() === String(config.teamLabel).trim()
    && String(row.ownerName ?? "").includes(String(config.accountLabel))
    && normalizedVisibleName(row.addName) === normalizedVisibleName(parameters.add_player_name)
    && String(row.addPosition ?? "").trim().toUpperCase() === expectedAddPosition.toUpperCase()
    && (
      !parameters.drop_player_id
      || (
        normalizedVisibleName(row.dropName) === normalizedVisibleName(parameters.drop_player_name)
        && String(row.dropPosition ?? "").trim().toUpperCase()
          === expectedDropPosition.toUpperCase()
      )
    )
  );
}

async function openWaiverPanel(page, config) {
  await navigate(page, config, "/team");
  let panel = page.locator(".waivers-panel");
  if (!(await panel.isVisible().catch(() => false))) {
    const waiverButton = page.getByText("WAIVER", { exact: true }).locator("..");
    await requireUniqueVisible(waiverButton, "My Waivers button");
    await waiverButton.click();
    panel = page.locator(".waivers-panel");
    await requireUniqueVisible(panel, "My Waivers panel");
  }
  return panel;
}

async function pendingWaiverRows(panel) {
  return panel.locator(".league-transaction-item-waiver").evaluateAll((items) =>
    items.map((row) => ({
      teamName: row.querySelector(".team-owner-with-description p.name")?.textContent ?? "",
      ownerName: row.querySelector(".team-owner-with-description .team-name")?.textContent ?? "",
      addName: row.querySelector(".add-player .name")?.textContent ?? "",
      addPosition: row.querySelector(".add-player .position")?.textContent ?? "",
      dropName: row.querySelector(".drop-player .name")?.textContent ?? "",
      dropPosition: row.querySelector(".drop-player .position")?.textContent ?? "",
      processesAt: row.querySelector(".waiver-clear")?.textContent ?? "",
    })),
  );
}

function exactPendingOrder(rows, expected, config) {
  return (
    rows.length === expected.length
    && rows.every((row, index) => matchesPendingWaiverRow(row, expected[index], config))
  );
}

function cssAttributeValue(value) {
  return String(value).replaceAll("\\", "\\\\").replaceAll('"', '\\"');
}

function exactSet(actual, expected) {
  if (!Array.isArray(actual) || actual.length !== expected.length) return false;
  const left = actual.map(String).sort();
  const right = expected.map(String).sort();
  return left.every((value, index) => value === right[index]);
}

function playerIds(container) {
  return container.locator('img[aria-label^="nfl Player "]').evaluateAll((items) =>
    items.map((item) => String(item.getAttribute("aria-label") ?? "").replace(/^nfl Player\s+/, "")),
  );
}

async function openTradeModal(page, config) {
  await navigate(page, config, "/team");
  await page.keyboard.press("Escape").catch(() => {});
  const trade = await requireUniqueVisible(
    page.getByText("TRADE", { exact: true }).locator(".."),
    "Trade button",
  );
  await trade.click({ noWaitAfter: true });
  return requireUniqueVisible(page.locator('[role="alertdialog"]'), "Sleeper trade dialog");
}

async function readTradeReview(modal, config) {
  const title = String(await modal.locator(".trade-title").textContent().catch(() => "")).trim();
  if (!/review trade offer/i.test(title)) return null;
  const rosters = modal.locator(".roster-trade-container");
  const count = await rosters.count();
  if (count !== 2) throw new Error(`Expected a two-team trade review; found ${count} teams`);
  let our = null;
  let counterparty = null;
  for (let index = 0; index < count; index += 1) {
    const roster = rosters.nth(index);
    const username = String(await roster.locator(".username").textContent().catch(() => "")).trim();
    if (username.includes(config.accountLabel)) our = roster;
    else counterparty = { username, roster };
  }
  if (!our || !counterparty?.username) throw new Error("Trade review did not identify both owners");
  const panels = our.locator(".roster-trade-summary .panel");
  if (await panels.count() !== 2) throw new Error("Trade review did not expose receives/sends panels");
  const receives = await playerIds(panels.nth(0));
  const sends = await playerIds(panels.nth(1));
  const footer = String(await modal.locator(".propose-trade-footer").innerText().catch(() => ""));
  return {
    counterparty_account_label: counterparty.username,
    receive_player_ids: receives,
    send_player_ids: sends,
    controls: footer.split(/\s+/).filter(Boolean),
    fingerprint: `${counterparty.username}:${receives.slice().sort().join(",")}:${sends.slice().sort().join(",")}`,
  };
}

function tradeMatches(review, parameters) {
  return Boolean(
    review
    && review.counterparty_account_label === parameters.counterparty_account_label
    && exactSet(review.receive_player_ids, parameters.receive_assets.map((row) => row.player_id))
    && exactSet(review.send_player_ids, parameters.send_assets.map((row) => row.player_id)),
  );
}

async function exactTradeAssetCard(list, asset) {
  const card = list.locator(".trade-center-player-box").filter({
    hasText: asset.player_display_name,
  });
  const exact = await requireUniqueVisible(card, `${asset.player_name} trade asset`);
  if (await exact.getByText(asset.player_display_name, { exact: true }).count() !== 1) {
    throw new Error(`${asset.player_name} trade asset display name is ambiguous`);
  }
  const text = String(await exact.innerText()).toUpperCase();
  if (!text.includes(`${asset.position} - ${asset.team}`.toUpperCase())) {
    throw new Error(`${asset.player_name} trade asset position/team does not match`);
  }
  return exact;
}

export async function prepareTradeProposal(page, config, parameters) {
  let modal = await openTradeModal(page, config);
  const title = String(await modal.locator(".trade-title").textContent().catch(() => "")).trim();
  if (title !== "Propose Trade") throw new Error("Sleeper trade center is not ready for a proposal");
  const partner = await requireUniqueVisible(
    modal.locator(".trade-partner-roster-item").filter({
      has: modal.getByText(parameters.counterparty_account_label, { exact: true }),
    }),
    `${parameters.counterparty_account_label} trade partner`,
  );
  await partner.click({ noWaitAfter: true });
  await modal.getByText("NEXT", { exact: true }).last().evaluate((element) => element.click());
  await modal.getByText("Add Trade Assets", { exact: true }).waitFor({ state: "visible" });
  const lists = modal.locator(".trade-center-asset-select-list");
  if (await lists.count() !== 2) throw new Error("Sleeper did not expose exactly two trade rosters");
  const ownLabel = String(await lists.nth(0).locator(".display-name").first().textContent().catch(() => ""));
  const partnerLabel = String(await lists.nth(1).locator(".display-name").first().textContent().catch(() => ""));
  if (!ownLabel.includes(config.accountLabel) || partnerLabel.trim() !== parameters.counterparty_account_label) {
    throw new Error("Sleeper trade asset columns do not match the exact owners");
  }
  for (const asset of parameters.send_assets) {
    await (await exactTradeAssetCard(lists.nth(0), asset)).click({ noWaitAfter: true });
  }
  for (const asset of parameters.receive_assets) {
    await (await exactTradeAssetCard(lists.nth(1), asset)).click({ noWaitAfter: true });
  }
  await modal.getByText("NEXT", { exact: true }).last().evaluate((element) => element.click());
  await modal.getByText("Review Trade Offer", { exact: true }).waitFor({ state: "visible" });
  const expiration = modal.getByText("Trade expires in", { exact: true })
    .locator("..")
    .locator(".app-dropdown");
  const dropdown = await requireUniqueVisible(expiration, "trade expiration selector");
  await dropdown.locator(".selected-value").click();
  await dropdown.locator(".app-dropdown-item .name")
    .filter({ hasText: new RegExp(`^${parameters.expiration_label}$`, "i") })
    .click();
  const selectedExpiration = String(await dropdown.locator(".selected-value").textContent()).trim();
  if (selectedExpiration.toLowerCase() !== parameters.expiration_label.toLowerCase()) {
    throw new Error("Sleeper trade expiration does not match the command");
  }
  const review = await readTradeReview(modal, config);
  if (!tradeMatches(review, parameters)) throw new Error("Final Sleeper trade summary is not exact");
  const submit = await requireUniqueVisible(
    modal.getByText("SEND TRADE", { exact: true }),
    "Send Trade control",
  );
  let committed = false;
  return {
    evidence: {
      exact_partner_confirmed: true,
      exact_assets_confirmed: true,
      bounded_expiration_confirmed: true,
      offer_fingerprint: review.fingerprint,
      expiration_label: selectedExpiration,
    },
    async commit() {
      if (committed) throw new Error("Trade proposal was already committed");
      committed = true;
      await submit.click({ noWaitAfter: true });
    },
    async abort() {
      if (committed) return;
      await page.keyboard.press("Escape").catch(() => {});
    },
  };
}

async function openExactTradeReview(page, config, parameters) {
  let modal = await openTradeModal(page, config);
  let review = await readTradeReview(modal, config);
  if (tradeMatches(review, parameters)) return { modal, review };
  const viewControls = modal.getByText("VIEW", { exact: true });
  const count = await viewControls.count();
  for (let index = 0; index < count; index += 1) {
    await viewControls.nth(index).click({ noWaitAfter: true });
    await page.waitForTimeout(300);
    modal = page.locator('[role="alertdialog"]').last();
    review = await readTradeReview(modal, config);
    if (tradeMatches(review, parameters)) return { modal, review };
    await page.keyboard.press("Escape").catch(() => {});
    modal = await openTradeModal(page, config);
  }
  throw new Error("The exact incoming trade offer is no longer visible");
}

export async function prepareTradeResponse(page, config, parameters, actionLabel) {
  const { modal, review } = await openExactTradeReview(page, config, parameters);
  if (review.fingerprint !== parameters.offer_fingerprint) {
    throw new Error("The incoming trade offer changed after the manager decision");
  }
  const action = await requireUniqueVisible(
    modal.getByText(new RegExp(`^${actionLabel}( TRADE)?$`, "i")),
    `${actionLabel} trade control`,
  );
  let committed = false;
  return {
    evidence: {
      exact_partner_confirmed: true,
      exact_assets_confirmed: true,
      offer_fingerprint: review.fingerprint,
    },
    async commit() {
      if (committed) throw new Error("Trade response was already committed");
      committed = true;
      await action.click({ noWaitAfter: true });
    },
    async abort() {
      if (committed) return;
      await page.keyboard.press("Escape").catch(() => {});
    },
  };
}

export async function verifyTradeOfferUi(page, config, parameters, expectedPresent) {
  try {
    const { review } = await openExactTradeReview(page, config, parameters);
    const present = tradeMatches(review, parameters);
    await page.keyboard.press("Escape").catch(() => {});
    return {
      verified: expectedPresent ? present : !present,
      message: expectedPresent
        ? "Sleeper confirmed the exact active trade offer"
        : "Sleeper still shows the declined trade offer",
      verification_channel: "authenticated_trade_ui",
      offer_fingerprint: review?.fingerprint ?? null,
    };
  } catch (error) {
    return {
      verified: !expectedPresent,
      message: expectedPresent
        ? error.message
        : "Sleeper no longer shows the exact declined trade offer",
      verification_channel: "authenticated_trade_ui",
    };
  }
}

async function lineupControl(page, playerId, playerName, slot) {
  const playerMarker = page.locator(
    `[aria-label="${cssAttributeValue(`nfl Player ${playerId}`)}"]`,
  );
  const row = await requireUniqueVisible(
    page.locator(".team-roster-item").filter({ has: playerMarker }),
    `${playerName} lineup row`,
  );
  return requireUniqueVisible(
    row.locator(
      `[aria-label="${cssAttributeValue(lineupSlotLabel(slot, playerName))}"]`,
    ),
    `${playerName} ${slot} position control`,
  );
}

async function setLineup(page, config, parameters) {
  await navigate(page, config, "/team");
  const incoming = await lineupControl(
    page,
    parameters.to_player_id,
    parameters.to_player_name,
    parameters.to_slot,
  );
  const outgoing = await lineupControl(
    page,
    parameters.from_player_id,
    parameters.from_player_name,
    parameters.from_slot,
  );
  await incoming.click();
  await outgoing.click();
}

async function draftPlayer(page, parameters) {
  const target = `https://sleeper.com/draft/nfl/${encodeURIComponent(parameters.draft_id)}`;
  if (page.url() !== target) await page.goto(target, { waitUntil: "domcontentloaded" });
  const grid = page.getByRole("grid");
  await grid.waitFor({ state: "visible", timeout: 15_000 });
  const search = page.getByRole("textbox", { name: /find player/i });
  await requireUniqueVisible(search, "draft player search");
  const searchName = sleeperPlayerSearchName(parameters.player_name);
  await search.fill(searchName);

  const row = await requireUniqueVisible(
    grid.locator(".player-rank-item2").filter({ hasText: searchName }),
    `${parameters.player_name} draft row`,
  );
  const rowText = (await row.innerText()).toUpperCase();
  if (!rowText.includes(String(parameters.player_position).toUpperCase())) {
    throw new Error("Draft row position does not match the recommendation");
  }
  if (
    parameters.player_team &&
    String(parameters.player_team).toUpperCase() !== "FA" &&
    !rowText.includes(String(parameters.player_team).toUpperCase())
  ) {
    throw new Error("Draft row team does not match the recommendation");
  }
  const submit = await requireUniqueVisible(
    row.locator(".draft-button"),
    `${parameters.player_name} draft button`,
  );
  await submit.click();
}

async function confirmModal(page, expectedPlayerName) {
  const modal = await requireUniqueVisible(
    page.locator('[role="dialog"], .modal, .ReactModal__Content'),
    "Sleeper confirmation dialog",
  );
  const modalText = (await modal.innerText()).toLowerCase();
  if (!modalText.includes(String(expectedPlayerName).toLowerCase())) {
    throw new Error("Sleeper confirmation dialog does not match the exact player");
  }
  const confirm = modal.getByRole("button", { name: CONFIRM_LABELS }).last();
  await requireUniqueVisible(confirm, "confirmation button");
  await confirm.click();
}

async function playerAction(page, config, parameters, actionLabel) {
  const playerName = parameters.player_display_name ?? parameters.player_name;
  await navigate(page, config, "/team");
  const row = await requireUniqueVisible(exactPlayerRow(page, playerName), `${playerName} roster row`);
  const rowText = (await row.innerText()).toUpperCase();
  if (parameters.player_position && parameters.player_team) {
    const expectedIdentity = `${parameters.player_position} - ${parameters.player_team}`.toUpperCase();
    if (!rowText.includes(expectedIdentity)) {
      throw new Error("Sleeper roster row position/team does not match the command");
    }
  }
  const trigger = row.locator(".player-action-button:not(.disabled)");
  await requireUniqueVisible(trigger, `${playerName} action button`);
  await trigger.click();
  const action = await requireUniqueVisible(
    page.getByText(actionLabel, { exact: true }),
    `${actionLabel} action`,
  );
  await action.click();
  await confirmModal(page, parameters.player_name);
}

export async function prepareAcquisitionAction(page, config, parameters, label) {
  await navigate(page, config, "/players");
  const search = page.getByPlaceholder(/find player/i);
  await requireUniqueVisible(search, "player search");
  await search.fill(
    sleeperAcquisitionSearchName(
      parameters.add_player_search_name,
      parameters.add_player_position,
    ),
  );
  const row = await requireUniqueVisible(
    exactPlayerRow(page, parameters.add_player_name),
    `${parameters.add_player_name} player row`,
  );
  const rowText = (await row.innerText()).toUpperCase();
  const expectedIdentity = `${parameters.add_player_position} - ${parameters.add_player_team}`
    .toUpperCase();
  if (!rowText.includes(expectedIdentity)) {
    throw new Error("Sleeper acquisition row position/team does not match the command");
  }
  const expectedClass = label === "Add" ? "add" : "waiver";
  const trigger = row.locator(`.player-action-button.${expectedClass}:not(.disabled)`);
  await requireUniqueVisible(trigger, `${parameters.add_player_name} action button`);
  await trigger.click();

  const modal = await requireUniqueVisible(
    page.locator('[role="alertdialog"]'),
    "Sleeper acquisition dialog",
  );
  const modalText = (await modal.innerText()).toUpperCase();
  if (!modalText.includes(expectedIdentity)) {
    throw new Error("Sleeper acquisition dialog position/team does not match the command");
  }
  if (parameters.drop_player_name) {
    const dropSearchName = parameters.drop_player_search_name;
    const dropChoice = await requireUniqueVisible(
      modal.getByRole("link").filter({ hasText: dropSearchName }),
      `${dropSearchName} drop choice`,
    );
    if (await dropChoice.getByText(dropSearchName, { exact: true }).count() !== 1) {
      throw new Error("Sleeper drop choice does not contain the exact full player name");
    }
    const dropText = (await dropChoice.innerText()).toUpperCase();
    const expectedDropIdentity =
      `${parameters.drop_player_position} - ${parameters.drop_player_team}`.toUpperCase();
    if (!dropText.includes(expectedDropIdentity)) {
      throw new Error("Sleeper drop row position/team does not match the command");
    }
    await dropChoice.click();
  }
  const submitName = label === "Add"
    ? /^Add Player$/i
    : /^(Claim|Claim Player|Submit Claim|Make Waiver Claim)$/i;
  const submit = modal.getByRole("button", { name: submitName });
  const submitButton = await requireUniqueVisible(submit, `${label} Player confirmation button`);
  if (await submitButton.isDisabled()) {
    throw new Error(`${label} Player confirmation button is disabled`);
  }
  let committed = false;
  return {
    evidence: {
      add_player_name: parameters.add_player_name,
      add_player_position: parameters.add_player_position,
      add_player_team: parameters.add_player_team,
      acquisition_type: label === "Add" ? "free_agent" : "waiver",
      drop_player_name: parameters.drop_player_name || null,
      row_identity_confirmed: true,
      dialog_identity_confirmed: true,
      exact_drop_confirmed: true,
      submit_control_enabled: true,
    },
    async commit() {
      if (committed) throw new Error("Acquisition dialog was already committed");
      await requireUniqueVisible(submit, `${label} Player confirmation button`);
      if (await submitButton.isDisabled()) {
        throw new Error(`${label} Player confirmation button became disabled`);
      }
      committed = true;
      await submitButton.click();
    },
    async abort() {
      if (committed) return;
      await page.keyboard.press("Escape").catch(() => {});
      if (await modal.isVisible().catch(() => false)) {
        const cancel = modal.getByRole("button", { name: /^(cancel|close)$/i });
        if (await cancel.count()) await cancel.last().click();
      }
      await modal.waitFor({ state: "hidden", timeout: 3_000 });
    },
  };
}

export async function verifyPendingWaiverUi(page, config, parameters) {
  const panel = await openWaiverPanel(page, config);
  try {
    const rows = await pendingWaiverRows(panel);
    const matches = rows.filter((row) => matchesPendingWaiverRow(row, parameters, config));
    if (matches.length !== 1) {
      return {
        verified: false,
        message: `Expected one exact authenticated pending waiver; found ${matches.length}`,
        verification_channel: "authenticated_pending_ui",
      };
    }
    return {
      verified: true,
      message: `Sleeper My Waivers confirmed pending ${parameters.add_player_name} for ${parameters.drop_player_name}`,
      verification_channel: "authenticated_pending_ui",
      transaction_status: "pending",
      add_player_name: parameters.add_player_name,
      drop_player_name: parameters.drop_player_name || null,
      processes_at: String(matches[0].processesAt).replaceAll(/\s+/g, " ").trim(),
    };
  } finally {
    await page.keyboard.press("Escape").catch(() => {});
  }
}

export async function verifyPendingWaiverStateUi(page, config, expectedClaims) {
  const panel = await openWaiverPanel(page, config);
  try {
    const rows = await pendingWaiverRows(panel);
    const verified = exactPendingOrder(rows, expectedClaims, config);
    return {
      verified,
      message: verified
        ? `Sleeper My Waivers confirmed the exact ${rows.length}-claim state`
        : "Sleeper My Waivers does not match the exact expected claim state",
      verification_channel: "authenticated_pending_ui",
      transaction_status: "pending_queue",
      observed_claim_count: rows.length,
    };
  } finally {
    await page.keyboard.press("Escape").catch(() => {});
  }
}

async function interactiveSleeperAlert(page) {
  const alerts = page.locator(".alert-modal");
  for (const alert of await alerts.all()) {
    if (!(await alert.isVisible().catch(() => false))) continue;
    const interactive = await alert.evaluate((element) => {
      const style = getComputedStyle(element);
      return style.opacity !== "0" && style.visibility !== "hidden" && style.zIndex !== "-1";
    });
    if (interactive) return alert;
  }
  return null;
}

export async function preparePendingWaiverCancellation(page, config, parameters) {
  const panel = await openWaiverPanel(page, config);
  const rows = await pendingWaiverRows(panel);
  if (!exactPendingOrder(rows, parameters.expected_claims_before, config)) {
    throw new Error("Pending waiver state changed before cancellation");
  }
  const indexes = rows
    .map((row, index) => (matchesPendingWaiverRow(row, parameters.claim, config) ? index : -1))
    .filter((index) => index >= 0);
  if (indexes.length !== 1) throw new Error(`Expected one exact pending claim; found ${indexes.length}`);
  // Sleeper renders the Cancel control as a sibling of the identity row inside
  // the nearest transaction card, not as a child of the waiver row itself.
  const row = panel.locator(".league-transaction-item-waiver").nth(indexes[0]);
  const item = row.locator(
    "xpath=ancestor::div[contains(concat(' ', normalize-space(@class), ' '), ' trans-item ')][1]",
  );
  await requireUniqueVisible(item, "exact pending waiver transaction card");
  const cancel = await requireUniqueVisible(
    item.locator(".action-btns"),
    "pending waiver Cancel control",
  );
  if ((await cancel.innerText()).trim().toLowerCase() !== "cancel") {
    throw new Error("Pending waiver action control is not the exact Cancel control");
  }
  let committed = false;
  return {
    evidence: {
      pending_claim_identity_confirmed: true,
      pending_queue_order_confirmed: true,
      cancel_control_confirmed: true,
      add_player_name: parameters.claim.add_player_name,
      drop_player_name: parameters.claim.drop_player_name || null,
    },
    async commit() {
      if (committed) throw new Error("Pending waiver cancellation was already committed");
      committed = true;
      await cancel.click();
      await page.waitForTimeout(200);
      const alert = await interactiveSleeperAlert(page);
      if (alert) {
        const confirm = await requireUniqueVisible(
          alert.getByText("Ok", { exact: true }),
          "pending waiver cancellation confirmation",
        );
        await confirm.click();
      }
    },
    async abort() {
      if (committed) return;
      await page.keyboard.press("Escape").catch(() => {});
    },
  };
}

async function cancelPendingWaiver(page, config, parameters) {
  const prepared = await preparePendingWaiverCancellation(page, config, parameters);
  await prepared.commit();
}

async function reorderPendingWaivers(page, config, parameters) {
  const panel = await openWaiverPanel(page, config);
  let rows = await pendingWaiverRows(panel);
  if (!exactPendingOrder(rows, parameters.expected_claims_before, config)) {
    throw new Error("Pending waiver state changed before reordering");
  }
  for (let targetIndex = 0; targetIndex < parameters.expected_claims_after.length; targetIndex += 1) {
    rows = await pendingWaiverRows(panel);
    const desired = parameters.expected_claims_after[targetIndex];
    const sourceIndex = rows.findIndex((row) => matchesPendingWaiverRow(row, desired, config));
    if (sourceIndex < 0) throw new Error("A claim disappeared while reordering waivers");
    if (sourceIndex === targetIndex) continue;
    const items = panel.locator(".league-transaction-item-waiver");
    await items.nth(sourceIndex).dragTo(items.nth(targetIndex));
  }
}

async function addPlayer(page, config, parameters, label) {
  const prepared = await prepareAcquisitionAction(page, config, parameters, label);
  await prepared.commit();
}

export async function executeUiAction(page, config, command) {
  const parameters = command.parameters;
  switch (command.action_type) {
    case "DRAFT_PLAYER":
      return draftPlayer(page, parameters);
    case "DROP_PLAYER":
      return playerAction(page, config, parameters, "Drop");
    case "MOVE_TO_IR":
      return playerAction(page, config, parameters, "Move to IR");
    case "REMOVE_FROM_IR":
      return playerAction(page, config, parameters, "Remove from IR");
    case "ADD_FREE_AGENT":
      return addPlayer(page, config, parameters, "Add");
    case "WAIVER_CLAIM":
      return addPlayer(page, config, parameters, "Claim");
    case "CANCEL_WAIVER_CLAIM":
      return cancelPendingWaiver(page, config, parameters);
    case "REORDER_WAIVER_CLAIMS":
      return reorderPendingWaivers(page, config, parameters);
    case "PROPOSE_TRADE": {
      const prepared = await prepareTradeProposal(page, config, parameters);
      return prepared.commit();
    }
    case "ACCEPT_TRADE": {
      const prepared = await prepareTradeResponse(page, config, parameters, "ACCEPT");
      return prepared.commit();
    }
    case "DECLINE_TRADE": {
      const prepared = await prepareTradeResponse(page, config, parameters, "DECLINE");
      return prepared.commit();
    }
    case "SET_LINEUP":
      return setLineup(page, config, parameters);
    case "SET_AUTOSUB":
      throw new Error(`${command.action_type} selector contract awaits post-draft qualification`);
    default:
      throw new Error(`Unsupported action: ${command.action_type}`);
  }
}
