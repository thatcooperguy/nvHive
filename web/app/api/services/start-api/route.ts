import { NextResponse } from 'next/server';
import { spawn } from 'node:child_process';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';

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

function nvhHome(): string {
  return process.env.NVH_HOME || path.join(os.homedir(), 'nvhive');
}

function nvhLogsDir(): string {
  return process.env.NVH_LOGS || path.join(nvhHome(), 'logs');
}

async function resolveNvhBinary(): Promise<string | null> {
  // 1. Explicit env override wins.
  const fromEnv = process.env.NVH_BIN;
  if (fromEnv) {
    try {
      await fs.access(fromEnv, fs.constants.X_OK);
      return fromEnv;
    } catch {
      /* fall through */
    }
  }
  // 2. Common rootless install locations.
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
  // 3. Last resort: rely on PATH. Returning the string lets spawn() do the
  // PATH lookup itself; if it's not there, spawn emits ENOENT below.
  return 'nvh';
}

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
  if (nvhBin === null) {
    return NextResponse.json(
      {
        started: false,
        reason: 'could not locate the `nvh` binary in $NVH_HOME/venv/bin, $NVH_HOME/bin, or ~/.local/bin',
        hint: 'set NVH_BIN to the absolute path of your `nvh` executable',
      },
      { status: 500 },
    );
  }

  // Append a separator so the user can tell this start from previous ones.
  const stamp = new Date().toISOString();
  try {
    await fs.appendFile(logPath, `\n--- nvh serve started from WebUI at ${stamp} ---\n`, 'utf8');
  } catch {
    /* logging is best-effort */
  }

  // Open the log file as a write stream so we can hand its fd to the child.
  // We use fs.open + the resulting filehandle's fd because spawn() expects
  // a fd for "inherit to this file."
  let logFd: number;
  try {
    const fh = await fs.open(logPath, 'a');
    logFd = fh.fd;
    // NOTE: we deliberately do NOT close `fh` here — the child inherits the
    // fd and we want it to stay open for the lifetime of the spawned process.
    // Node closes the parent's reference when this function returns; the
    // child's reference keeps the file open.
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
    child.unref();

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
