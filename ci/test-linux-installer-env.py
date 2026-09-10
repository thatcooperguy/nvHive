"""Execute generated Linux env/hooks/shims in private homes with stub services.

Run with Python's standard library; no installer downloads or real services run.
The installer functions are extracted verbatim, without evaluating its main body.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

PARSER = argparse.ArgumentParser()
PARSER.add_argument("--installer", type=Path, default=Path(__file__).resolve().parents[1] / "install.sh")
PARSER.add_argument("--bash", default=shutil.which("bash"))
OPTIONS, TEST_ARGS = PARSER.parse_known_args()
if not OPTIONS.bash:
    PARSER.error("Bash is required")
INSTALLER = OPTIONS.installer.read_text(encoding="utf-8")
NAMES = (
    "NVH_HOME NVH_VENV NVH_BIN NVH_MODELS NVH_CACHE NVH_LOGS NVH_STUDIO_HOME "
    "COMFYUI_HOME OLLAMA_MODELS HIVE_CONFIG_HOME NVH_CONFIG XDG_CACHE_HOME "
    "PIP_CACHE_DIR UV_CACHE_DIR HF_HOME HUGGINGFACE_HUB_CACHE TORCH_HOME TMPDIR"
).split()


def actual_function(name):
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}", INSTALLER, re.M | re.S)
    if match is None:
        raise AssertionError(f"Missing actual installer function: {name}")
    return match.group(0)


class LinuxShellPaths(unittest.TestCase):
    def setUp(self):
        fixture_parent = Path(__file__).resolve().parent
        self.temporary = tempfile.TemporaryDirectory(prefix="nvh-shell-contract-", dir=fixture_parent)
        self.root = Path(self.temporary.name)
        self.assertTrue(self.root.resolve().is_relative_to(fixture_parent))
        self.addCleanup(self.temporary.cleanup)
        self.environment = dict(os.environ)
        # Never load an operator's noninteractive shell startup file.
        for name in ("BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS"):
            self.environment.pop(name, None)
        self.environment["HOME"] = self.root.as_posix()
        self.posix_root = self.run_bash('printf "%s" "$PWD"').stdout.decode()
        self.environment["TEST_HOME"] = self.posix_root
        # NTFS permits these literal payloads. Double quotes are tested in saved
        # path values below, since a Windows directory cannot contain them.
        self.suffix = "vault space $(touch marker_dollar) `touch marker_backtick` 'quote"
        self.home = self.root / self.suffix
        self.home.mkdir()
        self.shell_home = f"{self.posix_root}/{self.suffix}"
        self.stub_bin = self.root / "stubs"
        self.stub_bin.mkdir()
        self.values = {name: f"{self.shell_home}/{name.lower()}" for name in NAMES}
        self.values["NVH_HOME"] = self.shell_home
        self.values["NVH_VENV"] = f"{self.shell_home}/venv"
        self.values["NVH_BIN"] = f"{self.shell_home}/bin"
        self.values["NVH_STUDIO_HOME"] += '"; touch marker_quote; #'
        self.values["COMFYUI_HOME"] += "'\"literal dollar $HOME and backslash\\"
        self.environment.update(self.values)
        self.environment.update({
            "TEST_STUB_BIN": f"{self.posix_root}/stubs",
            "TEST_RECORD": f"{self.posix_root}/service.log",
            "TEST_ARGS": f"{self.posix_root}/args.bin",
            "TEST_FUNCTIONS": f"{self.posix_root}/functions.sh",
            "TEST_ENV": f"{self.shell_home}/nvh-env.sh",
            "TEST_RC": f"{self.posix_root}/.bashrc",
            "TEST_PGREP_RUNNING": "0", "NVH_NO_OS_MOD": "0",
            "USE_ACTIVE_ENV": "false", "Y": "", "N": "",
        })
        source = "\n\n".join(actual_function(name) for name in (
            "write_nvh_env", "shell_rc_path", "install_shell_hook", "install_command_shims",
        ))
        (self.root / "functions.sh").write_text(source + "\n", encoding="utf-8", newline="\n")
        (self.root / ".bashrc").write_text("# keep this unrelated user line\n", encoding="utf-8")
        (self.home / "bin").mkdir()
        (self.home / "venv/bin").mkdir(parents=True)
        self.stub(self.stub_bin / "pgrep", 'printf "pgrep\\n" >> "$TEST_RECORD"\n[ "$TEST_PGREP_RUNNING" = "1" ] || exit 1\nprintf "%s\\n" "$NVH_BIN/ollama serve" | grep -Eq -- "$2"\n')
        self.stub(self.stub_bin / "curl", 'printf "curl\\n" >> "$TEST_RECORD"\nexit 1\n')
        self.stub(self.home / "bin/ollama", 'printf "ollama:%s\\n" "$1" >> "$TEST_RECORD"\nif [ "$1" = serve ]; then printf "%s\\0" "$OLLAMA_MODELS" > "$TEST_ARGS"; fi\n')
        self.stub(self.home / "venv/bin/nvh", 'printf "%s\\0" "$NVH_HOME" "$@" > "$TEST_ARGS"\nexit 17\n')
        self.stub(self.stub_bin / "python3", 'printf "%s\\0" "$@" > "$TEST_ARGS"\nexit 19\n')

    def stub(self, path, body):
        path.write_text("#!/bin/bash\n" + body, encoding="utf-8", newline="\n")
        path.chmod(0o700)

    def run_bash(self, script, *arguments, check=True):
        if "TEST_HOME" in self.environment:
            script = 'export HOME="$TEST_HOME"\n' + script
        result = subprocess.run(
            [OPTIONS.bash, "--noprofile", "--norc", "-c", script, "contract", *arguments],
            cwd=self.root, env=self.environment, stdin=subprocess.DEVNULL,
            capture_output=True, timeout=20,
        )
        if check:
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        return result

    def generate(self):
        self.run_bash('set -eu\nexport PATH="$TEST_STUB_BIN:$PATH"\nsource "$TEST_FUNCTIONS"\nwrite_nvh_env\ninstall_shell_hook\ninstall_shell_hook\ninstall_command_shims\n')

    def assert_no_payload(self):
        self.assertEqual(list(self.root.glob("marker_*")), [], "A literal path executed as shell source")

    def read_arguments(self):
        return (self.root / "args.bin").read_bytes().decode().split("\0")[:-1]

    def source_hook(self):
        return self.run_bash('export PATH="$TEST_STUB_BIN:$PATH"\nsource "$TEST_RC"\nwait\n')

    def test_env_roundtrips_all_saved_paths_and_receiving_path(self):
        self.generate()
        result = self.run_bash(
            'export PATH="/receiving-shell:$PATH"\nexpected_path="$PATH"\n'
            'source "$TEST_ENV"\n'
            'for name in ' + " ".join(NAMES) + ' TEMP TMP; do printf "%s\\0" "${!name}"; done\n'
            'printf "%s\\0%s\\0" "$PATH" "$expected_path"\n'
            , check=False
        )
        self.assert_no_payload()
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        fields = result.stdout.decode().split("\0")[:-1]
        self.assertEqual(fields[:len(NAMES)], [self.values[name] for name in NAMES])
        self.assertEqual(fields[len(NAMES):len(NAMES) + 2], [self.values["TMPDIR"]] * 2)
        prefix = f'{self.shell_home}/runtimes/node/current/bin:{self.values["NVH_VENV"]}/bin:{self.values["NVH_BIN"]}:'
        self.assertEqual(fields[-2], prefix + fields[-1])
        self.assert_no_payload()

    def test_login_hook_is_idempotent_and_only_starts_stub(self):
        self.generate()
        profile = (self.root / ".bashrc").read_text(encoding="utf-8")
        self.assertEqual(profile.count("# >>> nvhive rootless env >>>"), 1)
        self.assertIn("# keep this unrelated user line", profile)
        self.source_hook()
        self.assertEqual((self.root / "service.log").read_text().splitlines(), ["ollama:--version", "pgrep", "curl", "ollama:serve"])
        self.assertEqual(self.read_arguments(), [self.values["OLLAMA_MODELS"]])
        self.assert_no_payload()

    def test_existing_daemon_prevents_autostart(self):
        self.generate()
        self.environment["TEST_PGREP_RUNNING"] = "1"
        self.source_hook()
        self.assertEqual((self.root / "service.log").read_text().splitlines(), ["ollama:--version", "pgrep"])
        self.assert_no_payload()

    def test_missing_mount_hook_is_quiet(self):
        self.generate()
        (self.home / "nvh-env.sh").unlink()
        (self.home / "bin/ollama").unlink()
        result = self.source_hook()
        self.assertEqual(result.stderr, b"")
        self.assertFalse((self.root / "service.log").exists())
        self.assert_no_payload()

    def test_both_shims_preserve_arguments_and_exit_status(self):
        self.generate()
        arguments = ["ask", 'space and "quotes"', "$(touch marker_arg)", "`touch marker_arg2`", "", "--flag=value"]
        for name in ("nvh", "nvhive"):
            with self.subTest(name=name):
                result = self.run_bash('exec "$HOME/.local/bin/$1" "${@:2}"', name, *arguments, check=False)
                self.assertEqual(result.returncode, 17, result.stderr.decode(errors="replace"))
                self.assertEqual(self.read_arguments(), [self.shell_home, *arguments])
                self.assert_no_payload()

    def test_missing_venv_uses_python3_stub_and_preserves_arguments(self):
        self.generate()
        (self.home / "venv/bin/nvh").unlink()
        result = self.run_bash('export PATH="$TEST_STUB_BIN:$PATH"\nexec "$HOME/.local/bin/nvh" "$@"', "status", "with spaces", check=False)
        self.assertEqual(result.returncode, 19, result.stderr.decode(errors="replace"))
        self.assertEqual(self.read_arguments(), ["-m", "nvh.cli.main", "status", "with spaces"])
        self.assert_no_payload()

    def test_shell_profile_optouts_remain_effective(self):
        for name, value in (("NVH_NO_OS_MOD", "1"), ("USE_ACTIVE_ENV", "true")):
            with self.subTest(name=name):
                self.environment[name] = value
                self.run_bash('source "$TEST_FUNCTIONS"\ninstall_shell_hook\n')
                self.assertEqual((self.root / ".bashrc").read_text(), "# keep this unrelated user line\n")
                self.environment[name] = "0" if name == "NVH_NO_OS_MOD" else "false"
        self.assert_no_payload()


if __name__ == "__main__":
    unittest.main(argv=[__file__, *TEST_ARGS])
