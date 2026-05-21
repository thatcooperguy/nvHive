"""Tests for agent profile avatars (built-in SVGs + user upload + resolution)."""

from __future__ import annotations

from pathlib import Path


def test_built_in_avatar_returns_svg_for_each_default() -> None:
    """Every built-in profile must have a renderable avatar — no broken
    images in the picker on a fresh install."""
    from nvh.integrations.wizard.avatars import (
        has_built_in_avatar,
        render_built_in_avatar,
    )
    from nvh.integrations.wizard.profiles import BUILT_IN_PROFILES

    for profile in BUILT_IN_PROFILES:
        assert has_built_in_avatar(profile.name), f"missing avatar for {profile.name}"
        svg = render_built_in_avatar(profile.name)
        assert svg is not None
        assert svg.startswith("<svg")
        # Each SVG embeds a viewBox + the profile-specific aria-label.
        assert "viewBox" in svg
        assert profile.name in svg


def test_unknown_profile_avatar_returns_none() -> None:
    from nvh.integrations.wizard.avatars import render_built_in_avatar

    assert render_built_in_avatar("does-not-exist") is None


def test_built_in_profile_carries_avatar_url() -> None:
    """The exported profile dicts include the avatar URL the WebUI uses."""
    from nvh.integrations.wizard.profiles import list_profiles

    for profile in list_profiles(home_dir=None):
        if profile.built_in:
            assert profile.avatar.startswith("/v1/wizard/profiles/")
            assert profile.avatar.endswith("/avatar")


def test_resolve_avatar_returns_built_in_when_no_user_file(tmp_path: Path) -> None:
    from nvh.integrations.wizard.profiles import resolve_avatar

    ctype, body = resolve_avatar("wizard", home_dir=tmp_path)  # type: ignore[misc]
    assert ctype == "image/svg+xml"
    assert b"<svg" in body


def test_resolve_avatar_user_file_overrides_built_in(tmp_path: Path) -> None:
    """A PNG dropped into avatars/ replaces the built-in SVG at read time."""
    from nvh.integrations.wizard.profiles import (
        AgentProfile,
        avatars_dir,
        resolve_avatar,
        save_user_profile,
    )

    # Need a non-built-in profile so resolve_avatar checks the on-disk path.
    save_user_profile(
        AgentProfile(
            name="custom-with-avatar",
            title="Custom",
            description="",
            system_prompt="",
        ),
        home_dir=tmp_path,
    )
    avatar_dir = avatars_dir(home_dir=tmp_path)
    (avatar_dir / "custom-with-avatar.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")

    ctype, body = resolve_avatar("custom-with-avatar", home_dir=tmp_path)  # type: ignore[misc]
    assert ctype == "image/png"
    assert body.startswith(b"\x89PNG")


def test_resolve_avatar_unknown_returns_none(tmp_path: Path) -> None:
    from nvh.integrations.wizard.profiles import resolve_avatar

    assert resolve_avatar("nope", home_dir=tmp_path) is None
