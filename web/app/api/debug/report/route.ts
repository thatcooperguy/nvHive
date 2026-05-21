import { NextResponse } from 'next/server';
import { execFile } from 'node:child_process';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { promisify } from 'node:util';
import { nvhHome, nvhLogsDir, resolveNvhBinary } from '@/lib/nvh-bridge';

/**
 * One-shot "tell me everything you can find out" diagnostic aggregator.
 *
 * The DebugReportButton in the WebUI fires this on click, then renders the
 * payload as a phone-readable report the user can photograph and share.
 *
 * Why this lives in Next.js: same reason as /api/services/* — the most
 * useful debug report is the one the user can generate WHEN THE API IS
 * DOWN. Hosting this on FastAPI would make it unreachable in the failure
 * case it's designed to diagnose.
 *
 * What it captures:
 *   - API health (HTTP + engine_initialized) on :8000
 *   - Ollama reachability on :11434
 *   - Last 30 lines of each known log file
 *   - `nvh doctor --json` output (capped at 15s)
 *   - Environment: NVH_HOME, NVH_BIN, Node version, platform
 *
 * All capture steps run in parallel; total wall time is bounded by the
 * slowest probe (doctor at 15s).
 */

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

const execFileAsync = promisify(execFile);

const LOG_FILES = [
  { source: 'api', filename: 'api-server.log' },
  { source: 'webui', filename: 'webui-bootstrap.log' },
  { source: 'ollama', filename: 'ollama.log' },
  { source: 'install', filename: 'install.log' },
] as const;

interface LogTail {
  source: string;
  path: string;
  exists: boolean;
  sizeBytes: number;
  mtimeIso: string | null;
  lines: string[];
}

interface ServiceProbe {
  reachable: boolean;
  httpStatus: number | null;
  engineInitialized: boolean | null;
  body: string;
  error: string | null;
  elapsedMs: number;
}

interface DoctorResult {
  ran: boolean;
  format: 'json' | 'text' | 'error';
  binary: string;
  exitCode: number | null;
  report: unknown;
  stdout: string;
  stderr: string;
  elapsedMs: number;
}

// Binary resolution + path helpers live in @/lib/nvh-bridge so all three
// bridge routes (start-api, doctor, debug/report) share the same logic.
// The directory-vs-file bug surfaced on the real-rig debug report photo
// 2026-05-21 is fixed there.

async function probeService(url: string, timeoutMs = 3000): Promise<ServiceProbe> {
  const t0 = Date.now();
  try {
    const ctl = new AbortController();
    const tHandle = setTimeout(() => ctl.abort(), timeoutMs);
    const r = await fetch(url, { signal: ctl.signal, cache: 'no-store' });
    clearTimeout(tHandle);
    const body = await r.text();
    let engineInitialized: boolean | null = null;
    try {
      const parsed = JSON.parse(body) as { data?: { engine_initialized?: boolean } };
      if (typeof parsed?.data?.engine_initialized === 'boolean') {
        engineInitialized = parsed.data.engine_initialized;
      }
    } catch {
      /* body may not be JSON */
    }
    return {
      reachable: true,
      httpStatus: r.status,
      engineInitialized,
      body: body.slice(0, 400),
      error: null,
      elapsedMs: Date.now() - t0,
    };
  } catch (err) {
    return {
      reachable: false,
      httpStatus: null,
      engineInitialized: null,
      body: '',
      error: err instanceof Error ? err.message : String(err),
      elapsedMs: Date.now() - t0,
    };
  }
}

async function tailLog(source: string, filename: string, maxLines: number): Promise<LogTail> {
  const filePath = path.join(nvhLogsDir(), filename);
  let stat: Awaited<ReturnType<typeof fs.stat>> | null = null;
  try {
    stat = await fs.stat(filePath);
  } catch {
    return {
      source,
      path: filePath,
      exists: false,
      sizeBytes: 0,
      mtimeIso: null,
      lines: [],
    };
  }
  try {
    const raw = await fs.readFile(filePath, 'utf8');
    const lines = raw.split('\n');
    if (lines.length > 0 && lines[lines.length - 1] === '') lines.pop();
    return {
      source,
      path: filePath,
      exists: true,
      sizeBytes: stat.size,
      mtimeIso: new Date(stat.mtimeMs).toISOString(),
      lines: lines.slice(-maxLines),
    };
  } catch {
    return {
      source,
      path: filePath,
      exists: true,
      sizeBytes: stat.size,
      mtimeIso: new Date(stat.mtimeMs).toISOString(),
      lines: [],
    };
  }
}

async function runDoctor(): Promise<DoctorResult> {
  const t0 = Date.now();
  const nvhBin = await resolveNvhBinary();
  try {
    const { stdout, stderr } = await execFileAsync(nvhBin, ['doctor', '--json'], {
      timeout: 15_000,
      maxBuffer: 4 * 1024 * 1024,
      env: { ...process.env, NVH_HOME: nvhHome() },
    });
    let parsed: unknown = null;
    try {
      parsed = JSON.parse(stdout);
    } catch {
      return {
        ran: true,
        format: 'text',
        binary: nvhBin,
        exitCode: 0,
        report: null,
        stdout: stdout.slice(-4000),
        stderr: stderr.slice(-1000),
        elapsedMs: Date.now() - t0,
      };
    }
    return {
      ran: true,
      format: 'json',
      binary: nvhBin,
      exitCode: 0,
      report: parsed,
      stdout: '',
      stderr: stderr.slice(-1000),
      elapsedMs: Date.now() - t0,
    };
  } catch (err) {
    const e = err as NodeJS.ErrnoException & {
      stdout?: string;
      stderr?: string;
      code?: number | string;
    };
    return {
      ran: false,
      format: 'error',
      binary: nvhBin,
      exitCode: typeof e.code === 'number' ? e.code : null,
      report: null,
      stdout: (e.stdout || '').slice(-2000),
      stderr: (e.stderr || e.message || '').slice(-2000),
      elapsedMs: Date.now() - t0,
    };
  }
}

/**
 * Pattern-match the captured logs + probes to suggest likely root causes.
 * The user is showing a phone photo of a report — three line hints are way
 * more useful than 80 lines of raw log. Each hint is { match: bool, label,
 * suggestion } so the UI can render the green/red checks cleanly.
 */
function diagnose(
  apiProbe: ServiceProbe,
  ollamaProbe: ServiceProbe,
  logs: LogTail[],
): { label: string; hit: boolean; suggestion: string }[] {
  const apiLog = logs.find(l => l.source === 'api');
  const apiText = (apiLog?.lines || []).join('\n');
  const installLog = logs.find(l => l.source === 'install');
  const installText = (installLog?.lines || []).join('\n');

  const hints: { label: string; hit: boolean; suggestion: string }[] = [];

  hints.push({
    label: 'FastAPI reachable on :8000',
    hit: apiProbe.reachable && apiProbe.httpStatus === 200,
    suggestion: !apiProbe.reachable
      ? 'API process is not listening. Click [Restart API] in the System Console.'
      : apiProbe.httpStatus !== 200
        ? `API responding HTTP ${apiProbe.httpStatus}. Engine likely failed to initialize — see API log.`
        : '',
  });

  hints.push({
    label: 'Engine initialized',
    hit: apiProbe.engineInitialized === true,
    suggestion: apiProbe.engineInitialized === false
      ? 'API is up but engine_initialized is false. The engine is still warming OR crashed during init — check API log for tracebacks.'
      : apiProbe.engineInitialized === null && apiProbe.reachable
        ? 'API responding but /v1/health body does not include engine_initialized — possibly older nvh build.'
        : '',
  });

  hints.push({
    label: 'Ollama reachable on :11434',
    hit: ollamaProbe.reachable && ollamaProbe.httpStatus === 200,
    suggestion: ollamaProbe.reachable
      ? ''
      : 'Ollama daemon not responding. Wizard will fall back to cloud providers if configured. Run `nvh workstation --all -y` to install + start Ollama.',
  });

  hints.push({
    label: 'No ImportError in API log',
    hit: !/ImportError|ModuleNotFoundError/.test(apiText),
    suggestion: /ImportError|ModuleNotFoundError/.test(apiText)
      ? 'API failed to import a dependency. Run: `pip install -e .[serve,nvidia]` from the nvHive repo.'
      : '',
  });

  hints.push({
    label: 'No "Address already in use" in API log',
    hit: !/Address already in use|address already in use|EADDRINUSE/.test(apiText),
    suggestion: /Address already in use|address already in use|EADDRINUSE/.test(apiText)
      ? 'Port 8000 is held by another process. Re-run install with NVH_PORT_CONFLICT_KILL_FOREIGN=1 or stop the conflicting process.'
      : '',
  });

  hints.push({
    label: 'No FATAL/Traceback in install log',
    hit: !/Traceback|FATAL|fatal error/.test(installText),
    suggestion: /Traceback|FATAL|fatal error/.test(installText)
      ? 'Install captured an error. Open the System Console → Install tab for the full trace.'
      : '',
  });

  return hints;
}

export async function GET() {
  const apiUrl = 'http://localhost:8000/v1/health';
  const ollamaUrl = 'http://localhost:11434/api/tags';

  // Fire every probe in parallel — total wall time ≈ slowest single probe.
  const [apiProbe, ollamaProbe, doctor, ...logs] = await Promise.all([
    probeService(apiUrl),
    probeService(ollamaUrl),
    runDoctor(),
    ...LOG_FILES.map(({ source, filename }) => tailLog(source, filename, 30)),
  ]);

  const diagnostics = diagnose(apiProbe, ollamaProbe, logs);

  return NextResponse.json({
    generatedAt: new Date().toISOString(),
    env: {
      nvhHome: nvhHome(),
      nvhBin: process.env.NVH_BIN || null,
      nvhLogs: nvhLogsDir(),
      nodeVersion: process.version,
      platform: `${os.platform()} ${os.release()} ${os.arch()}`,
    },
    services: {
      api: { url: apiUrl, ...apiProbe },
      ollama: { url: ollamaUrl, ...ollamaProbe },
    },
    logs,
    doctor,
    diagnostics,
  });
}
