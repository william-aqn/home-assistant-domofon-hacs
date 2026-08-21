"""The SIP status sensor's option list, checked without importing Home Assistant.

Two things ride on this list and neither fails loudly:

* a state missing from it is a state the sensor cannot report -- Home Assistant
  rejects a value outside ``options`` and the entity goes to ``unknown``;
* the *order* of the list picks the colours drawn in the history chart, because the
  frontend indexes its palette with ``options.indexOf(state)``.

``sensor.py`` imports Home Assistant, which is not installed for these tests, so the
list is read out of the source with ``ast`` rather than imported.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from custom_components.loki.sip.client import SipState

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "loki"
SENSOR = COMPONENT / "sensor.py"

# Positions whose palette colour carries meaning. `--color-1..54` in the frontend's
# theme: index 8 is green (#01ab63) and index 2 is red (#ff725c). Pinned so that a
# reorder for some other reason has to acknowledge what it costs.
GREEN = 8
RED = 2


@pytest.fixture(name="options")
def options_fixture() -> list[str]:
    """The SipState member names listed in ``_attr_options``, in order."""
    tree = ast.parse(SENSOR.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign):
            continue
        target = node.target
        if not isinstance(target, ast.Name) or target.id != "_attr_options":
            continue
        assert isinstance(node.value, ast.List), "_attr_options is not a list literal"
        names: list[str] = []
        for element in node.value.elts:
            # Each element is written `SipState.NAME.value`.
            assert isinstance(element, ast.Attribute) and element.attr == "value"
            member = element.value
            assert isinstance(member, ast.Attribute)
            names.append(member.attr)
        return names
    pytest.fail("_attr_options not found in sensor.py")


def test_options_cover_every_state(options: list[str]) -> None:
    """Every SipState is reportable, and none is listed twice."""
    assert len(options) == len(set(options)), "a state is listed twice"
    assert set(options) == {state.name for state in SipState}


def test_registered_keeps_the_green_slot(options: list[str]) -> None:
    """The state a person looks for is the one drawn in green."""
    assert options[GREEN] == SipState.REGISTERED.name
    assert options[RED] == SipState.FAILED.name


def test_every_option_has_a_label() -> None:
    """A state without a translation renders in the UI as its raw identifier."""
    strings = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
    labelled = strings["entity"]["sensor"]["sip_status"]["state"]
    assert {state.value for state in SipState} == set(labelled)
