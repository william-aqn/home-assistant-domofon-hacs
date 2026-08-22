"""Stopping must actually give the account back.

Every case here was found by an adversarial read of the change that introduced the
withdrawal, and every one of them ends the same way: a binding left on the account,
which the next process reads as another device and refuses to register alongside.
"""

from __future__ import annotations

import asyncio

import pytest

from custom_components.loki.sip import client as client_module
from custom_components.loki.sip.client import LokiSipClient, SipState
from custom_components.loki.sip.errors import SipSafetyError, SipTransportError
from tests.test_sip_client import Recorder, WireTap
from tests.test_sip_registration import PASSWORD, _config, _hard_stop


@pytest.mark.asyncio
async def test_a_stop_before_registered_still_hands_the_binding_back() -> None:
    """The binding exists several seconds before the state says REGISTERED.

    It is created the moment the REGISTER is answered; REGISTERED waits for the
    verification probe, and when the binding lists disagree for five seconds more.
    A switch flicked inside that window used to leave the binding behind -- which is
    exactly the switch this whole mechanism exists to make safe.
    """
    registrar = WireTap(password=PASSWORD, echo_instance_id=False, reply_delay=0.3)
    port = await registrar.start()
    recorder = Recorder()
    client = LokiSipClient(_config(port), recorder)
    task = asyncio.create_task(client.async_run())
    try:
        async with asyncio.timeout(15):
            while client.state is not SipState.VERIFYING:
                assert client.state is not SipState.REGISTERED, "too late to be a test"
                await asyncio.sleep(0.01)
        assert registrar.bindings, "the account should be carrying our binding by now"
        await client.async_stop()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await registrar.stop()

    assert registrar.bindings == []
    assert not registrar.wildcard_seen


@pytest.mark.asyncio
async def test_a_refresh_cannot_outrun_a_stop() -> None:
    """A renewal queued behind the withdrawal must not put the binding back.

    Both wait on the same lock, and the renewal is entitled to be woken first. If it
    goes through after the withdrawal, the account ends up holding a fresh binding for
    a full expiry and the stop only looks clean.
    """
    registrar = WireTap(password=PASSWORD)
    port = await registrar.start()
    client = LokiSipClient(_config(port), Recorder())
    client._stopping = True

    with pytest.raises(SipTransportError, match="останавливается"):
        await client._register_request(contacts=[], expires=300)

    # A withdrawal is the one exchange a stopping client still has business making,
    # so this one gets past the guard -- and then fails further along, on a client
    # that never connected. Which error it is does not matter; where it comes from
    # does, and it comes from the message builder rather than the guard.
    with pytest.raises(SipSafetyError, match="sent_by"):
        await client._register_request(contacts=["<sip:u@h:1>;expires=0"], expires=0)
    await registrar.stop()


@pytest.mark.asyncio
async def test_every_block_raises_its_own_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The card is raised once per block, not once per client.

    Left as a single latch, the first self-inflicted block after a restart would
    silence every later one -- including the day the resident's phone genuinely takes
    the account, when the doorbell would stop with no explanation at all.
    """
    # Reconnecting after a dropped connection goes through the backoff curve, which
    # starts at thirty seconds. Not what is under test here.
    monkeypatch.setattr(client_module, "BACKOFF_BASE", 0.05)
    monkeypatch.setattr(client_module, "BACKOFF_MAX", 0.2)

    registrar = WireTap(password=PASSWORD, echo_instance_id=False)
    registrar.seed_foreign(1)
    port = await registrar.start()
    recorder = Recorder()
    # All three: the seeded binding advertises expires=300, and the retry quite
    # correctly waits that out unless the ceiling says otherwise.
    fast = {
        "blocked_retry_min": 0.05,
        "blocked_retry_base": 0.05,
        "blocked_retry_max": 0.2,
    }
    client = LokiSipClient(_config(port, **fast), recorder)
    task = asyncio.create_task(client.async_run())
    try:
        async with asyncio.timeout(15):
            while client.state is not SipState.BLOCKED:
                await asyncio.sleep(0.02)
        assert recorder.terminal_count == 1

        registrar.bindings.clear()
        async with asyncio.timeout(15):
            while client.state is not SipState.REGISTERED:
                await asyncio.sleep(0.02)

        # Somebody else turns up, and the connection goes with them.
        registrar.bindings.clear()
        registrar.seed_foreign(1)
        registrar.drop_connection()
        async with asyncio.timeout(20):
            while client.state is not SipState.BLOCKED:
                await asyncio.sleep(0.02)
    finally:
        await _hard_stop(client, task)
        await registrar.stop()

    assert recorder.terminal_count == 2


@pytest.mark.asyncio
async def test_a_stop_takes_back_the_row_the_registrar_kept() -> None:
    """Registering leaves two rows behind, and a stop has to take back both.

    The first REGISTER carries the address of our own socket -- inside a container,
    one the registrar can never reach. The corrected registration asks for that row to
    go in the same message, and this registrar keeps it anyway. Nothing notices,
    because at that moment both rows are ours.

    The next start is what notices. A container comes up with a new internal address,
    so the leftover matches nothing we hold or remember, and the account looks occupied
    by a stranger carrying our own number. Measured on the live account before this was
    fixed: known_contacts 2, foreign_same_user true, address_changed false.
    """
    registrar = WireTap(
        password=PASSWORD,
        echo_instance_id=False,
        rewrite_contact=True,
        nat_port=None,
        ignore_reap=True,
    )
    port = await registrar.start()
    client = LokiSipClient(_config(port), Recorder())
    task = asyncio.create_task(client.async_run())
    try:
        async with asyncio.timeout(15):
            while client.state is not SipState.REGISTERED:
                await asyncio.sleep(0.02)
        # Both rows are on the account, and both are ours.
        assert len(registrar.bindings) == 2
        await client.async_stop()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await registrar.stop()

    assert registrar.bindings == [], "a row was left for the next start to trip over"
    assert not registrar.wildcard_seen
