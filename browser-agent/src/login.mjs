import { setTimeout as delay } from "node:timers/promises";
import { loadConfig } from "./config.mjs";
import {
  ensureChromeDebugSession,
  inspectSleeperSession,
  launchSleeperSession,
  waitForLeagueLoginNavigation,
} from "./session.mjs";

const config = loadConfig();
const loginConfig = { ...config, headless: false };
await ensureChromeDebugSession(loginConfig);
console.log(`Normal Chrome opened with the isolated profile at ${config.profileDir}`);
console.log("Complete the Sleeper CAPTCHA and sign in, then open the configured league.");
console.log("Playwright will not attach until Chrome reaches that league page.");

await waitForLeagueLoginNavigation(loginConfig);
const { close, page } = await launchSleeperSession(loginConfig);

while (true) {
  const session = await inspectSleeperSession(page, config);
  if (session.sessionAvailable && session.correctLeague) {
    console.log("Sleeper session verified and saved for the browser agent.");
    await delay(1_000);
    await close();
    process.exit(0);
  }
  await delay(1_000);
}
