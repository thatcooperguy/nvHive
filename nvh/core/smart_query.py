"""Smart query features: confidence-gated escalation and response verification.

These features make every query smarter:
- Escalation: tries free first, escalates to premium only when needed
- Verification: cross-checks responses with a second model
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nvh.providers.base import CompletionResponse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Confidence Assessment (pure heuristic — no LLM call)
# ---------------------------------------------------------------------------

# Hedging phrases that indicate low confidence
_HEDGING_PHRASES = [
    "i'm not sure",
    "i'm not certain",
    "i think",
    "it might be",
    "it could be",
    "possibly",
    "i believe",
    "it seems",
    "arguably",
    "i don't know",
    "i cannot",
    "i can't",
    "this is a complex",
    "it depends",
    "there are many",
    "generally speaking",
]

_ERROR_SIGNALS = ["i apologize", "sorry", "error", "unable to"]


def assess_confidence(response_text: str) -> float:
    """Assess confidence of an LLM response (0.0-1.0).

    Detects hedging language, uncertainty markers, and response quality signals.
    Pure heuristic — no LLM call needed.
    """
    text_lower = response_text.lower()

    # Count hedging signals
    hedge_count = sum(1 for h in _HEDGING_PHRASES if h in text_lower)

    # Very short responses often indicate uncertainty
    word_count = len(response_text.split())
    length_penalty = 0.0
    if word_count < 20:
        length_penalty = 0.3
    elif word_count < 50:
        length_penalty = 0.1

    # Questions back to the user indicate confusion
    question_count = response_text.count("?")
    question_penalty = min(0.3, question_count * 0.1)

    # "I apologize" or error-like responses
    error_penalty = 0.2 if any(e in text_lower for e in _ERROR_SIGNALS) else 0.0

    confidence = 1.0 - min(
        0.8,
        (
            hedge_count * 0.12
            + length_penalty
            + question_penalty
            + error_penalty
        ),
    )

    return max(0.1, min(1.0, confidence))


# ---------------------------------------------------------------------------
# Confidence-Gated Escalation
# ---------------------------------------------------------------------------

ESCALATION_THRESHOLD = 0.6  # Below this, escalate to premium


async def query_with_escalation(
    engine: Any,
    prompt: str,
    provider: str | None = None,
    model: str | None = None,
    escalation_threshold: float = ESCALATION_THRESHOLD,
    **kwargs: Any,
) -> tuple[CompletionResponse, dict[str, Any]]:
    """Try cheap first, escalate to premium if low confidence.

    Returns ``(response, metadata)`` where metadata includes:
    - escalated: bool
    - initial_provider: str
    - initial_confidence: float
    - escalation_reason: str | None
    """
    # If user explicitly chose a provider, don't escalate
    if provider:
        resp = await engine.query(
            prompt=prompt, provider=provider, model=model, **kwargs,
        )
        return resp, {"escalated": False}

    # Try cheapest strategy first — force escalate=False to prevent recursion
    initial = await engine.query(
        prompt=prompt, strategy="cheapest", escalate=False, verify=False, **kwargs,
    )

    confidence = assess_confidence(initial.content)

    if confidence >= escalation_threshold:
        # Good enough — return the cheap response
        initial.metadata["confidence"] = confidence
        initial.metadata["escalated"] = False
        return initial, {
            "escalated": False,
            "initial_provider": initial.provider,
            "initial_confidence": confidence,
            "escalation_reason": None,
        }

    # Confidence too low — escalate to best strategy (no recursion)
    escalated = await engine.query(
        prompt=prompt, strategy="best", escalate=False, verify=False, **kwargs,
    )
    escalated.metadata["confidence"] = assess_confidence(escalated.content)
    escalated.metadata["escalated"] = True
    escalated.metadata["escalation_from"] = initial.provider
    escalated.metadata["initial_confidence"] = confidence

    return escalated, {
        "escalated": True,
        "initial_provider": initial.provider,
        "initial_confidence": confidence,
        "escalation_reason": (
            f"Low confidence ({confidence:.0%}) from {initial.provider}"
        ),
    }


# ---------------------------------------------------------------------------
# Response Verification
# ---------------------------------------------------------------------------

_VERIFY_PROMPT = """\
You are a response verifier. Check this AI response for:
1. Factual errors or hallucinations
2. Logical inconsistencies
3. Missing important caveats
4. Incorrect code (if applicable)

Question: {question}

Response to verify:
{response}

Respond in EXACTLY this format:
VERDICT: correct | partially_correct | incorrect
CONFIDENCE: <0-10>
ISSUES: <comma-separated list, or "none">
CORRECTION: <brief correction if needed, or "none">"""


@dataclass
class VerificationResult:
    """Result of cross-model verification."""

    verdict: str = "unverified"  # "correct", "partially_correct", "incorrect", "unverified"
    confidence: float = 0.5  # 0.0-1.0
    issues: list[str] = field(default_factory=list)
    correction: str | None = None
    verifier_provider: str = "none"


def _parse_verification(text: str, provider: str) -> VerificationResult:
    """Parse the verifier's structured response.

    Uses the same key:value line parsing pattern as
    ``orchestrator.evaluate_response()``.
    """
    result = VerificationResult(verifier_provider=provider)

    for line in text.strip().splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip()
            if key == "verdict":
                cleaned = value.lower().strip()
                if cleaned in ("correct", "partially_correct", "incorrect"):
                    result.verdict = cleaned
            elif key == "confidence":
                try:
                    raw = int(value.split("/")[0].strip())
                    result.confidence = max(0.0, min(1.0, raw / 10.0))
                except Exception:
                    pass
            elif key == "issues":
                result.issues = [
                    i.strip()
                    for i in value.split(",")
                    if i.strip() and i.strip().lower() != "none"
                ]
            elif key == "correction":
                if value.lower() != "none":
                    result.correction = value

    return result


async def verify_response(
    engine: Any,
    question: str,
    response_text: str,
    response_provider: str,
) -> VerificationResult:
    """Have a different model verify a response.

    Picks a verifier that is NOT the same provider that
    generated the response (independent verification).
    """
    # Pick a different provider for verification — prefer healthy ones so
    # we don't burn budget on a circuit-broken advisor and mislabel the
    # response as "unverified".
    available = engine.registry.list_enabled()

    def _healthy(p: str) -> bool:
        try:
            return engine.rate_manager.get_health_score(p) >= 0.2
        except Exception:
            return True

    healthy = [p for p in available if _healthy(p)]
    pool = healthy or available  # fall back to all enabled if none are healthy

    verifier: str | None = None
    for p in pool:
        if p != response_provider:
            verifier = p
            break
    if not verifier:
        verifier = pool[0] if pool else None

    if not verifier:
        return VerificationResult(
            verdict="unverified",
            confidence=0.5,
            issues=["No verifier available"],
            correction=None,
            verifier_provider="none",
        )

    prompt = _VERIFY_PROMPT.format(
        question=question[:500],
        response=response_text[:2000],
    )

    try:
        # Force verify=False and escalate=False to prevent infinite loops
        result = await engine.query(
            prompt=prompt,
            provider=verifier,
            temperature=0.0,
            max_tokens=300,
            escalate=False,
            verify=False,
        )
        return _parse_verification(result.content, verifier)
    except Exception as exc:
        logger.debug("Verification failed: %s", exc)
        return VerificationResult(
            verdict="unverified",
            confidence=0.5,
            issues=["Verification failed"],
            correction=None,
            verifier_provider=verifier,
        )
