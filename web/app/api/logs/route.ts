import { NextResponse } from 'next/server';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';

/**
 * Server-side log tail route.
 *
 * Why this lives in Next.js, not FastAPI: the most common failure mode the
 * banner surfaces is "FastAPI is down." A logs-endpoint hosted on FastAPI
 * obviously can't answer in that case. The Next.js process is already running
 * as the rootless user, so it can read `$NVH_HOME/logs/*.log` from disk and
 * surface the API's own crash log directly to the user. This is the entire
 * point of the in-WebUI System Console — visibility when the API is dead,
 * not just when it's alive.
 *
 * Query params:
 *   source = api | webui | ollama | install     (default: api)
 *   lines  = N                                  (default: 200, max: 2000)
 *
 * Response:
 *   { source, path, exists, mtime, sizeBytes, lines: string[] }
 *
 * Security note: this route only reads files inside `$NVH_HOME/logs/` and
 * only accepts a whitelist of source names — no path traversal possible.
 * It is read-only.
 */

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

const SOURCES: Record<string, string> = {
  api: 'api-server.log',
  webui: 'webui-bootstrap.log',
  ollama: 'ollama.log',
  install: 'install.log',
};

function nvhLogsDir(): string {
  const explicitLogs = process.env.NVH_LOGS;
  if (explicitLogs) return explicitLogs;
  const home = process.env.NVH_HOME;
  if (home) return path.join(home, 'logs');
  // Final fallback — same convention as install.sh + storage.py default.
  return path.join(os.homedir(), 'nvhive', 'logs');
}

async function readTail(filePath: string, maxLines: number): Promise<string[]> {
  // Simple "read whole file, slice tail" — sufficient for the log sizes
  // nvHive writes (api-server.log rotates around 5 MB in practice). If we
  // ever ship bigger logs, swap this for a streaming-from-tail read.
  const raw = await fs.readFile(filePath, 'utf8');
  const lines = raw.split('\n');
  // Drop a trailing empty line if the file ends with '\n'.
  if (lines.length > 0 && lines[lines.length - 1] === '') lines.pop();
  return lines.slice(-maxLines);
}

export async function GET(request: Request) {
  const url = new URL(request.url);
  const source = (url.searchParams.get('source') || 'api').toLowerCase();
  const linesParam = url.searchParams.get('lines');
  let maxLines = 200;
  if (linesParam) {
    const parsed = Number.parseInt(linesParam, 10);
    if (Number.isFinite(parsed) && parsed > 0) maxLines = Math.min(parsed, 2000);
  }

  const filename = SOURCES[source];
  if (!filename) {
    return NextResponse.json(
      { error: `unknown source "${source}". valid: ${Object.keys(SOURCES).join(', ')}` },
      { status: 400 },
    );
  }

  const logsDir = nvhLogsDir();
  const filePath = path.join(logsDir, filename);

  let stat: Awaited<ReturnType<typeof fs.stat>> | null = null;
  try {
    stat = await fs.stat(filePath);
  } catch {
    return NextResponse.json({
      source,
      path: filePath,
      exists: false,
      mtime: 0,
      sizeBytes: 0,
      lines: [],
      hint: `log file not found at ${filePath}. it gets created when the service runs.`,
    });
  }

  try {
    const lines = await readTail(filePath, maxLines);
    return NextResponse.json({
      source,
      path: filePath,
      exists: true,
      mtime: stat.mtimeMs,
      sizeBytes: stat.size,
      lines,
    });
  } catch (err) {
    return NextResponse.json(
      {
        source,
        path: filePath,
        exists: true,
        mtime: stat.mtimeMs,
        sizeBytes: stat.size,
        lines: [],
        error: err instanceof Error ? err.message : String(err),
      },
      { status: 500 },
    );
  }
}
