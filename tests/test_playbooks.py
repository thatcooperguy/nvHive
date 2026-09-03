"""Spark playbooks (nvh/integrations/installs/playbooks.py).

Hermetic throughout: ``NVH_HOME`` is ``tmp_path``, platform facts are seeded
(never probed), the login name is ``alice``, and ``subprocess`` inside
:mod:`system_settings` is a recording fake — both drivers spawn through the
one ``run_host_command`` there (the job path as the Wizard does, the terminal
path with ``interactive=True``). Nothing here runs ``sudo``, ``apt``,
``docker`` or ``curl`` for real; the jobs module is the real one, in-process,
writing under ``tmp_path``.

Invariants pinned (design brief phase 2b §2-4, §9):

  - Every id is an upstream folder name; every rendered step, check and
    verify command of every playbook passes ``denied_reason()``; every
    playbook has sources and a verify list, and an undo when it needs sudo.
  - Pipe-to-shell is download-then-run, flagged ``pipe-to-shell: unpinned``
    with the upstream one-liner quoted; comfy-ui verifies the README's sha256.
  - Docker playbooks start with the ``usermod -aG docker`` step; when it runs
    the run halts with the re-login note — never ``newgrp``.
  - The runner skips steps whose check exits 0, stops at the first failure,
    hands off with ONE command (``nvh playbook install <id>``) when sudo needs
    a password, writes a receipt with an honest, sticky ``no_root`` and one
    vault ``Decisions/`` note per run that touched the host.
  - ``WizardToolRegistry.execute()`` stays the single enforcement point: card
    with plan and token, confirmed run starts the job, kill switch refuses.
"""

from __future__ import annotations

import asyncio
import json
import re
import shlex
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import nvh.core.local_models as lm
import nvh.integrations.installs.playbooks as pb
import nvh.integrations.wizard.system_settings as ss
from nvh.config.settings import _DEFAULT_LOCAL_BUDGET_GB
from nvh.integrations.services import jobs, receipts
from nvh.integrations.wizard.tools import PRIVILEGED_ENV, audit_privileged_change, default_registry
from nvh.utils import platform_facts as pf

FIRST_TIER = (
    "ollama", "cli-coding-agent", "open-webui", "comfy-ui", "dgx-dashboard", "vscode",
    "tailscale", "vllm", "llama-cpp", "lm-studio", "openclaw", "nemoclaw",
)
DEFERRED_IDS = ("sglang", "nim-llm", "nemotron", "unsloth", "connect-two-sparks", "connect-to-your-spark")
DOCKER_PLAYBOOKS = ("open-webui", "vllm", "openclaw", "nemoclaw")
#: Never in a rendered command (policies (b), (e) and the deny list's spirit).
FORBIDDEN_SUBSTRINGS = (
    "apt upgrade", "apt-get upgrade", "dist-upgrade", "newgrp", "passwd", "useradd", "userdel",
    "nvidia-driver", "reboot", "shutdown", "pip install", "npm install",
)
PIPE_TO_SHELL = re.compile(r"\|\s*(sh|bash)\b")
SPARK_LABEL = "NVIDIA DGX Spark (GB10, 128 GB unified)"


# ───────────────────────────────────────────────────────────────────────────
# Fixtures and doubles
# ───────────────────────────────────────────────────────────────────────────


def seed(
    *, can_sudo: bool = False, in_sudo_group: bool = False, has_root: bool = False, host_probe_pending: bool = False,
) -> None:
    pf.seed_platform_facts(pf.PlatformFacts(
        os="linux", arch="arm64", machine="aarch64", distro="DGX OS 7.2", kernel="6.11.0-1016-nvidia",
        is_dgx_os=True, gpu_name="NVIDIA GB10", unified_memory=True, memory_total_gb=128.0,
        memory_available_gb=100.0, device_class="dgx-spark", device_label=SPARK_LABEL,
        has_root=has_root, can_sudo=can_sudo, in_sudo_group=in_sudo_group or can_sudo or has_root,
        host_probe_pending=host_probe_pending,
    ))


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NVH_HOME", str(tmp_path))
    monkeypatch.delenv(PRIVILEGED_ENV, raising=False)
    monkeypatch.setattr(ss, "_current_user", lambda: "alice")
    monkeypatch.setattr(pb, "_user_home", lambda: "/home/alice")
    seed()


class FakeSubprocess:
    """Stands in for the ``subprocess`` module; ``responder(argv) -> (code, stdout, stderr)``."""

    DEVNULL = -3
    PIPE = -1

    class TimeoutExpired(Exception):  # noqa: N818 — must match subprocess's name
        def __init__(self, cmd, timeout):
            super().__init__(f"timeout {timeout}")

    def __init__(self, responder=None) -> None:
        self.calls: list[list[str]] = []
        self.kwargs: list[dict[str, Any]] = []
        self.responder = responder or (lambda argv: (0, "done\n", ""))

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        self.kwargs.append(kwargs)
        answer = self.responder(list(argv))
        if isinstance(answer, BaseException):
            raise answer
        code, out, err = answer
        return SimpleNamespace(returncode=code, stdout=out.encode(), stderr=err.encode())


@pytest.fixture()
def fake_host(monkeypatch) -> FakeSubprocess:
    """The job path: ``run_host_command`` inside system_settings."""
    fake = FakeSubprocess()
    monkeypatch.setattr(ss, "subprocess", fake)
    return fake


@pytest.fixture()
def fake_terminal(monkeypatch) -> FakeSubprocess:
    """The CLI path: the same ``run_host_command`` with ``interactive=True`` — so the same ``subprocess`` seam."""
    fake = FakeSubprocess()
    monkeypatch.setattr(ss, "subprocess", fake)
    return fake


def _dir(tmp_path: Path, playbook_id: str) -> str:
    return (tmp_path / "playbooks" / playbook_id).as_posix()


def _decisions(tmp_path: Path) -> list[Path]:
    folder = tmp_path / "vault" / "Decisions"
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.glob("*.md") if "#privileged" in p.read_text(encoding="utf-8"))


async def _drain(job_id: str) -> list[dict[str, Any]]:
    """Wait for the job task, then return the persisted event payloads in order."""
    task = jobs._TASKS.get(job_id)
    if task is not None:
        await task
    return [record["payload"] for record in jobs.read_events(job_id, limit=500)]


def _is_check(argv: list[str], needle: str) -> bool:
    return " ".join(argv).find(needle) >= 0


# ───────────────────────────────────────────────────────────────────────────
# The catalogue
# ───────────────────────────────────────────────────────────────────────────


def test_first_tier_ids_trace_to_upstream_and_deferred_is_disjoint() -> None:
    assert [p.id for p in pb.PLAYBOOKS] == list(FIRST_TIER)
    for playbook in pb.PLAYBOOKS:
        assert pb.get_playbook(playbook.id) is playbook
        assert pb.get_playbook(playbook.id.upper()) is playbook
        assert playbook.source_urls and playbook.source_urls[0] == pb.UPSTREAM_TREE + playbook.id
    assert [d.id for d in pb.DEFERRED] == list(DEFERRED_IDS)
    assert not set(DEFERRED_IDS) & set(FIRST_TIER)
    for entry in pb.deferred():
        assert entry["reason"] and entry["source_url"] == pb.UPSTREAM_TREE + entry["id"]
    assert pb.get_playbook("nope") is None and pb.get_playbook(None) is None  # type: ignore[arg-type]


@pytest.mark.parametrize("playbook_id", FIRST_TIER)
def test_every_rendered_command_passes_the_deny_list(playbook_id: str) -> None:
    """Steps, checks and verify commands — rendered as they would run — all pass ``denied_reason()``."""
    playbook = pb.get_playbook(playbook_id)
    assert playbook is not None
    plan = pb.compile_plan(playbook)
    assert plan.denied() is None
    ctx = pb._context(playbook)
    rendered = list(plan.commands())
    for step in playbook.executable_steps():
        if step.check:
            argv = pb._render_argv(step.check, ctx)
            assert ss.denied_reason(argv) is None, (step.title, argv)
            rendered.append(shlex.join(argv))
    for argv in playbook.verify:
        argv = pb._render_argv(argv, ctx)
        assert ss.denied_reason(argv) is None, argv
        rendered.append(shlex.join(argv))
    assert rendered or not playbook.executable_steps()
    for text in rendered:
        assert "@" not in text, text  # every placeholder resolved
        assert not PIPE_TO_SHELL.search(text), text
        for bad in FORBIDDEN_SUBSTRINGS:
            assert bad not in text, (bad, text)


@pytest.mark.parametrize("playbook_id", FIRST_TIER)
def test_playbook_invariants(playbook_id: str) -> None:
    playbook = pb.get_playbook(playbook_id)
    assert playbook is not None
    assert playbook.title and playbook.summary and playbook.category and playbook.risk
    assert playbook.prerequisites and playbook.estimated_minutes > 0
    assert playbook.source_urls and playbook.verify, "every playbook has sources and a verify list"
    if playbook.requires_sudo:
        assert playbook.undo, "a playbook that needs sudo must carry an undo preview"
    for step in playbook.steps:
        assert step.title
        assert step.is_manual != bool(step.argv), step.title  # manual xor executable
        if step.unpinned:
            assert step.upstream, "unpinned steps quote the upstream command verbatim"
            if step.argv and step.argv[0] in {"sh", "bash"}:
                assert "|" in step.upstream, "a download-then-run step quotes the upstream pipe"
        # A shell script word never carries a placeholder: paths reach `bash -c` only as
        # positional parameters, so a quote or `$` in NVH_HOME cannot break or inject.
        for argv in (step.argv, step.check or ()):
            if tuple(argv[:2]) in {("bash", "-c"), ("sh", "-c")}:
                assert "@" not in argv[2], (step.title, argv[2])
        assert "newgrp" not in " ".join(step.argv)
        if step.halt_after:
            assert "newgrp" not in step.halt_after and "log out" in step.halt_after.lower()
    if playbook_id in DOCKER_PLAYBOOKS:
        first = playbook.executable_steps()[0]
        assert first.argv == ("usermod", "-aG", "docker", pb.USER_TOKEN) and first.sudo is True
        assert first.check == pb.DOCKER_GROUP_CHECK and first.halt_after == pb.RELOGIN_NOTE
    assert playbook.requires_sudo == any(s.sudo for s in playbook.executable_steps())
    assert playbook.sudo_steps == sum(1 for s in playbook.executable_steps() if s.sudo)
    for argv in playbook.verify:
        if tuple(argv[:2]) in {("bash", "-c"), ("sh", "-c")}:
            assert "@" not in argv[2], argv


def test_shell_scripts_take_paths_as_positional_parameters(tmp_path: Path) -> None:
    """An apostrophe, a space, `;`, `#` or a `$(…)` in NVH_HOME neither breaks a `bash -c` step nor injects into it.

    ``nvh_home`` expands ``$VAR`` references on purpose (``NVH_HOME=$HOME/.nvh`` is a supported
    setting), so a ``$HOME`` in this directory name is consumed by that layer and never reaches
    the shell -- on Linux CI it became ``/home/runner`` and a literal expectation mismatched.
    A command substitution survives the expansion, so it is the honest injection probe, and
    the expected path goes through the same resolution as the rendered one on both platforms.
    """
    odd = tmp_path / "it's $(touch pwned); #"
    comfy = pb.get_playbook("comfy-ui")
    plan = pb.compile_plan(comfy, home_dir=odd)
    assert plan.denied() is None
    verify_setup = plan.steps[2]
    assert verify_setup.argv[:2] == ("bash", "-c") and "$1" in verify_setup.argv[2] and "@" not in verify_setup.argv[2]
    expected = f"{(odd.resolve() / 'playbooks' / 'comfy-ui').as_posix()}/setup.sh"
    assert expected == f"{(pb.playbooks_root(odd) / 'comfy-ui').as_posix()}/setup.sh"  # the NVH_HOME resolution
    assert "$(touch pwned)" in expected  # the probe survived nvh_home's $VAR expansion
    assert verify_setup.argv[3:] == ("sha256sum", expected)
    assert "touch pwned" not in verify_setup.argv[2]  # the path is data, not script
    assert shlex.split(verify_setup.render()) == list(verify_setup.argv)  # the card shows exactly the argv that runs
    lm = pb.get_playbook("lm-studio")
    ctx = pb._context(lm, odd)
    check = pb._render_argv(lm.executable_steps()[0].check, ctx)
    assert check[:2] == ("bash", "-c") and "$1/.lmstudio/bin/lms" in check[2] and check[-1] == "/home/alice"


def _rendered_pull_tag(plan: ss.Plan) -> str:
    """The one ``ollama pull <tag>`` command of a compiled plan, as the tag."""
    pulls = [shlex.split(c) for c in plan.commands() if shlex.split(c)[:2] == ["ollama", "pull"]]
    assert len(pulls) == 1 and len(pulls[0]) == 3, plan.commands()
    return pulls[0][2]


def test_ollama_model_tag_comes_from_the_tier_table(monkeypatch) -> None:
    """The ollama playbook pulls this host's chat pick from nvh.core.local_models -- never a hand-typed tag.

    The catalogue carries the ``@MODEL_CHAT@`` placeholder (title, pull, check and the laptop's
    curl) and ``compile_plan`` renders it from the platform facts: the seeded 128 GB unified
    Spark gets the table's top chat pick that fits the pool after its OS reserve, neutral facts
    (no unified pool, or one the facts could not size) get the table's default -- the tag
    ``hive config init`` names -- and facts that blow up still render, never crash.
    """
    ollama = pb.get_playbook("ollama")
    pull = next(s for s in ollama.executable_steps() if s.argv[:2] == ("ollama", "pull"))
    assert pull.argv == ("ollama", "pull", pb.MODEL_CHAT_TOKEN) and pull.check == ("ollama", "show", pb.MODEL_CHAT_TOKEN)
    assert pb.MODEL_CHAT_TOKEN in pull.title and pb.MODEL_CHAT_GB_TOKEN in pull.title
    curl_step = next(s for s in ollama.manual_steps() if s.title.startswith("Validate API connectivity"))
    assert f'"model": "{pb.MODEL_CHAT_TOKEN}"' in curl_step.manual
    for step in ollama.steps:  # no literal tag anywhere: every `ollama pull|show` word is the placeholder
        for argv in (step.argv, step.check or ()):
            if argv[:1] == ("ollama",) and argv[1:2] in {("pull",), ("show",)}:
                assert argv[2:] == (pb.MODEL_CHAT_TOKEN,), (step.title, argv)

    # Seeded Spark facts (128 GB unified, 16 GB OS reserve -> 112 GB budget): the top chat pick that fits.
    spark = lm.tier_budget([SimpleNamespace(vram_mb=128 * 1024, unified_memory=True)])
    expected = lm.pick(spark, "chat")
    assert spark.unified and spark.budget_gb == 112.0 and expected is lm.tier_for(spark).picks["chat"]
    tag = _rendered_pull_tag(pb.compile_plan(ollama))
    assert tag == expected.tag and tag in lm.all_tags() and lm.pick_for_tag(tag) is expected
    detail = pb.plan_dict("ollama")
    step = next(s for s in detail["steps"] if s["command"] == f"ollama pull {tag}")
    assert step["check"] == f"ollama show {tag}"
    assert step["title"] == f"Download and verify a language model ({tag}, about {expected.weights_gb:g} GB)"
    curl = [m for m in detail["manual_steps"] if m.startswith("Validate API connectivity")]
    assert len(curl) == 1 and f'"model": "{tag}"' in curl[0]
    assert any(tag in note for note in detail["notes"])  # the card says where the tag came from
    assert "@" not in json.dumps(detail, default=str)  # every placeholder resolved, titles and notes included
    rows = {row["id"]: row for row in pb.catalogue()}
    assert "@" not in json.dumps(rows["ollama"], default=str)

    # Neutral facts (no unified pool, no sized GPU): the table's default, and never the Spark's pick.
    pf.seed_platform_facts(pf.PlatformFacts(os="linux", arch="x86_64"))
    fallback = lm.pick(_DEFAULT_LOCAL_BUDGET_GB, "chat")
    assert fallback.tag != expected.tag and fallback.tag in lm.all_tags()
    assert _rendered_pull_tag(pb.compile_plan(ollama)) == fallback.tag

    # A unified pool the facts could not size is neutral too.
    pf.seed_platform_facts(pf.PlatformFacts(os="linux", arch="arm64", unified_memory=True, memory_total_gb=0.0))
    assert _rendered_pull_tag(pb.compile_plan(ollama)) == fallback.tag

    def boom():
        raise RuntimeError("facts exploded")

    monkeypatch.setattr(ss, "_facts", boom)
    assert _rendered_pull_tag(pb.compile_plan(ollama)) == fallback.tag


def test_lm_studio_model_step_is_manual_prose_without_a_pinned_tag() -> None:
    """LM Studio's catalogue ids are not Ollama tags, so the table cannot pick for it: the step is words."""
    lm_studio = pb.get_playbook("lm-studio")
    model = next(s for s in lm_studio.manual_steps() if s.title.startswith("Download and load a model"))
    assert "lms get" in model.manual and "lms load" in model.manual and "lms ls" in model.manual
    assert "Nemotron" in model.manual and "nemotron-" not in model.manual.lower()  # a family in words, not a tag
    assert "@" not in model.manual and "nvidia/" not in model.manual


def test_pipe_to_shell_is_download_then_run_and_flagged(tmp_path: Path) -> None:
    ollama = pb.get_playbook("ollama")
    plan = pb.compile_plan(ollama)
    target = f"{_dir(tmp_path, 'ollama')}/ollama-install.sh"
    assert plan.commands()[:2] == [
        shlex.join(["curl", "-fsSL", "https://ollama.com/install.sh", "-o", target]),
        shlex.join(["sudo", "sh", target]),  # install.sh uses sudo inside, so the run is honest about it
    ]
    flagged = [n for n in plan.notes if n.startswith(pb.UNPINNED_NOTE)]
    assert len(flagged) == 1 and "`curl -fsSL https://ollama.com/install.sh | sh`" in flagged[0]
    detail = pb.plan_dict("ollama")
    assert detail["unpinned"] is True
    assert detail["steps"][0]["unpinned"] is True and detail["steps"][0]["sudo"] is False
    assert detail["steps"][1]["unpinned"] is True and detail["steps"][1]["sudo"] is True
    assert detail["steps"][1]["upstream"] == "curl -fsSL https://ollama.com/install.sh | sh"
    assert detail["rootless_alternative"] == "rootless-ollama"

    # comfy-ui publishes sha256 sums: download, verify, run — no pipe, no sudo.
    comfy = pb.compile_plan(pb.get_playbook("comfy-ui"))
    assert comfy.needs_sudo is False
    joined = "\n".join(comfy.commands())
    assert "97b03fb341b40bd8524549b234883427dda2e8bca4ceb1662a074dcc9a7cf3f8" in joined
    assert "7dc75b155a198a49537832c4a363d321080b130be0a6945a0bc0afe78da8badc" in joined
    assert sum(1 for c in comfy.commands() if "sha256sum -c -" in c) == 2
    assert not any(PIPE_TO_SHELL.search(c) for c in comfy.commands())
    assert comfy.commands()[3] == shlex.join(["env", "-C", _dir(tmp_path, "comfy-ui"), "bash", "setup.sh"])

    # vscode's "latest stable" .deb is neither pinned nor checksummed and is installed as
    # root: the same `unpinned` flag, rendered as an "unpinned download" note (no pipe).
    vscode = pb.plan_dict("vscode")
    assert vscode["unpinned"] is True and all(step["unpinned"] for step in vscode["steps"])
    assert "|" not in vscode["steps"][0]["upstream"] and "sudo dpkg -i vscode-arm64.deb" in vscode["steps"][0]["upstream"]
    notes = [n for n in vscode["notes"] if n.startswith(pb.UNPINNED_DOWNLOAD_NOTE)]
    assert len(notes) == 1 and "pins no version" in notes[0] and not any(n.startswith(pb.UNPINNED_NOTE) for n in vscode["notes"])
    assert vscode["commands"] == [
        shlex.join(["wget", pb._VSCODE_DEB_URL, "-O", f"{_dir(tmp_path, 'vscode')}/vscode-arm64.deb"]),
        shlex.join(["sudo", "apt-get", "install", "-y", f"{_dir(tmp_path, 'vscode')}/vscode-arm64.deb"]),
    ]  # one install step: apt resolves the .deb's dependencies, so no unreachable `apt-get install -f` follows a failed dpkg


def test_compile_plan_shapes(tmp_path: Path) -> None:
    tailscale = pb.compile_plan(pb.get_playbook("tailscale"))
    assert tailscale.name == "tailscale" and tailscale.needs_sudo is True
    assert "10 command(s), 8 with sudo, and leaves 3 manual step(s)" in tailscale.changes
    manual = [n for n in tailscale.notes if n.startswith("MANUAL:")]
    assert len(manual) == 3 and "sudo tailscale up" in manual[0]  # browser login stays manual
    assert tailscale.undo[0].startswith("sudo tailscale down") and tailscale.warning
    assert tailscale.commands()[0] == "sudo apt update" and tailscale.commands()[7] == "sudo apt install -y tailscale"
    assert "sudo install -m 0644" in tailscale.commands()[3]  # key installed from NVH_HOME, not piped through tee
    # The playbook may install and enable sshd (steps 9-10); the undo and the warning both say so.
    assert tailscale.commands()[8] == "sudo apt install -y openssh-server" and "systemctl enable ssh" in tailscale.commands()[9]
    assert any(line.startswith("sudo systemctl disable ssh --now") and "only if" in line for line in tailscale.undo)
    assert any(line.startswith("sudo apt remove openssh-server") and "only if" in line for line in tailscale.undo)
    assert "openssh-server" in tailscale.warning and "every interface" in tailscale.warning

    # Services published on every interface carry a warning, not just a note.
    for playbook_id, port in (("open-webui", "8080"), ("vllm", "8000")):
        published = pb.compile_plan(pb.get_playbook(playbook_id))
        assert f"port {port}" in published.warning.lower() and "every interface" in published.warning
        assert any(f"127.0.0.1:{port}:{port}" in n for n in published.notes)  # the local-only variant is spelled out
    assert "administrator" in pb.compile_plan(pb.get_playbook("open-webui")).warning
    assert "no API key" in pb.compile_plan(pb.get_playbook("vllm")).warning

    llama = pb.compile_plan(pb.get_playbook("llama-cpp"))
    clone = f"{_dir(tmp_path, 'llama-cpp')}/llama.cpp"
    assert llama.commands()[2] == shlex.join(["git", "clone", "https://github.com/ggml-org/llama.cpp", clone])
    assert llama.commands()[3].startswith(shlex.join(["env", "-C", clone, "cmake", "-B", "build"]))
    assert "-DCMAKE_CUDA_ARCHITECTURES=121a-real" in llama.commands()[3]
    assert llama.undo[0] == f"rm -rf {clone}"

    webui = pb.compile_plan(pb.get_playbook("open-webui"))
    assert webui.commands()[0] == "sudo usermod -aG docker alice"
    assert "sudo gpasswd -d alice docker" in webui.undo
    assert any(pb.RELOGIN_NOTE in n for n in webui.notes)
    assert "newgrp" not in "\n".join(webui.commands())

    dashboard = pb.compile_plan(pb.get_playbook("dgx-dashboard"))
    assert dashboard.steps == () and dashboard.needs_sudo is False
    assert dashboard.to_dict()["commands"] == [] and dashboard.to_dict()["ok"] is True
    assert sum(1 for n in dashboard.notes if n.startswith("MANUAL:")) == 4
    assert "Update Now" in dashboard.warning

    vllm = pb.compile_plan(pb.get_playbook("vllm"))
    assert "nvcr.io/nvidia/vllm:26.05.post1-py3" in vllm.commands()[1]  # pinned, never :latest
    assert not any(":latest" in c for c in vllm.commands())


def test_compile_plan_needs_a_valid_login_name_only_where_a_step_uses_it(monkeypatch) -> None:
    monkeypatch.setattr(ss, "_current_user", lambda: "")
    with pytest.raises(pb.PlaybookError, match="login name"):
        pb.compile_plan(pb.get_playbook("open-webui"))
    assert pb.compile_plan(pb.get_playbook("vscode")).needs_sudo is True  # no @USER@ anywhere
    detail = pb.plan_dict("open-webui")
    assert detail["ok"] is False and "login name" in detail["error"] and detail["commands"] == []
    assert len(pb.catalogue()) == 12  # the catalogue never raises


def test_plan_dict_reports_elevation_from_the_platform_facts() -> None:
    detail = pb.plan_dict("tailscale")
    assert detail["ok"] is True and detail["id"] == "tailscale" and detail["sudo"] is True
    assert detail["needs_terminal_expected"] is True and detail["can_elevate"] is False  # neutral facts
    assert detail["handoff_command"] == "nvh playbook install tailscale"
    assert detail["estimates"] == {"minutes": 25, "disk_gb": 0.1}
    assert "estimated_minutes" not in detail and "estimated_disk_gb" not in detail  # one estimates shape
    assert detail["sudo_steps"] == 8 and detail["steps_total"] == 10  # counted once, on the playbook
    assert detail["steps"][0]["check"] == "dpkg -s tailscale" and detail["steps"][0]["sudo"] is True
    assert detail["verify"][0] == "tailscale version" and len(detail["manual_steps"]) == 3
    assert detail["prerequisites"] and detail["source_urls"] == [pb.UPSTREAM_TREE + "tailscale"]
    assert detail["commands"] == pb.compile_plan(pb.get_playbook("tailscale")).commands()

    seed(can_sudo=True)
    detail = pb.plan_dict("tailscale")
    assert detail["needs_terminal_expected"] is False and detail["can_elevate"] is True
    seed(in_sudo_group=True)
    detail = pb.plan_dict("tailscale")
    assert detail["needs_terminal_expected"] is True and detail["can_elevate"] is True
    seed()
    assert pb.plan_dict("comfy-ui")["needs_terminal_expected"] is False  # no sudo → no hand-off

    unknown = pb.plan_dict("nope")
    assert unknown["ok"] is False and "unknown playbook" in unknown["error"]
    assert unknown["commands"] == [] and unknown["playbooks"] == list(FIRST_TIER)


def test_every_entry_point_refuses_with_the_same_shape(monkeypatch) -> None:
    """plan_dict, start_run and run_in_terminal share one resolver, so an unknown id or an unplannable host is answered alike."""
    answers = [pb.plan_dict("nope"), pb.start_run("nope"), pb.run_in_terminal("nope", assume_yes=True)]
    for answer in answers:
        assert answer["ok"] is False and answer["applied"] is False and answer["commands"] == []
        assert "unknown playbook 'nope'" in answer["error"] and answer["playbooks"] == list(FIRST_TIER)
    monkeypatch.setattr(ss, "_current_user", lambda: "")
    answers = [pb.plan_dict("open-webui"), pb.start_run("open-webui"), pb.run_in_terminal("open-webui", assume_yes=True)]
    for answer in answers:
        assert answer["ok"] is False and answer["applied"] is False and answer["commands"] == []
        assert "login name" in answer["error"] and answer["id"] == "open-webui" and answer["playbook"] == "open-webui"


def test_catalogue_reports_receipt_status(tmp_path: Path) -> None:
    rows = {row["id"]: row for row in pb.catalogue()}
    assert list(rows) == list(FIRST_TIER)
    vscode = rows["vscode"]
    assert vscode["requires_sudo"] is True and vscode["sudo_steps"] == 1 and vscode["manual_steps"] == 2
    assert vscode["steps_total"] == 2 and vscode["installed"] is False and vscode["receipt_path"] is None
    assert vscode["rootless_alternative"] is None and vscode["estimated_disk_gb"] == 0.2
    assert vscode["sudo_step_titles"] == ["Install VS Code (apt resolves the .deb's dependencies)"]
    assert vscode["unpinned"] is True and rows["comfy-ui"]["unpinned"] is False
    assert rows["ollama"]["rootless_alternative"] == "rootless-ollama"
    assert rows["nemoclaw"]["rootless_alternative"] == "nemoclaw-sandbox"
    assert rows["openclaw"]["rootless_alternative"] == "openclaw-agent"
    assert rows["dgx-dashboard"]["requires_sudo"] is False and rows["dgx-dashboard"]["steps_total"] == 0
    assert rows["open-webui"]["manual"][0].startswith("Create the administrator account")
    assert all(row["handoff_command"] == f"nvh playbook install {row['id']}" for row in rows.values())

    receipts.write_receipt(
        kind="playbook", item_id="vscode", title="VS Code", install_path=tmp_path / "playbooks" / "vscode", no_root=False,
    )
    rows = {row["id"]: row for row in pb.catalogue()}
    assert rows["vscode"]["installed"] is True and rows["vscode"]["receipt_status"] == "installed"
    assert Path(rows["vscode"]["receipt_path"]) == receipts.receipt_path("playbook", "vscode")
    assert rows["ollama"]["installed"] is False

    receipts.write_receipt(kind="playbook", item_id="vscode", title="VS Code", install_path=tmp_path, status="partial")
    assert {r["id"]: r for r in pb.catalogue()}["vscode"]["installed"] is False
    assert all(row["installed"] is False for row in pb.catalogue(home_dir=tmp_path / "other"))


# ───────────────────────────────────────────────────────────────────────────
# The job runner (start_run → services.jobs → run_host_command)
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_run_skips_done_steps_and_writes_receipt_and_audit(fake_host: FakeSubprocess, tmp_path: Path) -> None:
    seed(can_sudo=True)
    state = {"installed": False}

    def responder(argv: list[str]):
        if _is_check(argv, "dpkg -s code | grep"):
            return (0 if state["installed"] else 1, "", "")
        if argv[:3] == ["sudo", "-n", "apt-get"]:
            state["installed"] = True
            return (0, "Unpacking code (1.104.0)\n", "")
        return (0, "ok\n", "")

    fake_host.responder = responder
    out = pb.start_run("vscode")
    assert out["ok"] is True and out["applied"] is False  # the start is not an apply
    assert out["playbook"] == "vscode" and out["steps_total"] == 2 and out["sudo"] is True
    assert out["needs_terminal_expected"] is False and out["handoff_command"] == "nvh playbook install vscode"
    events = await _drain(out["job_id"])
    assert events[0]["event"] == "plan" and events[0]["steps_total"] == 2 and events[0]["playbook"] == "vscode"
    assert events[-1]["event"] == "complete"
    job = jobs.load_job(out["job_id"])
    assert job["kind"] == "playbook-run" and job["status"] == "complete" and job["request"]["playbook"] == "vscode"

    deb = f"{_dir(tmp_path, 'vscode')}/vscode-arm64.deb"
    check = ["bash", "-c", "dpkg -s code | grep -q 'Status: install ok installed'"]
    assert fake_host.calls == [
        check,
        ["wget", "https://code.visualstudio.com/sha/download?build=stable&os=linux-deb-arm64", "-O", deb],
        check,
        ["sudo", "-n", "apt-get", "install", "-y", deb],
        ["dpkg", "-s", "code"],  # verify
        ["code", "--version"],
    ]
    assert all(kw["stdin"] == FakeSubprocess.DEVNULL for kw in fake_host.kwargs)  # nothing can prompt
    done = [e for e in events if e["event"] == "step" and e["status"] == "complete"]
    assert [e.get("skipped", False) for e in done] == [False, False]
    assert [e["step"] for e in done] == [0, 1] and done[1]["command"] == shlex.join(["sudo", "apt-get", "install", "-y", deb])
    assert any(e["event"] == "log" and "Unpacking code" in e["message"] for e in events)
    final = events[-1]
    assert final["applied"] is True and final["partial"] is False and final["no_root"] is False
    assert final["steps_run"] == 2 and final["steps_total"] == 2 and final["audit"]["saved"] is True
    assert [v["ok"] for v in final["verify"]] == [True, True]
    assert Path(final["receipt_path"]) == receipts.receipt_path("playbook", "vscode")

    receipt = receipts.load_receipt("playbook:vscode")
    assert receipt["kind"] == "playbook" and receipt["status"] == "installed" and receipt["no_root"] is False
    assert receipt["install_path"] == _dir(tmp_path, "vscode") and receipt["health"]["install_path_exists"] is True
    assert receipt["metadata"]["repair_command"] == "nvh playbook install vscode"
    assert receipt["metadata"]["undo"] == list(pb.get_playbook("vscode").undo)
    assert receipt["metadata"]["requires_sudo"] is True
    assert [s["exit_code"] for s in receipt["metadata"]["steps"]] == [0, 0]

    # A second run skips both steps (the check passes) and reports so.
    fake_host.calls.clear()
    events = await _drain(pb.start_run("vscode")["job_id"])
    skipped = [e for e in events if e["event"] == "step" and e["status"] == "complete"]
    assert [e.get("skipped", False) for e in skipped] == [True, True]
    assert any(e["event"] == "log" and e.get("skipped") and "already done" in e["message"] for e in events)
    assert events[-1]["applied"] is False and events[-1]["steps_run"] == 0 and fake_host.calls[0] == check
    repair = receipts.repair_plan("playbook:vscode")
    assert repair["commands"] == ["nvh playbook install vscode"] and repair["safe_to_run_without_root"] is False
    uninstall = receipts.uninstall_plan("playbook:vscode")
    assert uninstall["commands"] == list(pb.get_playbook("vscode").undo)
    assert uninstall["safe_to_run_without_root"] is False and uninstall["destructive"] is True

    notes = _decisions(tmp_path)
    assert len(notes) == 1 and Path(final["audit"]["path"]) == notes[0]
    text = notes[0].read_text(encoding="utf-8")
    assert text.startswith("# Privileged change: Install the vscode playbook")
    assert "#privileged" in text and "#playbook_install" in text and '"id": "vscode"' in text
    assert f"`{shlex.join(['sudo', '-n', 'apt-get', 'install', '-y', deb])}` — exit 0" in text and "Unpacking code" in text
    assert SPARK_LABEL in text and "Outcome: applied" in text


@pytest.mark.asyncio
async def test_start_run_stops_at_the_first_failure(fake_host: FakeSubprocess, tmp_path: Path) -> None:
    seed(can_sudo=True)

    def responder(argv: list[str]):
        if argv[:2] == ["dpkg", "-s"]:
            return (1, "", "dpkg-query: package 'clang' is not installed")
        if argv[:4] == ["sudo", "-n", "apt", "install"]:
            return (100, "", "E: Unable to locate package clang")
        return (0, "", "")

    fake_host.responder = responder
    out = pb.start_run("llama-cpp")
    events = await _drain(out["job_id"])
    final = events[-1]
    assert final["event"] == "error" and "exited 100" in final["error"] and final.get("needs_terminal") is None
    assert final["applied"] is True and final["partial"] is True and final["no_root"] is False and final["steps_run"] == 2
    assert jobs.load_job(out["job_id"])["status"] == "failed"
    deps = ["git", "clang", "cmake", "libcurl4-openssl-dev", "libssl-dev"]
    assert fake_host.calls == [
        ["dpkg", "-s", *deps],
        ["sudo", "-n", "apt", "update"],
        ["dpkg", "-s", *deps],
        ["sudo", "-n", "apt", "install", "-y", *deps],
    ]  # no git clone, no cmake: the run stopped
    failed = [e for e in events if e["event"] == "step" and e["status"] == "failed"]
    assert len(failed) == 1 and failed[0]["step"] == 1 and failed[0]["exit_code"] == 100
    assert not any(e.get("verify") for e in events)  # verify only after a complete run

    receipt = receipts.load_receipt("playbook:llama-cpp")
    assert receipt["status"] == "failed" and receipt["no_root"] is False
    text = _decisions(tmp_path)[0].read_text(encoding="utf-8")
    assert text.startswith("# Privileged change (partial): Install the llama-cpp playbook")
    assert f"Outcome: partial — `{shlex.join(['sudo', '-n', 'apt', 'install', '-y', *deps])}` exited 100" in text
    assert "`sudo -n apt update` — exit 0" in text and "Unable to locate package clang" in text


@pytest.mark.asyncio
async def test_start_run_hands_off_one_command_when_sudo_needs_a_password(fake_host: FakeSubprocess, tmp_path: Path) -> None:
    seed(in_sudo_group=True)  # sudo group, no passwordless sudo
    fake_host.responder = lambda argv: (1, "", "")  # every check says "not done"
    out = pb.start_run("tailscale")
    assert out["needs_terminal_expected"] is True
    events = await _drain(out["job_id"])
    handoff = [e for e in events if e["event"] == "needs_terminal"]
    assert len(handoff) == 1
    assert handoff[0]["command"] == "nvh playbook install tailscale" and handoff[0]["step"] == 0
    assert handoff[0]["step_command"] == "sudo apt update" and handoff[0]["hint"] == ss.NEEDS_TERMINAL_HINT
    final = events[-1]
    assert final["event"] == "error" and final["needs_terminal"] is True
    assert final["command"] == "nvh playbook install tailscale" and final["applied"] is False and final["partial"] is True
    assert fake_host.calls == [["dpkg", "-s", "tailscale"]]  # the check ran; no sudo was spawned
    with pytest.raises(KeyError):
        receipts.load_receipt("playbook:tailscale")
    assert _decisions(tmp_path) == []  # a hand-off that ran nothing is not an apply

    seed()  # neither passwordless sudo nor the sudo group
    out = pb.start_run("tailscale")
    events = await _drain(out["job_id"])
    assert events[-1]["event"] == "error" and events[-1]["error"] == ss.CANNOT_ELEVATE_ERROR
    assert events[-1]["applied"] is False and len(fake_host.calls) == 2
    assert _decisions(tmp_path) == []


@pytest.mark.asyncio
async def test_docker_group_step_halts_with_the_relogin_note_and_no_root_stays_honest(
    fake_host: FakeSubprocess, tmp_path: Path,
) -> None:
    seed(can_sudo=True)
    in_group = {"yes": False}

    def responder(argv: list[str]):
        if argv == list(pb.DOCKER_GROUP_CHECK):
            return (0 if in_group["yes"] else 1, "", "")
        if argv[:2] == ["docker", "image"] or argv[:2] == ["docker", "container"]:
            return (1, "", "No such object")
        return (0, "", "")

    fake_host.responder = responder
    out = pb.start_run("open-webui")
    events = await _drain(out["job_id"])
    final = events[-1]
    assert final["event"] == "complete" and final["halted"] is True and final["partial"] is True
    assert final["applied"] is True and final["no_root"] is False and final["steps_run"] == 1
    assert "log out and back in" in final["message"].lower() and "newgrp" not in final["message"]
    assert fake_host.calls == [list(pb.DOCKER_GROUP_CHECK), ["sudo", "-n", "usermod", "-aG", "docker", "alice"]]
    assert any(e["event"] == "log" and e["message"].startswith("MANUAL:") for e in events)
    receipt = receipts.load_receipt("playbook:open-webui")
    assert receipt["status"] == "partial" and receipt["no_root"] is False
    assert _decisions(tmp_path)[0].read_text(encoding="utf-8").startswith(
        "# Privileged change (partial): Install the open-webui playbook",
    )

    # After the re-login: the group check passes, the sudo step is skipped and
    # the docker steps run without sudo — yet the receipt remembers the sudo.
    in_group["yes"] = True
    fake_host.calls.clear()
    out = pb.start_run("open-webui")
    events = await _drain(out["job_id"])
    final = events[-1]
    assert final["event"] == "complete" and final.get("halted") is None and final["partial"] is False
    assert final["applied"] is True and final["no_root"] is True and final["steps_run"] == 2
    assert fake_host.calls[0] == list(pb.DOCKER_GROUP_CHECK)
    assert ["docker", "pull", "ghcr.io/open-webui/open-webui:ollama"] in fake_host.calls
    assert fake_host.calls[4][:3] == ["docker", "run", "-d"] and "--name" in fake_host.calls[4]
    assert ["docker", "start", "open-webui"] not in fake_host.calls  # running check exits 0 → skipped
    assert not any(c[:2] == ["sudo", "-n"] for c in fake_host.calls)
    receipt = receipts.load_receipt("playbook:open-webui")
    assert receipt["status"] == "installed" and receipt["no_root"] is False  # sticky: a sudo step ran last time
    assert len(_decisions(tmp_path)) == 2


@pytest.mark.asyncio
async def test_cancel_mid_step_still_writes_the_receipt_and_the_audit(fake_host: FakeSubprocess, tmp_path: Path) -> None:
    """POST /v1/jobs/{id}/cancel → cancel_job → task.cancel(): the CancelledError lands inside the engine's
    ``await run_cmd``. The step already done and the one in flight (its thread cannot be interrupted, so the
    command may finish on the host) both reach the receipt and the vault note before the job marks itself canceled."""
    seed(can_sudo=True)
    started, release = threading.Event(), threading.Event()

    def responder(argv: list[str]):
        if argv[:2] == ["dpkg", "-s"]:
            return (1, "", "")  # nothing installed yet
        if argv[:4] == ["sudo", "-n", "apt", "install"]:
            started.set()
            release.wait(10)  # blocks the worker thread like a real apt would
            return (0, "Setting up curl\n", "")
        return (0, "", "")

    fake_host.responder = responder
    out = pb.start_run("tailscale")  # step 1: sudo -n apt update; step 2: sudo -n apt install -y curl gnupg (blocks)
    task = jobs._TASKS[out["job_id"]]
    try:
        for _ in range(500):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        jobs.cancel_job(out["job_id"])
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()

    job = jobs.load_job(out["job_id"])
    assert job["status"] == "canceled"
    payloads = [record["payload"] for record in jobs.read_events(out["job_id"], limit=500)]
    assert payloads[-1]["event"] == "canceled"
    assert fake_host.calls[-1] == ["sudo", "-n", "apt", "install", "-y", "curl", "gnupg"]

    receipt = receipts.load_receipt("playbook:tailscale")
    assert receipt["status"] == "partial" and receipt["no_root"] is False  # a sudo step ran; another was spawned
    assert receipt["metadata"]["outcome"] == "canceled" and receipt["metadata"]["requires_sudo"] is True
    steps = receipt["metadata"]["steps"]
    assert [s["exit_code"] for s in steps] == [0, None] and steps[1].get("canceled") is True
    assert steps[1]["command"] == "sudo apt install -y curl gnupg"
    assert receipts.repair_plan("playbook:tailscale")["safe_to_run_without_root"] is False

    notes = _decisions(tmp_path)
    assert len(notes) == 1
    text = notes[0].read_text(encoding="utf-8")
    assert text.startswith("# Privileged change (partial): Install the tailscale playbook")
    assert "Outcome: partial — canceled by the user during step 2 (Install curl and gnupg)" in text
    assert "may have completed on the host" in text and "`sudo -n apt update` — exit 0" in text
    assert "exit None" in text  # the in-flight command, recorded without pretending to know its exit code


@pytest.mark.asyncio
async def test_partial_run_before_the_first_sudo_step_is_not_a_rootless_repair(fake_host: FakeSubprocess, tmp_path: Path) -> None:
    """ollama: the user-space download runs, then `sudo sh` needs a password. The receipt is honest
    (``no_root: True`` — nothing with sudo ran) but the repair still needs sudo, and says so."""
    seed(in_sudo_group=True)
    fake_host.responder = lambda argv: (1, "", "") if argv[:2] == ["bash", "-c"] else (0, "", "")  # `command -v ollama`: missing
    out = pb.start_run("ollama")
    events = await _drain(out["job_id"])
    final = events[-1]
    assert final["event"] == "error" and final["needs_terminal"] is True
    assert final["applied"] is True and final["partial"] is True and final["no_root"] is True and final["steps_run"] == 1
    assert fake_host.calls[1][:2] == ["curl", "-fsSL"] and not any(call[:1] == ["sudo"] for call in fake_host.calls)

    receipt = receipts.load_receipt("playbook:ollama")
    assert receipt["status"] == "partial" and receipt["no_root"] is True and receipt["metadata"]["requires_sudo"] is True
    repair = receipts.repair_plan("playbook:ollama")
    assert repair["commands"] == ["nvh playbook install ollama"]
    assert repair["safe_to_run_without_root"] is False and "sudo" in repair["reason"] and "terminal" in repair["reason"]
    uninstall = receipts.uninstall_plan("playbook:ollama")
    assert uninstall["commands"] == list(pb._OLLAMA_UNDO) and uninstall["safe_to_run_without_root"] is False  # `sudo …` lines
    assert len(_decisions(tmp_path)) == 1  # the download touched NVH_HOME: audited


@pytest.mark.asyncio
async def test_manual_only_playbook_completes_without_touching_the_host(fake_host: FakeSubprocess, tmp_path: Path) -> None:
    out = pb.start_run("dgx-dashboard")
    assert out["ok"] is True and out["steps_total"] == 0 and out["sudo"] is False
    events = await _drain(out["job_id"])
    final = events[-1]
    assert final["event"] == "complete" and final["applied"] is False and final["steps_run"] == 0
    assert len(final["manual_steps"]) == 4 and "4 manual step(s)" in final["message"]
    assert [v["ok"] for v in final["verify"]] == [True, True]
    assert fake_host.calls == [
        ["curl", "-sf", "-o", "/dev/null", "http://localhost:11000"],
        ["cat", "/opt/nvidia/dgx-dashboard-service/jupyterlab_ports.yaml"],
    ]
    receipt = receipts.load_receipt("playbook:dgx-dashboard")
    assert receipt["status"] == "installed" and receipt["no_root"] is True
    assert _decisions(tmp_path) == [] and final["audit"] is None


@pytest.mark.asyncio
async def test_start_run_refuses_before_starting_a_job(monkeypatch, fake_host: FakeSubprocess) -> None:
    out = pb.start_run("nope")
    assert out["ok"] is False and out["applied"] is False and "unknown playbook" in out["error"]
    monkeypatch.setattr(ss, "_current_user", lambda: "")
    out = pb.start_run("vllm")
    assert out["ok"] is False and "login name" in out["error"]
    monkeypatch.setattr(ss, "denied_reason", lambda argv: "BLOCKED: test")
    out = pb.start_run("vscode")
    assert out["ok"] is False and out["denied"] is True and out["error"] == "BLOCKED: test"
    assert jobs.list_jobs(kind="playbook-run") == [] and fake_host.calls == []


# ───────────────────────────────────────────────────────────────────────────
# The terminal path (CLI): interactive sudo, same receipt and audit
# ───────────────────────────────────────────────────────────────────────────


def test_run_in_terminal_uses_interactive_sudo_and_writes_the_same_receipt(fake_terminal: FakeSubprocess, tmp_path: Path) -> None:
    seed(in_sudo_group=True)  # no passwordless sudo — the terminal asks, nvHive never does
    state = {"installed": False}

    def responder(argv: list[str]):
        if _is_check(argv, "dpkg -s code | grep"):
            return (0 if state["installed"] else 1, "", "")
        if argv[:2] == ["sudo", "apt-get"]:
            state["installed"] = True
            return (0, "Unpacking code\n", "")
        return (0, "ok\n", "")

    fake_terminal.responder = responder
    emitted: list[dict[str, Any]] = []
    out = pb.run_in_terminal("vscode", assume_yes=True, echo=False, emit=emitted.append)
    assert out["ok"] is True and out["event"] == "complete" and out["applied"] is True and out["no_root"] is False
    deb = f"{_dir(tmp_path, 'vscode')}/vscode-arm64.deb"
    install = ["sudo", "apt-get", "install", "-y", deb]
    assert fake_terminal.calls[3] == install  # plain sudo, no -n
    assert fake_terminal.kwargs[3]["stdin"] is None  # the terminal's stdin, so sudo can ask there
    assert fake_terminal.kwargs[0]["stdin"] == FakeSubprocess.DEVNULL  # a check needs no terminal: as on the job path
    assert all(kw["capture_output"] is True and "stdout" not in kw for kw in fake_terminal.kwargs)  # echo=False captures
    assert [e["event"] for e in emitted] == [e["event"] for e in out["events"]] and emitted[-1] is out["events"][-1]
    receipt = receipts.load_receipt("playbook:vscode")
    assert receipt["status"] == "installed" and receipt["no_root"] is False and receipt["metadata"]["mode"] == "terminal"
    notes = _decisions(tmp_path)
    assert len(notes) == 1 and "# Privileged change: Install the vscode playbook" in notes[0].read_text(encoding="utf-8")
    assert f"`{shlex.join(install)}` — exit 0" in notes[0].read_text(encoding="utf-8")

    # Declined confirmation: nothing runs, and the answer is the shared refusal shape.
    fake_terminal.calls.clear()
    out = pb.run_in_terminal("vscode", assume_yes=False, echo=False, confirm=lambda prompt: False)
    assert out["ok"] is False and out["canceled"] is True and out["applied"] is False and fake_terminal.calls == []
    assert out["commands"] and out["playbook"] == "vscode"

    # Confirmed with echo: step output streams to the terminal (stdout=None) while
    # checks and verify stay captured; the prompt names the counts.
    state["installed"] = False
    fake_terminal.calls.clear()
    fake_terminal.kwargs.clear()
    prompts: list[str] = []
    out = pb.run_in_terminal("vscode", assume_yes=False, echo=True, confirm=lambda p: prompts.append(p) or True)
    assert out["ok"] is True and prompts == ["Run 2 command(s) (1 with sudo) for the vscode playbook?"]
    index = fake_terminal.calls.index(install)
    streamed = fake_terminal.kwargs[index]
    assert streamed["stdout"] is None and streamed["stderr"] is None and "capture_output" not in streamed
    assert streamed["stdin"] is None
    assert fake_terminal.kwargs[0]["capture_output"] is True  # the first call is a check
    assert pb.run_in_terminal("nope", assume_yes=True)["ok"] is False

    # The manual-only playbook gets its own question (the CLI used to retype both).
    prompts.clear()
    out = pb.run_in_terminal("dgx-dashboard", assume_yes=False, echo=False, confirm=lambda p: prompts.append(p) or False)
    assert out["canceled"] is True
    assert prompts == ["The dgx-dashboard playbook has no commands to run (4 manual step(s)). Record it and list the manual steps?"]


def _vscode_responder(argv: list[str]):
    """Every check but vscode's own says "already done"; the install itself is what runs."""
    return (1, "", "") if _is_check(argv, "dpkg -s code | grep") else (0, "ok\n", "")


def test_terminal_path_is_the_shared_runner(monkeypatch, fake_terminal: FakeSubprocess, tmp_path: Path) -> None:
    """One runner: the CLI path is ``run_host_command(interactive=True)``, not a twin with its own ``subprocess``."""
    assert pb.run_host_command is ss.run_host_command
    assert not hasattr(pb, "_terminal_command") and not hasattr(pb, "subprocess")
    seed(in_sudo_group=True)
    calls: list[tuple[list[str], dict[str, Any]]] = []
    real = ss.run_host_command

    def recording(argv, **kwargs):
        calls.append((list(argv), dict(kwargs)))
        return real(argv, **kwargs)

    monkeypatch.setattr(pb, "run_host_command", recording)
    fake_terminal.responder = _vscode_responder
    out = pb.run_in_terminal("vscode", assume_yes=True, echo=True)
    assert out["ok"] is True
    deb = f"{_dir(tmp_path, 'vscode')}/vscode-arm64.deb"
    steps = [(argv, kw) for argv, kw in calls if kw.get("interactive")]
    checks = [(argv, kw) for argv, kw in calls if not kw.get("interactive")]
    assert steps and checks and len(steps) + len(checks) == len(calls)
    # Steps: interactive, echo passed through, sudo and sudo_user from the Step — the runner decides the prefix.
    assert steps[-1][0] == ["apt-get", "install", "-y", deb]
    assert steps[-1][1] == {"sudo": True, "sudo_user": None, "timeout": steps[-1][1]["timeout"], "interactive": True, "echo": True}
    assert ["sudo", "apt-get", "install", "-y", deb] in fake_terminal.calls  # plain sudo, no -n
    assert not any(call[:2] == ["sudo", "-n"] for call in fake_terminal.calls)
    # Checks and verify probes: exactly the job path's call — no terminal, no sudo, the check timeout.
    assert all(kw == {"sudo": False, "timeout": pb.CHECK_TIMEOUT_S} for _, kw in checks)


def test_terminal_path_deny_list_holds_inside_the_runner(monkeypatch, fake_terminal: FakeSubprocess) -> None:
    """With the plan-level gate out of the way, the runner still refuses before any spawn — one deny list, one place."""
    seed(in_sudo_group=True)
    monkeypatch.setattr(pb, "_denied", lambda playbook, plan: None)
    real = ss.denied_reason
    monkeypatch.setattr(ss, "denied_reason", lambda argv: "BLOCKED: test" if argv[:1] == ["sudo"] else real(argv))
    fake_terminal.responder = _vscode_responder
    out = pb.run_in_terminal("vscode", assume_yes=True, echo=False)
    assert out["ok"] is False and out["error"] == "BLOCKED: test"
    failed = [e for e in out["events"] if e["event"] == "step" and e["status"] == "failed"]
    assert len(failed) == 1 and failed[0]["denied"] is True and failed[0]["sudo"] is True
    assert not any(call[:1] == ["sudo"] for call in fake_terminal.calls)


def test_terminal_path_refuses_an_account_that_cannot_elevate_before_spawning_sudo(fake_terminal: FakeSubprocess, tmp_path: Path) -> None:
    seed()  # neither root, passwordless sudo nor the sudo group: sudo would only fail three prompts later
    fake_terminal.responder = _vscode_responder
    out = pb.run_in_terminal("vscode", assume_yes=True, echo=False)
    assert out["ok"] is False and out["error"] == ss.CANNOT_ELEVATE_ERROR
    assert not any(call[:1] == ["sudo"] for call in fake_terminal.calls)
    # A privilege probe still in flight carries placeholder answers (a cold cache in a fresh CLI
    # process): that is not a "no", so sudo itself decides — as the old terminal twin always let it.
    seed(host_probe_pending=True)
    fake_terminal.calls.clear()
    out = pb.run_in_terminal("vscode", assume_yes=True, echo=False)
    assert out["ok"] is True
    assert ["sudo", "apt-get", "install", "-y", f"{_dir(tmp_path, 'vscode')}/vscode-arm64.deb"] in fake_terminal.calls


def test_terminal_path_spawn_failures_are_in_band(fake_terminal: FakeSubprocess) -> None:
    seed(in_sudo_group=True)
    fake_terminal.responder = lambda argv: FileNotFoundError("sudo") if argv[:1] == ["sudo"] else _vscode_responder(argv)
    out = pb.run_in_terminal("vscode", assume_yes=True, echo=False)
    assert out["ok"] is False and out["error"] == "sudo: command not found"
    fake_terminal.responder = (
        lambda argv: FakeSubprocess.TimeoutExpired(argv, 900) if argv[:1] == ["sudo"] else _vscode_responder(argv)
    )
    out = pb.run_in_terminal("vscode", assume_yes=True, echo=False)
    assert out["ok"] is False and out["error"].startswith("timed out after")
    failed = [e for e in out["events"] if e["event"] == "step" and e["status"] == "failed"]
    assert len(failed) == 1 and failed[0]["denied"] is False


# ───────────────────────────────────────────────────────────────────────────
# The three Wizard tools through the registry — execute() is the enforcement point
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tools_through_the_registry(monkeypatch, fake_host: FakeSubprocess, tmp_path: Path) -> None:
    seed(can_sudo=True)
    fake_host.responder = lambda argv: (1, "", "") if _is_check(argv, "dpkg -s code | grep") else (0, "done\n", "")
    reg = default_registry()
    for name, cls in (("playbook_list", "auto"), ("playbook_plan", "auto"), ("playbook_install", "privileged")):
        assert reg.get(name).safety_class == cls
        assert not any("passw" in key.lower() for key in reg.get(name).parameters)
    assert reg.get("playbook_install").planner is not None

    listed = await reg.execute("playbook_list")
    assert listed["ok"] is True and listed["safety_class"] == "auto"
    assert [row["id"] for row in listed["result"]["playbooks"]] == list(FIRST_TIER) and listed["result"]["count"] == 12
    assert [d["id"] for d in listed["result"]["deferred"]] == list(DEFERRED_IDS)

    planned = await reg.execute("playbook_plan", arguments={"id": "vscode"})
    deb = f"{_dir(tmp_path, 'vscode')}/vscode-arm64.deb"
    assert planned["ok"] is True and planned["result"]["commands"][1] == shlex.join(["sudo", "apt-get", "install", "-y", deb])
    assert planned["result"]["needs_terminal_expected"] is False and fake_host.calls == []
    # The blocking handlers share system_settings' wrapper (``_threaded``): a handler that raises
    # answers with the same `{ok: False, error: "<label> failed: …", commands: []}` as system_settings_plan.
    real_plan_dict = pb.plan_dict

    def boom(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("boom")

    pb.plan_dict = boom  # type: ignore[assignment]
    try:
        broken = await reg.execute("playbook_plan", arguments={"id": "vscode"})
    finally:
        pb.plan_dict = real_plan_dict  # type: ignore[assignment]
    assert broken["ok"] is True and broken["result"] == {"ok": False, "error": "playbook_plan failed: RuntimeError: boom", "commands": []}

    card = await reg.execute("playbook_install", arguments={"id": "vscode"})
    assert card["needs_confirmation"] is True and card["privileged"] is True
    assert card["plan"]["commands"] == planned["result"]["commands"] and card["plan"]["sudo"] is True
    assert card["summary"] == "Install the vscode playbook" and isinstance(card["approval_token"], str)
    assert fake_host.calls == [] and jobs.list_jobs(kind="playbook-run") == []

    refused = await reg.execute("playbook_install", arguments={"id": "vscode"}, confirmed=True)
    assert refused["approval_required"] is True
    swapped = await reg.execute("playbook_install", arguments={"id": "tailscale"}, confirmed=True, approval_token=card["approval_token"])
    assert swapped["approval_required"] is True  # the token is bound to the card's id
    assert fake_host.calls == []

    done = await reg.execute("playbook_install", arguments={"id": "vscode"}, confirmed=True, approval_token=card["approval_token"])
    assert done["ok"] is True and done["result"]["playbook"] == "vscode" and done["result"]["steps_total"] == 2
    assert isinstance(done["result"]["job_id"], str)
    assert "audit" not in done  # starting the job is not the apply; the runner audits when it finishes
    events = await _drain(done["result"]["job_id"])
    assert events[-1]["event"] == "complete" and events[-1]["applied"] is True
    assert len(_decisions(tmp_path)) == 1  # exactly one note, written by the runner
    again = await reg.execute("playbook_install", arguments={"id": "vscode"}, confirmed=True, approval_token=card["approval_token"])
    assert again["approval_required"] is True  # single use

    progress = await reg.execute("playbook_list", arguments={"job_id": done["result"]["job_id"]})
    assert progress["result"]["job"]["kind"] == "playbook-run" and progress["result"]["job"]["status"] == "complete"
    assert {r["id"]: r for r in progress["result"]["playbooks"]}["vscode"]["installed"] is True
    assert "job_error" in (await reg.execute("playbook_list", arguments={"job_id": "nope"}))["result"]

    bad = await reg.execute("playbook_install", arguments={"id": "nope"})
    assert bad["plan"]["ok"] is False and bad["plan"]["commands"] == []
    confirmed_bad = await reg.execute("playbook_install", arguments={"id": "nope"}, confirmed=True, approval_token=bad["approval_token"])
    assert confirmed_bad["ok"] is True and confirmed_bad["result"]["ok"] is False and "audit" not in confirmed_bad

    monkeypatch.setenv(PRIVILEGED_ENV, "0")
    off = await reg.execute("playbook_install", arguments={"id": "vscode"}, confirmed=True)
    assert off["disabled"] is True and "NVH_ALLOW_PRIVILEGED=0" in off["error"]
    assert (await reg.execute("playbook_list"))["ok"] is True  # auto tools are untouched


def test_audit_privileged_change_is_the_shared_sink(tmp_path: Path) -> None:
    alt = tmp_path / "alt-home"
    result = {
        "ok": True, "applied": True,
        "steps": [{"command": "sudo -n apt install -y tailscale", "exit_code": 0, "stdout": "Setting up tailscale", "stderr": ""}],
    }
    status = audit_privileged_change("playbook_install", {"id": "tailscale"}, result, summary="Install the tailscale playbook", home_dir=alt)
    assert status["saved"] is True and status["category"] == "Decisions"
    assert _decisions(tmp_path) == [] and len(_decisions(alt)) == 1
    text = _decisions(alt)[0].read_text(encoding="utf-8")
    assert text.startswith("# Privileged change: Install the tailscale playbook")
    assert "Tool: `playbook_install`" in text and "#playbook_install" in text and "Setting up tailscale" in text
    # ``result.summary`` wins over the caller's summary; the name is the last resort.
    status = audit_privileged_change("x_tool", None, {"ok": True, "summary": "From the result"}, summary="ignored")
    assert status["saved"] is True
    assert Path(status["path"]).read_text(encoding="utf-8").startswith("# Privileged change: From the result")
    status = audit_privileged_change("y_tool", None, {"ok": True})
    assert Path(status["path"]).read_text(encoding="utf-8").startswith("# Privileged change: y_tool")
    # A failed apply keeps the verdict and the error in the title and the outcome line.
    status = audit_privileged_change("z_tool", {"unit": "foo"}, {"ok": False, "applied": True, "error": "exited 1"})
    text = Path(status["path"]).read_text(encoding="utf-8")
    assert text.startswith("# Privileged change (failed): z_tool") and "Outcome: failed — exited 1" in text
