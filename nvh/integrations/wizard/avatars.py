"""Built-in agent profile avatars.

These are inline SVGs (no external assets) so the avatar is always available
even on first boot before any portrait generation has happened. Each built-in
profile gets a distinct color + monogram + shape so users can tell agents
apart at a glance in the picker, chat headers, and trace cards.

Custom user agents either point ``profile.avatar`` at a generated PNG under
``NVH_HOME/agent-profiles/avatars/`` (the create-agent flow drops files
there from ComfyUI) or an absolute URL.
"""

from __future__ import annotations

from typing import Final

# Each built-in maps to (background_color, accent_color, monogram, shape).
# The shape switches the silhouette behind the monogram so we don't rely on
# color alone for agent identification (accessibility win).
#  shape: "circle" | "rounded-square" | "hex" | "diamond" | "shield" | "book"
_BUILT_IN_AVATARS: Final[dict[str, tuple[str, str, str, str]]] = {
    "wizard": ("#76B900", "#0a0a0a", "W", "hex"),
    "coder": ("#0ea5e9", "#082f49", "{}", "rounded-square"),
    "researcher": ("#a855f7", "#3b0764", "R", "book"),
    "writer": ("#f59e0b", "#451a03", "W", "circle"),
    "ops": ("#dc2626", "#450a0a", "OPS", "shield"),
    "vault-rag": ("#10b981", "#022c22", "V", "diamond"),
}

# Stable size baked in; the UI scales via CSS but the viewBox stays 64x64.
SIZE = 64


def _shape_path(shape: str) -> str:
    """Return an SVG path/element for the avatar's silhouette."""
    if shape == "circle":
        return '<circle cx="32" cy="32" r="28" fill="currentColor" />'
    if shape == "rounded-square":
        return '<rect x="6" y="6" width="52" height="52" rx="12" fill="currentColor" />'
    if shape == "hex":
        return '<polygon points="32,4 56,18 56,46 32,60 8,46 8,18" fill="currentColor" />'
    if shape == "diamond":
        return '<polygon points="32,4 60,32 32,60 4,32" fill="currentColor" />'
    if shape == "shield":
        return (
            '<path d="M32 4 L56 12 L56 32 Q56 50 32 60 Q8 50 8 32 L8 12 Z" '
            'fill="currentColor" />'
        )
    if shape == "book":
        return (
            '<rect x="8" y="10" width="48" height="44" rx="4" fill="currentColor" />'
            '<rect x="30" y="10" width="4" height="44" fill="rgba(0,0,0,0.18)" />'
        )
    # Fallback: simple circle
    return '<circle cx="32" cy="32" r="28" fill="currentColor" />'


def render_built_in_avatar(name: str) -> str | None:
    """Return an inline SVG string for a built-in profile, or None if unknown."""
    spec = _BUILT_IN_AVATARS.get(name)
    if spec is None:
        return None
    bg, accent, monogram, shape = spec
    # Tiny SVG: ~400 bytes uncompressed; trivially cacheable.
    # The monogram font is intentionally bold + monospace so it reads on chips
    # as small as 24x24.
    font_size = 28 if len(monogram) <= 1 else (22 if len(monogram) == 2 else 18)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" '
        f'width="{SIZE}" height="{SIZE}" role="img" aria-label="{name} avatar">'
        f'<g style="color: {bg}">{_shape_path(shape)}</g>'
        f'<text x="50%" y="50%" text-anchor="middle" dominant-baseline="central" '
        f'font-family="ui-monospace, Menlo, monospace" font-weight="800" '
        f'font-size="{font_size}" fill="{accent}">{monogram}</text>'
        f'</svg>'
    )


def has_built_in_avatar(name: str) -> bool:
    return name in _BUILT_IN_AVATARS
