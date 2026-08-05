# nvHive — Trademark and Branding Policy

**Status:** `nvHive` is a common-law trademark in use in commerce since
2026-03 in connection with software for AI-LLM orchestration and rootless
GPU workspace installation. It is **not** federally registered (yet). The
common-law rights described here arise from actual use under U.S. trademark
law and are geographically limited to the markets of use.

This policy explains how the nvHive name and brand may be used. It is
intended to prevent confusion between official releases and third-party
forks of noncommercial use permitted by the PolyForm Noncommercial
License 1.0.0 (versions 0.40.0 and earlier remain MIT).

## What is covered

The following identify the official nvHive project:

- The names `nvHive`, `NVHive`, `nvhive`, and the `nvh` CLI command when
  used in connection with software.
- nvHive logos, product screenshots, UI styling and visual identity
  (trade dress), release badges, release artifacts, and product
  descriptions used to identify the project.
- The official GitHub repository:
  https://github.com/thatcooperguy/nvHive
- The official PyPI distribution:
  https://pypi.org/project/nvhive/

**The source-code license grants rights to the code for noncommercial
use. It does not
grant trademark rights, branding rights, endorsement rights, or PyPI
publishing rights to the `nvhive` distribution name.**

## What you can do without asking

- Use, fork, modify, and redistribute the source code for noncommercial
  purposes under the PolyForm Noncommercial License 1.0.0.
- Truthfully describe your project as based on, derived from, compatible
  with, or originally forked from nvHive (nominative fair use).
- Link to the official repository or PyPI package.
- Reference the name in documentation when describing compatibility,
  attribution, or provenance.

## What requires a distinct identity

If you publish a fork or commercial redistribution, please use a distinct:

- **Project name** that does not lead with `nvHive` / `nvhive` / `nvh-` as
  the primary product identity.
- **Package name** on PyPI or other registries (do not publish to a name
  that could be confused with `nvhive`).
- **Release channel** (do not publish binaries, installers, containers,
  or app store listings that appear to be official nvHive releases).
- **Visual identity** (do not reuse the logo, color palette, or UI
  screenshots in a way that implies official endorsement).

This is the *standard* fork-renaming convention in open source — same
posture the Linux Foundation, Apache, and Mozilla projects apply.

## PyPI and release channels

The `nvhive` PyPI distribution is the canonical package for the official
project. Only maintainers of `thatcooperguy/nvHive` should publish releases
under that name or configure PyPI trusted publishing for that distribution.

Third-party builds must not claim to be official releases or attempt to
publish to the `nvhive` PyPI namespace. Use a clearly different distribution
name and include fork metadata in your `pyproject.toml`.

## NVIDIA and other third-party marks

nvHive is independent and is not endorsed by NVIDIA. References to NVIDIA,
CUDA, GeForce, NIM, NeMo, Nemotron, ComfyUI, Blender, Godot, GitHub, or
other software names are descriptive compatibility references only. Those
marks belong to their respective owners.

## What this policy does NOT do

- It does not assert federal trademark registration. There is no ® symbol
  in use; `™` may appear where appropriate to mark common-law use.
- It does not reserve domains, social media handles, or marketplace
  listings beyond those listed under "What is covered" above. Common-law
  trademark rights do not extend to those by default.
- It does not waive or modify the license grant on existing source
  code, which remains in force.

## Questions

For branding, distribution, or fork questions, open an issue at:
https://github.com/thatcooperguy/nvHive/issues
