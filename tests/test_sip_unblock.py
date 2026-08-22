"""What happens after the gate refuses.

Refusing was once the end: the client stopped, a repair card appeared, and the
doorbell stayed dead until somebody went and toggled a switch. On the live account
almost every refusal turned out to be the client's own binding from before a restart,
still there until it lapsed -- so the wait was minutes and the person was hours.

It now keeps looking, and registers the moment the account is free. The refusal itself
is unchanged: an account somebody else holds is still never registered on.
"""

from __future__ import annotations

import asyncio

import pytest

from custom_components.loki.sip.client import LokiSipClient, SipState
from tests.test_sip_client import Recorder, WireTap
from tests.test_sip_registration import PASSWORD, _config, _hard_stop

# Real values are minutes; the point here is the sequence, not the clock.
FAST = {"blocked_retry_min": 0.05, "blocked_retry_base": 0.05, "blocked_retry_max": 0.2}


@pytest.mark.asyncio
async def test_a_blocked_account_is_taken_up_once_it_frees() -> None:
    """Blocked, then registered -- without anybody touching a switch."""
    registrar = WireTap(password=PASSWORD, echo_instance_id=False)
    registrar.seed_foreign(1)
    port = await registrar.start()
    recorder = Recorder()
    client = LokiSipClient(_config(port, **FAST), recorder)
    task = asyncio.create_task(client.async_run())
    try:
        async with asyncio.timeout(15):
            while client.state is not SipState.BLOCKED:
                await asyncio.sleep(0.02)

        # The card is raised, because a person may well need to act -- but the client
        # does not stop, and it must not raise the card again on every retry.
        assert recorder.terminal is not None
        assert recorder.terminal[0] is SipState.BLOCKED
        assert len(registrar.bindings) == 1, "a refusal must not register anything"

        # The other device goes away, exactly as an expiring leftover would.
        registrar.bindings.clear()

        async with asyncio.timeout(15):
            while client.state is not SipState.REGISTERED:
                await asyncio.sleep(0.02)
    finally:
        await _hard_stop(client, task)
        await registrar.stop()

    assert len(registrar.bindings) == 1
    assert registrar.evictions == 0
    blocked = [state for state, _ in recorder.states if state is SipState.BLOCKED]
    assert len(blocked) >= 1
    # One card, however many retries it took.
    assert recorder.terminal_count == 1


@pytest.mark.asyncio
async def test_a_busy_account_is_never_registered_on() -> None:
    """The retry loop must not wear the gate down: still blocked, still no binding."""
    registrar = WireTap(password=PASSWORD, echo_instance_id=False)
    registrar.seed_foreign(1)
    port = await registrar.start()
    recorder = Recorder()
    client = LokiSipClient(_config(port, **FAST), recorder)
    task = asyncio.create_task(client.async_run())
    try:
        async with asyncio.timeout(15):
            while client.state is not SipState.BLOCKED:
                await asyncio.sleep(0.02)
        # Long enough for a good many retries at the timings above.
        await asyncio.sleep(1.0)
    finally:
        await _hard_stop(client, task)
        await registrar.stop()

    # Not "where is it now": with these timings it cycles connect -> probe -> refuse
    # several times a second, and catching it mid-probe says nothing. The invariant is
    # that it never got past the gate, however many times it tried.
    assert not any(state is SipState.REGISTERED for state, _ in recorder.states)
    assert any(state is SipState.BLOCKED for state, _ in recorder.states)
    assert len(registrar.bindings) == 1
    assert registrar.evictions == 0
    assert not registrar.wildcard_seen
