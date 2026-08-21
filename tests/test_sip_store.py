"""What the SIP client remembers across restarts.

Only the pure half is exercised here: ``SipStore`` itself is a thin wrapper around
Home Assistant's ``Store``, and the interesting rules -- tolerating a corrupted file,
and bounding the resolved-door table -- live on the dataclass.

The module imports Home Assistant, so these skip when it is absent and run in CI.
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("homeassistant")

from custom_components.loki.sip_store import (
    CONTACT_REFRESH_AFTER,
    CONTACT_TTL,
    MAX_CONTACTS,
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
        contacts=[("sip:u@1.2.3.4:5060;transport=tcp", 1700000000.0)],
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


# ------------------------------------------------------- our own contact URIs


def test_a_fresh_contact_is_offered_back() -> None:
    """The whole point: a restart must recognise its own leftover binding."""
    state = SipStoredState()
    assert state.record_contact("sip:u@h:1;transport=tcp") is True
    assert state.fresh_contacts() == ["sip:u@h:1;transport=tcp"]


def test_a_stale_contact_is_not_claimed_as_ours() -> None:
    """A NAT port handed to another device must never be withdrawn as our own."""
    state = SipStoredState(contacts=[("sip:u@h:1", time.time() - CONTACT_TTL - 1)])
    assert state.fresh_contacts() == []


def test_each_contact_ages_on_its_own_clock() -> None:
    """One stamp for the list would let a new URI vouch for arbitrarily old ones."""
    now = time.time()
    state = SipStoredState(
        contacts=[("sip:u@old", now - CONTACT_TTL - 60), ("sip:u@new", now)]
    )
    assert state.fresh_contacts() == ["sip:u@new"]


def test_a_future_dated_contact_is_rejected_rather_than_trusted_forever() -> None:
    """A clock correction or a restored backup must not create an eternal claim."""
    state = SipStoredState(contacts=[("sip:u@h:1", time.time() + 86400)])
    assert state.fresh_contacts() == []


def test_re_recording_the_same_uri_only_asks_for_a_write_once_it_ages() -> None:
    """Every renewal calls this; only the ones that matter should hit the disk."""
    state = SipStoredState()
    assert state.record_contact("sip:u@h:1") is True
    assert state.record_contact("sip:u@h:1") is False

    # Now pretend the registration has been held for a while.
    uri, _at = state.contacts[-1]
    state.contacts[-1] = (uri, time.time() - CONTACT_REFRESH_AFTER - 1)
    assert state.record_contact("sip:u@h:1") is True
    assert state.fresh_contacts() == ["sip:u@h:1"]


def test_the_contact_list_is_bounded() -> None:
    state = SipStoredState()
    for index in range(MAX_CONTACTS + 3):
        state.record_contact(f"sip:u@h:{index}")
    assert len(state.contacts) == MAX_CONTACTS
    assert state.contacts[-1][0] == f"sip:u@h:{MAX_CONTACTS + 2}"


def test_contacts_survive_a_round_trip_and_tolerate_rubbish() -> None:
    state = SipStoredState()
    state.record_contact("sip:u@h:1")
    assert SipStoredState.from_dict(state.as_dict()).contacts == state.contacts

    mangled = {"contacts": [{"uri": "sip:ok@h", "at": 1.0}, {"at": 2.0}, 7]}
    assert SipStoredState.from_dict(mangled).contacts == [("sip:ok@h", 1.0)]


def test_a_store_written_by_the_previous_shape_still_yields_its_contacts() -> None:
    """Dropping them on upgrade blocks the client out of its own account.

    Not hypothetical: the shape changed from a bare list plus one shared timestamp to
    a list of stamped entries, and the very first restart afterwards blocked.
    """
    now = time.time()
    legacy = {"contacts": ["sip:u@h:1", "sip:u@h:2"], "contacts_at": now}
    state = SipStoredState.from_dict(legacy)
    assert state.fresh_contacts() == ["sip:u@h:1", "sip:u@h:2"]
    # And the shared stamp is still honoured as a stamp, not treated as fresh forever.
    stale = {"contacts": ["sip:u@h:1"], "contacts_at": now - CONTACT_TTL - 1}
    assert SipStoredState.from_dict(stale).fresh_contacts() == []


def test_booleans_are_not_accepted_as_device_ids() -> None:
    """isinstance(True, int) is True, and door True would resolve to device 1."""
    state = SipStoredState.from_dict({"resolved": {"sip:1@h": True}})
    assert state.resolved == {}


def test_the_resolved_cache_evicts_by_least_recent_use() -> None:
    """The door that rings daily must outlive one seen once."""
    state = SipStoredState()
    for index in range(MAX_RESOLVED):
        state.remember(f"sip:{index}@h", index)

    state.remember("sip:0@h", 0)  # touched again, so no longer the oldest
    state.remember("sip:new@h", 999)

    assert len(state.resolved) == MAX_RESOLVED
    assert "sip:0@h" in state.resolved
    assert "sip:1@h" not in state.resolved
