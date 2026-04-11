// Capture WebUI walkthrough as a video, then convert to GIF
// Also captures individual page screenshots for the README
//
// Usage: node docs/capture_gif.mjs
//
// Prerequisites:
//   - nvh serve running on :8000
//   - nvh webui running on :3000 (or :80)
//   - playwright installed: npx playwright install chromium
//   - ffmpeg installed (for GIF conversion)

import { chromium } from 'playwright';
import { execSync } from 'child_process';
import { mkdirSync, existsSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const OUT_DIR = join(__dirname, 'screenshots');
const FRAMES_DIR = join(OUT_DIR, 'frames');
const BASE = process.env.WEBUI_URL || 'http://localhost:3000';

mkdirSync(OUT_DIR, { recursive: true });
mkdirSync(FRAMES_DIR, { recursive: true });

// Pages to visit and screenshot
const PAGES = [
  { path: '/',             name: 'chat',          label: 'Chat (Home)',    wait: 2000 },
  { path: '/council',      name: 'council',       label: 'Council',        wait: 2000 },
  { path: '/query',        name: 'query',         label: 'Query',          wait: 2000 },
  { path: '/providers',    name: 'advisors',      label: 'Advisors',       wait: 2000 },
  { path: '/analytics',    name: 'analytics',     label: 'Analytics',      wait: 2000 },
  { path: '/system',       name: 'system',        label: 'System',         wait: 2000 },
  { path: '/integrations', name: 'integrations',  label: 'Integrations',   wait: 2000 },
  { path: '/setup',        name: 'setup',         label: 'Setup',          wait: 2000 },
  { path: '/settings',     name: 'settings',      label: 'Settings',       wait: 2000 },
  // Return to chat for the video loop
  { path: '/',             name: 'chat-end',      label: 'Chat (End)',     wait: 1500 },
];

async function captureWebUI() {
  console.log(`Connecting to WebUI at ${BASE}...`);

  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1280, height: 720 },
    deviceScaleFactor: 2,
    colorScheme: 'dark',
    recordVideo: { dir: OUT_DIR, size: { width: 1280, height: 720 } },
  });

  const page = await context.newPage();

  for (let i = 0; i < PAGES.length; i++) {
    const p = PAGES[i];
    console.log(`[${i + 1}/${PAGES.length}] ${p.label}...`);

    try {
      await page.goto(`${BASE}${p.path}`, { waitUntil: 'networkidle', timeout: 15000 });
    } catch (e) {
      // networkidle can timeout if the page has polling (useProviderHealth)
      // — that's fine, the page is loaded, just still fetching in the background
      console.log(`  (networkidle timeout — continuing)`);
    }

    await page.waitForTimeout(p.wait);

    // Capture individual screenshot
    const screenshotPath = join(FRAMES_DIR, `${String(i + 1).padStart(2, '0')}-${p.name}.png`);
    await page.screenshot({ path: screenshotPath, fullPage: false });

    // Also save a top-level copy for commonly referenced pages
    if (['chat', 'council', 'advisors', 'system', 'setup', 'settings'].includes(p.name)) {
      await page.screenshot({
        path: join(OUT_DIR, `${p.name}.png`),
        fullPage: false,
      });
    }
  }

  await page.close();
  await context.close();

  // Get the video path
  const videoPath = await page.video().path();
  console.log(`\nVideo saved: ${videoPath}`);

  // Convert to GIF using ffmpeg
  const gifPath = join(OUT_DIR, 'webui-walkthrough.gif');
  console.log('Converting to GIF...');
  try {
    execSync(
      `ffmpeg -y -i "${videoPath}" -vf "fps=8,scale=640:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse" -loop 0 "${gifPath}"`,
      { stdio: 'pipe' }
    );
    console.log(`GIF saved: ${gifPath}`);
  } catch (e) {
    console.error('ffmpeg conversion failed:', e.message);
    console.log('Video is still available at:', videoPath);
    console.log('Install ffmpeg: scoop install ffmpeg / choco install ffmpeg');
  }

  await browser.close();
  console.log('\nDone! Screenshots in docs/screenshots/frames/');
}

captureWebUI().catch(console.error);
