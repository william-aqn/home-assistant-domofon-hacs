"""Test bootstrap.

``models.py`` and the SIP message parser are deliberately free of Home Assistant
imports so their logic can be tested quickly. Importing them through the package
would still execute ``custom_components/loki/__init__.py``, which does need Home
Assistant. When Home Assistant is not installed we therefore pre-register stub
package objects so submodules resolve (and relative imports still work) without the
package ``__init__`` running.

Install ``pytest-homeassistant-custom-component`` to run the full suite.
"""

from __future__ import annotations

from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _stub_storage() -> None:
    """Provide the minimum of ``homeassistant`` that ``sip_store`` imports.

    ``sip_store`` is pure logic sitting behind one Home Assistant import, and its
    rules -- which remembered Contact URI may still be claimed as ours -- decide
    whether the client withdraws a binding that belongs to somebody else. That is too
    load-bearing to leave to CI alone, so the import is stubbed rather than the tests
    skipped.

    Only the names bound at import time are provided. ``Store`` itself refuses to be
    constructed: anything that genuinely needs Home Assistant belongs in CI, and a
    stub that quietly pretended to persist would be worse than no stub at all.
    """
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object  # type: ignore[attr-defined]

    class _Store:
        """Stand-in for ``homeassistant.helpers.storage.Store``."""

        def __init__(self, *args: object, **kwargs: object) -> None:
            """Refuse: the real Store is only available where HA is installed."""
            raise RuntimeError("the real Store is only available in CI")

    storage = types.ModuleType("homeassistant.helpers.storage")
    storage.Store = _Store  # type: ignore[attr-defined]

    helpers = types.ModuleType("homeassistant.helpers")
    helpers.storage = storage  # type: ignore[attr-defined]

    root = types.ModuleType("homeassistant")
    root.core = core  # type: ignore[attr-defined]
    root.helpers = helpers  # type: ignore[attr-defined]

    for name, module in (
        ("homeassistant", root),
        ("homeassistant.core", core),
        ("homeassistant.helpers", helpers),
        ("homeassistant.helpers.storage", storage),
    ):
        sys.modules.setdefault(name, module)


try:
    import homeassistant  # noqa: F401
except ModuleNotFoundError:
    for name, path in (
        ("custom_components", ROOT / "custom_components"),
        ("custom_components.loki", ROOT / "custom_components" / "loki"),
    ):
        if name not in sys.modules:
            module = types.ModuleType(name)
            module.__path__ = [str(path)]  # type: ignore[attr-defined]
            sys.modules[name] = module

    _stub_storage()
