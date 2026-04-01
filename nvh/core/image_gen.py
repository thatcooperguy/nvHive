"""NVHive Image Generation — create images from text prompts.

Supports multiple providers:
- OpenAI DALL-E (if API key available)
- Stability AI (if API key available)
- Pollinations AI (free, no API key)
- Local Stable Diffusion via Ollama (if available)
"""

import os
import tempfile
from pathlib import Path


async def generate_image(
    prompt: str,
    provider: str = "auto",
    output_path: str | None = None,
    size: str = "1024x1024",
) -> str:
    """Generate an image from a text prompt. Returns path to saved image."""
    import httpx

    if output_path is None:
        output_path = tempfile.mktemp(suffix=".png")

    if provider == "auto":
        if os.environ.get("OPENAI_API_KEY"):
            provider = "openai"
        else:
            provider = "pollinations"  # free, no key

    if provider == "openai":
        key = os.environ.get("OPENAI_API_KEY", "")
        if not key:
            try:
                import keyring
                key = keyring.get_password("nvhive", "openai_api_key") or ""
            except Exception:
                pass
        if not key:
            raise ValueError("OpenAI API key required. Run: nvh auth openai")
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.openai.com/v1/images/generations",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": "dall-e-3", "prompt": prompt, "size": size, "n": 1},
                timeout=60,
            )
            resp.raise_for_status()
            image_url = resp.json()["data"][0]["url"]
            img_resp = await client.get(image_url, timeout=30)
            Path(output_path).write_bytes(img_resp.content)

    elif provider == "stability":
        key = os.environ.get("STABILITY_API_KEY", "")
        if not key:
            try:
                import keyring
                key = keyring.get_password("nvhive", "stability_api_key") or ""
            except Exception:
                pass
        if not key:
            raise ValueError("Stability AI API key required. Set STABILITY_API_KEY.")
        width, height = (int(x) for x in size.split("x"))
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.stability.ai/v1/generation/stable-diffusion-xl-1024-v1-0/text-to-image",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json={
                    "text_prompts": [{"text": prompt, "weight": 1}],
                    "width": width,
                    "height": height,
                    "samples": 1,
                    "steps": 30,
                },
                timeout=60,
            )
            resp.raise_for_status()
            import base64
            image_b64 = resp.json()["artifacts"][0]["base64"]
            Path(output_path).write_bytes(base64.b64decode(image_b64))

    elif provider == "pollinations":
        # Free, no API key needed
        from urllib.parse import quote
        url = f"https://image.pollinations.ai/prompt/{quote(prompt)}?width=1024&height=1024&nologo=true"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=60, follow_redirects=True)
            resp.raise_for_status()
            Path(output_path).write_bytes(resp.content)

    else:
        raise ValueError(
            f"Unknown image provider: {provider!r}. "
            "Choose from: auto, openai, stability, pollinations"
        )

    return output_path


def open_image(path: str) -> None:
    """Open an image file with the system default viewer."""
    import subprocess
    import sys

    if sys.platform == "darwin":
        subprocess.run(["open", path], check=False)
    elif sys.platform == "win32":
        subprocess.run(["start", path], shell=True, check=False)
    else:
        for viewer in ["xdg-open", "eog", "feh", "display"]:
            try:
                subprocess.run([viewer, path], check=False)
                return
            except FileNotFoundError:
                continue
