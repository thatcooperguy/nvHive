import { NextResponse } from 'next/server';
import { execFile } from 'node:child_process';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { promisify } from 'node:util';

/**
 * Run `nvh doctor --json` from inside the WebUI and return its parsed output.
 *
 * This is the "ask the Wizard's CLI counterpart to diagnose itself" route
 * that backs the System Console's [Run Doctor] button. Because doctor is a
 * pure CLI introspection (it doesn't need the API server running), it's the
 * right tool to call when the API is down — answers "is the venv complete?
 * does ollama work? are providers configured?" without depending on the
 * very thing that's broken.
 *
 * Hard cap: 30s execution time. doctor's slow path is provider key
 * validation (network calls), which can hang on a flaky link.
 */

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

const execFileAsync = promisify(execFile);

function nvhHome(): string {
  return process.env.NVH_HOME || path.join(os.homedir(), 'nvhive');
}

async function resolveNvhBinary(): Promise<string> {
  const fromEnv = process.env.NVH_BIN;
  if (fromEnv) {
    try {
      await fs.access(fromEnv, fs.constants.X_OK);
      return fromEnv;
    } catch {
      /* fall through */
    }
  }
  const home = nvhHome();
  const candidates = [
    path.join(home, 'venv', 'bin', 'nvh'),
    path.join(home, 'bin', 'nvh'),
    path.join(os.homedir(), '.local', 'bin', 'nvh'),
  ];
  for (const candidate of candidates) {
    try {
      await fs.access(candidate, fs.constants.X_OK);
      return candidate;
    } catch {
      /* try next */
    }
  }
  return 'nvh';
}

export async function GET() {
  const nvhBin = await resolveNvhBinary();
  try {
    const { stdout, stderr } = await execFileAsync(nvhBin, ['doctor', '--json'], {
      timeout: 30_000,
      maxBuffer: 4 * 1024 * 1024,
      env: { ...process.env, NVH_HOME: nvhHome() },
    });
    let parsed: unknown = null;
    try {
      parsed = JSON.parse(stdout);
    } catch {
      // Older `nvh doctor` versions may not support --json — fall back to
      // returning the raw text so the SystemConsole can still surface it.
      return NextResponse.json({
        ok: true,
        format: 'text',
        binary: nvhBin,
        stdout,
        stderr,
      });
    }
    return NextResponse.json({ ok: true, format: 'json', binary: nvhBin, report: parsed });
  } catch (err) {
    const e = err as NodeJS.ErrnoException & { stdout?: string; stderr?: string; code?: number | string };
    return NextResponse.json(
      {
        ok: false,
        binary: nvhBin,
        reason: e.message,
        exitCode: typeof e.code === 'number' ? e.code : null,
        stdout: e.stdout || '',
        stderr: e.stderr || '',
      },
      { status: 500 },
    );
  }
}
