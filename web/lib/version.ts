// Keep in lock-step with nvh/__init__.py __version__ / pyproject.toml.
// tests/test_release_hardening.py::test_webui_version_matches_package
// fails CI when this drifts (found showing 0.35.1 while the package
// was at 0.39.0 — five releases of visible drift in the top bar).
export const NVHIVE_VERSION = '0.40.0';
