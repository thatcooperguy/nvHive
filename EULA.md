# nvHive Supplemental Terms

**Effective Date:** 2026-04-30
**Version:** 1.1

This document explains user-facing responsibilities for nvHive integrations,
third-party services, and project branding. It does not replace the MIT License
for the nvHive source code. Source code rights are governed by [LICENSE](LICENSE).

## Source Code License

The nvHive source code is licensed under the MIT License. You may use, copy,
modify, merge, publish, distribute, sublicense, and sell copies of the software
subject to the conditions in [LICENSE](LICENSE).

The MIT License does not grant trademark, endorsement, package publishing, or
official release-channel rights. See [NOTICE](NOTICE.md) and
[TRADEMARKS](TRADEMARKS.md).

## Official Project Identity

The official nvHive project is maintained at:

- GitHub: https://github.com/thatcooperguy/nvHive
- PyPI: https://pypi.org/project/nvhive/

Forks and third-party builds should clearly identify themselves as unofficial
and should use distinct project names, package names, release channels, and
branding unless explicitly approved by the nvHive maintainers.

## NVIDIA Relationship and Third-Party Marks

nvHive is an independent project. It is not developed, maintained, endorsed, or
officially affiliated with NVIDIA Corporation.

NVIDIA, GeForce, CUDA, DGX, NIM, Nemotron, NeMo, and related marks are
trademarks or registered trademarks of NVIDIA Corporation. Other third-party
names and marks belong to their respective owners. See
[THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES.md).

## Third-Party AI Providers

nvHive can connect to third-party AI providers and model hosts. When you add an
API key, start a local runtime, download a model, or install an optional tool,
you are responsible for that provider's terms, license, privacy policy, rate
limits, and costs.

nvHive stores provider configuration locally. It does not operate a hosted
backend, analytics service, or telemetry pipeline for normal local use.

## Data Handling

Data processed by local runtimes stays on the machine running nvHive unless you
choose to share it. Data sent to cloud providers is governed by those providers'
terms and privacy policies.

Use `nvh safe` or local-only routing when prompts, files, or outputs should not
leave your machine.

## AI Output and Operational Risk

AI-generated outputs may be inaccurate, incomplete, biased, insecure, or
inappropriate. Review outputs, generated code, dependency changes, and agent
actions before relying on them in production.

Rootless installers, setup packs, and self-healing checks are best-effort tools.
They cannot guarantee compatibility with every cloud image, GPU driver, kernel,
CUDA runtime, Python runtime, provider API, or third-party model.

## Warranty Disclaimer

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. THE AUTHORS AND
CONTRIBUTORS ARE NOT LIABLE FOR DAMAGES ARISING FROM USE OF THE SOFTWARE.

## Contact

For questions, open an issue at:
https://github.com/thatcooperguy/nvHive/issues
