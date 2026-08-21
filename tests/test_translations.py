"""Consistency checks for strings.json and the translation files.

Translation drift is silent: a missing key renders as a raw identifier in the UI, and a
placeholder the code never supplies renders literally as ``{phone}``. Neither shows up
in any other test, so they are checked structurally here.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

import pytest

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "loki"
STRINGS = COMPONENT / "strings.json"
TRANSLATIONS = COMPONENT / "translations"

PLACEHOLDER_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _key_paths(node: Any, prefix: str = "") -> set[str]:
    """Every leaf path in a nested dict, as dotted strings."""
    if not isinstance(node, dict):
        return {prefix}
    paths: set[str] = set()
    for key, value in node.items():
        paths |= _key_paths(value, f"{prefix}.{key}" if prefix else key)
    return paths


def _placeholders(node: Any) -> dict[str, set[str]]:
    """Map each leaf path to the placeholder names its text uses."""
    result: dict[str, set[str]] = {}

    def walk(value: Any, prefix: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, f"{prefix}.{key}" if prefix else key)
        elif isinstance(value, str) and (found := set(PLACEHOLDER_RE.findall(value))):
            result[prefix] = found

    walk(node, "")
    return result


@pytest.fixture(scope="module")
def strings() -> dict[str, Any]:
    return _load(STRINGS)


def test_every_translation_has_the_same_keys_as_strings(
    strings: dict[str, Any],
) -> None:
    """A key present in one language and missing in another renders as raw text."""
    expected = _key_paths(strings)

    for path in sorted(TRANSLATIONS.glob("*.json")):
        actual = _key_paths(_load(path))
        missing = expected - actual
        extra = actual - expected
        assert not missing, f"{path.name} is missing: {sorted(missing)}"
        assert not extra, (
            f"{path.name} has keys absent from strings.json: {sorted(extra)}"
        )


def test_placeholders_match_across_languages(strings: dict[str, Any]) -> None:
    """A translator dropping a {placeholder} loses information silently."""
    expected = _placeholders(strings)

    for path in sorted(TRANSLATIONS.glob("*.json")):
        actual = _placeholders(_load(path))
        for key, names in expected.items():
            assert actual.get(key, set()) == names, (
                f"{path.name}: placeholders for {key} are {actual.get(key)}, "
                f"expected {names}"
            )


def test_flow_placeholders_are_actually_supplied(strings: dict[str, Any]) -> None:
    """Every {placeholder} in a config-flow step must be one the flow passes.

    Home Assistant renders an unsupplied placeholder literally, so a typo here shows
    the user a stray '{phone}' instead of their number.
    """
    source = (COMPONENT / "config_flow.py").read_text(encoding="utf-8")
    supplied = set(re.findall(r'"([a-z_][a-z0-9_]*)":\s*(?:self\.)?_?\w+', source))
    # description_placeholders keys are written as plain dict literals in the flow.
    supplied |= set(
        re.findall(r"description_placeholders=\{[\"']([a-z_]+)[\"']", source)
    )

    steps = strings.get("config", {}).get("step", {})
    for step_name, step in steps.items():
        for name in PLACEHOLDER_RE.findall(step.get("description", "")):
            assert name in supplied, (
                f"config step '{step_name}' uses {{{name}}}, but config_flow.py never "
                f"supplies it in description_placeholders"
            )


def _all_placeholder_keys() -> set[str]:
    """Every dict key the component's Python could pass as a placeholder.

    Scans the whole package rather than a hand-listed subset: the previous version
    listed two files and silently stopped covering a message the moment it was raised
    from a third.
    """
    sources = "\n".join(
        path.read_text(encoding="utf-8") for path in COMPONENT.rglob("*.py")
    )
    return set(re.findall(r'[\"\']([a-z_][a-z0-9_]*)[\"\']\s*:', sources))


def test_exception_placeholders_are_supplied(strings: dict[str, Any]) -> None:
    """Same rule for exception messages raised with translation_placeholders."""
    supplied = _all_placeholder_keys()

    for key, message in strings.get("exceptions", {}).items():
        for name in PLACEHOLDER_RE.findall(message.get("message", "")):
            assert name in supplied, (
                f"exception '{key}' uses {{{name}}}, which nothing supplies"
            )


def test_issue_placeholders_are_supplied(strings: dict[str, Any]) -> None:
    """Repair cards render placeholders too, including inside their fix flow."""
    supplied = _all_placeholder_keys()

    def check(node: Any, where: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                check(value, f"{where}.{key}")
        elif isinstance(node, str):
            for name in PLACEHOLDER_RE.findall(node):
                assert name in supplied, (
                    f"issue text at '{where}' uses {{{name}}}, which nothing supplies"
                )

    check(strings.get("issues", {}), "issues")
