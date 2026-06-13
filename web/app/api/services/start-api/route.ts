import { NextResponse } from 'next/server';
import { spawn } from 'node:child_process';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { nvhHome, nvhLogsDir, resolveNvhBinary } from '@/lib/nvh-bridge';

/**
 * Start (or restart) the nvHive FastAPI server from inside the WebUI.
 *
 * Context: when the API is down, the WebUI shows a red banner that used to
 * say "run `nvh serve` in a terminal." That broke the out-of-the-box promise
 * — a fresh-install user shouldn't need a terminal at all. The Next.js
 * process is already running as the rootless user with $NVH_HOME on PATH,
 * so it can fork `nvh serve` directly and the API comes up without any
 * shell access from the user.
 *
 * What this route does:
 *   1. Resolves the `nvh` binary path (env, then PATH, then $NVH_HOME/bin).
 *   2. Spawns `nvh serve --port 8000` with stdout+stderr piped to
 *      $NVH_HOME/logs/api-server.log so the SystemConsole can tail it.
 *   3. Detaches the child + closes stdio so it survives this request handler.
 *   4. Returns 202 Accepted with the spawned PID and log path.
 *
 * What it does NOT do:
 *   - Wait for /v1/health to return 200 — that's the banner's job. Polling
 *     here would tie the route up for 15-30s; instead we kick the process,
 *     return immediately, and let the banner's existing health probe pick
 *     up the new state.
 *   - Run as a different user. The rootless contract is "everything stays
 *     as the user that ran the installer."
 *
 * Method: POST (this mutates state).
 */

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

interface StartApiRequestBody {
  port?: number;
}

// Binary resolution lives in @/lib/nvh-bridge so we don't ship the
// "treat NVH_BIN as the executable" bug to multiple routes. install.sh
// exports NVH_BIN as the rootless bin DIRECTORY, not the `nvh` file —
// see the shared module for the full history + fix.

export async function POST(request: Request) {
  let body: StartApiRequestBody = {};
  try {
    body = (await request.json()) as StartApiRequestBody;
  } catch {
    /* empty body is fine */
  }
  const port = body.port && Number.isFinite(body.port) ? body.port : 8000;

  const logsDir = nvhLogsDir();
  await fs.mkdir(logsDir, { recursive: true });
  const logPath = path.join(logsDir, 'api-server.log');

  const nvhBin = await resolveNvhBinary();
  // resolveNvhBinary() never returns null — it falls through to the bare
  // string "nvh" so the spawn below can use PATH lookup. If even PATH
  // doesn't have it, the spawn()'s async 'error' event surfaces ENOENT
  // to the user via the spawn-vs-error race below (NOT via try/catch —
  // see the comment at the race).

  // Append a separator so the user can tell this start from previous ones.
  const stamp = new Date().toISOString();
  try {
    await fs.appendFile(logPath, `\n--- nvh serve started from WebUI at ${stamp} ---\n`, 'utf8');
  } catch {
    /* logging is best-effort */
  }

  // Open the log file as a raw fd to hand to the child. CRITICAL: use
  // `fs.openSync(...)` (returns int) NOT `await fs.open(...)` (returns
  // FileHandle). FileHandle has a GC finalizer that calls .close() on
  // the fd; once `fh` becomes unreachable (immediately on this function
  // returning), GC can close the fd BEFORE `spawn()` has finished
  // duplicating it into the child, producing an `nvh serve` whose
  // stdout/stderr writes to a closed fd → silent crash on first log
  // line. Real-rig audit 2026-05-22 (Agent D) flagged this as a
  // blocker. The raw-int form has no finalizer, so the parent's fd
  // stays alive long enough for the child to inherit it; both parent
  // and child references close cleanly when their respective processes
  // exit.
  const fsSync = await import('node:fs');
  let logFd: number;
  try {
    logFd = fsSync.openSync(logPath, 'a');
  } catch (err) {
    return NextResponse.json(
      {
        started: false,
        reason: `could not open log file ${logPath}: ${err instanceof Error ? err.message : String(err)}`,
      },
      { status: 500 },
    );
  }

  try {
    const child = spawn(nvhBin, ['serve', '--port', String(port)], {
      detached: true,
      stdio: ['ignore', logFd, logFd],
      env: {
        ...process.env,
        // Make sure the child sees the same NVH_HOME the WebUI is using.
        NVH_HOME: nvhHome(),
      },
    });
    // 2026-06-10 audit: spawn() does NOT throw on a missing binary — it
    // returns a ChildProcess and emits an async 'error' event (ENOENT),
    // so the try/catch alone never fired for the advertised "nvh not on
    // PATH" case. The route returned 202 {started:true, pid:undefined}
    // ("API spawning (pid undefined)" in the SystemConsole) and the
    // unhandled 'error' event could then crash the Next.js process — the
    // one component still working. Race 'spawn' vs 'error' before
    // responding so spawn failure is a real 500 with the actual reason.
    const spawnErr = await new Promise<Error | null>(resolve => {
      child.once('spawn', () => resolve(null));
      child.once('error', err => resolve(err));
    });
    if (spawnErr || child.pid === undefined) {
      // Throw into the existing catch so the fd cleanup + 500 response
      // live in exactly one place.
      throw spawnErr ?? new Error('spawn reported success but child.pid is undefined');
    }
    child.unref();
    // Close the parent's reference to logFd now that the child has
    // dup'd it via `stdio`. Without this we'd leak one fd per
    // Restart-API click for the lifetime of the Next.js process.
    // (The child keeps its own dup, so the file stays open for
    // `nvh serve`.)
    try { fsSync.closeSync(logFd); } catch { /* best-effort */ }

    return NextResponse.json(
      {
        started: true,
        pid: child.pid,
        binary: nvhBin,
        port,
        log: logPath,
        startedAt: stamp,
        hint: 'cold-start usually takes 5-15s. the banner will clear when /v1/health responds.',
      },
      { status: 202 },
    );
  } catch (err) {
    // Spawn failed — clean up the fd we opened.
    try { fsSync.closeSync(logFd); } catch { /* best-effort */ }
    return NextResponse.json(
      {
        started: false,
        binary: nvhBin,
        reason: err instanceof Error ? err.message : String(err),
      },
      { status: 500 },
    );
  }
}
