"""The SIP client: look before registering, and keep watching afterwards.

The account is a single address-of-record that the resident's phone may already be
registered to. Displacing that binding is invisible from their side -- the phone does
not notice, and simply stops ringing until its own timer fires minutes later. So the
client looks first, refuses to proceed if somebody else is there, and after registering
proves that nobody vanished.

``SipConfig.register=False`` keeps it in probe-only mode, where it reports what it finds
and is structurally unable to change anything.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
import contextlib
from dataclasses import dataclass, field
from enum import StrEnum
import logging
import random
import secrets
import time
from typing import Protocol

from .const import CRLF, MAX_HEADER_BYTES, TIMER_F
from .digest import DigestChallenge, challenges_from
from .errors import (
    SipBlockedError,
    SipError,
    SipEvictionError,
    SipFramingError,
    SipPermanentError,
    SipTransportError,
    SipUnverifiableError,
)
from .messages import MessageBuilder, Ping, Pong, SipMessage, StreamFramer
from .registration import Binding, RegistrationState, parse_bindings, uri_equal
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

# RFC 5626 §4.4.1 keepalive. Short because a lapsed NAT mapping costs a doorbell.
KEEPALIVE = 90.0

# A binding this close to its own expiry vanished on its own, not because of us.
EXPIRY_SLACK = 90.0

# A single observation is not enough to latch a permanent failure.
EVICTION_CONFIRM_DELAY = 5.0


class SipState(StrEnum):
    """Where the client is. Surfaced to the user as a sensor."""

    DISABLED = "disabled"
    CONNECTING = "connecting"
    PROBING = "probing"
    BASELINE = "baseline"
    REGISTERING = "registering"
    VERIFYING = "verifying"
    REGISTERED = "registered"
    BACKOFF = "backoff"
    # Terminal states. Each latches: a restart must not quietly retry a manoeuvre we
    # have already decided is unsafe.
    BLOCKED = "blocked"
    EVICTED = "evicted"
    FAILED = "failed"


# Which terminal state each permanent failure lands in. Anything not listed is a
# plain failure; eviction and blocking are called out because they mean something
# different happened to the resident's phone.
_TERMINAL_STATES: dict[type[Exception], SipState] = {
    SipBlockedError: SipState.BLOCKED,
    SipEvictionError: SipState.EVICTED,
    SipUnverifiableError: SipState.FAILED,
}


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
    # False keeps the client in the probe-only mode: it looks and reports, and cannot
    # change anything on the account.
    register: bool = False
    expires: int = 300
    # The first registration ever made on an account uses a short expiry so that a
    # mistake -- or Home Assistant being killed mid-flow -- heals in a minute rather
    # than in the several minutes a phone takes to notice on its own timer.
    first_expires: int = 60
    first_registration_done: bool = False


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
                    terminal = _TERMINAL_STATES.get(type(err), SipState.FAILED)
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

        if not self._config.register:
            # Probe-only mode. The useful outcome is the answer itself, so hold the
            # connection open rather than reconnecting in a loop: an idle TCP session
            # costs the registrar nothing and keeps the reported snapshot fresh.
            self._failures = 0
            await self._idle()
            return

        await self._register_and_verify(bindings)
        self._failures = 0
        self._set_state(
            SipState.REGISTERED,
            f"срок действия {self._state.granted_expires} с",
        )
        await self._serve()

    async def _register_and_verify(self, before: Sequence[Binding]) -> None:
        """Register, correct the Contact if needed, then prove nothing was displaced."""
        self._set_state(SipState.REGISTERING, None)

        first_time = not self._config.first_registration_done
        expires = (
            self._config.first_expires if first_time else self._config.expires
        )
        # Withdraw our own leftovers in the same message that creates the new binding,
        # so the table never holds two of ours at once.
        reap = [binding.uri for binding in before if self._reapable(binding)]
        started = time.monotonic()

        response = await self._register_once(expires=expires, reap=reap)

        # The registrar tells us the address it actually sees. Behind a container
        # bridge the socket's own address is a private one the registrar can never
        # reach, so without this correction the registration succeeds and the phone
        # never rings -- the worst possible failure, because everything looks healthy.
        if (rewritten := self._rewritten_contact(response)) is not None:
            _LOGGER.debug("Rewriting Contact from received/rport")
            self._state.set_contact(rewritten)
            response = await self._register_once(expires=expires, reap=reap)

        self._resolve_granted(response, requested=expires)

        self._set_state(SipState.VERIFYING, None)
        after, verify_response = await self._probe()
        self._publish(SipState.VERIFYING, after, verify_response)
        self._assert_bindings_visible(after)
        await self._assert_no_eviction(before, after, time.monotonic() - started)

    async def _register_once(
        self, *, expires: int, reap: Sequence[str]
    ) -> SipMessage:
        """Send one REGISTER carrying our live Contact."""
        contacts = self._state.build_contacts(
            live=self._state.contact_uri, reap=reap
        )
        return await self._register_request(contacts=contacts, expires=expires)

    def _rewritten_contact(self, response: SipMessage) -> str | None:
        """A corrected Contact URI, if the registrar sees us at another address."""
        via = response.value("via")
        if not via:
            return None
        params = parse_params(split_semis(via)[1:])
        received, rport = params.get("received"), params.get("rport")
        if not received or not rport or not rport.isdigit():
            return None

        corrected = self._state.make_contact_uri(received, int(rport))
        return None if corrected == self._state.contact_uri else corrected

    def _resolve_granted(self, response: SipMessage, *, requested: int) -> None:
        """Work out how long the registration actually lasts.

        In order of authority: the expires parameter on the Contact the registrar
        echoed back for us, then the response's Expires header, then what we asked for.
        Renewing against the requested value when the server granted less is how a
        registration silently lapses.
        """
        rows = tuple(
            header.value for header in response.headers if header.name == "contact"
        )
        for binding in parse_bindings(rows):
            if binding.expires is not None and self._is_ours(binding):
                self._state.granted_expires = binding.expires
                return

        header = response.value("expires")
        if header.isdigit():
            self._state.granted_expires = int(header)
            return

        _LOGGER.warning(
            "Регистратор не сообщил срок действия регистрации; исходим из "
            "запрошенных %s с",
            requested,
        )
        self._state.granted_expires = requested

    async def _serve(self) -> None:
        """Hold the registration: read, keep the connection warm, renew in time.

        All three run together and any one failing tears the flow down, because a
        registration whose refresh task has died is a registration that will lapse
        without anybody noticing.
        """
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(self._read_loop())
                group.create_task(self._keepalive_loop())
                group.create_task(self._refresh_loop())
        except* (SipError, OSError, TimeoutError) as group_error:
            raise group_error.exceptions[0] from None

    async def _read_loop(self) -> None:
        """Answer whatever the registrar sends while we hold a binding."""
        while not self._stopping:
            message = await self._read(timeout=None)
            if isinstance(message, Ping):
                await self._send(CRLF)
            elif isinstance(message, SipMessage) and not message.is_response:
                # INVITE handling belongs to the next stage. Until it exists, decline
                # the branch immediately rather than leave it hanging: an unresolved
                # branch stops a forking proxy from delivering other branches' final
                # responses, which would break the decline button on the phone.
                await self._send(
                    MessageBuilder.response(
                        message, 486, "Busy Here", to_tag=secrets.token_hex(4)
                    )
                )

    async def _keepalive_loop(self) -> None:
        """Send a CRLF now and then so the connection is not silently dropped.

        RFC 5626 §4.4.1. Mains-powered, so the interval is short: the cost of an
        idle NAT mapping expiring is a doorbell that does not ring.
        """
        while not self._stopping:
            await asyncio.sleep(random.uniform(0.8 * KEEPALIVE, KEEPALIVE))  # noqa: S311
            await self._send(CRLF)

    async def _refresh_loop(self) -> None:
        """Renew the registration before the granted expiry runs out."""
        while not self._stopping:
            await asyncio.sleep(self._state.refresh_delay())
            self._state.forget_stale_priors(self._state.granted_expires or 300)
            response = await self._register_once(
                expires=self._config.expires, reap=[]
            )
            self._resolve_granted(response, requested=self._config.expires)
            _LOGGER.debug(
                "Registration refreshed, granted %ss", self._state.granted_expires
            )

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

    def _is_ours(self, binding: Binding) -> bool:
        """Whether a binding is one of ours.

        Deciding this on the instance id alone would be wrong: only outbound-aware
        registrars store that parameter, so on the first reconnect our own previous
        binding -- new source port, no instance id echoed -- would read as somebody
        else's and the client would block itself on a perfectly healthy account.
        """
        if binding.instance_id:
            return binding.instance_id == self._state.instance_id.lower()

        known = [
            self._state.contact_uri,
            *(prior.uri for prior in self._state.prior_contacts),
        ]
        return any(uri and uri_equal(binding.uri, uri) for uri in known)

    def _reapable(self, binding: Binding) -> bool:
        """Whether we may withdraw this binding.

        A ``;expires=0`` row is only ever aimed at a binding present in the current
        snapshot, positively attributable to us, and not carrying somebody else's
        instance id. A NAT port can be handed to another device, so "it used to be
        our address" is not on its own good enough.
        """
        if binding.uri == self._state.contact_uri:
            return False
        if (
            binding.instance_id
            and binding.instance_id != self._state.instance_id.lower()
        ):
            return False
        return self._is_ours(binding)

    def _foreign(self, bindings: Sequence[Binding]) -> list[Binding]:
        """Bindings that belong to somebody else."""
        return [binding for binding in bindings if not self._is_ours(binding)]

    def _assert_bindings_visible(self, after: Sequence[Binding]) -> None:
        """Refuse to hold a registration we cannot supervise.

        If our own binding is missing from the list we just fetched, the registrar is
        not reporting bindings -- and then every eviction check is blind. Registering
        anyway would mean gambling with the resident's doorbell.
        """
        if not any(self._is_ours(binding) for binding in after):
            raise SipUnverifiableError(
                "регистратор не сообщает привязки — проверить вытеснение невозможно"
            )

    async def _assert_no_eviction(
        self, before: Sequence[Binding], after: Sequence[Binding], elapsed: float
    ) -> None:
        """Stop for good if registering displaced somebody."""
        vanished = self._vanished(before, after, elapsed)
        if not vanished:
            return

        # One observation is not enough to latch a permanent, user-visible failure: a
        # binding can disappear because it expired on its own, or because the phone
        # reconnected and minted a new source port. Confirm before acting.
        await asyncio.sleep(EVICTION_CONFIRM_DELAY)
        recheck, _response = await self._probe()
        vanished = self._vanished(before, recheck, elapsed + EVICTION_CONFIRM_DELAY)
        if not vanished:
            _LOGGER.debug("Binding difference settled on the second observation")
            return

        await self._withdraw_own_contact()
        raise SipEvictionError(
            f"регистрация вытеснила чужих привязок: {len(vanished)}"
        )

    def _vanished(
        self, before: Sequence[Binding], after: Sequence[Binding], elapsed: float
    ) -> list[Binding]:
        """Foreign bindings present before and absent afterwards."""
        gone: list[Binding] = []
        for binding in before:
            if self._is_ours(binding):
                continue
            # Natural expiry is not eviction. The registrar reports each binding's
            # remaining lifetime, so use it rather than blaming ourselves for a clock.
            if (
                binding.expires is not None
                and binding.expires <= elapsed + EXPIRY_SLACK
            ):
                continue
            # Match on instance id where there is one: a phone's Contact URI changes on
            # every reconnect because the source port is part of it.
            if binding.instance_id and any(
                other.instance_id == binding.instance_id for other in after
            ):
                continue
            if any(uri_equal(other.uri, binding.uri) for other in after):
                continue
            gone.append(binding)
        return gone

    async def _withdraw_own_contact(self) -> None:
        """Take our binding back off the account, and nothing else."""
        if not self._state.contact_uri:
            return
        with contextlib.suppress(SipError, OSError, TimeoutError):
            await self._register_request(
                contacts=[f"<{self._state.contact_uri}>;expires=0"], expires=0
            )
            _LOGGER.debug("Withdrew our own binding")

    def _gate(self, bindings: Sequence[Binding]) -> None:
        """Refuse to go further if the account is already in use by somebody else."""
        bindings = self._foreign(bindings)
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
        # The socket's own address is only a first guess -- behind a container bridge
        # the registrar sees a different one and tells us so via received/rport.
        self._state.set_contact(self._state.make_contact_uri(host, port))
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
