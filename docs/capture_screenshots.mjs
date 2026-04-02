// Capture WebUI screenshots for README
// Usage: npx playwright test --config=docs/capture_screenshots.mjs
// Or: node docs/capture_screenshots.mjs

import { chromium } from 'playwright';
import { mkdirSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const SCREENSHOTS_DIR = join(__dirname, 'screenshots');
const BASE_URL = 'http://localhost:3000';

mkdirSync(SCREENSHOTS_DIR, { recursive: true });

const pages = [
  { path: '/',              name: 'chat',          title: 'Chat Interface' },
  { path: '/council',       name: 'council',       title: 'Council Mode' },
  { path: '/integrations',  name: 'integrations',  title: 'Integrations' },
  { path: '/providers',     name: 'advisors',      title: 'Advisors' },
  { path: '/system',        name: 'system',        title: 'System Dashboard' },
  { path: '/settings',      name: 'settings',      title: 'Settings' },
  { path: '/setup',         name: 'setup',         title: 'Setup Wizard' },
  { path: '/query',         name: 'query',         title: 'Query Builder' },
];

async function capture() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2, // Retina quality
    colorScheme: 'dark',
  });

  for (const page of pages) {
    const tab = await context.newPage();
    const url = `${BASE_URL}${page.path}`;
    console.log(`Capturing ${page.title} (${url})...`);

    try {
      await tab.goto(url, { waitUntil: 'networkidle', timeout: 15000 });
      // Extra wait for any animations/loading
      await tab.waitForTimeout(1500);

      const outPath = join(SCREENSHOTS_DIR, `${page.name}.png`);
      await tab.screenshot({ path: outPath, fullPage: false });
      console.log(`  -> ${outPath}`);
    } catch (e) {
      console.error(`  FAILED: ${e.message}`);
    }

    await tab.close();
  }

  await browser.close();
  console.log('\nDone! Screenshots saved to docs/screenshots/');
}

capture().catch(console.error);
