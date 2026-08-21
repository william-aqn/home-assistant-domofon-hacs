"""What the SIP client remembers across restarts.

Only the pure half is exercised here: ``SipStore`` itself is a thin wrapper around
Home Assistant's ``Store``, and the interesting rules -- tolerating a corrupted file,
and bounding the resolved-door table -- live on the dataclass.

The module imports Home Assistant, so these skip when it is absent and run in CI.
"""

from __future__ import annotations

import pytest

pytest.importorskip("homeassistant")

from custom_components.loki.sip_store import (
    MAX_RESOLVED,
    SipStoredState,
)


def test_a_missing_file_yields_a_usable_state() -> None:
    """First run: an instance id must exist, because everything else depends on it."""
    state = SipStoredState.from_dict(None)
    assert state.instance_id
    assert state.first_registration_done is False
    assert state.terminal is None
    assert state.resolved == {}


def test_two_fresh_states_do_not_share_an_instance_id() -> None:
    """A shared id would make two accounts look like one device to the registrar."""
    assert SipStoredState.from_dict(None).instance_id != (
        SipStoredState.from_dict(None).instance_id
    )


def test_a_round_trip_preserves_everything() -> None:
    state = SipStoredState(
        instance_id="abc",
        first_registration_done=True,
        terminal="blocked",
        terminal_detail="somebody else is here",
        resolved={"sip:1@h": 7},
    )
    assert SipStoredState.from_dict(state.as_dict()) == state


@pytest.mark.parametrize("raw", [None, [], "", 0, {"instance_id": ""}])
def test_a_corrupt_file_never_raises(raw: object) -> None:
    """Refusing to load would cost the doorbell; a default costs one extra binding."""
    assert SipStoredState.from_dict(raw).instance_id


def test_non_integer_resolutions_are_dropped_rather_than_trusted() -> None:
    """A device id is what a notification's open button acts on: no coercion."""
    state = SipStoredState.from_dict(
        {"resolved": {"sip:1@h": 7, "sip:2@h": "8", "sip:3@h": None, 4: 9}}
    )
    assert state.resolved == {"sip:1@h": 7}


def test_a_wrongly_typed_terminal_latch_is_ignored() -> None:
    assert SipStoredState.from_dict({"terminal": 5}).terminal is None


def test_remembering_the_same_door_twice_reports_no_change() -> None:
    """The caller only writes to disk when this says something changed."""
    state = SipStoredState()
    assert state.remember("sip:1@h", 7) is True
    assert state.remember("sip:1@h", 7) is False
    assert state.remember("sip:1@h", 8) is True


def test_the_resolved_table_is_bounded_and_drops_the_oldest() -> None:
    """A backend answering with a fresh URI each time must not grow the file forever."""
    state = SipStoredState()
    for index in range(MAX_RESOLVED + 5):
        state.remember(f"sip:{index}@h", index)

    assert len(state.resolved) == MAX_RESOLVED
    assert "sip:0@h" not in state.resolved
    assert f"sip:{MAX_RESOLVED + 4}@h" in state.resolved
