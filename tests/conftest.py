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
