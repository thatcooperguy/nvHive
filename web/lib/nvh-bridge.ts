import { existsSync, promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';

/**
 * Shared rootless-CLI bridge helpers used by:
 *   - /api/services/start-api  (spawn `nvh serve`)
 *   - /api/services/doctor     (execFile `nvh doctor --json`)
 *   - /api/debug/report        (same doctor call, aggregated alongside log
 *                               tails + service probes)
 *
 * Why a shared module: the resolver had a directory-vs-file bug that
 * shipped to all three routes simultaneously (PR #77 + #78). install.sh
 * exports NVH_BIN="$NVH_HOME/bin" — the rootless BIN DIRECTORY, not the
 * `nvh` executable. The previous resolver did `fs.access(NVH_BIN, X_OK)`
 * which succeeds for traversable directories (X_OK on a dir = "can list
 * entries"), so the resolver returned the directory string and every
 * spawn/execFile failed with EACCES. Real-rig debug report photo
 * 2026-05-21 surfaced this end-to-end: `binary=/home/kiosk/nvhive/bin
 * stderr: spawn /home/kiosk/nvhive/bin EACCES`.
 *
 * Fix: stop honoring NVH_BIN as the executable. Honor a clearer env var
 * NVH_BIN_EXE for the explicit override (must be a file), AND treat
 * NVH_BIN as the bin DIRECTORY (look for `nvh` inside). Plus require the
 * candidate to be a regular file via fs.stat, not just X_OK-accessible.
 */

/**
 * Resolve $NVH_HOME for the bridge routes.
 *
 * 2026-06-10 audit: the old one-liner fell back to ~/nvhive and (in the
 * logs route copy) claimed that matched "install.sh + storage.py default"
 * — it doesn't. install.sh defaults NVH_HOME to $HOME/.nvh (so does
 * storage.py); ~/nvhive is what the mount-autopilot picks on
 * persistent-$HOME rigs. When the Next.js server runs without NVH_HOME
 * exported (manual dev server, shell without nvh-env.sh sourced), the
 * bridges resolved binaries + logs under a directory the default install
 * never creates.
 *
 * Resolution order:
 *   1. NVH_HOME env — exported by install.sh / nvh-env.sh.
 *   2. ~/nvhive, if it exists — mount-autopilot persistent-home installs.
 *   3. ~/.nvh, if it exists — install.sh + storage.py default.
 *   4. ~/nvhive — last-resort fallback so error messages still name the
 *      canonical persistent-home path on machines with no install at all.
 */
export function nvhHome(): string {
  if (process.env.NVH_HOME) return process.env.NVH_HOME;
  const persistentHome = path.join(os.homedir(), 'nvhive');
  if (existsSync(persistentHome)) return persistentHome;
  const dotNvh = path.join(os.homedir(), '.nvh');
  if (existsSync(dotNvh)) return dotNvh;
  return persistentHome;
}

export function nvhLogsDir(): string {
  return process.env.NVH_LOGS || path.join(nvhHome(), 'logs');
}

/**
 * Returns true iff `candidate` is a regular file the current process can
 * execute. Critically, this returns FALSE for directories — that's the
 * whole point of the EACCES fix.
 */
async function isExecutableFile(candidate: string): Promise<boolean> {
  try {
    const stat = await fs.stat(candidate);
    if (!stat.isFile()) return false;
    await fs.access(candidate, fs.constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

/**
 * Resolve the `nvh` binary path the bridge routes should spawn/execFile.
 *
 * Resolution order:
 *   1. NVH_BIN_EXE — explicit FILE override. Must be a regular executable
 *      file. Provided for users who installed `nvh` somewhere exotic and
 *      want to point the bridges at it directly.
 *   2. NVH_BIN/nvh — install.sh's convention: NVH_BIN is the rootless bin
 *      DIRECTORY ($NVH_HOME/bin). Look for `nvh` inside it.
 *   3. Canonical rootless install paths:
 *        - $NVH_HOME/venv/bin/nvh      (pip-installed CLI entry point)
 *        - $NVH_HOME/bin/nvh           (rootless install shim)
 *        - ~/.local/bin/nvh            (user pip install location)
 *   4. Bare "nvh" — last resort, spawn() will use PATH lookup. If that
 *      also fails the route handler surfaces the ENOENT to the user.
 *
 * Returns the resolved string. Every step is validated to be a regular
 * file with execute permission (never a directory), so the caller can
 * trust the result against the EACCES failure mode.
 */
export async function resolveNvhBinary(): Promise<string> {
  const home = nvhHome();

  // 1. Explicit file override.
  const exeOverride = process.env.NVH_BIN_EXE;
  if (exeOverride && (await isExecutableFile(exeOverride))) {
    return exeOverride;
  }

  // 2. NVH_BIN-as-directory (install.sh convention).
  const binDirEnv = process.env.NVH_BIN;
  if (binDirEnv) {
    const candidate = path.join(binDirEnv, 'nvh');
    if (await isExecutableFile(candidate)) {
      return candidate;
    }
  }

  // 3. Canonical rootless install paths.
  const canonicalCandidates = [
    path.join(home, 'venv', 'bin', 'nvh'),
    path.join(home, 'bin', 'nvh'),
    path.join(os.homedir(), '.local', 'bin', 'nvh'),
  ];
  for (const candidate of canonicalCandidates) {
    if (await isExecutableFile(candidate)) {
      return candidate;
    }
  }

  // 4. Fall through to PATH lookup.
  return 'nvh';
}
