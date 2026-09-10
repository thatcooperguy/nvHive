#!/usr/bin/env python3
"""Run the real installer against disposable homes and inert command doubles.

No brew, Git, pip, model, network or user-shell-profile operation is performed.
This checks shell control flow, not a real macOS dependency installation.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


def shell_path(path: Path) -> str:
    value = path.resolve().as_posix()
    if os.name == "nt" and len(value) > 2 and value[1] == ":":
        return f"/{value[0].lower()}{value[2:]}"
    return value


def executable(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8", newline="\n")
    path.chmod(0o755)


STUBS = r'''#!/bin/bash
set -euo pipefail
name="${0##*/}"
printf '%s\n' "$name $*" >> "$TEST_LOG"
case "$name" in
  brew) [ "${1:-}" = --version ] && printf 'Homebrew fixture\n' || exit 97 ;;
  uname) printf 'x86_64\n' ;;
  git)
    case "${1:-}" in
      pull) exit "${GIT_FAIL:-0}" ;;
      clone)
        target="${@: -1}"
        case "$target" in "$TEST_ROOT"/*) ;; *) exit 98 ;; esac
        mkdir -p "$target/.git"
        ;;
      *) exit 97 ;;
    esac
    ;;
  pip)
    if [ "${PIP_FAIL:-0}" != 0 ]; then exit "$PIP_FAIL"; fi
    if [ "${NO_NVH:-0}" != 1 ]; then
      printf '#!/bin/bash\nexit 0\n' > "$NVH_HOME/venv/bin/nvh"
      chmod +x "$NVH_HOME/venv/bin/nvh"
    fi
    ;;
  python3.12)
    if [ "${1:-}" = --version ]; then printf 'Python 3.12 fixture\n'; exit 0; fi
    if [ "${1:-}" = -m ] && [ "${2:-}" = venv ]; then
      target="$3"
      case "$target" in "$TEST_ROOT"/*) ;; *) exit 98 ;; esac
      mkdir -p "$target/bin"
      printf 'export PATH="$NVH_HOME/venv/bin:$PATH"\n' > "$target/bin/activate"
      printf '#!/bin/bash\nexit 0\n' > "$target/bin/python3"
      # Tier discovery returns no model: no download is required by this test.
      printf '#!/bin/bash\nexit 0\n' > "$target/bin/python"
      chmod +x "$target/bin/python" "$target/bin/python3"
      exit 0
    fi
    exit 97
    ;;
  *) printf 'UNEXPECTED external command: %s\n' "$name" >&2; exit 97 ;;
esac
'''


class MacInstallerContract(unittest.TestCase):
    installer: Path
    bash: str

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="nvh-mac-contract-", dir=Path(__file__).resolve().parent)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "user home"
        self.install = self.root / "external drive" / "NVHive"
        self.config = self.root / "separate config"
        self.bin = self.root / "commands"
        self.log = self.root / "commands.log"
        self.home.mkdir()
        self.config.mkdir()
        (self.home / ".zshrc").write_text("# Existing user settings\n", encoding="utf-8")
        (self.config / "config.yaml").write_text("preserve: existing\n", encoding="utf-8")
        for name in ("brew", "uname", "git", "pip", "python3.12", "curl", "ollama", "sysctl",
                     "system_profiler", "unzip", "tar"):
            executable(self.bin / name, STUBS)
        self.env = {
            "HOME": shell_path(self.home),
            "PATH": shell_path(self.bin) + ":/usr/bin:/bin",
            "TEST_COMMAND_PATH": shell_path(self.bin) + ":/usr/bin:/bin",
            "TEST_COMMAND_BIN": shell_path(self.bin),
            "NVH_HOME": shell_path(self.install),
            "NVH_CONFIG": shell_path(self.config),
            "TEST_ROOT": shell_path(self.root),
            "TEST_LOG": shell_path(self.log),
            "LC_ALL": "C",
            # Required by Windows subprocess/runtime, not a shell profile.
            **{key: os.environ[key] for key in ("SystemRoot", "WINDIR", "TEMP", "TMP") if key in os.environ},
        }

    def existing(self, *, broken: bool = False, git: bool = True) -> None:
        (self.install / "repo").mkdir(parents=True)
        if git:
            (self.install / "repo/.git").mkdir()
        activate = self.install / "venv/bin/activate"
        executable(activate, 'export PATH="$NVH_HOME/venv/bin:$PATH"\n')
        if not broken:
            executable(self.install / "venv/bin/python3", "#!/bin/bash\nexit 0\n")
            executable(self.install / "venv/bin/nvh", "#!/bin/bash\nexit 0\n")

    def run_installer(self) -> subprocess.CompletedProcess[str]:
        # Git Bash prepends its own tools to inherited PATH at process startup.
        # Restore our doubles inside the shell before executing any installer
        # code; fail closed if a real dependency executable would be selected.
        launch = '''export PATH="$TEST_COMMAND_PATH"
for name in brew uname git pip python3.12 curl ollama sysctl system_profiler unzip tar; do
  [ "$(command -v "$name")" = "$TEST_COMMAND_BIN/$name" ] || exit 96
done
source "$1"'''
        result = subprocess.run(
            [self.bash, "--noprofile", "--norc", "-c", launch, "fixture", shell_path(self.installer)],
            env=self.env, cwd=self.root, text=True, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20, check=False,
        )
        self.assertNotIn("UNEXPECTED external command", result.stdout, result.stdout)
        return result

    def assert_persisted(self) -> None:
        env_file = self.install / "nvh-env.sh"
        self.assertTrue(env_file.is_file())
        # Re-enter through the real generated shell hook with no location
        # variables inherited, reproducing a later terminal session.
        env = dict(self.env)
        for key in ("NVH_HOME", "NVH_CONFIG", "HIVE_CONFIG_HOME", "NVHIVE_HOME"):
            env.pop(key, None)
        command = 'source "$HOME/.zshrc"; printf "%s\\0" "$NVH_HOME" "$NVH_CONFIG" "$HIVE_CONFIG_HOME" "$NVH_VENV" "$PATH"'
        shells = [[self.bash, "--noprofile", "--norc"]]
        # macOS CI also exercises the actual default shell's parsing of the
        # generated hook and literal environment values.
        if zsh := shutil.which("zsh"):
            shells.append([zsh, "-f"])
        for shell in shells:
            with self.subTest(shell=shell[0]):
                readback = subprocess.run(
                    [*shell, "-c", command], env=env, cwd=self.root,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, check=True,
                ).stdout.decode("utf-8").split("\0")
                self.assertEqual(readback[:4], [shell_path(self.install), shell_path(self.config),
                                               shell_path(self.config), shell_path(self.install / "venv")])
                self.assertTrue(readback[4].startswith(shell_path(self.install / "venv/bin") + ":"))
        self.assertEqual((self.config / "config.yaml").read_text(), "preserve: existing\n")
        self.assertTrue((self.home / ".zshrc").read_text().startswith("# Existing user settings\n"))

    def test_existing_custom_locations_survive_new_shell_and_reinstall(self) -> None:
        self.existing()
        first = self.run_installer()
        self.assertEqual(first.returncode, 0, first.stdout)
        self.assertIn("NVHive ready.", first.stdout)
        self.assert_persisted()
        before = (self.home / ".zshrc").read_bytes()
        second = self.run_installer()
        self.assertEqual(second.returncode, 0, second.stdout)
        self.assertEqual((self.home / ".zshrc").read_bytes(), before)

    def test_legacy_home_and_config_aliases_persist(self) -> None:
        self.existing()
        self.env["NVHIVE_HOME"] = self.env.pop("NVH_HOME")
        self.env["HIVE_CONFIG_HOME"] = self.env.pop("NVH_CONFIG")
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assert_persisted()

    def test_git_failure_stops_before_pip_or_ready(self) -> None:
        self.existing()
        self.env["GIT_FAIL"] = "23"
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("NVHive ready.", result.stdout)
        self.assertNotIn("pip ", self.log.read_text())
        self.assertFalse((self.install / "nvh-env.sh").exists())

    def test_package_failure_is_not_reported_ready(self) -> None:
        self.existing()
        self.env["PIP_FAIL"] = "24"
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("NVHive ready.", result.stdout)
        self.assertFalse((self.install / "nvh-env.sh").exists())

    def test_tarball_existing_install_reinstalls_without_git_pull(self) -> None:
        self.existing(git=False)
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stdout)
        log = self.log.read_text()
        self.assertNotIn("git pull", log)
        self.assertIn("pip install -q -e", log)
        self.assert_persisted()

    def test_healed_environment_persists_custom_locations(self) -> None:
        self.existing(broken=True)
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("Healing Python venv", result.stdout)
        self.assert_persisted()

    def test_healing_failure_does_not_report_ready(self) -> None:
        self.existing(broken=True)
        self.env["PIP_FAIL"] = "24"
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("NVHive ready.", result.stdout)

    def test_missing_command_does_not_report_ready(self) -> None:
        self.existing()
        (self.install / "venv/bin/nvh").unlink()
        # An unrelated global launcher cannot establish this venv is ready.
        executable(self.bin / "nvh", "#!/bin/bash\nexit 0\n")
        self.env["NO_NVH"] = "1"
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("NVHive ready.", result.stdout)
        self.assertFalse((self.install / "nvh-env.sh").exists())
        self.assertEqual((self.home / ".zshrc").read_text(), "# Existing user settings\n")

    def test_fresh_install_preserves_config_and_persists_shell(self) -> None:
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("NVHive is ready!", result.stdout)
        self.assert_persisted()

    def test_literal_shell_metacharacters_are_not_evaluated(self) -> None:
        # Deliberately use harmless echo substitution: the only desired value
        # is the literal path, not its shell-expanded spelling.
        self.install = self.root / "drive $(echo SUBSTITUTED) `echo BACKTICK` $NAME"
        self.config = self.root / "config 'quoted' $HOME"
        self.config.mkdir()
        (self.config / "config.yaml").write_text("preserve: existing\n", encoding="utf-8")
        self.env["NVH_HOME"] = shell_path(self.install)
        self.env["NVH_CONFIG"] = shell_path(self.config)
        self.existing()
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assert_persisted()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installer", type=Path, default=Path(__file__).resolve().parents[1] / "install-mac.sh")
    parser.add_argument("--bash", default=shutil.which("bash"))
    args = parser.parse_args()
    if not args.bash:
        parser.error("Bash is required; no installer was run")
    MacInstallerContract.installer = args.installer.resolve(strict=True)
    MacInstallerContract.bash = args.bash
    unittest.main(argv=[__file__], verbosity=2)
