"""Registration, against a registrar whose eviction policy we control.

The account is shared with the resident's phone, and displacing its binding is
invisible from their side: the phone does not notice and simply stops ringing until
its own timer fires minutes later. These tests exist to prove the client notices what
the phone cannot.
"""

from __future__ import annotations

import asyncio

import pytest

from custom_components.loki.sip.client import LokiSipClient, SipConfig, SipState
from custom_components.loki.sip.errors import SipSafetyError
from custom_components.loki.sip.registration import (
    Binding,
    PriorContact,
    RegistrationState,
)
from tests.test_sip_client import Recorder, WireTap

PASSWORD = "secret"
USER = "1009999"


def _config(port: int, **overrides: object) -> SipConfig:
    base = {
        "host": "127.0.0.1",
        "user": USER,
        "password": PASSWORD,
        "port": port,
        "require_baseline": False,
        "register": True,
        "first_registration_done": True,
        "expires": 300,
    }
    base.update(overrides)
    return SipConfig(**base)  # type: ignore[arg-type]


async def _run_until(
    registrar: WireTap,
    recorder: Recorder,
    config_overrides: dict[str, object],
    *,
    done: str,
    timeout: float = 15.0,
) -> LokiSipClient:
    """Run the client until it reaches a state or reports something terminal."""
    port = await registrar.start()
    client = LokiSipClient(_config(port, **config_overrides), recorder)
    task = asyncio.create_task(client.async_run())
    try:
        async with asyncio.timeout(timeout):
            while True:
                if recorder.terminal is not None:
                    break
                if any(state.value == done for state, _ in recorder.states):
                    break
                await asyncio.sleep(0.02)
    finally:
        await client.async_stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await registrar.stop()
    return client


# ------------------------------------------------------------------ contacts


def test_build_contacts_refuses_a_wildcard() -> None:
    """A wildcard Contact with Expires: 0 wipes every binding on the account."""
    state = RegistrationState(host="h", user="u", sent_by="203.0.113.1:5060")

    with pytest.raises(SipSafetyError, match="wildcard"):
        state.build_contacts(live="*", reap=[])
    with pytest.raises(SipSafetyError, match="wildcard"):
        state.build_contacts(live=None, reap=["*"])


def test_build_contacts_refuses_to_withdraw_what_it_is_registering() -> None:
    """Registering and withdrawing the same URI in one message is incoherent."""
    state = RegistrationState(host="h", user="u")
    uri = "sip:u@203.0.113.1:5060;transport=tcp"

    with pytest.raises(SipSafetyError, match="withdraw"):
        state.build_contacts(live=uri, reap=[uri])


def test_prior_contacts_are_remembered_then_forgotten() -> None:
    """Old URIs are kept long enough to withdraw, and no longer.

    A NAT port can be handed to another device, so an address that used to be ours is
    not ours forever -- and withdrawing it then would be the harm we are avoiding.
    """
    state = RegistrationState(host="h", user="u")

    state.set_contact("sip:u@a:1;transport=tcp")
    state.set_contact("sip:u@b:2;transport=tcp")

    assert [prior.uri for prior in state.prior_contacts] == ["sip:u@a:1;transport=tcp"]

    state.prior_contacts = [PriorContact("sip:u@a:1;transport=tcp", 0.0)]
    state.forget_stale_priors(ttl=1.0)

    assert state.prior_contacts == []


def test_prior_contacts_are_bounded() -> None:
    """A flapping connection must not build an unbounded withdrawal list."""
    state = RegistrationState(host="h", user="u")

    for index in range(30):
        state.set_contact(f"sip:u@host:{index};transport=tcp")

    assert len(state.prior_contacts) <= 8


# ------------------------------------------------------------------- expiry


@pytest.mark.parametrize(
    ("granted", "low", "high"),
    [(300, 235, 300), (60, 49, 60), (3600, 3535, 3600), (10, 1, 10)],
)
def test_refresh_happens_before_the_granted_expiry(
    granted: int, low: float, high: float
) -> None:
    """Renewing against what we asked for, when less was granted, lets it lapse."""
    state = RegistrationState(host="h", user="u", granted_expires=granted)

    delays = [state.refresh_delay() for _ in range(30)]

    assert all(low <= delay < high for delay in delays), delays


# -------------------------------------------------------------- registration


@pytest.mark.asyncio
async def test_registers_on_an_empty_account_and_verifies() -> None:
    """The happy path: nobody was there, we register, nothing vanished."""
    registrar = WireTap(password=PASSWORD, max_contacts=3)
    recorder = Recorder()

    await _run_until(registrar, recorder, {}, done="registered")

    assert recorder.terminal is None
    assert any(s is SipState.REGISTERED for s, _ in recorder.states)
    assert len(registrar.bindings) == 1
    assert registrar.evictions == 0
    # No wildcard ever, on any path.
    assert registrar.wildcard_seen is False


@pytest.mark.asyncio
async def test_eviction_is_detected_and_the_client_stops() -> None:
    """The registrar keeps one contact, so registering displaced the phone."""
    registrar = WireTap(password=PASSWORD, max_contacts=1)
    registrar.seed_foreign(1)
    recorder = Recorder()

    # The guard would normally refuse outright; switch it off to reach the detector
    # that exists for the case where the phone registers between probe and REGISTER.
    await _run_until(
        registrar, recorder, {"strict_guard": False}, done="registered"
    )

    assert recorder.terminal is not None
    state, kind, _ = recorder.terminal
    assert state is SipState.EVICTED
    assert kind == "SipEvictionError"
    # Having done harm, it must at least take its own binding back off the account.
    assert not any("127.0.0.1" in binding.uri for binding in registrar.bindings)


@pytest.mark.asyncio
async def test_a_registrar_that_hides_bindings_is_refused() -> None:
    """With no visible bindings the eviction guard is blind, so we do not stay."""
    registrar = WireTap(password=PASSWORD, report_bindings=False)
    recorder = Recorder()

    await _run_until(registrar, recorder, {}, done="registered")

    assert recorder.terminal is not None
    assert recorder.terminal[1] == "SipUnverifiableError"


@pytest.mark.asyncio
async def test_contact_is_rewritten_from_received_and_rport() -> None:
    """Behind a container bridge our socket address is unreachable from outside.

    Without this correction the registration succeeds and the doorbell never rings --
    the worst kind of failure, because every indicator looks healthy.
    """
    registrar = WireTap(password=PASSWORD, rewrite_contact=True)
    recorder = Recorder()

    await _run_until(registrar, recorder, {}, done="registered")

    registered = [binding.uri for binding in registrar.bindings]
    assert registered, "nothing was registered"
    assert any("203.0.113.9:44444" in uri for uri in registered), registered
    # The first REGISTER already created a binding for the address we guessed, which
    # the registrar cannot reach. Leaving it behind fills the account's table with our
    # own dead entries -- observed happening against a real Asterisk.
    assert len(registered) == 1, f"a superseded binding was left behind: {registered}"
    assert not any("127.0.0.1" in uri for uri in registered), registered


@pytest.mark.asyncio
async def test_first_registration_uses_a_short_expiry() -> None:
    """A mistake should heal in a minute, not in the minutes a phone takes to notice."""
    registrar = WireTap(password=PASSWORD)
    recorder = Recorder()

    await _run_until(
        registrar, recorder, {"first_registration_done": False}, done="registered"
    )

    expires_rows = [
        row
        for request in registrar.seen
        for row in request
        if row.lower().startswith("expires")
    ]
    assert "Expires: 60" in expires_rows, expires_rows


@pytest.mark.asyncio
async def test_registration_is_refreshed_while_held() -> None:
    """The refresh must work while the reader is also running on the same socket.

    Only one coroutine may read a stream, so a refresh that reads the socket itself
    collides with the loop handling incoming requests -- and asyncio raises. That can
    only happen once a registration is being held, so no short-lived test would catch
    it: this one holds one and waits for a renewal.
    """
    registrar = WireTap(password=PASSWORD, max_contacts=3)
    recorder = Recorder()
    port = await registrar.start()
    # Granted 10s means the client renews after about half of it.
    client = LokiSipClient(_config(port, expires=10), recorder)

    task = asyncio.create_task(client.async_run())
    try:
        async with asyncio.timeout(30):
            while not any(s is SipState.REGISTERED for s, _ in recorder.states):
                await asyncio.sleep(0.02)
            before = sum(
                1
                for request in registrar.seen
                if request and request[0].startswith("REGISTER")
            )
            while True:
                now = sum(
                    1
                    for request in registrar.seen
                    if request and request[0].startswith("REGISTER")
                )
                if now > before:
                    break
                await asyncio.sleep(0.05)
    finally:
        await client.async_stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await registrar.stop()

    assert recorder.terminal is None, f"refresh failed: {recorder.terminal}"
    assert len(registrar.bindings) == 1, "refresh must not create a second binding"


async def _invite_while_registered(
    recorder: Recorder, **overrides: object
) -> tuple[list[str], WireTap]:
    """Register, then have the registrar push an INVITE down the same connection."""
    registrar = WireTap(password=PASSWORD)
    port = await registrar.start()
    client = LokiSipClient(_config(port, **overrides), recorder)

    task = asyncio.create_task(client.async_run())
    try:
        async with asyncio.timeout(20):
            while not any(s is SipState.REGISTERED for s, _ in recorder.states):
                await asyncio.sleep(0.02)
            replies = await registrar.send_invite(timeout=2.0)
    finally:
        await client.async_stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await registrar.stop()
    return replies, registrar


@pytest.mark.asyncio
async def test_an_incoming_call_rings_and_is_never_answered() -> None:
    """180 Ringing, and nothing above it.

    A 2xx would take the call away from the resident's phone; a 6xx would cancel
    every other branch including theirs. Neither may ever leave this client.
    """
    recorder = Recorder()

    replies, _registrar = await _invite_while_registered(recorder)

    assert replies, "the client sent nothing back"
    assert replies[0].startswith("SIP/2.0 100"), replies
    assert replies[1].startswith("SIP/2.0 180"), replies
    assert not any(reply.startswith("SIP/2.0 2") for reply in replies), replies
    assert not any(reply.startswith("SIP/2.0 6") for reply in replies), replies

    # The call reached Home Assistant, with the caller's display name intact.
    assert recorder.calls, "the ring was not announced"
    _call_id, remote_uri = recorder.calls[0]
    assert "Дверь" in remote_uri, remote_uri


@pytest.mark.asyncio
async def test_a_call_nothing_will_answer_is_released_at_once() -> None:
    """Leaving the branch open would freeze the decline button on the phone."""
    recorder = Recorder(deliver=False)

    replies, _registrar = await _invite_while_registered(recorder)

    assert any(reply.startswith("SIP/2.0 486") for reply in replies), replies
    assert not any(reply.startswith("SIP/2.0 6") for reply in replies), replies
    assert recorder.ended and recorder.ended[0][1] == "undelivered"


# ----------------------------------------------------------------- ownership


def _client(**overrides: object) -> LokiSipClient:
    config = SipConfig(host="h", user=USER, password=PASSWORD, **overrides)  # type: ignore[arg-type]
    return LokiSipClient(config, Recorder())


def test_our_binding_is_recognised_by_instance_id() -> None:
    """The registrar echoes the instance id when it supports outbound."""
    client = _client()
    state = client._state
    state.set_contact("sip:u@203.0.113.1:5060;transport=tcp")

    ours = Binding("sip:u@anything:9;transport=tcp", 300, state.instance_id, "1")
    theirs = Binding(
        "sip:u@203.0.113.7:5060;transport=tcp",
        300,
        "0" * 8 + "-0000-0000-0000-000000000001",
        None,
    )

    assert client._is_ours(ours) is True
    assert client._is_ours(theirs) is False


def test_our_binding_is_recognised_by_uri_when_no_instance_id() -> None:
    """Not every registrar stores RFC 5626 parameters.

    Relying on the instance id alone would make our own previous binding read as
    somebody else's on the first reconnect, and the client would block itself on a
    perfectly healthy account.
    """
    client = _client()
    state = client._state
    state.set_contact("sip:u@203.0.113.1:5060;transport=tcp")
    state.set_contact("sip:u@203.0.113.1:6000;transport=tcp")

    current = Binding("sip:u@203.0.113.1:6000;transport=tcp", 300, None, None)
    previous = Binding("sip:u@203.0.113.1:5060;transport=tcp", 300, None, None)
    stranger = Binding("sip:u@198.51.100.5:5060;transport=tcp", 300, None, None)

    assert client._is_ours(current) is True
    assert client._is_ours(previous) is True
    assert client._is_ours(stranger) is False


def test_only_our_own_stale_binding_is_reapable() -> None:
    """A ;expires=0 row aimed at anything else is indistinguishable from eviction."""
    client = _client()
    state = client._state
    state.set_contact("sip:u@203.0.113.1:5060;transport=tcp")
    state.set_contact("sip:u@203.0.113.1:6000;transport=tcp")

    live = Binding("sip:u@203.0.113.1:6000;transport=tcp", 300, None, None)
    stale = Binding("sip:u@203.0.113.1:5060;transport=tcp", 300, None, None)
    stranger = Binding("sip:u@198.51.100.5:5060;transport=tcp", 300, None, None)
    # Same address as one of ours, but explicitly somebody else's device.
    reused_port = Binding(
        "sip:u@203.0.113.1:5060;transport=tcp",
        300,
        "aaaaaaaa-0000-0000-0000-000000000001",
        None,
    )

    assert client._reapable(live) is False, "never withdraw the binding we just made"
    assert client._reapable(stale) is True
    assert client._reapable(stranger) is False
    assert client._reapable(reused_port) is False


def test_natural_expiry_is_not_eviction() -> None:
    """Blaming ourselves for a clock would disable us on a healthy system."""
    client = _client()
    expiring = Binding("sip:other@h:1;transport=tcp", 30, None, None)
    healthy = Binding("sip:other@h:2;transport=tcp", 3600, None, None)

    vanished = client._vanished([expiring, healthy], [], elapsed=1.0)

    assert [binding.uri for binding in vanished] == ["sip:other@h:2;transport=tcp"]


def test_a_phone_that_only_changed_port_has_not_vanished() -> None:
    """PJSIP puts the source port in its Contact, so it changes on every reconnect."""
    client = _client()
    instance = "0" * 8 + "-0000-0000-0000-000000000001"
    before = Binding("sip:other@h:5060;transport=tcp", 3600, instance, None)
    after = Binding("sip:other@h:41234;transport=tcp", 3600, instance, None)

    assert client._vanished([before], [after], elapsed=1.0) == []
