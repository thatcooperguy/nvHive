#!/usr/bin/env python3
"""Offline guard tests: real Bash/Git, fake Docker, no installer downloads."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "ci/integration-test-install.sh"
BASH = shutil.which("bash") or ("C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else None)


def run(argv, **kwargs):
    return subprocess.run(argv, text=True, capture_output=True, timeout=60, **kwargs)


class HarnessGuards(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="nvh-installer-contract-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "candidate checkout"
        self.repo.mkdir()
        (self.repo / "ci").mkdir()
        shutil.copyfile(HARNESS, self.repo / "ci/integration-test-install.sh")
        (self.repo / "install.sh").write_text("#!/bin/bash\nexit 37\n", encoding="utf-8")
        self.env = os.environ.copy()
        for key in list(self.env):
            if key.startswith(("GIT_", "NVH_")) or key in ("BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS"):
                self.env.pop(key)
        self.env.update({
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1",
            "TMPDIR": self.root.as_posix(), "XDG_CONFIG_HOME": self.root.as_posix(),
        })
        self.git("init", "-q")
        self.git("add", ".")
        self.git("-c", "user.name=Installer Fixture", "-c", "user.email=fixture.invalid@example.invalid",
                 "-c", "commit.gpgsign=false", "commit", "-qm", "Isolated harness fixture")
        self.rev = self.git("rev-parse", "HEAD").strip()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.evidence = self.root / "evidence"
        docker = self.bin / "docker"
        # Deliberately stop at build. Copy its exact context before trap cleanup.
        docker.write_text(
            '#!/bin/bash\nset -euo pipefail\nprintf "%s\\n" "$@" > "$NVH_FIXTURE_CALL"\n'
            'if [ "$1" = build ]; then cp -R -- "${@: -1}" "$NVH_FIXTURE_CONTEXT"; fi\nexit 41\n',
            encoding="utf-8", newline="\n")
        docker.chmod(0o755)
        self.env.update({
            "PATH": self.bin.as_posix() + os.pathsep + self.env.get("PATH", ""),
            "NVH_FIXTURE_CALL": (self.root / "docker-call").as_posix(),
            "NVH_FIXTURE_CONTEXT": self.evidence.as_posix(),
        })

    def git(self, *args, cwd=None):
        p = run(["git", "-C", str(cwd or self.repo), *args], env=self.env)
        self.assertEqual(0, p.returncode, p.stderr)
        return p.stdout

    def harness(self, **extra):
        env = self.env | extra
        # Bash on Windows needs POSIX PATH separators for the fake executable.
        command = 'export PATH="$NVH_FIXTURE_BIN:$PATH"; bash ci/integration-test-install.sh'
        env["NVH_FIXTURE_BIN"] = self.bin.as_posix()
        return run([BASH, "-c", command], cwd=self.repo, env=env)

    def test_bundle_pins_candidate_and_excludes_untracked_and_git_config(self):
        (self.repo / "untracked-private.txt").write_text("fixture only", encoding="utf-8")
        self.git("config", "http.extraHeader", "fixture-private-header")
        p = self.harness(NVH_TEST_SKIP_MODEL="1")
        self.assertEqual(41, p.returncode, p.stdout + p.stderr)
        self.assertEqual({"candidate.bundle", "Dockerfile"}, {p.name for p in self.evidence.iterdir()})
        clone = self.root / "bundle-check"
        clone.mkdir()
        self.git("init", "-q", cwd=clone)
        self.git("fetch", "-q", str(self.evidence / "candidate.bundle"), "HEAD:refs/heads/main", cwd=clone)
        self.assertEqual(self.rev, self.git("rev-parse", "main", cwd=clone).strip())
        self.git("checkout", "-q", "main", cwd=clone)
        self.assertFalse((clone / "untracked-private.txt").exists())
        self.assertNotIn("fixture-private-header", (clone / ".git/config").read_text())

    def test_dirty_candidate_refused_before_docker(self):
        (self.repo / "install.sh").write_text("exit 0\n", encoding="utf-8")
        p = self.harness()
        self.assertEqual(2, p.returncode, p.stdout + p.stderr)
        self.assertFalse((self.root / "docker-call").exists())

    def test_invalid_options_refused_before_docker(self):
        for option in ({"NVH_TEST_SKIP_MODEL": "maybe"}, {"NVH_TEST_DOCKERFILE": "ignored-before"}):
            with self.subTest(option=option):
                p = self.harness(**option)
                self.assertEqual(2, p.returncode, p.stdout + p.stderr)
                self.assertFalse((self.root / "docker-call").exists())

    def test_shallow_candidate_refused_before_docker(self):
        shallow = self.root / "shallow"
        self.git("clone", "-q", "--depth=1", self.repo.as_uri(), str(shallow))
        self.repo = shallow
        p = self.harness()
        self.assertEqual(2, p.returncode, p.stdout + p.stderr)
        self.assertFalse((self.root / "docker-call").exists())

    def test_real_inner_pipeline_preserves_installer_failure(self):
        text = HARNESS.read_text(encoding="utf-8")
        match = re.search(r'docker exec "\$CONTAINER_ID" bash -c \'\n(.*?)\n\'', text, re.S)
        self.assertIsNotNone(match)
        # Relocate only the container's source directory; run its actual command.
        script = match[1].replace("cd /home/kiosk/candidate", 'cd "$NVH_FIXTURE_SOURCE"')
        env = self.env | {"NVH_FIXTURE_SOURCE": self.repo.as_posix(), "NVH_TEST_EXPECTED_REV": self.rev}
        p = run([BASH, "-c", script], env=env)
        self.assertEqual(37, p.returncode, p.stdout + p.stderr)


class LinuxConfigContract(unittest.TestCase):
    def test_actual_config_assignments_keep_installer_and_cli_path_equal(self):
        source = (ROOT / "install.sh").read_text(encoding="utf-8")
        statements = [line for line in source.splitlines()
                      if line.startswith(("NVH_CONFIG=", "HIVE_CONFIG_HOME="))]
        self.assertEqual(2, len(statements))
        script = "\n".join(statements) + '\nprintf "%s\\n%s\\n" "$NVH_CONFIG" "$HIVE_CONFIG_HOME"\n'
        cases = [({}, "/fixture/config"), ({"HIVE_CONFIG_HOME": "/legacy"}, "/legacy"),
                 ({"NVH_CONFIG": "/canonical"}, "/canonical"),
                 ({"NVH_CONFIG": "/canonical", "HIVE_CONFIG_HOME": "/legacy"}, "/canonical")]
        for inputs, expected in cases:
            with self.subTest(inputs=inputs):
                env = {k: v for k, v in os.environ.items()
                       if k not in ("NVH_CONFIG", "HIVE_CONFIG_HOME", "BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS")}
                env.update({"NVH_HOME": "/fixture", **inputs})
                p = run([BASH, "-c", script], env=env)
                self.assertEqual(0, p.returncode, p.stderr)
                self.assertEqual([expected, expected], p.stdout.splitlines())


if __name__ == "__main__":
    if not BASH:
        raise SystemExit("Bash is required; no guard was executed")
    unittest.main(verbosity=2)
