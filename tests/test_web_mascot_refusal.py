"""The mascot must not flinch at a specialist's deliberate refusal.

A local-only specialist declining an explicit pin arrives on the Wizard stream
as an ``error`` event whose ``fallback_reason`` is
:data:`nvh.integrations.wizard.chat.LOCAL_ONLY_FALLBACK_REASON`. WizardChat
already treats that as the turn's answer (attributed bubble, no red banner —
``wizardErrorBanner``), but the mascot mapping used to key off the event
*type* alone, so the same event drove the sprite into the error strip and the
aria-live label announced "something went wrong" over a perfectly good
answer.

web/ has no test runner, so — like ``tests/test_release_hardening.py`` — these
pin the source contract that ``npx tsc --noEmit`` cannot express: the rule
lives in ONE place (``deriveMascotState`` in ``web/lib/mascot.ts``), it reads
``fallback_reason`` through the same ``isWizardDeliberateRefusal`` predicate
the banner uses, a refusal lands exactly like ``done``, and the web constant
still spells the server's reason.
"""

from __future__ import annotations

import re
from pathlib import Path

from nvh.integrations.wizard.chat import LOCAL_ONLY_FALLBACK_REASON

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


def _read(rel: str) -> str:
    return (WEB / rel).read_text(encoding="utf-8")


def _block(source: str, start: str, end: str) -> str:
    """Slice ``source`` from the (unique) ``start`` marker to the next ``end``."""
    assert source.count(start) == 1, f"expected exactly one {start!r}"
    begin = source.index(start)
    stop = source.index(end, begin + len(start))
    return source[begin : stop + len(end)]


def _switch_case(fn: str, case: str, until: str) -> str:
    """The body of one ``case '<case>':`` arm up to the next ``until`` label."""
    start = fn.index(f"case '{case}':")
    return fn[start : fn.index(until, start)]


# ───────────────────────────────────────────────────────────────────────────
# Constant parity: the web predicate and the server reason must agree
# ───────────────────────────────────────────────────────────────────────────


def test_web_local_only_reason_matches_server_constant() -> None:
    api = _read("lib/api.ts")
    match = re.search(r"export const WIZARD_LOCAL_ONLY_FALLBACK_REASON = '([^']+)';", api)
    assert match, "api.ts must export WIZARD_LOCAL_ONLY_FALLBACK_REASON"
    assert match.group(1) == LOCAL_ONLY_FALLBACK_REASON

    predicate = _block(api, "export function isWizardDeliberateRefusal(", "\n}\n")
    assert "fallback_reason === WIZARD_LOCAL_ONLY_FALLBACK_REASON" in predicate


# ───────────────────────────────────────────────────────────────────────────
# mascot.ts: the rule lives in deriveMascotState and reads fallback_reason
# ───────────────────────────────────────────────────────────────────────────


def test_mascot_event_shape_carries_fallback_reason() -> None:
    mascot = _read("lib/mascot.ts")
    shape = _block(mascot, "export interface MascotEventLike {", "\n}\n")
    assert re.search(r"fallback_reason\?: string \| null;", shape), (
        "MascotEventLike must accept the error event's fallback_reason so the "
        "mapping can read it"
    )


def test_derive_mascot_state_lands_a_refusal_like_done() -> None:
    mascot = _read("lib/mascot.ts")
    # One predicate for banner and mascot alike — no second spelling of the reason.
    assert "import { isWizardDeliberateRefusal } from '@/lib/api';" in mascot
    assert LOCAL_ONLY_FALLBACK_REASON not in mascot, (
        "mascot.ts must not re-spell the reason literal; go through isWizardDeliberateRefusal"
    )

    fn = _block(mascot, "export function deriveMascotState(", "\n}\n")
    done_arm = _switch_case(fn, "done", "case 'error':")
    error_arm = _switch_case(fn, "error", "default:")

    # The bug: a type-only mapping that ignores the payload.
    assert not re.search(r"case 'error':\s*return 'error';", fn)

    # A refusal takes the SAME landing as done (asking if cards pend, else happy);
    # anything else is still the error strip.
    assert "isWizardDeliberateRefusal(event)" in error_arm
    assert "settledState(event)" in error_arm
    assert "settledState(event)" in done_arm
    assert re.search(
        r"isWizardDeliberateRefusal\(event\)\s*\?\s*settledState\(event\)\s*:\s*'error'",
        error_arm,
    )

    landing = _block(mascot, "function settledState(", "\n}\n")
    assert re.search(r"hasPendingToolCalls\(event\)\s*\?\s*'asking'\s*:\s*'happy'", landing)


def test_derive_mascot_resume_has_nothing_to_resume_for_a_refusal() -> None:
    mascot = _read("lib/mascot.ts")
    fn = _block(mascot, "export function deriveMascotResume(", "\n}\n")
    assert "isWizardDeliberateRefusal(event)" in fn
    # The refusal guard must run before the pending-cards → asking resume, or a
    # refusal over pending cards would schedule a pointless asking → asking hop.
    assert fn.index("isWizardDeliberateRefusal(event)") < fn.index("'asking'")
    assert re.search(r"if \(isWizardDeliberateRefusal\(event\)\) return undefined;", fn)


def test_error_label_is_reserved_for_genuine_failures() -> None:
    """The aria-live label is keyed off the state, so the state fix IS the
    label fix — pin the two labels the split relies on."""
    mascot = _read("lib/mascot.ts")
    labels = _block(mascot, "export const MASCOT_STATE_LABELS", "\n};\n")
    assert "error: 'something went wrong'" in labels
    assert "happy: 'the Wizard finished'" in labels
    assert "asking: 'the Wizard needs your confirmation'" in labels


# ───────────────────────────────────────────────────────────────────────────
# WizardChat.tsx: the whole error event reaches the mapping
# ───────────────────────────────────────────────────────────────────────────


def test_wizard_chat_hands_the_whole_error_event_to_the_mascot() -> None:
    chat = _read("components/WizardChat.tsx")
    publish = _block(chat, "const publishMascot = (", "\n    };\n")
    # Spread, not rebuilt: fallback_reason must survive the tool_calls attach.
    assert "{ ...event, tool_calls: pendingConfirm }" in publish
    assert "applyMascotEvent(" in publish

    # Banner and mascot share the one predicate from api.ts.
    banner = _block(chat, "function wizardErrorBanner(", "\n}\n")
    assert "isWizardDeliberateRefusal(event)" in banner
    assert "isWizardDeliberateRefusal," in _block(chat, "import {\n  AUTO_PROFILE,", "} from '@/lib/api';")
