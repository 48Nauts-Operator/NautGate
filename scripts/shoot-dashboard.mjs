// Capture the dashboard tabs used on the marketing site.
//   node scripts/shoot-dashboard.mjs [baseUrl]
// Playwright comes from the feature-demo skill rather than a devDependency here —
// this runs by hand a few times a release, not in CI.
// Playwright is not a devDependency (this runs by hand a few times a release).
// Point PLAYWRIGHT_MODULE at an existing install rather than hardcoding one
// person's path:  PLAYWRIGHT_MODULE=/path/to/playwright/index.mjs node scripts/shoot-dashboard.mjs
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? "playwright");
import { mkdirSync } from "node:fs";

const BASE = process.argv[2] || "http://localhost:8090";
const OUT = new URL("../assets/screenshots/", import.meta.url).pathname;
const TABS = [
  ["overview", "overview"],
  ["audit", "audit-log"],
  ["insights", "insights"],
  ["bench", "bench"],
  ["cost", "cost"],
  ["modelhealth", "model-health"],
];

mkdirSync(OUT, { recursive: true });
const browser = await chromium.launch();
const page = await browser.newPage({
  viewport: { width: 1440, height: 900 },
  deviceScaleFactor: 2,
});
await page.goto(`${BASE}/dashboard`, { waitUntil: "networkidle" });

for (const [tab, name] of TABS) {
  await page.click(`a[data-tab='${tab}']`);
  await page.waitForTimeout(2500);            // charts fetch + animate in
  await page.screenshot({ path: `${OUT}${name}.png` });
  console.log(`${name}.png`);
}

await browser.close();
