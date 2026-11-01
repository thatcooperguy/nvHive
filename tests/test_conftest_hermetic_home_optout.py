"""Pins the ``HERMETIC_HOME = False`` escape hatch of tests/conftest.py's ``_hermetic_home``.

Every other module runs against a throwaway home. This one opts out and checks
that it then gets exactly the pre-fixture behaviour: ``Path.home`` is pathlib's
own, ``HOME`` / ``USERPROFILE`` agree with it, and neither ``NVH_HOME`` nor the
settings module's default config path points into pytest's basetemp. Nothing
here reads the real home's contents. A refactor that moved the opt-out check
below the patching, or resolved the module differently, fails here.
"""

from __future__ import annotations

import os
from pathlib import Path

HERMETIC_HOME = False


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
    except ValueError:
        return False
    return True


def test_opted_out_module_sees_the_unpatched_home(tmp_path_factory) -> None:
    basetemp = tmp_path_factory.getbasetemp().resolve()
    # pathlib's own classmethod, not the conftest lambda.
    assert Path.home.__func__.__name__ == "home"
    home = Path.home()
    assert not _under(home, basetemp)
    assert home == Path(os.path.expanduser("~"))
    # The storage root and the derived default config path are wherever the operator left them.
    nvh_home = os.environ.get("NVH_HOME", "")
    assert not nvh_home or not _under(Path(nvh_home), basetemp)
    from nvh.config import settings

    assert not _under(Path(settings.DEFAULT_CONFIG_PATH), basetemp)
