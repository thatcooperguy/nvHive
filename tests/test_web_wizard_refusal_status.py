"""The Wizard bubble must not dress a specialist's refusal as the offline helper.

A pinned local-only specialist declining because no local provider is up
arrives as a stream ``error`` event with ``mode: deterministic`` text and
``fallback_reason`` == :data:`nvh.integrations.wizard.chat.LOCAL_ONLY_FALLBACK_REASON`.
The banner (``wizardErrorBanner``) and the mascot (``deriveMascotState``)
already read that reason through ``isWizardDeliberateRefusal``; the bubble's
own chrome used to key on ``mode === 'deterministic'`` alone, so the header
and avatar credited the specialist while the footer said "offline helper" and
the status dot went grey "offline" — two stories on one message. The same
happened on reload, because the hydrated ``Message`` never carried the reason
the server persists in the wizard-meta tail.

web/ has no test runner, so — like ``tests/test_web_mascot_refusal.py`` —
these pin the source contract ``npx tsc --noEmit`` cannot express: the
``Message`` carries ``fallbackReason`` from BOTH the live event and the
persisted tail, one predicate (``isDeliberateRefusalMessage``) decides the
footer and the dot through ``isWizardDeliberateRefusal``, a refusal gets no
offline footer and a 'declined' dot, and a genuine fallback still gets both.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from nvh.integrations.wizard import chat as chat_mod
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


# ───────────────────────────────────────────────────────────────────────────
# The Message carries the reason — from the event AND from the persisted tail
# ───────────────────────────────────────────────────────────────────────────


def test_message_type_carries_fallback_reason() -> None:
    chat = _read("components/WizardChat.tsx")
    shape = _block(chat, "interface Message {", "\n}\n")
    assert re.search(r"fallbackReason\?: string \| null;", shape), (
        "Message must carry the error event's / wizard-meta's fallback_reason so "
        "the footer and the status dot can read its value"
    )


def test_error_event_copies_fallback_reason_onto_the_message() -> None:
    chat = _read("components/WizardChat.tsx")
    arm = _block(chat, "case 'error': {", "\n          }\n")
    fallback_spread = _block(arm, "...(event.fallback", "\n                : {}),")
    # The reason lands in the SAME spread that flips mode to deterministic:
    # there is no way to get the offline mode without the reason beside it.
    assert "mode: 'deterministic' as const" in fallback_spread
    assert "fallbackReason: event.fallback_reason ?? null" in fallback_spread


def test_hydrate_restores_fallback_reason_from_the_wizard_meta_tail() -> None:
    chat = _read("components/WizardChat.tsx")
    hydrated = _block(chat, "const hydrated: Message[] =", "setMessages(hydrated);")
    assert "fallbackReason: metaFallbackReason(meta)" in hydrated

    reader = _block(chat, "function metaFallbackReason(", "\n}\n")
    assert "'fallback_reason' in meta" in reader
    assert "meta.fallback_reason" in reader


def test_hydrate_key_is_the_key_the_server_persists() -> None:
    """The web reads ``fallback_reason`` from the tail; ``_TurnSetup.meta_for``
    is what writes a deterministic row's tail, so it must emit that key."""
    meta_for = inspect.getsource(chat_mod._TurnSetup.meta_for)
    assert '"fallback_reason": result["fallback_reason"]' in meta_for
    assert '"mode": result["mode"]' in meta_for


# ───────────────────────────────────────────────────────────────────────────
# One predicate, fed the message's reason, through isWizardDeliberateRefusal
# ───────────────────────────────────────────────────────────────────────────


def test_refusal_predicate_reuses_the_shared_one() -> None:
    chat = _read("components/WizardChat.tsx")
    assert LOCAL_ONLY_FALLBACK_REASON not in chat, (
        "WizardChat.tsx must not re-spell the reason literal; go through isWizardDeliberateRefusal"
    )
    fn = _block(chat, "function isDeliberateRefusalMessage(", "\n}\n")
    assert "message.mode === 'deterministic'" in fn
    assert "isWizardDeliberateRefusal({ fallback_reason: message.fallbackReason })" in fn


# ───────────────────────────────────────────────────────────────────────────
# Footer and status dot decide by the reason's value, not by mode alone
# ───────────────────────────────────────────────────────────────────────────


def test_status_dot_says_declined_for_a_refusal_and_offline_for_a_fallback() -> None:
    chat = _read("components/WizardChat.tsx")
    fn = _block(chat, "function statusForMessage(", "\n}\n")
    det = _block(fn, "if (message.mode === 'deterministic') {", "\n  }\n")

    # The bug: grey "offline" returned unconditionally for every deterministic row.
    assert not re.search(
        r"if \(message\.mode === 'deterministic'\) \{\s*return \{ color: '#737373', label: 'offline' \};",
        fn,
    )
    # The refusal check runs BEFORE the offline return, and the two labels differ.
    assert "isDeliberateRefusalMessage(message)" in det
    assert det.index("isDeliberateRefusalMessage(message)") < det.index("label: 'offline'")
    assert re.search(
        r"if \(isDeliberateRefusalMessage\(message\)\) \{\s*return \{ color: '#76B900', label: 'declined' \};",
        det,
    )
    # A genuine deterministic fallback still gets the grey dot.
    assert "return { color: '#737373', label: 'offline' };" in det


def test_offline_helper_footer_is_skipped_for_a_refusal() -> None:
    chat = _read("components/WizardChat.tsx")
    # The bug: the footer keyed on mode alone.
    assert not re.search(
        r"\{message\.mode === 'deterministic' && \(\s*<div[^>]*>\s*offline helper",
        chat,
    ), "the offline-helper footer must not render on mode alone"
    # The fix: the same predicate as the dot gates the footer; a genuine
    # fallback (deterministic, not a refusal) still shows it.
    assert re.search(
        r"\{message\.mode === 'deterministic' && !isDeliberateRefusalMessage\(message\) && \(\s*"
        r"<div[^>]*>\s*offline helper",
        chat,
    )
