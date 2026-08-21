#!/usr/bin/env python3
"""Syntax-check the repo's YAML, including Home Assistant's custom tags.

Home Assistant extends YAML with tags such as ``!input``, ``!secret`` and ``!include``.
A stock ``yaml.safe_load`` refuses them, so blueprints cannot be linted without teaching
the loader that they exist. This checks structure only -- it does not validate a
blueprint against Home Assistant's schema, which needs a real HA install.

Usage: python scripts/validate_yaml.py [paths...]
"""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import yaml

HA_TAGS = (
    "!input",
    "!secret",
    "!include",
    "!include_dir_list",
    "!include_dir_merge_list",
    "!include_dir_named",
    "!include_dir_merge_named",
    "!env_var",
)

DEFAULT_PATHS = ("blueprints", "custom_components", ".github")


class HALoader(yaml.SafeLoader):
    """SafeLoader that tolerates Home Assistant's custom tags."""


def _passthrough(loader: yaml.Loader, node: yaml.Node) -> Any:
    """Represent an HA tag by its raw value rather than failing."""
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


for tag in HA_TAGS:
    HALoader.add_constructor(tag, _passthrough)


def main(argv: list[str]) -> int:
    """Load every YAML file under the given paths; report failures."""
    roots = [Path(p) for p in (argv or DEFAULT_PATHS)]
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
        elif root.is_dir():
            files.extend(sorted(root.rglob("*.yaml")))
            files.extend(sorted(root.rglob("*.yml")))

    if not files:
        print("no YAML files found")
        return 0

    failed = 0
    for path in files:
        try:
            with path.open(encoding="utf-8") as handle:
                yaml.load(handle, Loader=HALoader)  # noqa: S506 - HALoader is SafeLoader
        except yaml.YAMLError as err:
            failed += 1
            print(f"FAIL {path}\n     {err}")
        else:
            print(f"ok   {path}")

    if failed:
        print(f"\n{failed} file(s) failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
