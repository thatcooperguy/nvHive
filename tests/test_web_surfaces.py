"""The 0.46 web surfaces keep the shape the docs describe.

Wave C of the 0.46 adds changed only the browser: the Wizard chat moved from
``web/components/WizardChat.tsx`` to ``web/components/chat/WizardThread.tsx``
and grew a question card, a plan line and an inline image render; ``/vault``
gained the notes list; the session cost pill replaced the chat page's budget
pill and the layout's age pill; ``/schedules`` arrived with a sidebar entry.
Every one of those is a literal a page must keep, a file that must (or must
not) exist, or a wiring rule between two files — none of which ``npx tsc
--noEmit`` or ``node --test`` can express.

web/ has no Python runner, so — like ``tests/test_web_mascot_refusal.py`` —
these pin the source contract from the repository checkout. Hermetic: no
Node, no network, no build; every assertion is a read of a tracked file.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
DOCS = ROOT / "docs"


def _read(rel: str) -> str:
    return (WEB / rel).read_text(encoding="utf-8")


def _doc(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


def _block(source: str, start: str, end: str) -> str:
    """Slice ``source`` from the (unique) ``start`` marker to the next ``end``."""
    assert source.count(start) == 1, f"expected exactly one {start!r}"
    begin = source.index(start)
    stop = source.index(end, begin + len(start))
    return source[begin : stop + len(end)]


def _web_sources() -> list[Path]:
    files: list[Path] = []
    for folder in ("app", "components", "lib"):
        for suffix in ("*.ts", "*.tsx"):
            files.extend((WEB / folder).rglob(suffix))
    return sorted(files)


# ───────────────────────────────────────────────────────────────────────────
# C1a: the extraction — one thread component, no importer of the old path
# ───────────────────────────────────────────────────────────────────────────


def test_wizard_page_mounts_the_extracted_thread() -> None:
    page = _read("app/wizard/page.tsx")
    assert "import WizardThread from '@/components/chat/WizardThread';" in page
    assert '<WizardThread conversationParam="conversation" />' in page
    assert (WEB / "components" / "chat" / "WizardThread.tsx").is_file()
    assert not (WEB / "components" / "WizardChat.tsx").exists(), (
        "WizardChat.tsx was replaced by components/chat/WizardThread.tsx; a "
        "second copy would drift"
    )


def test_no_web_file_imports_the_retired_wizard_chat_path() -> None:
    offenders = [
        str(path.relative_to(WEB))
        for path in _web_sources()
        if "'@/components/WizardChat'" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], offenders


def test_thread_resumes_from_either_conversation_param() -> None:
    thread = _read("components/chat/WizardThread.tsx")
    # The literal tests/test_chat_history.py pins, plus the merged-surface twin.
    assert "searchParams?.get('conversation')" in thread
    assert "searchParams?.get('c')" in thread
    assert "conversationParam" in thread


# ───────────────────────────────────────────────────────────────────────────
# C2a: the question card is answered in the chat, never executed
# ───────────────────────────────────────────────────────────────────────────


def test_question_card_never_reaches_the_execute_route() -> None:
    card = _read("components/chat/ToolCard.tsx")
    assert "isQuestionCall(call)" in card, "the card must branch on the predicate, not merely import it"
    assert "executeWizardTool" not in card, "the card renders; the thread runs"

    thread = _read("components/chat/WizardThread.tsx")
    run_tool = thread[thread.index("const runTool = async (") :]
    run_tool = run_tool[: run_tool.index("executeWizardTool(")]
    assert "if (isQuestionCall(call)) return;" in run_tool, (
        "runTool must early-return for a question before anything is POSTed"
    )


def test_answer_question_folds_the_outcome_into_history() -> None:
    thread = _read("components/chat/WizardThread.tsx")
    answer = _block(thread, "const answerQuestion = (", "\n  };\n")
    assert "historyPatch: {" in answer
    assert "status: 'answered'" in answer
    assert "send({" in answer

    assert "const send = async (opts: SendOptions = {}) =>" in thread
    assert "opts.historyPatch" in thread
    # The fold itself: the request history is built from the patched list and
    # the same patch is committed to state; the function lives in
    # lib/privileged.ts under a node row, not as a private helper here.
    send = thread[thread.index("const send = async (opts: SendOptions = {}) =>") :]
    assert "const source = patch ? applyHistoryPatch(messages, patch) : messages;" in send
    assert "if (patch) setMessages(prev => applyHistoryPatch(prev, patch));" in send
    assert "function applyHistoryPatch(" not in thread
    # An answer (or `?starter=` text) never runs as a slash command: only the
    # composer issues those — a `/dev/nvme0n1` choice is a path, not a command.
    assert "if (fromComposer && typed.startsWith('/'))" in send

    privileged = _read("lib/privileged.ts")
    assert "export const QUESTION_SKIPPED_NOTE = 'skipped by the user; not answered';" in privileged
    assert "'answered'" in _block(privileged, "export type ToolCardStatus =", ";\n")
    assert "export function applyHistoryPatch<" in privileged


def test_mascot_reads_safety_class_and_names_the_question_bubble() -> None:
    mascot = _read("lib/mascot.ts")
    shape = _block(mascot, "export interface MascotEventLike {", "\n}\n")
    assert "safety_class" in shape

    derive = _block(mascot, "export function deriveMascotState(", "\n}\n")
    tool_call_arm = derive[derive.index("case 'tool_call':") :]
    tool_call_arm = tool_call_arm[: tool_call_arm.index("case 'tool_result':")]
    assert "safety_class" in tool_call_arm, (
        "a confirm or privileged tool_call must not move the sprite to working"
    )

    assert re.search(
        r"export\s+(?:const\s+MASCOT_ASKING_QUESTION_LABEL\b|\{[^}]*\bMASCOT_ASKING_QUESTION_LABEL\b[^}]*\})",
        mascot,
    ), "mascot.ts must export MASCOT_ASKING_QUESTION_LABEL (directly or as a re-export)"

    rules = _read("lib/mascotRules.ts")
    label = re.search(r"export const MASCOT_ASKING_QUESTION_LABEL = '([^']+)';", rules)
    assert label and label.group(1) == "the Wizard has a question for you"
    assert label.group(1) in _doc("MASCOT.md")


def test_thread_wires_the_question_bubble_and_the_proposing_line() -> None:
    thread = _read("components/chat/WizardThread.tsx")
    surface = _block(thread, "const surfaceConfirmCards = (", "\n  };\n")
    assert "mascotAskingLabel(classes, calls.map(c => c.name))" in surface
    assert "id: `question:${messageId}`" in surface
    stream = thread[thread.index("case 'tool_call':") :]
    stream = stream[: stream.index("case 'tool_result':")]
    assert "proposing ${event.name}" in stream
    assert "event.safety_class" in stream


def test_tool_card_renders_the_plan_line_the_image_and_the_vault_link() -> None:
    card = _read("components/chat/ToolCard.tsx")
    assert "plan: {planLine}" in card
    assert "<GeneratedImage" in card
    assert "fetchApiBlob(image.url)" in card
    assert "vaultNoteHref(saved.id)" in card
    # Answer is disabled, not swallowed, while a turn streams.
    assert "disabled={!answer || busy}" in card
    assert "busy: sending" in _read("components/chat/WizardThread.tsx")


def test_deferred_notes_translate_the_reason() -> None:
    assert "deferredReasonLabel(" in _read("components/chat/DeferredToolNotes.tsx")


def test_clear_drops_both_conversation_spellings() -> None:
    thread = _read("components/chat/WizardThread.tsx")
    clear = _block(thread, "case 'clear':", "return true;")
    assert "url.searchParams.delete('conversation');" in clear
    assert "url.searchParams.delete('c');" in clear


def test_create_agent_modal_names_both_image_backends() -> None:
    modal = _read("components/CreateAgentModal.tsx")
    assert "no NVIDIA API key, no local ComfyUI" in modal
    assert "ComfyUI portrait generation isn" not in modal
    # The avatar route sends `plan` as the summary string, not an object.
    assert "typeof data.plan === 'string'" in modal


# ───────────────────────────────────────────────────────────────────────────
# C2c: one pill — the budget pill and the age pill are gone
# ───────────────────────────────────────────────────────────────────────────


def test_session_cost_pill_replaces_the_budget_and_age_pills() -> None:
    assert "export { default } from './SessionCostPill';" in _read("components/SessionAgePill.tsx")
    assert (WEB / "components" / "SessionCostPill.tsx").is_file()

    chat_page = _read("app/page.tsx")
    assert "BudgetPill" not in chat_page
    assert "<SessionCostPill" in chat_page

    rules = _read("lib/sessionCost.ts")
    assert "'nvh_session_started_at'" in rules, "the pill keeps the age pill's sessionStorage anchor"

    settings = _read("app/settings/page.tsx")
    assert "Hourly desktop rate" in settings
    assert "SESSION_COST_RATE_COMMAND" in settings

    pill = _read("components/SessionCostPill.tsx")
    assert "getBudgetStatus(start)" in pill, "the pill asks for the ledger since this tab opened"
    assert "sessionCostFromStatus(" in pill


# ───────────────────────────────────────────────────────────────────────────
# C3: schedules page, sidebar entry, setup filter, integrations snippet
# ───────────────────────────────────────────────────────────────────────────


def test_sidebar_links_the_schedules_page() -> None:
    sidebar = _read("components/Sidebar.tsx")
    nav = _block(sidebar, "const BOTTOM_NAV = [", "\n];")
    assert "href: '/schedules'" in nav


def test_schedules_page_offers_ask_and_convene_only() -> None:
    page_path = WEB / "app" / "schedules" / "page.tsx"
    assert page_path.is_file()
    page = page_path.read_text(encoding="utf-8")
    values = set(re.findall(r'<option value="([^"]*)"', page))
    assert {"ask", "convene"} <= values
    assert "do" not in values, "do-mode schedules are CLI-only; the API runner refuses them"
    assert "'/wizard?conversation='" in page, "threads open on the Wizard surface until C1b lands"


def test_schedules_live_panel_tells_a_lost_watcher_from_a_failed_job() -> None:
    page = _read("app/schedules/page.tsx")
    assert "onError: (message, job) =>" in page, "a polling failure (no job) is not a failed job"
    assert "lostContact" in page
    assert "Reconnect" in page


def test_setup_page_lists_install_jobs_only() -> None:
    assert "exclude_kind: 'schedule-run'" in _read("app/setup/page.tsx")


def test_integrations_page_shows_the_standard_path_snippet() -> None:
    page = _read("app/integrations/page.tsx")
    assert "OPENAI_BASE_URL=http://127.0.0.1:8000/v1\n" in page
    assert "ANTHROPIC_BASE_URL=http://127.0.0.1:8000/v1/anthropic" in page


def test_schedule_mode_type_has_no_do() -> None:
    types = _read("lib/types.ts")
    match = re.search(r"export type ScheduleMode = ([^;]+);", types)
    assert match, "types.ts must export ScheduleMode"
    assert "'ask'" in match.group(1) and "'convene'" in match.group(1)
    assert "'do'" not in match.group(1)


# ───────────────────────────────────────────────────────────────────────────
# C2b: the vault page derives its folders from the server
# ───────────────────────────────────────────────────────────────────────────


def test_vault_page_has_no_hand_typed_folder_list() -> None:
    page = _read("app/vault/page.tsx")
    assert "const CATEGORIES" not in page
    assert "categoriesFromStatus(" in page
    assert "listVaultNotes(" in page
    assert "deleteVaultNote(" in page
    assert "useSearchParams()" in page
    # The deep link scrolls only to a rendered row; a hidden one is revealed first.
    assert "visibleNotes.some(note => note.id === deepLinkId)" in page


# ───────────────────────────────────────────────────────────────────────────
# Every new pure helper has a Node test beside it and stays importable by Node
# ───────────────────────────────────────────────────────────────────────────

NEW_PURE_HELPERS = {
    # helper module: the .test.mjs that imports it
    "apiShapes": "apiShapes.test.mjs",
    "mascotRules": "mascot.test.mjs",
    "vaultNotes": "vaultNotes.test.mjs",
    "sessionCost": "sessionCost.test.mjs",
    "schedules": "schedules.test.mjs",
}

RUNTIME_IMPORT = re.compile(r"^import\s+(?!type\b)", re.MULTILINE)


@pytest.mark.parametrize("helper", sorted(NEW_PURE_HELPERS))
def test_new_pure_helper_has_a_node_test(helper: str) -> None:
    source = WEB / "lib" / f"{helper}.ts"
    test_file = WEB / "lib" / NEW_PURE_HELPERS[helper]
    assert source.is_file(), source
    assert test_file.is_file(), test_file
    assert f"from './{helper}.ts'" in test_file.read_text(encoding="utf-8"), (
        f"{test_file.name} must import {helper}.ts by explicit extension"
    )


@pytest.mark.parametrize("helper", sorted(NEW_PURE_HELPERS))
def test_new_pure_helper_imports_nothing_node_cannot_resolve(helper: str) -> None:
    source = _read(f"lib/{helper}.ts")
    assert "'use client'" not in source
    assert "from 'react'" not in source
    assert "from 'next" not in source
    assert "from '@/" not in source, "the @/ alias is a Next resolution; Node has none"
    assert not RUNTIME_IMPORT.search(source), (
        f"{helper}.ts may only `import type`: a runtime import of another .ts "
        "module fails either tsc (.ts extension) or Node (extensionless)"
    )


# ───────────────────────────────────────────────────────────────────────────
# The docs describe what shipped
# ───────────────────────────────────────────────────────────────────────────


def test_webui_doc_names_the_new_surfaces() -> None:
    webui = _doc("WEBUI.md")
    assert "`/schedules`" in webui
    assert "/v1/vault/notes" in webui
    assert "question card" in webui
    assert "npm test" in webui
    assert "components/chat/WizardThread.tsx" in webui


def test_mascot_doc_describes_the_question_rows() -> None:
    mascot = _doc("MASCOT.md")
    assert "question_pending" in mascot
    assert "`safety_class`" in mascot
    assert "WizardThread" in mascot
    assert "WizardChat" not in mascot


def test_testing_doc_lists_the_web_checks() -> None:
    testing = _doc("TESTING.md")
    assert "test_web_surfaces.py" in testing
    assert "node --test lib/*.test.mjs" in testing
