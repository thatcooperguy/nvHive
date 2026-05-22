import { NextResponse } from 'next/server';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { nvhHome, resolveNvhBinary } from '@/lib/nvh-bridge';

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

// Binary resolution lives in @/lib/nvh-bridge — see the shared module
// for the NVH_BIN-as-directory bug history.

export async function GET() {
  const nvhBin = await resolveNvhBinary();
  // Helper — `nvh doctor --json` returns exit=1 when ANY check fails (e.g.
  // the "Environment: local / on-prem detected" check returns "not detected
  // (local / on-prem)" on cloud GPU rigs, making the overall exit code 1).
  // The JSON payload is still valid and useful — failed checks are exactly
  // what we want to show in the report. So on non-zero exit, try to parse
  // err.stdout as JSON before falling back to error-format.
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
    const e = err as NodeJS.ErrnoException & {
      stdout?: string; stderr?: string; code?: number | string;
    };
    // Non-zero exit with JSON stdout = "doctor ran, some checks failed."
    // That's GOOD signal — surface the report as `ok: true` with the
    // exit code attached so the UI can render the failed-checks bullets.
    if (e.stdout) {
      try {
        const parsed = JSON.parse(e.stdout) as unknown;
        return NextResponse.json({
          ok: true,
          format: 'json',
          binary: nvhBin,
          report: parsed,
          // Preserve the non-zero exit so the UI can show "some checks failed"
          // chrome without hiding the report content.
          exitCode: typeof e.code === 'number' ? e.code : null,
          stderr: (e.stderr || '').slice(-1000),
        });
      } catch {
        /* fall through to the error response below */
      }
    }
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
