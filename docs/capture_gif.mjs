// Capture WebUI walkthrough as a video, then convert to GIF
// Usage: node docs/capture_gif.mjs

import { chromium } from 'playwright';
import { execSync } from 'child_process';
import { mkdirSync, existsSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const OUT_DIR = join(__dirname, 'screenshots');
const BASE = 'http://localhost:3000';

mkdirSync(OUT_DIR, { recursive: true });

async function captureWebUI() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({
    viewport: { width: 1280, height: 720 },
    deviceScaleFactor: 2,
    colorScheme: 'dark',
    recordVideo: { dir: OUT_DIR, size: { width: 1280, height: 720 } },
  });

  const page = await context.newPage();

  // Page 1: Chat (home)
  console.log('Recording: Chat...');
  await page.goto(BASE, { waitUntil: 'networkidle' });
  await page.waitForTimeout(2000);

  // Page 2: Council
  console.log('Recording: Council...');
  await page.goto(`${BASE}/council`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(2000);

  // Page 3: Integrations
  console.log('Recording: Integrations...');
  await page.goto(`${BASE}/integrations`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(2000);

  // Page 4: Advisors
  console.log('Recording: Advisors...');
  await page.goto(`${BASE}/providers`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(2000);

  // Page 5: System
  console.log('Recording: System...');
  await page.goto(`${BASE}/system`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(2000);

  // Page 6: Setup
  console.log('Recording: Setup...');
  await page.goto(`${BASE}/setup`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(2000);

  // Back to chat
  console.log('Recording: Back to Chat...');
  await page.goto(BASE, { waitUntil: 'networkidle' });
  await page.waitForTimeout(1500);

  await page.close();
  await context.close();

  // Get the video path
  const videoPath = await page.video().path();
  console.log(`Video saved: ${videoPath}`);

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
  }

  await browser.close();
}

captureWebUI().catch(console.error);
