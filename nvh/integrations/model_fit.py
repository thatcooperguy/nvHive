"""Student-friendly local model fit recommendations."""

from __future__ import annotations

from typing import Any

from nvh.integrations.storage import storage_status
from nvh.integrations.studio_packs import model_catalog_with_status

USE_CASE_LABELS = {
    "starter": "Best first install",
    "chat": "Chat and homework help",
    "coding": "Coding helper",
    "reasoning": "Math and reasoning",
    "embedding": "Search and notes",
    "vision": "Vision/image understanding",
}


def _score_model(model: dict[str, Any], *, free_gb: float | None, vram_gb: int) -> tuple[int, list[str]]:
    score = int(100 - int(model.get("priority", 100)))
    reasons: list[str] = []
    if model.get("installed"):
        score += 40
        reasons.append("already installed")
    if model.get("recommended"):
        score += 35
        reasons.append("recommended default")
    if model.get("fits_vram"):
        score += 20
        reasons.append("fits detected VRAM")
    elif vram_gb:
        score -= 35
        reasons.append("may exceed detected VRAM")
    estimated_disk = float(model.get("estimated_disk_gb", 0) or 0)
    if free_gb is not None and estimated_disk > free_gb:
        score -= 50
        reasons.append("not enough free storage")
    elif free_gb is not None and estimated_disk * 2 < free_gb:
        score += 10
        reasons.append("comfortable disk fit")
    capabilities = {str(cap).lower() for cap in model.get("capabilities", [])}
    if "coding" in capabilities:
        score += 8
    if "fast" in capabilities:
        score += 5
    return max(0, score), reasons


def model_fit_report(home_dir: str | None = None) -> dict[str, Any]:
    """Return a simplified model queue by student use case."""
    catalog = model_catalog_with_status()
    storage = storage_status(home_dir=home_dir).as_dict()
    free_gb = storage.get("free_gb")
    vram_gb = int(catalog.get("detected_vram_gb") or 0)
    ranked: list[dict[str, Any]] = []

    for model in catalog.get("models", []):
        score, reasons = _score_model(model, free_gb=free_gb, vram_gb=vram_gb)
        capabilities = [str(cap).lower() for cap in model.get("capabilities", [])]
        if model.get("recommended"):
            use_case = "starter"
        elif "coding" in capabilities:
            use_case = "coding"
        elif "reasoning" in capabilities:
            use_case = "reasoning"
        elif "embedding" in capabilities:
            use_case = "embedding"
        elif "vision" in capabilities or "vision-capable family" in capabilities:
            use_case = "vision"
        else:
            use_case = str(model.get("category") or "chat")
        ranked.append({
            **model,
            "fit_score": score,
            "fit_reasons": reasons,
            "use_case": use_case,
            "use_case_label": USE_CASE_LABELS.get(use_case, use_case.title()),
        })

    ranked.sort(key=lambda item: item["fit_score"], reverse=True)
    best_by_use_case: dict[str, dict[str, Any]] = {}
    for model in ranked:
        best_by_use_case.setdefault(model["use_case"], model)

    recommended_queue = [
        model for model in ranked
        if model.get("recommended") and not model.get("installed")
    ]
    if not recommended_queue:
        recommended_queue = [
            model for model in ranked
            if model.get("fits_vram") and not model.get("installed")
        ][:3]
    queue_disk_gb = round(
        sum(float(model.get("estimated_disk_gb", 0) or 0) for model in recommended_queue),
        1,
    )
    storage_fits_queue = free_gb is None or queue_disk_gb <= float(free_gb)
    if storage_fits_queue:
        summary = f"{len(recommended_queue)} model(s) queued for the detected {vram_gb or 'unknown'} GB VRAM profile."
    else:
        summary = (
            f"{len(recommended_queue)} model(s) fit the GPU profile, but the queue needs "
            f"about {queue_disk_gb} GB and storage reports {free_gb} GB free."
        )

    return {
        "summary": summary,
        "detected_vram_gb": vram_gb,
        "free_gb": free_gb,
        "recommended_queue_disk_gb": queue_disk_gb,
        "storage_fits_queue": storage_fits_queue,
        "recommended_ids": [model["id"] for model in recommended_queue],
        "best_by_use_case": best_by_use_case,
        "models": ranked,
        "ollama_available": catalog.get("ollama_available", False),
        "ollama_running": catalog.get("ollama_running", False),
    }
