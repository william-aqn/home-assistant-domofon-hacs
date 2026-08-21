"""The SIP client: connect, look, decide whether it is safe to proceed.

This stage deliberately cannot register. It connects, asks the registrar what bindings
the account already has, and decides whether registering would displace one -- and then
stops. That is a genuinely useful thing to ship on its own: it answers the only
question that matters before any risk is taken, and it cannot take that risk itself,
because nothing here is able to build a Contact header.

Registration arrives in the next stage, on top of the gate proven here.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
import contextlib
from dataclasses import dataclass, field
from enum import StrEnum
import logging
import random
import time
from typing import Protocol

from .const import MAX_HEADER_BYTES, TIMER_F
from .digest import DigestChallenge, challenges_from
from .errors import (
    SipBlockedError,
    SipFramingError,
    SipPermanentError,
    SipTransportError,
)
from .messages import MessageBuilder, Ping, Pong, SipMessage, StreamFramer
from .registration import Binding, RegistrationState, parse_bindings
from .uri import parse_params, split_semis

_LOGGER = logging.getLogger(__name__)

CONNECT_TIMEOUT = 30.0

# A registered client must refresh inside one granted expiry, so a quiet window longer
# than two of them is evidence that nobody is using the account. One sample is not: a
# dozing phone looks exactly like an empty account.
BASELINE_SAMPLES = 4
BASELINE_INTERVAL = 150.0

# RFC 5626 §4.5.
BACKOFF_BASE = 30.0
BACKOFF_MAX = 1800.0


class SipState(StrEnum):
    """Where the client is. Surfaced to the user as a sensor."""

    DISABLED = "disabled"
    CONNECTING = "connecting"
    PROBING = "probing"
    BASELINE = "baseline"
    BACKOFF = "backoff"
    # Terminal states. Each latches: a restart must not quietly retry a manoeuvre we
    # have already decided is unsafe.
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SipSnapshot:
    """What the client learned on its last look at the account."""

    state: SipState
    detail: str | None = None
    bindings: tuple[Binding, ...] = ()
    realm: str | None = None
    algorithm: str | None = None
    local: str | None = None
    received: str | None = None
    rport: str | None = None

    @property
    def foreign_count(self) -> int:
        """How many bindings belong to somebody else."""
        return len(self.bindings)


class SipEvents(Protocol):
    """What the client reports upwards. Implemented by the Home Assistant bridge."""

    def on_state(self, state: SipState, detail: str | None) -> None:
        """The state machine moved."""

    def on_snapshot(self, snapshot: SipSnapshot) -> None:
        """A fresh look at the account's bindings."""

    def on_terminal(self, state: SipState, kind: str, detail: str) -> None:
        """The client stopped for good and a person has to act."""


@dataclass
class SipConfig:
    """Everything the client needs to talk to one account."""

    host: str
    user: str
    password: str
    port: int = 5060
    # Layer one of the eviction defence, and the only part that *prevents* rather than
    # detects: if somebody else is registered, do not proceed at all.
    strict_guard: bool = True
    # Skipped once the account is known to have been used by us before.
    require_baseline: bool = True
    baseline_samples: int = BASELINE_SAMPLES
    baseline_interval: float = BASELINE_INTERVAL


@dataclass
class _Transport:
    """One TCP connection and the framer reading it."""

    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    framer: StreamFramer = field(default_factory=StreamFramer)


class LokiSipClient:
    """Connects to the registrar and reports what it finds, without registering."""

    def __init__(
        self,
        config: SipConfig,
        events: SipEvents,
        *,
        state: RegistrationState | None = None,
    ) -> None:
        """Initialise the client. Nothing is opened until async_run."""
        self._config = config
        self._events = events
        self._state = state or RegistrationState(
            host=config.host, user=config.user, port=config.port
        )
        self._transport: _Transport | None = None
        self._pending: list[SipMessage | Ping | Pong] = []
        self._challenges: dict[str, DigestChallenge] = {}
        self._seen_nonces: set[str] = set()
        self._failures = 0
        self._stopping = False
        self._current = SipState.DISABLED

    @property
    def state(self) -> SipState:
        """Current state."""
        return self._current

    # ------------------------------------------------------------- supervisor

    async def async_run(self) -> None:
        """Run until cancelled, or until something terminal happens."""
        try:
            while not self._stopping:
                try:
                    await self._one_flow()
                except asyncio.CancelledError:
                    raise
                except SipPermanentError as err:
                    kind = type(err).__name__
                    terminal = (
                        SipState.BLOCKED
                        if isinstance(err, SipBlockedError)
                        else SipState.FAILED
                    )
                    self._set_state(terminal, str(err))
                    self._events.on_terminal(terminal, kind, str(err))
                    return
                except (
                    SipTransportError,
                    SipFramingError,
                    OSError,
                    TimeoutError,
                ) as err:
                    _LOGGER.debug("SIP flow ended: %s", err)
                except Exception:
                    _LOGGER.exception("Unexpected SIP failure; backing off")

                if self._stopping:
                    return
                self._failures += 1
                delay = self._backoff()
                self._set_state(SipState.BACKOFF, f"повтор через {delay:.0f} с")
                await asyncio.sleep(delay)
        finally:
            await self._close()

    async def async_stop(self) -> None:
        """Ask the client to stop and close its socket."""
        self._stopping = True
        await self._close()

    def _backoff(self) -> float:
        """RFC 5626 §4.5: base 30s, cap 1800s, 50-100% jitter.

        Jitter matters more than it looks: without it every Home Assistant on the
        operator's network retries in lockstep after an outage.
        """
        window = min(BACKOFF_MAX, BACKOFF_BASE * 2 ** min(self._failures, 6))
        # Not a cryptographic choice: this only spreads retries in time.
        return random.uniform(0.5 * window, window)  # noqa: S311

    # --------------------------------------------------------------- one flow

    async def _one_flow(self) -> None:
        """Connect, probe, gate, and hold the answer."""
        self._set_state(SipState.CONNECTING, None)
        await self._connect()

        self._set_state(SipState.PROBING, None)
        bindings, response = await self._probe()
        self._publish(SipState.PROBING, bindings, response)

        # The gate. Layer one, and the only check that prevents rather than detects.
        self._gate(bindings)

        if self._config.require_baseline:
            bindings = await self._baseline()

        # Registration belongs to the next stage. Until then the useful outcome is the
        # answer itself, so hold the connection open rather than reconnecting in a
        # loop: an idle TCP session costs the registrar nothing and keeps the reported
        # snapshot fresh.
        self._failures = 0
        await self._idle()

    async def _idle(self) -> None:
        """Keep the connection alive and answer the registrar's pings."""
        while not self._stopping:
            message = await self._read(timeout=None)
            if isinstance(message, Ping):
                await self._send(b"\r\n")
            elif isinstance(message, SipMessage) and not message.is_response:
                # Nothing can legitimately be sent to us: we hold no binding, so no
                # proxy has our address. Decline politely rather than ignore.
                await self._send(
                    MessageBuilder.response(message, 486, "Busy Here", to_tag=None)
                )

    # ----------------------------------------------------------------- gates

    def _gate(self, bindings: Sequence[Binding]) -> None:
        """Refuse to go further if the account is already in use by somebody else."""
        if not bindings:
            return

        if not self._config.strict_guard:
            _LOGGER.warning(
                "Account already has %d binding(s); the strict guard is off",
                len(bindings),
            )
            return

        raise SipBlockedError(
            f"на этом аккаунте уже зарегистрировано устройств: {len(bindings)}"
        )

    async def _baseline(self) -> list[Binding]:
        """Sample the account repeatedly before trusting that it is unused.

        A registered client must refresh inside one granted expiry, so several quiet
        minutes is evidence. A single empty answer is not: a phone between flows, or
        simply asleep, looks identical to an account nobody uses.
        """
        self._set_state(SipState.BASELINE, "проверяю, что аккаунт не используется")
        latest: list[Binding] = []

        for sample in range(2, self._config.baseline_samples + 1):
            await asyncio.sleep(self._config.baseline_interval)
            latest, response = await self._probe()
            self._publish(SipState.BASELINE, latest, response)
            self._gate(latest)
            self._set_state(
                SipState.BASELINE,
                f"{sample}/{self._config.baseline_samples} чисто",
            )

        return latest

    # ----------------------------------------------------------------- probe

    async def _probe(self) -> tuple[list[Binding], SipMessage]:
        """Ask the registrar what bindings exist, changing nothing.

        RFC 3261 §10.2.3: a REGISTER with no Contact returns the current bindings, and
        §10.3 step 6 short-circuits past every step that would add, update or remove
        one. Neither the bindings nor their recorded Call-ID and CSeq are touched.
        """
        response = await self._register_request(contacts=(), expires=None)
        return parse_bindings(
            tuple(
                header.value
                for header in response.headers
                if header.name == "contact"
            )
        ), response

    async def _register_request(
        self, *, contacts: Sequence[str], expires: int | None
    ) -> SipMessage:
        """Send a REGISTER, answering at most one authentication challenge."""
        for attempt in (1, 2):
            auth = self._auth_headers()
            request = MessageBuilder.register(
                self._state,
                contacts=contacts,
                expires=expires,
                auth=auth,
                cseq=self._state.next_cseq(),
                branch=self._state.new_branch(),
            )
            await self._send(request)

            response = await self._final_response()

            if response.status not in (401, 407) or attempt == 2:
                return self._check_final(response)

            if not self._absorb_challenge(response):
                return self._check_final(response)

        raise SipTransportError("unreachable")

    def _check_final(self, response: SipMessage) -> SipMessage:
        """Turn a final status into either a result or a decision to stop."""
        status = response.status or 0
        if status == 200:
            return response
        if status in (401, 407):
            raise SipPermanentError(
                "регистратор отклонил учётные данные SIP — вероятно, они устарели"
            )
        if status == 403:
            raise SipPermanentError(
                "аутентификация прошла, но SIP на этом аккаунте не разрешён (403)"
            )
        if status == 404:
            raise SipPermanentError(
                "регистратор не знает этот адрес (404) — данные учётной записи устарели"
            )
        if status >= 600:
            # A registrar must never send one; something else is in the path.
            raise SipPermanentError(
                f"регистратор ответил {status}, чего делать не должен"
            )
        raise SipTransportError(f"неожиданный ответ {status}")

    def _absorb_challenge(self, response: SipMessage) -> bool:
        """Remember a challenge to answer. False if it cannot be answered."""
        proxy = response.status == 407
        name = "proxy-authenticate" if proxy else "www-authenticate"
        rows = [
            header.value for header in response.headers if header.name == name
        ]
        offers = challenges_from(rows, proxy=proxy)
        if not offers:
            return False

        challenge = offers[0]
        if challenge.nonce in self._seen_nonces and not challenge.stale:
            # The server replayed a nonce we already answered: the credentials are
            # wrong, and hammering it is how an IP ends up banned.
            return False
        self._seen_nonces.add(challenge.nonce)
        # The official client accepts any realm, so we do the same rather than pinning
        # one we have never seen.
        self._challenges[challenge.realm] = challenge
        return True

    def _auth_headers(self) -> list[str]:
        """Credentials for every challenge we have been given."""
        return [
            challenge.header(
                self._config.user,
                self._config.password,
                "REGISTER",
                self._state.registrar_uri,
            )
            for challenge in self._challenges.values()
        ]

    # ------------------------------------------------------------- transport

    async def _connect(self) -> None:
        """Open the TCP connection and record the address the registrar will see."""
        await self._close()
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                reader, writer = await asyncio.open_connection(
                    self._config.host, self._config.port, happy_eyeballs_delay=0.25
                )
        except (OSError, TimeoutError) as err:
            raise SipTransportError(f"не удалось подключиться: {err}") from err

        self._transport = _Transport(reader, writer)
        host, port = writer.get_extra_info("sockname")[:2]
        self._state.sent_by = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        # A fresh connection is a fresh registration identity as far as digest is
        # concerned; keeping stale nonces would produce a replay on the first request.
        self._challenges.clear()
        self._seen_nonces.clear()

    async def _send(self, data: bytes) -> None:
        """Write to the socket."""
        if self._transport is None:
            raise SipTransportError("not connected")
        try:
            self._transport.writer.write(data)
            await self._transport.writer.drain()
        except OSError as err:
            raise SipTransportError(f"обрыв записи: {err}") from err

    async def _read(self, *, timeout: float | None) -> SipMessage | Ping | Pong:
        """Read the next framed item, answering nothing."""
        if self._transport is None:
            raise SipTransportError("not connected")

        while True:
            if self._pending:
                return self._pending.pop(0)
            try:
                async with asyncio.timeout(timeout):
                    chunk = await self._transport.reader.read(MAX_HEADER_BYTES)
            except TimeoutError as err:
                raise SipTransportError("регистратор не ответил") from err
            except OSError as err:
                raise SipTransportError(f"обрыв чтения: {err}") from err
            if not chunk:
                raise SipTransportError("регистратор закрыл соединение")
            self._pending.extend(self._transport.framer.feed(chunk))

    async def _final_response(self) -> SipMessage:
        """Read until a final response arrives, answering keepalives on the way."""
        deadline = time.monotonic() + TIMER_F
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SipTransportError("регистратор не ответил вовремя")
            item = await self._read(timeout=remaining)
            if isinstance(item, Ping):
                await self._send(b"\r\n")
            elif (
                isinstance(item, SipMessage)
                and item.is_response
                and (item.status or 0) >= 200
            ):
                return item
                # A 100 to a REGISTER is legal, just uninteresting.

    async def _close(self) -> None:
        """Close the socket, tolerating one that is already gone."""
        transport, self._transport = self._transport, None
        self._pending.clear()
        if transport is None:
            return
        transport.writer.close()
        with contextlib.suppress(OSError):
            await transport.writer.wait_closed()

    # ------------------------------------------------------------- reporting

    def _set_state(self, state: SipState, detail: str | None) -> None:
        self._current = state
        _LOGGER.debug("SIP state: %s (%s)", state, detail)
        self._events.on_state(state, detail)

    def _publish(
        self, state: SipState, bindings: Sequence[Binding], response: SipMessage
    ) -> None:
        """Report what the last probe saw, including how the registrar sees us."""
        via = response.value("via")
        params = parse_params(split_semis(via)[1:]) if via else {}
        challenge = next(iter(self._challenges.values()), None)

        self._events.on_snapshot(
            SipSnapshot(
                state=state,
                bindings=tuple(bindings),
                realm=challenge.realm if challenge else None,
                algorithm=challenge.algorithm if challenge else None,
                local=self._state.sent_by,
                received=params.get("received"),
                rport=params.get("rport"),
            )
        )
