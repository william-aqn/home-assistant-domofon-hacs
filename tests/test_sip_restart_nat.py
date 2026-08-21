"""A restart behind NAT, against a registrar shaped like the live one.

The existing restart tests use a registrar that reports the Contact it was sent. The
live one is behind NAT: it tells us through ``received``/``rport`` that it sees us at
another address, we correct the Contact, and the binding it ends up holding is the
corrected one. That correction is the part no test covered, and it is where a restart
stopped recognising its own binding on the real account -- twice, on two consecutive
Home Assistant restarts, and on demand by switching SIP off and straight back on.

The whole chain is exercised, not just the client: what the bridge persists goes
through ``as_dict``/``from_dict`` in between, because a URI that survives in memory
and not on disk fails exactly the same way.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from custom_components.loki.sip.client import LokiSipClient, SipState
from custom_components.loki.sip.registration import RegistrationState
from custom_components.loki.sip_store import SipStoredState
from tests.test_sip_client import Recorder, WireTap
from tests.test_sip_registration import (
    PASSWORD,
    USER,
    _config,
    _hard_stop,
    _register_once,
)

INSTANCE = "11111111-2222-3333-4444-555555555555"


def _persisted(uri: str) -> SipStoredState:
    """What the bridge writes on REGISTERED, read back as a fresh process would."""
    stored = SipStoredState(instance_id=INSTANCE, first_registration_done=True)
    stored.record_contact(uri)
    return SipStoredState.from_dict(json.loads(json.dumps(stored.as_dict())))


@pytest.mark.asyncio
async def test_restart_behind_nat_reclaims_its_binding() -> None:
    """The corrected Contact is the one to remember, and it must be recognised."""
    registrar = WireTap(
        password=PASSWORD,
        echo_instance_id=False,
        rewrite_contact=True,
        # A real NAT: a fresh port for every connection, so the restart cannot fall
        # back on recognising its own address.
        nat_port=None,
    )
    port = await registrar.start()
    recorder = Recorder()
    try:
        previous = await _register_once(port, INSTANCE)
        assert len(registrar.bindings) == 1
        # The binding the registrar holds is the corrected one, not the address the
        # socket reported -- the same thing the live registrar does.
        assert registrar.bindings[0].uri == previous

        revived = _persisted(previous)
        state = RegistrationState(
            host="127.0.0.1", user=USER, port=port, instance_id=revived.instance_id
        )
        state.adopt_prior_contacts(revived.fresh_contacts())

        client = LokiSipClient(_config(port), recorder, state=state)
        task = asyncio.create_task(client.async_run())
        try:
            async with asyncio.timeout(15):
                while client.state is not SipState.REGISTERED:
                    assert recorder.terminal is None, recorder.terminal
                    await asyncio.sleep(0.02)
        finally:
            await _hard_stop(client, task)
    finally:
        await registrar.stop()

    assert recorder.terminal is None
    assert len(registrar.bindings) == 1


@pytest.mark.asyncio
async def test_restart_when_the_nat_hands_back_the_same_port() -> None:
    """The corrected Contact and the leftover can be the very same URI.

    A NAT is entitled to give a new connection the mapping the old one had just
    released. Then the binding being reclaimed and the binding being created are one
    string, and asking a registrar to register and withdraw it in a single message is
    refused outright -- which used to leave the client retrying that message for ever.
    """
    registrar = WireTap(
        password=PASSWORD,
        echo_instance_id=False,
        rewrite_contact=True,
        # Fixed: every connection is seen at the same address, which is the case
        # this test exists for.
        nat_port=44444,
    )
    port = await registrar.start()
    recorder = Recorder()
    try:
        previous = await _register_once(port, INSTANCE)
        revived = _persisted(previous)
        state = RegistrationState(
            host="127.0.0.1", user=USER, port=port, instance_id=revived.instance_id
        )
        state.adopt_prior_contacts(revived.fresh_contacts())

        client = LokiSipClient(_config(port), recorder, state=state)
        task = asyncio.create_task(client.async_run())
        try:
            async with asyncio.timeout(15):
                while client.state is not SipState.REGISTERED:
                    assert recorder.terminal is None, recorder.terminal
                    await asyncio.sleep(0.02)
        finally:
            await _hard_stop(client, task)
    finally:
        await registrar.stop()

    assert recorder.terminal is None
    assert len(registrar.bindings) == 1
