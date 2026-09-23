import { chromium } from "playwright-core";
import { spawn } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";

function leaguePath(config, route = "") {
  const base = new URL(config.leagueUrl);
  base.pathname = `/leagues/${config.leagueId}${route}`;
  base.search = "";
  base.hash = "";
  return base.toString();
}

function positiveWeek(value) {
  const week = Number.parseInt(String(value ?? ""), 10);
  return Number.isInteger(week) && week >= 1 && week <= 18 ? week : null;
}

export async function fetchCurrentNflPeriod(config, fetchImpl = fetch) {
  const response = await fetchImpl(new URL("state/nfl", config.sleeperApiUrl), {
    signal: AbortSignal.timeout(5_000),
  });
  if (!response.ok) throw new Error(`Sleeper NFL state returned HTTP ${response.status}`);
  const state = await response.json();
  const week = positiveWeek(state?.week);
  const season = String(state?.season ?? "").trim();
  if (!week || !/^\d{4}$/.test(season)) {
    throw new Error("Sleeper NFL state omitted a valid season or week");
  }
  return {
    season,
    week,
    display_week: positiveWeek(state?.display_week),
  };
}

async function selectRosterWeek(page, week) {
  const target = positiveWeek(week);
  if (!target) throw new Error("A valid target week is required for roster observations");
  const dropdown = page.locator(".week-selector-dropdown").first();
  const selected = dropdown.locator(":scope > .label .text");
  await selected.waitFor({ state: "visible", timeout: 10_000 });
  if (String(await selected.textContent()).trim() !== `Wk. ${target}`) {
    const previousFirstRow = await page.locator(".team-roster-item").first().innerText().catch(() => "");
    await dropdown.locator(":scope > .label").click();
    await dropdown.locator(".week-items .week").filter({ hasText: new RegExp(`^Week ${target}$`) }).click();
    await selected.filter({ hasText: new RegExp(`^Wk\\. ${target}$`) }).waitFor({
      state: "visible",
      timeout: 10_000,
    });
    if (previousFirstRow) {
      await page.waitForFunction(
        ({ selector, previous }) => document.querySelector(selector)?.textContent !== previous,
        { selector: ".team-roster-item", previous: previousFirstRow },
        { timeout: 10_000 },
      ).catch(() => {});
    }
  }
  const observed = positiveWeek(String(await selected.textContent()).match(/\d+/)?.[0]);
  if (observed !== target) throw new Error(`Sleeper roster remained on Week ${observed ?? "unknown"}`);
}

async function selectFreeAgentProjectionWeek(page, week) {
  const target = positiveWeek(week);
  if (!target) throw new Error("A valid target week is required for free-agent observations");
  const projectionOption = page.locator(".option-item").filter({ hasText: /^Projection$/ }).first();
  await projectionOption.waitFor({ state: "visible", timeout: 10_000 });
  if (!String(await projectionOption.getAttribute("class")).split(/\s+/).includes("selected")) {
    await projectionOption.click();
  }
  const weekDropdown = page.locator(".filter-dropdown").filter({
    has: page.locator(".app-dropdown-item .name").filter({ hasText: /^Week 17$/ }),
  }).first();
  const selected = weekDropdown.locator(".selected-value");
  await selected.waitFor({ state: "visible", timeout: 10_000 });
  if (String(await selected.textContent()).trim() !== `Week ${target}`) {
    const previousFirstRow = await page.locator(".player-list-item").first().innerText().catch(() => "");
    await selected.click();
    await weekDropdown.locator(".app-dropdown-item .name")
      .filter({ hasText: new RegExp(`^Week ${target}$`) })
      .click();
    await selected.filter({ hasText: new RegExp(`^Week ${target}$`) }).waitFor({
      state: "visible",
      timeout: 10_000,
    });
    if (previousFirstRow) {
      await page.waitForFunction(
        ({ selector, previous }) => document.querySelector(selector)?.textContent !== previous,
        { selector: ".player-list-item", previous: previousFirstRow },
        { timeout: 10_000 },
      ).catch(() => {});
    }
  }
  const observed = positiveWeek(String(await selected.textContent()).match(/\d+/)?.[0]);
  const projectionSelected = String(await projectionOption.getAttribute("class"))
    .split(/\s+/)
    .includes("selected");
  if (observed !== target || !projectionSelected) {
    throw new Error("Sleeper free-agent projection view did not match the target week");
  }
}

async function selectMatchupWeek(page, week) {
  const target = positiveWeek(week);
  if (!target) throw new Error("A valid target week is required for matchup observations");
  const dropdown = page.locator(".week-selector-dropdown").first();
  const selected = dropdown.locator(":scope > .label .text");
  await selected.waitFor({ state: "visible", timeout: 10_000 });
  if (String(await selected.textContent()).trim() !== `Wk. ${target}`) {
    await dropdown.locator(":scope > .label").click();
    await dropdown.locator(".week-items .week").filter({ hasText: new RegExp(`^Week ${target}$`) }).click();
    await selected.filter({ hasText: new RegExp(`^Wk\\. ${target}$`) }).waitFor({
      state: "visible",
      timeout: 10_000,
    });
  }
  const observed = positiveWeek(String(await selected.textContent()).match(/\d+/)?.[0]);
  if (observed !== target) throw new Error(`Sleeper matchup remained on Week ${observed ?? "unknown"}`);
}

export async function ensureLeaguePage(page, config) {
  const target = leaguePath(config, "/team");
  if (!page.url().includes(`/leagues/${config.leagueId}/team`)) {
    await page.goto(target, { waitUntil: "domcontentloaded" });
  }
  await page.locator("body").waitFor({ state: "visible", timeout: 15_000 });
}

async function cdpTargets(config) {
  const endpoint = new URL("json", config.browserCdpUrl);
  const response = await fetch(endpoint, { signal: AbortSignal.timeout(2_000) });
  if (!response.ok) throw new Error(`Chrome debugging endpoint returned HTTP ${response.status}`);
  const targets = await response.json();
  return Array.isArray(targets) ? targets : [];
}

export async function ensureChromeDebugSession(config) {
  try {
    await cdpTargets(config);
    return;
  } catch {
    const args = [
      "-na",
      config.browserApplication,
      "--args",
      `--remote-debugging-address=${config.browserCdpUrl.hostname}`,
      `--remote-debugging-port=${config.browserCdpUrl.port}`,
      `--user-data-dir=${config.profileDir}`,
      "--no-first-run",
      "--no-default-browser-check",
      config.headless ? "--headless=new" : "--start-maximized",
      leaguePath(config, "/team"),
    ];
    spawn("/usr/bin/open", args, { detached: true, stdio: "ignore" }).unref();
  }
  for (let attempt = 0; attempt < 40; attempt += 1) {
    await delay(500);
    try {
      await cdpTargets(config);
      return;
    } catch {
      // Normal Chrome is still starting.
    }
  }
  throw new Error("Normal Chrome did not expose its localhost debugging endpoint");
}

export async function waitForLeagueLoginNavigation(config) {
  while (true) {
    const targets = await cdpTargets(config);
    const leagueTarget = targets.find((target) =>
      String(target.url ?? "").includes(`/leagues/${config.leagueId}`),
    );
    if (leagueTarget) return leagueTarget.url;
    await delay(1_000);
  }
}

export async function launchSleeperSession(config) {
  if (config.browserCdpUrl) {
    await ensureChromeDebugSession(config);
    const browser = await chromium.connectOverCDP(config.browserCdpUrl.toString());
    const context = browser.contexts()[0];
    if (!context) throw new Error("Normal Chrome did not provide a browser context");
    const existing = context
      .pages()
      .find((candidate) => candidate.url().includes(`/leagues/${config.leagueId}/team`));
    const page = existing ?? context.pages()[0] ?? (await context.newPage());
    if (!page.url().includes(`/leagues/${config.leagueId}/team`)) {
      await page.goto(leaguePath(config, "/team"), { waitUntil: "domcontentloaded" });
    }
    return { context, page, close: () => browser.close() };
  }
  const context = await chromium.launchPersistentContext(config.profileDir, {
    channel: config.browserChannel,
    headless: config.headless,
    viewport: config.headless ? { width: 1440, height: 1000 } : null,
    args: config.headless ? [] : ["--start-maximized"],
  });
  const existing = context
    .pages()
    .find((candidate) => candidate.url().includes(`/leagues/${config.leagueId}/team`));
  const page = existing ?? context.pages()[0] ?? (await context.newPage());
  if (!page.url().includes(`/leagues/${config.leagueId}/team`)) {
    await page.goto(leaguePath(config, "/team"), { waitUntil: "domcontentloaded" });
  }
  return { context, page, close: () => context.close() };
}

export async function inspectSleeperSession(page, config) {
  const current = new URL(page.url());
  const expected = new URL(config.leagueUrl);
  const body = await page.locator("body").innerText({ timeout: 10_000 }).catch(() => "");
  const correctOrigin = current.origin === expected.origin;
  const correctLeague = current.pathname.includes(`/leagues/${config.leagueId}`);
  const loggedIn =
    correctOrigin &&
    correctLeague &&
    body.includes(config.accountLabel) &&
    body.includes(config.teamLabel) &&
    !/log\s*in|sign\s*in/i.test(body.slice(0, 1_000));
  const actionButtons = await page.locator(".player-action-button:not(.disabled)").count();
  return {
    browserRunning: !page.isClosed(),
    sessionAvailable: loggedIn,
    correctOrigin,
    correctLeague,
    actionButtons,
    url: page.url(),
  };
}

export function parseLineupRow({ label, playerAlt, playerName, slot, text }) {
  const labelMatch = String(label ?? "").match(/^Slot\s+([A-Z/]+)\s+-\s+(.+)$/i);
  const playerMatch = String(playerAlt ?? "").match(/^nfl Player\s+(.+)$/i);
  const resolvedSlot = labelMatch?.[1] ?? String(slot ?? "").trim();
  const resolvedName = labelMatch?.[2] ?? String(playerName ?? "").trim();
  if (!resolvedSlot || !resolvedName || !playerMatch) return null;
  const projections = [...String(text ?? "").matchAll(/\b\d+\.\d{1,2}\b/g)];
  const projection = projections.length
    ? Number.parseFloat(projections[projections.length - 1][0])
    : null;
  return {
    slot: resolvedSlot.toUpperCase(),
    player_name: resolvedName.trim(),
    player_id: playerMatch[1].trim(),
    projected_points: Number.isFinite(projection) ? projection : null,
    visible_text: String(text ?? "").replaceAll(/\s+/g, " ").trim().slice(0, 500),
  };
}

export async function inspectLineupSnapshot(page, config, period) {
  if (!page.url().includes(`/leagues/${config.leagueId}/team`)) return null;
  await selectRosterWeek(page, period?.week);
  const teamPanels = page.locator(".team-panel");
  await teamPanels.first().waitFor({ state: "visible", timeout: 10_000 });
  const rawRows = await teamPanels.evaluateAll((panels, teamLabel) => {
    const panel = panels.find((candidate) => candidate.innerText.includes(teamLabel)) ?? panels[0];
    return [...(panel?.querySelectorAll(".team-roster-item") ?? [])].map((row) => {
      const link = row.querySelector(".cell-position");
      const image = row?.querySelector(
        '[aria-label^="nfl Player "], img[alt^="nfl Player "]',
      );
      return {
        label: link?.getAttribute("aria-label"),
        slot: row.querySelector(".league-slot-position-square")?.textContent,
        playerName: row.querySelector(".player-name")?.textContent,
        playerAlt: image?.getAttribute("aria-label") ?? image?.getAttribute("alt"),
        text: row?.innerText ?? "",
      };
    });
  }, config.teamLabel);
  const players = rawRows.map(parseLineupRow).filter(Boolean);
  return {
    observed_at: new Date().toISOString(),
    season: period.season,
    week: period.week,
    view_type: "weekly_projection",
    league_id: config.leagueId,
    roster_id: config.rosterId,
    source_url: page.url(),
    observed_slot_count: rawRows.length,
    players,
  };
}

export async function inspectPendingWaiverSnapshot(page, config) {
  await ensureLeaguePage(page, config);
  let panel = page.locator(".waivers-panel");
  if (!(await panel.isVisible().catch(() => false))) {
    const waiverButton = page.getByText("WAIVER", { exact: true }).locator("..");
    await waiverButton.waitFor({ state: "visible", timeout: 10_000 });
    await waiverButton.click();
    await panel.waitFor({ state: "visible", timeout: 10_000 });
  }
  try {
    const claims = await panel.locator(".league-transaction-item-waiver").evaluateAll((items) =>
      items.map((row, sequence) => ({
        sequence: sequence + 1,
        owner_team_name:
          row.querySelector(".team-owner-with-description p.name")?.textContent?.trim() ?? "",
        owner_account_label:
          row.querySelector(".team-owner-with-description .team-name")?.textContent?.trim() ?? "",
        add_player_name: row.querySelector(".add-player .name")?.textContent?.trim() ?? "",
        add_player_position:
          row.querySelector(".add-player .position")?.textContent?.trim() ?? "",
        drop_player_name: row.querySelector(".drop-player .name")?.textContent?.trim() ?? "",
        drop_player_position:
          row.querySelector(".drop-player .position")?.textContent?.trim() ?? "",
        processes_at:
          row.querySelector(".waiver-clear")?.textContent?.replaceAll(/\s+/g, " ").trim() ?? "",
      })),
    );
    return {
      observed_at: new Date().toISOString(),
      league_id: config.leagueId,
      roster_id: config.rosterId,
      claims: claims.filter(
        (claim) =>
          claim.owner_team_name === config.teamLabel
          && claim.owner_account_label.includes(config.accountLabel),
      ),
    };
  } finally {
    await page.keyboard.press("Escape").catch(() => {});
    await panel.waitFor({ state: "hidden", timeout: 3_000 }).catch(() => {});
  }
}

async function visibleTradeModal(page) {
  const candidates = page.locator('[role="alertdialog"]');
  for (const candidate of await candidates.all()) {
    if (await candidate.isVisible().catch(() => false)) return candidate;
  }
  return null;
}

async function tradeReviewSnapshot(modal, config) {
  const title = String(await modal.locator(".trade-title").textContent().catch(() => "")).trim();
  if (!/review trade offer/i.test(title)) return null;
  const rosters = modal.locator(".roster-trade-container");
  if (await rosters.count() !== 2) return null;
  let our = null;
  let counterparty = null;
  for (let index = 0; index < 2; index += 1) {
    const roster = rosters.nth(index);
    const username = String(await roster.locator(".username").textContent().catch(() => "")).trim();
    if (username.includes(config.accountLabel)) our = roster;
    else counterparty = { username, roster };
  }
  if (!our || !counterparty?.username) return null;
  const panels = our.locator(".roster-trade-summary .panel");
  if (await panels.count() !== 2) return null;
  const ids = async (panel) => panel.locator('img[aria-label^="nfl Player "]').evaluateAll((items) =>
    items.map((item) => String(item.getAttribute("aria-label") ?? "").replace(/^nfl Player\s+/, "")),
  );
  const receivePlayerIds = await ids(panels.nth(0));
  const sendPlayerIds = await ids(panels.nth(1));
  if (!receivePlayerIds.length || !sendPlayerIds.length) return null;
  const footer = String(await modal.locator(".propose-trade-footer").innerText().catch(() => ""));
  const direction = /\bACCEPT\b/i.test(footer) && /\bDECLINE\b/i.test(footer)
    ? "incoming"
    : /\bCANCEL\b/i.test(footer)
      ? "outgoing"
      : "unknown";
  const material = {
    counterparty_account_label: counterparty.username,
    receive_player_ids: receivePlayerIds.slice().sort(),
    send_player_ids: sendPlayerIds.slice().sort(),
  };
  return {
    ...material,
    direction,
    offer_fingerprint: `${counterparty.username}:${receivePlayerIds.slice().sort().join(",")}:${sendPlayerIds.slice().sort().join(",")}`,
    controls: {
      accept: /\bACCEPT\b/i.test(footer),
      decline: /\bDECLINE\b/i.test(footer),
      cancel: /\bCANCEL\b/i.test(footer),
    },
  };
}

export async function inspectTradeSnapshot(page, config) {
  const target = leaguePath(config, "/team");
  if (page.url() !== target) await page.goto(target, { waitUntil: "domcontentloaded" });
  await page.locator("body").waitFor({ state: "visible", timeout: 15_000 });
  await page.keyboard.press("Escape").catch(() => {});
  const tradeButton = page.getByText("TRADE", { exact: true }).locator("..");
  await tradeButton.waitFor({ state: "visible", timeout: 10_000 });
  await tradeButton.click({ noWaitAfter: true });
  let modal = await visibleTradeModal(page);
  if (!modal) throw new Error("Sleeper trade dialog did not open");
  const title = String(await modal.locator(".trade-title").textContent().catch(() => "")).trim();
  const offers = [];
  const direct = await tradeReviewSnapshot(modal, config);
  if (direct) offers.push(direct);
  if (!direct && title !== "Propose Trade") {
    const viewControls = modal.getByText("VIEW", { exact: true });
    const count = Math.min(await viewControls.count(), 12);
    for (let index = 0; index < count; index += 1) {
      await viewControls.nth(index).click({ noWaitAfter: true });
      await page.waitForTimeout(250);
      modal = (await visibleTradeModal(page)) ?? modal;
      const offer = await tradeReviewSnapshot(modal, config);
      if (offer) offers.push(offer);
      await page.keyboard.press("Escape").catch(() => {});
      await tradeButton.click({ noWaitAfter: true });
      modal = (await visibleTradeModal(page)) ?? modal;
    }
  }
  await page.keyboard.press("Escape").catch(() => {});
  return {
    observed_at: new Date().toISOString(),
    league_id: config.leagueId,
    roster_id: config.rosterId,
    interface_title: title,
    proposal_builder_available: title === "Propose Trade" || offers.length > 0,
    offers,
  };
}

export function parseFreeAgentRow({ name, positionText, status, cells, actionClass }) {
  const positionMatch = String(positionText ?? "").match(/^([A-Z]+)\s+-\s+([A-Z]{2,3})/i);
  const projectionText = Array.isArray(cells) ? cells[0] : null;
  const projectedPoints = Number.parseFloat(String(projectionText ?? ""));
  if (!String(name ?? "").trim() || !positionMatch || !Number.isFinite(projectedPoints)) {
    return null;
  }
  return {
    display_name: String(name).trim(),
    position: positionMatch[1].toUpperCase(),
    team: positionMatch[2].toUpperCase(),
    injury_status: String(status ?? "").trim() || null,
    acquisition_type: String(actionClass ?? "").split(/\s+/).includes("waiver")
      ? "waiver"
      : String(actionClass ?? "").split(/\s+/).includes("add")
        ? "free_agent"
        : null,
    projected_points: projectedPoints,
    projected_stat_cells: (cells ?? []).map((value) => String(value).trim()).slice(1, 13),
  };
}

function normalizedPlayerName(value) {
  return String(value ?? "").toLowerCase().replaceAll(/[^a-z0-9]/g, "");
}

function abbreviatedPlayerName(value) {
  const tokens = String(value ?? "").trim().split(/\s+/).filter(Boolean);
  while (tokens.length && /^(jr\.?|sr\.?|ii|iii|iv|v)$/i.test(tokens.at(-1))) tokens.pop();
  if (!tokens.length) return "";
  return normalizedPlayerName(tokens[0]).slice(0, 1) + normalizedPlayerName(tokens.at(-1));
}

function targetedSearchName(target) {
  if (String(target.position).toUpperCase() === "DEF") return String(target.team);
  return String(target.name).replace(/\s+(?:Jr\.?|Sr\.?|II|III|IV|V)$/i, "").trim();
}

export function attachTargetIdentity(row, target, observedAt) {
  if (!row || !target) return null;
  const samePosition = String(row.position).toUpperCase() === String(target.position).toUpperCase();
  const sameTeam = String(row.team).toUpperCase() === String(target.team).toUpperCase();
  const sameName =
    String(row.position).toUpperCase() === "DEF" ||
    normalizedPlayerName(row.display_name) === abbreviatedPlayerName(target.name);
  if (!samePosition || !sameTeam || !sameName || !String(target.player_id ?? "")) return null;
  return {
    ...row,
    player_id: String(target.player_id),
    target_name: String(target.name),
    observed_at: observedAt,
  };
}

async function freeAgentRawRows(page, visibleOnly = false) {
  const rows = page.locator(visibleOnly ? ".player-list-item:visible" : ".player-list-item");
  return rows.evaluateAll((items) =>
    items.map((row) => ({
      name: row.querySelector(".player-meta-container .name")?.textContent ?? "",
      positionText: row.querySelector(".player-meta-container .position")?.textContent ?? "",
      status: row.querySelector(".player-meta-container .injury-status")?.textContent ?? "",
      actionClass: row.querySelector(".player-action-button")?.className ?? "",
      cells: [...row.querySelectorAll(".cell.all")].map((cell) => cell.textContent ?? ""),
    })),
  );
}

export async function inspectFreeAgentSnapshot(page, config, period) {
  if (!page.url().includes(`/leagues/${config.leagueId}/players`)) return null;
  await selectFreeAgentProjectionWeek(page, period?.week);
  const rows = page.locator(".player-list-item");
  await rows.first().waitFor({ state: "visible", timeout: 15_000 });
  const rawRows = await freeAgentRawRows(page);
  return {
    observed_at: new Date().toISOString(),
    season: period.season,
    week: period.week,
    view_type: "weekly_projection",
    league_id: config.leagueId,
    source_url: page.url(),
    observed_row_count: rawRows.length,
    players: rawRows.map(parseFreeAgentRow).filter(Boolean),
  };
}

export async function inspectTargetedFreeAgentSnapshot(page, config, targets, period) {
  if (!page.url().includes(`/leagues/${config.leagueId}/players`)) return null;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    if (await page.locator('[role="alertdialog"]:visible').count() === 0) break;
    await page.keyboard.press("Escape").catch(() => {});
    await page.waitForTimeout(150);
  }
  if (await page.locator('[role="alertdialog"]:visible').count()) {
    throw new Error("Sleeper left an unrelated player dialog open before observation");
  }
  await selectFreeAgentProjectionWeek(page, period?.week);
  const search = page.getByPlaceholder(/find player/i);
  await search.waitFor({ state: "visible", timeout: 15_000 });
  const observedAt = new Date().toISOString();
  const players = [];
  for (const target of (targets ?? []).slice(0, 32)) {
    await search.fill(targetedSearchName(target), { timeout: 5_000 });
    await page.waitForTimeout(400);
    const rawRows = await freeAgentRawRows(page, true);
    const matches = rawRows
      .map(parseFreeAgentRow)
      .map((row) => attachTargetIdentity(row, target, observedAt))
      .filter(Boolean);
    if (matches.length === 1) players.push(matches[0]);
  }
  await search.fill("", { timeout: 5_000 });
  await page.waitForTimeout(200);
  return {
    observed_at: observedAt,
    season: period.season,
    week: period.week,
    view_type: "weekly_projection",
    league_id: config.leagueId,
    source_url: page.url(),
    target_count: Math.min((targets ?? []).length, 32),
    matched_target_count: players.length,
    players,
  };
}

export function mergeFreeAgentSnapshots(base, targeted) {
  if (!base) return targeted;
  const rows = new Map();
  const key = (row) =>
    `${String(row.position).toUpperCase()}:${String(row.team).toUpperCase()}:${normalizedPlayerName(row.display_name)}`;
  for (const row of base.players ?? []) rows.set(key(row), row);
  for (const row of targeted?.players ?? []) rows.set(key(row), row);
  return {
    ...base,
    observed_row_count: rows.size,
    targeted_player_count: targeted?.players?.length ?? 0,
    targeted_requested_count: targeted?.target_count ?? 0,
    players: [...rows.values()],
  };
}

export function parseMatchupPlayer({ imageUrl, name, positionText, projection }) {
  const positionMatch = String(positionText ?? "").match(/^([A-Z]+)\s+-\s+([A-Z]{2,3})/i);
  const playerMatch = String(imageUrl ?? "").match(/\/nfl\/players\/(?:thumb\/)?([^/.]+)\./i);
  const defenseMatch = String(imageUrl ?? "").match(/\/team_logos\/nfl\/([^/.]+)\./i);
  const projectedPoints = Number.parseFloat(String(projection ?? ""));
  if (!positionMatch || !Number.isFinite(projectedPoints)) return null;
  const position = positionMatch[1].toUpperCase();
  const playerId = position === "DEF" ? defenseMatch?.[1]?.toUpperCase() : playerMatch?.[1];
  if (!playerId) return null;
  return {
    player_id: playerId,
    display_name: String(name ?? "").trim(),
    position,
    team: positionMatch[2].toUpperCase(),
    projected_points: projectedPoints,
  };
}

export async function inspectMatchupSnapshot(page, config, period) {
  if (!page.url().includes(`/leagues/${config.leagueId}/matchup`)) return null;
  await selectMatchupWeek(page, period?.week);
  const rows = page.locator(".player-matchup-body-row");
  const rowsAvailable = await rows.first().waitFor({ state: "visible", timeout: 5_000 })
    .then(() => true)
    .catch(() => false);
  if (!rowsAvailable) {
    return {
      observed_at: new Date().toISOString(),
      season: period.season,
      week: period.week,
      view_type: "weekly_projection",
      league_id: config.leagueId,
      roster_id: config.rosterId,
      source_url: page.url(),
      unavailable_reason: `Sleeper has not populated the Week ${period.week} matchup player grid`,
      starter_count: 0,
      our_team: { players: [] },
      opponent: { players: [] },
    };
  }
  const rawRows = await rows.evaluateAll((items) =>
    items.map((row) =>
      [...row.querySelectorAll(":scope > .matchup-player-item")].map((player) => ({
        imageUrl: player.querySelector(".player-image")?.getAttribute("data") ?? "",
        name: player.querySelector(".player-name > div")?.textContent ?? "",
        positionText: player.querySelector(".player-pos")?.textContent ?? "",
        projection: player.querySelector(".projections")?.textContent ?? "",
      })),
    ),
  );
  const ownerRows = await page.locator(".matchup-owner-item").evaluateAll((items) =>
    items.slice(0, 2).map((owner) => ({
      username: owner.querySelector(".team-name")?.textContent?.trim() ?? "",
      team_name: owner.querySelector(".name")?.textContent?.trim() ?? "",
      win_probability: Number.parseFloat(
        owner.querySelector(".win-percentage-number")?.textContent ?? "",
      ),
      projected_total: Number.parseFloat(
        owner.querySelector(".roster-score-and-projection-matchup .projections")?.textContent ?? "",
      ),
    })),
  );
  if (
    ownerRows.length !== 2
    || !ownerRows[0].username.includes(config.accountLabel)
    || ownerRows[0].team_name !== config.teamLabel
  ) {
    return null;
  }
  const sides = [[], []];
  for (const row of rawRows) {
    for (const side of [0, 1]) {
      const parsed = parseMatchupPlayer(row[side] ?? {});
      if (parsed) sides[side].push(parsed);
    }
  }
  return {
    observed_at: new Date().toISOString(),
    season: period.season,
    week: period.week,
    view_type: "weekly_projection",
    league_id: config.leagueId,
    roster_id: config.rosterId,
    source_url: page.url(),
    starter_count: Math.min(11, rawRows.length),
    our_team: { ...ownerRows[0], players: sides[0] },
    opponent: { ...ownerRows[1], players: sides[1] },
  };
}

export { leaguePath };
