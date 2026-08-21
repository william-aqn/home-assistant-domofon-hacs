"""The client, against a registrar whose policy we control.

The assertion that matters most is negative: across every path exercised here, not one
byte of a Contact header reaches the wire. This stage is supposed to be incapable of
changing anything on the account, and that is checked rather than assumed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from custom_components.loki.sip.client import (
    LokiSipClient,
    SipConfig,
    SipSnapshot,
    SipState,
)
from custom_components.loki.sip.registration import Binding
from tests.fake_registrar import FakeRegistrar

PASSWORD = "secret"
USER = "1009999"


@dataclass
class Recorder:
    """Collects everything the client reports."""

    states: list[tuple[SipState, str | None]] = field(default_factory=list)
    snapshots: list[SipSnapshot] = field(default_factory=list)
    terminal: tuple[SipState, str, str] | None = None

    def on_state(self, state: SipState, detail: str | None) -> None:
        self.states.append((state, detail))

    def on_snapshot(self, snapshot: SipSnapshot) -> None:
        self.snapshots.append(snapshot)

    def on_terminal(self, state: SipState, kind: str, detail: str) -> None:
        self.terminal = (state, kind, detail)


class WireTap(FakeRegistrar):
    """A registrar that keeps every request line it was sent."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.seen: list[list[str]] = []

    def _respond(self, rows: list[str]) -> str:
        self.seen.append(list(rows))
        return super()._respond(rows)

    @property
    def contact_rows(self) -> list[str]:
        """Every Contact header the client ever sent."""
        return [
            row
            for request in self.seen
            for row in request
            if row.lower().startswith("contact")
        ]


def _config(port: int, **overrides: object) -> SipConfig:
    base = {
        "host": "127.0.0.1",
        "user": USER,
        "password": PASSWORD,
        "port": port,
        "require_baseline": False,
    }
    base.update(overrides)
    return SipConfig(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------- probe


@pytest.mark.asyncio
async def test_probe_reports_an_empty_account_and_never_sends_a_contact() -> None:
    """The whole stage: look, report, touch nothing."""
    registrar = WireTap(password=PASSWORD)
    recorder = Recorder()
    port = await registrar.start()
    client = LokiSipClient(_config(port), recorder)
    task = asyncio.create_task(client.async_run())
    try:
        async with asyncio.timeout(10):
            while not recorder.snapshots:
                await asyncio.sleep(0.02)
    finally:
        await client.async_stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await registrar.stop()

    assert registrar.contact_rows == [], "this stage must not send a Contact"
    assert registrar.bindings == []
    assert recorder.snapshots[0].bindings == ()
    assert recorder.snapshots[0].realm == "fake.registrar"
    assert recorder.snapshots[0].algorithm == "MD5"


@pytest.mark.asyncio
async def test_probe_reports_a_foreign_binding_and_blocks() -> None:
    """Somebody else is registered, so the client refuses to go any further."""
    registrar = WireTap(password=PASSWORD)
    registrar.seed_foreign(1)
    recorder = Recorder()
    port = await registrar.start()
    client = LokiSipClient(_config(port), recorder)

    task = asyncio.create_task(client.async_run())
    try:
        async with asyncio.timeout(10):
            while recorder.terminal is None:
                await asyncio.sleep(0.02)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await registrar.stop()

    assert recorder.terminal is not None
    state, kind, _ = recorder.terminal
    assert state is SipState.BLOCKED
    assert kind == "SipBlockedError"
    # The binding it found belongs to a PJSIP client -- the official app.
    binding = recorder.snapshots[0].bindings[0]
    assert isinstance(binding, Binding)
    assert binding.looks_like_pjsua is True
    # Blocking must leave the account exactly as it was found.
    assert registrar.contact_rows == []
    assert len(registrar.bindings) == 1


@pytest.mark.asyncio
async def test_strict_guard_off_reports_but_does_not_block() -> None:
    """The escape hatch still reports the danger; it just does not stop."""
    registrar = WireTap(password=PASSWORD)
    registrar.seed_foreign(2)
    recorder = Recorder()
    port = await registrar.start()
    client = LokiSipClient(_config(port, strict_guard=False), recorder)

    task = asyncio.create_task(client.async_run())
    try:
        async with asyncio.timeout(10):
            while not recorder.snapshots:
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.2)
    finally:
        await client.async_stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await registrar.stop()

    assert recorder.terminal is None
    assert recorder.snapshots[0].foreign_count == 2
    assert registrar.contact_rows == []


# ------------------------------------------------------------------ failures


@pytest.mark.asyncio
async def test_bad_credentials_stop_rather_than_retry() -> None:
    """Hammering a rejected credential is how an IP ends up banned."""
    registrar = WireTap(password=PASSWORD)
    recorder = Recorder()
    port = await registrar.start()
    client = LokiSipClient(_config(port, password="wrong"), recorder)

    task = asyncio.create_task(client.async_run())
    try:
        async with asyncio.timeout(10):
            while recorder.terminal is None:
                await asyncio.sleep(0.02)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await registrar.stop()

    assert recorder.terminal is not None
    assert recorder.terminal[0] is SipState.FAILED


@pytest.mark.asyncio
async def test_unreachable_registrar_backs_off_instead_of_giving_up() -> None:
    """A closed port is transient: keep trying, slowly."""
    recorder = Recorder()
    client = LokiSipClient(
        SipConfig(host="127.0.0.1", user=USER, password=PASSWORD, port=1),
        recorder,
    )

    task = asyncio.create_task(client.async_run())
    try:
        async with asyncio.timeout(10):
            while not any(s is SipState.BACKOFF for s, _ in recorder.states):
                await asyncio.sleep(0.02)
    finally:
        await client.async_stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert recorder.terminal is None, "a transport failure is not terminal"
    assert any(s is SipState.BACKOFF for s, _ in recorder.states)


def test_backoff_grows_and_stays_within_the_rfc_bounds() -> None:
    """RFC 5626 §4.5: base 30s, cap 1800s, 50-100% jitter."""
    client = LokiSipClient(
        SipConfig(host="h", user=USER, password=PASSWORD), Recorder()
    )

    client._failures = 1
    first = [client._backoff() for _ in range(50)]
    client._failures = 10
    late = [client._backoff() for _ in range(50)]

    assert all(30 <= value <= 60 for value in first)
    assert all(900 <= value <= 1800 for value in late)
    # Jitter is not decoration: without it every installation retries in lockstep.
    assert len(set(first)) > 1


# ------------------------------------------------------------------ baseline


@pytest.mark.asyncio
async def test_baseline_samples_repeatedly_before_trusting_an_empty_account() -> None:
    """One empty answer is not evidence: a dozing phone looks exactly the same."""
    registrar = WireTap(password=PASSWORD)
    recorder = Recorder()
    port = await registrar.start()
    client = LokiSipClient(
        _config(
            port,
            require_baseline=True,
            baseline_samples=3,
            baseline_interval=0.05,
        ),
        recorder,
    )

    task = asyncio.create_task(client.async_run())
    try:
        async with asyncio.timeout(10):
            while len(recorder.snapshots) < 3:
                await asyncio.sleep(0.02)
    finally:
        await client.async_stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await registrar.stop()

    assert any(s is SipState.BASELINE for s, _ in recorder.states)
    assert len(recorder.snapshots) >= 3
    assert registrar.contact_rows == []


@pytest.mark.asyncio
async def test_a_phone_appearing_during_baseline_blocks() -> None:
    """The exact case the baseline exists for: it was asleep, then it woke up."""
    registrar = WireTap(password=PASSWORD)
    recorder = Recorder()
    port = await registrar.start()
    client = LokiSipClient(
        _config(
            port,
            require_baseline=True,
            baseline_samples=4,
            baseline_interval=0.05,
        ),
        recorder,
    )

    task = asyncio.create_task(client.async_run())
    try:
        async with asyncio.timeout(10):
            while not recorder.snapshots:
                await asyncio.sleep(0.02)
            registrar.seed_foreign(1)  # the phone comes back
            while recorder.terminal is None:
                await asyncio.sleep(0.02)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await registrar.stop()

    assert recorder.terminal is not None
    assert recorder.terminal[0] is SipState.BLOCKED
    assert registrar.contact_rows == []
