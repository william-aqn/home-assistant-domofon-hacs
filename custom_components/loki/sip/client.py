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
import time
from typing import Protocol

from .const import ALLOW, CRLF, MAX_HEADER_BYTES, PING, TIMER_F
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
from .transactions import InviteTransaction, TransactionTable, transaction_key
from .uri import name_addr, parse_params, parse_uri, split_semis

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

# How long our branch may stay unresolved. Must be comfortably under a proxy's Timer C
# (over 180 s): while our branch is open the proxy will not forward the other branches'
# final responses, so the decline button on the resident's phone stops working.
BRANCH_DEADLINE = 115.0


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


# A few seconds to hand the binding back on the way out. Home Assistant does not wait
# long at shutdown, and a de-registration that does not make it costs only what we had
# before it was attempted.
STOP_WITHDRAW_TIMEOUT = 3.0

# Looking again at an account somebody else holds. Nearly always our own binding from
# before a restart, so the first look lands just after the one we saw would lapse; the
# rest is a doubling curve, because an account that is genuinely in use must not be
# probed every minute for ever.
# Where a binding that is not ours sits, as far as a person needs to know.
FOREIGN_PUBLIC = "наш публичный адрес"
FOREIGN_LOCAL = "наш локальный адрес"
FOREIGN_ELSEWHERE = "другой адрес"
FOREIGN_UNKNOWN = "адрес неизвестен"


def _host_only(sent_by: str | None) -> str | None:
    """The host out of a ``host:port`` sent-by, brackets and all."""
    if not sent_by:
        return None
    if sent_by.startswith("["):
        return sent_by.partition("]")[0].lstrip("[") or None
    return sent_by.rsplit(":", 1)[0] or None


BLOCKED_RETRY_MIN = 30.0
BLOCKED_RETRY_BASE = 60.0
BLOCKED_RETRY_MAX = 900.0

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
    # Everything the registrar reported, and the subset that is not ours. Two fields
    # rather than one because a count of "somebody else is here" is what gates the
    # whole design, and deriving it from the full list at each call site is how one
    # of those sites eventually forgets to.
    bindings: tuple[Binding, ...] = ()
    foreign: tuple[Binding, ...] = ()
    realm: str | None = None
    algorithm: str | None = None
    local: str | None = None
    received: str | None = None
    rport: str | None = None

    @property
    def foreign_count(self) -> int:
        """How many bindings belong to somebody else."""
        return len(self.foreign)

    @property
    def foreign_where(self) -> str | None:
        """Where the other binding sits, relative to us -- named without naming it.

        The three answers mean different things, and together they are what separates
        "our own leftover from before a restart" from "the resident's phone":

        * at the public address the registrar sees us at -- almost always ours, from
          a previous connection, still there until it lapses;
        * at the address we send in our own Contact -- also ours, held by a registrar
          that stores what it was given rather than what it saw;
        * anywhere else -- somebody else, or us from an address that has changed
          since.

        Reported, never acted on. A phone on the same home Wi-Fi shares the public
        address, so this can only ever be a hint to a person.
        """
        if not self.foreign:
            return None
        mine = {host for host in (self.received, _host_only(self.local)) if host}
        if not mine:
            return FOREIGN_UNKNOWN
        for binding in self.foreign:
            parsed = parse_uri(binding.uri)
            if parsed is None or parsed.host not in mine:
                continue
            return FOREIGN_PUBLIC if parsed.host == self.received else FOREIGN_LOCAL
        return FOREIGN_ELSEWHERE

    @property
    def foreign_expires_in(self) -> int | None:
        """How long the longest-lived foreign binding has left, if it says."""
        left = [
            binding.expires
            for binding in self.foreign
            if binding.expires is not None and binding.expires > 0
        ]
        return max(left) if left else None


class SipEvents(Protocol):
    """What the client reports upwards. Implemented by the Home Assistant bridge."""

    def on_state(self, state: SipState, detail: str | None) -> None:
        """The state machine moved."""

    def on_snapshot(self, snapshot: SipSnapshot) -> None:
        """A fresh look at the account's bindings."""

    def on_terminal(self, state: SipState, kind: str, detail: str) -> None:
        """The client stopped for good and a person has to act."""

    async def on_incoming(self, call_id: str, remote_uri: str) -> bool:
        """Ring in Home Assistant. False means nothing will answer -> decline now."""
        raise NotImplementedError

    def on_call_end(self, call_id: str, reason: str) -> None:
        """A ringing call finished, one way or another."""


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
    # How soon to look again at an account somebody else holds. Configurable for the
    # same reason the baseline is: the real values are minutes long.
    blocked_retry_min: float = BLOCKED_RETRY_MIN
    blocked_retry_base: float = BLOCKED_RETRY_BASE
    blocked_retry_max: float = BLOCKED_RETRY_MAX
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
        # Exactly one coroutine may read a StreamReader, so a single reader task owns
        # it and hands finished responses to whoever is waiting. Without this the
        # refresh loop and the incoming-request loop race on the socket and asyncio
        # raises -- which only happens once a registration is being held, i.e. not in
        # any short-lived test.
        self._reader_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._responses: asyncio.Queue[SipMessage] = asyncio.Queue()
        # Serialises request/response exchanges so one reply cannot be taken by the
        # wrong sender.
        self._request_lock = asyncio.Lock()
        self._transactions = TransactionTable()
        self._branch_deadlines: dict[tuple[str, str, str], asyncio.Task[None]] = {}
        self._challenges: dict[str, DigestChallenge] = {}
        self._seen_nonces: set[str] = set()
        self._failures = 0
        self._blocks = 0
        self._stopping = False
        self._current = SipState.DISABLED
        # What the registrar calls our binding, in its own words. Persisted by the
        # bridge, because what we think our Contact is and what the registrar reports
        # back are not always the same string.
        self._own_bindings: tuple[str, ...] = ()
        self._last_foreign: tuple[Binding, ...] = ()

    @property
    def state(self) -> SipState:
        """Current state."""
        return self._current

    @property
    def contact_uri(self) -> str | None:
        """The Contact URI our binding is registered at, once there is one.

        Persisted by the bridge. Without it a restart cannot recognise its own
        binding: the source port changes, and this registrar does not echo
        ``+sip.instance``, so the only handle left is the URI itself.
        """
        return self._state.contact_uri

    @property
    def own_binding_uris(self) -> tuple[str, ...]:
        """Every binding the registrar reports that we recognised as ours.

        Taken from the registrar's own answer rather than from what we sent. A
        registrar may hold a Contact in a form of its choosing -- rewritten to the
        address it sees, carrying parameters of its own -- and a restart comparing
        its remembered string against that form has to be comparing the same thing,
        or it reads its own binding as another device's and locks itself out.
        """
        return self._own_bindings

    # ------------------------------------------------------------- supervisor

    async def async_run(self) -> None:
        """Run until cancelled, or until something terminal happens."""
        try:
            while not self._stopping:
                try:
                    await self._one_flow()
                except asyncio.CancelledError:
                    raise
                except SipBlockedError as err:
                    # No longer the end of the road. Every block seen on the live
                    # account was our own binding from before a restart, still there
                    # until it lapsed -- and waiting that out costs nothing, while
                    # waiting for somebody to notice a repair card costs the doorbell.
                    # The gate itself is unchanged: an account genuinely in use is
                    # still never registered on.
                    if not self._blocks:
                        self._events.on_terminal(
                            SipState.BLOCKED, type(err).__name__, str(err)
                        )
                    delay = self._blocked_delay()
                    self._blocks += 1
                    self._set_state(
                        SipState.BLOCKED, f"{err}; перепроверю через {delay:.0f} с"
                    )
                    if self._stopping:
                        return
                    await asyncio.sleep(delay)
                    continue
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
        """Ask the client to stop, handing the binding back on the way out."""
        await self._withdraw_on_stop()
        self._stopping = True
        await self._close()

    async def _withdraw_on_stop(self) -> None:
        """Give the binding back before letting go of the connection.

        Without this every restart leaves a live binding behind, and the next process
        reads it as another device and refuses to register on its own account.
        Measured on the live registrar: two consecutive Home Assistant restarts, and
        both times the doorbell stayed dead for the whole five minutes the leftover
        took to lapse.

        Best effort and strictly bounded. Failing here leaves exactly the situation
        that existed before the attempt, which the rest of the design already handles.
        """
        if self._stopping or self._current is not SipState.REGISTERED:
            return
        if not self._state.contact_uri:
            return
        try:
            async with asyncio.timeout(STOP_WITHDRAW_TIMEOUT):
                await self._withdraw_own_contact()
        except (SipError, OSError, TimeoutError) as err:
            _LOGGER.debug("Привязку при остановке снять не удалось: %s", err)

    def _blocked_delay(self) -> float:
        """When to look at a busy account again.

        The binding we just refused to touch usually says when it lapses, and that is
        the answer: one expiry from now it is gone. Without an expiry to go on, a
        doubling curve.
        """
        config = self._config
        if (left := self._blocking_expiry()) is not None:
            return min(
                config.blocked_retry_max, max(config.blocked_retry_min, left + 5.0)
            )
        return min(
            config.blocked_retry_max,
            config.blocked_retry_base * 2 ** min(self._blocks, 4),
        )

    def _blocking_expiry(self) -> float | None:
        """The longest expiry among the bindings that blocked us, if any said."""
        left = [
            binding.expires
            for binding in self._last_foreign
            if binding.expires is not None and binding.expires > 0
        ]
        return float(max(left)) if left else None

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
        expires = self._config.first_expires if first_time else self._config.expires
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
            # The REGISTER above already created a binding for the address we guessed,
            # which the registrar cannot reach. Withdraw it in the same message that
            # creates the corrected one: leaving it behind fills the account's table
            # with our own dead entries, and squeezing the resident's phone out of that
            # table is precisely the harm this design exists to prevent.
            superseded = self._state.contact_uri
            self._state.set_contact(rewritten)
            # Anything equal to the corrected Contact is dropped from the withdrawal
            # list: a NAT that hands back a port we held before makes the binding we
            # are reclaiming and the binding we are creating one and the same, and
            # registering and withdrawing one URI in a single message is refused
            # outright -- which left the client retrying for ever.
            reap_next = [
                uri
                for uri in ([*reap, superseded] if superseded else reap)
                if uri and not uri_equal(uri, rewritten)
            ]
            response = await self._register_once(expires=expires, reap=reap_next)

        self._resolve_granted(response, requested=expires)

        self._set_state(SipState.VERIFYING, None)
        after, verify_response = await self._probe()
        self._publish(SipState.VERIFYING, after, verify_response)
        # In the registrar's words, not ours -- see `own_binding_uris`.
        self._own_bindings = tuple(
            binding.uri for binding in after if self._is_ours(binding)
        )
        self._assert_bindings_visible(after)
        await self._assert_no_eviction(before, after, time.monotonic() - started)

    async def _register_once(self, *, expires: int, reap: Sequence[str]) -> SipMessage:
        """Send one REGISTER carrying our live Contact."""
        contacts = self._state.build_contacts(live=self._state.contact_uri, reap=reap)
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
        """Hold the registration: keep the connection warm and renew it in time.

        The reader task is already running and owns the socket; these two only send.
        Any one of the three failing tears the flow down, because a registration whose
        refresh task has died is one that will lapse without anybody noticing.
        """
        try:
            async with asyncio.TaskGroup() as group:
                group.create_task(self._refresh_loop())
                group.create_task(self._await_reader())
        except* (SipError, OSError, TimeoutError) as group_error:
            raise group_error.exceptions[0] from None

    async def _await_reader(self) -> None:
        """Fail the whole flow if the reader stops."""
        if self._reader_task is None:
            raise SipTransportError("reader is not running")
        await self._reader_task
        raise SipTransportError("соединение с регистратором закрыто")

    async def _keepalive_loop(self) -> None:
        """Ping now and then so the connection is not silently dropped.

        RFC 5626 §4.4.1. Runs for the whole life of the connection, not just while a
        registration is held: the baseline phase leaves the socket idle for minutes at
        a time, and a registrar that has no binding for us has every reason to reclaim
        it. Measured against the live registrar -- without this the third baseline
        probe went unanswered and the flow restarted, so the baseline never finished
        and the client could never register at all.
        """
        try:
            while not self._stopping:
                delay = random.uniform(0.8 * KEEPALIVE, KEEPALIVE)  # noqa: S311
                await asyncio.sleep(delay)
                await self._send(PING)
        except (SipError, OSError):
            # The reader notices the same failure and is the one that reports it.
            return

    async def _sleep_watching_reader(self, delay: float) -> None:
        """Sleep, but give up at once if the connection dies underneath us.

        Only ``_serve`` used to watch the reader, so a connection lost during the
        baseline went unnoticed until the next probe timed out thirty seconds later --
        reported as an unexplained timeout rather than as the dropped connection it
        was.
        """
        if self._reader_task is None:
            raise SipTransportError("reader is not running")
        done, _pending = await asyncio.wait({self._reader_task}, timeout=delay)
        if done:
            raise SipTransportError("соединение с регистратором закрыто")

    async def _refresh_loop(self) -> None:
        """Renew the registration before the granted expiry runs out."""
        while not self._stopping:
            await asyncio.sleep(self._state.refresh_delay())
            self._state.forget_stale_priors(self._state.granted_expires or 300)
            response = await self._register_once(expires=self._config.expires, reap=[])
            self._resolve_granted(response, requested=self._config.expires)
            # Republished, not merely logged. The bridge persists what a
            # restart needs off this transition, and a registration held for
            # hours would otherwise have been recorded once, at the start, and
            # then aged out of its own freshness window.
            self._set_state(
                SipState.REGISTERED,
                f"срок действия {self._state.granted_expires} с",
            )
            _LOGGER.debug(
                "Registration refreshed, granted %ss", self._state.granted_expires
            )

    async def _idle(self) -> None:
        """Hold the connection open in probe-only mode, doing nothing else."""
        await self._await_reader()

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
        raise SipEvictionError(f"регистрация вытеснила чужих привязок: {len(vanished)}")

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
        # Kept even when the account is clean: this is what tells the retry when the
        # account is likely to be free again.
        self._last_foreign = tuple(bindings)
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
            await self._sleep_watching_reader(self._config.baseline_interval)
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
                header.value for header in response.headers if header.name == "contact"
            )
        ), response

    async def _register_request(
        self, *, contacts: Sequence[str], expires: int | None
    ) -> SipMessage:
        """Send a REGISTER, answering at most one authentication challenge.

        Serialised: responses are matched to requests by arrival order, so two
        exchanges in flight at once would hand a reply to the wrong sender. In
        practice the refresh loop and a manual withdrawal are the two that could
        overlap.
        """
        async with self._request_lock:
            return await self._register_exchange(contacts=contacts, expires=expires)

    async def _register_exchange(
        self, *, contacts: Sequence[str], expires: int | None
    ) -> SipMessage:
        """One REGISTER exchange, including the challenge round trip."""
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
        rows = [header.value for header in response.headers if header.name == name]
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
        while not self._responses.empty():
            self._responses.get_nowait()
        self._reader_task = asyncio.create_task(self._reader_main())
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    async def _reader_main(self) -> None:
        """The only coroutine that reads the socket.

        Answers keepalives, declines anything sent to us, and queues final responses
        for whoever issued the request. Everything else on this connection only writes.
        """
        assert self._transport is not None
        transport = self._transport
        try:
            while True:
                try:
                    chunk = await transport.reader.read(MAX_HEADER_BYTES)
                except OSError:
                    return
                if not chunk:
                    return

                for item in transport.framer.feed(chunk):
                    if isinstance(item, Ping):
                        await self._send(CRLF)
                    elif isinstance(item, Pong):
                        continue
                    elif item.is_response:
                        if (item.status or 0) >= 200:
                            await self._responses.put(item)
                        # A 1xx to a REGISTER is legal, just uninteresting.
                    else:
                        await self._on_request(item)
        except (SipError, OSError):
            return

    async def _on_request(self, request: SipMessage) -> None:
        """Answer a request the registrar sent us."""
        self._transactions.prune()
        if request.method == "INVITE":
            await self._on_invite(request)
        elif request.method == "CANCEL":
            await self._on_cancel(request)
        elif request.method in ("ACK",):
            return  # nothing to answer
        elif request.method == "OPTIONS":
            await self._send(
                MessageBuilder.response(request, 200, "OK", extra=[("Allow", ALLOW)])
            )
        else:
            await self._send(
                MessageBuilder.response(request, 405, "Method Not Allowed")
            )

    async def _on_invite(self, request: SipMessage) -> None:
        """Ring in Home Assistant, without ever taking the call.

        We answer 180 Ringing and stop there: sending a 2xx would take the call away
        from the resident's phone. But we must not sit on 180 forever either -- while
        our branch is unresolved a forking proxy will not forward the other branches'
        final responses, so the decline button on their phone stops working. Hence the
        deadline.
        """
        key = transaction_key(request)

        if (existing := self._transactions.get(key)) is not None:
            # A retransmission. Once a final has been sent it must be repeated: a
            # provisional here would put the branch back into Proceeding and reset the
            # proxy's Timer C.
            if existing.final_sent and existing.last_final:
                await self._send(existing.last_final)
            else:
                await self._send(
                    MessageBuilder.response(
                        request, 180, "Ringing", to_tag=existing.to_tag
                    )
                )
            return

        from_header = request.first("from")
        remote_uri = name_addr(from_header.text()) if from_header else ""
        transaction = self._transactions.add(
            key,
            InviteTransaction(
                call_id=request.call_id, remote_uri=remote_uri, request=request
            ),
        )

        await self._send(MessageBuilder.response(request, 100, "Trying"))
        await self._send(
            MessageBuilder.response(request, 180, "Ringing", to_tag=transaction.to_tag)
        )

        delivered = False
        try:
            delivered = await self._events.on_incoming(transaction.call_id, remote_uri)
        except Exception:
            _LOGGER.exception("Failed to announce the incoming call")

        if not delivered:
            # Nothing in Home Assistant is going to answer, so release the branch now
            # rather than let it time out.
            await self._decline(transaction, "undelivered")
            return

        self._branch_deadlines[key] = asyncio.get_running_loop().create_task(
            self._branch_deadline(transaction, key)
        )

    async def _branch_deadline(
        self,
        transaction: InviteTransaction,
        key: tuple[str, str, str],
    ) -> None:
        """Release the branch before the proxy's own timer would have to."""
        try:
            await asyncio.sleep(BRANCH_DEADLINE)
            if not transaction.final_sent:
                await self._decline(transaction, "timeout")
        except asyncio.CancelledError:
            raise
        except (SipError, OSError):
            return
        finally:
            self._branch_deadlines.pop(key, None)

    async def _on_cancel(self, request: SipMessage) -> None:
        """The caller gave up. Acknowledge, then close the INVITE it names."""
        # A CANCEL shares the INVITE's branch, so the INVITE's key differs only in the
        # method. The 200 carries the CANCEL's own CSeq, echoed verbatim.
        await self._send(MessageBuilder.response(request, 200, "OK"))

        branch, sent_by, _method = transaction_key(request)
        transaction = self._transactions.get((branch, sent_by, "INVITE"))
        if transaction is None or transaction.final_sent:
            return
        transaction.cancelled = True
        # Built from the INVITE, never from the CANCEL: RFC 3261 §9.2 requires
        # the 487 to carry the INVITE's CSeq, and echoing the CANCEL's would give
        # it "CSeq: n CANCEL" -- a response no proxy matches to the transaction it
        # is meant to end.
        await self._finalise(transaction, 487, "Request Terminated", reason="cancelled")

    async def _decline(self, transaction: InviteTransaction, reason: str) -> None:
        """Decline our branch only.

        486 and not 603: a 6xx is a global decline that makes the proxy cancel every
        other branch, the resident's phone included.
        """
        await self._finalise(transaction, 486, "Busy Here", reason=reason)

    async def _finalise(
        self,
        transaction: InviteTransaction,
        code: int,
        phrase: str,
        *,
        reason: str,
    ) -> None:
        """Send a final response once, built from this transaction's own INVITE.

        Taking the request from the transaction rather than from the caller is
        the whole point: every caller used to have to pass the right one, and two
        of them passed the wrong one -- the CANCEL, and whichever INVITE arrived
        last.
        """
        if transaction.final_sent:
            return
        message = MessageBuilder.response(
            transaction.request, code, phrase, to_tag=transaction.to_tag
        )
        transaction.final_sent = True
        transaction.last_final = message
        await self._send(message)
        _LOGGER.debug("Call %s ended (%s)", transaction.call_id, reason)
        self._events.on_call_end(transaction.call_id, reason)

    async def async_end_call(self, call_id: str, reason: str = "ended") -> bool:
        """End a ringing call from Home Assistant's side."""
        transaction = self._transactions.by_call_id(call_id)
        if transaction is None or transaction.final_sent:
            return False
        await self._decline(transaction, reason)
        return True

    async def _send(self, data: bytes) -> None:
        """Write to the socket."""
        if self._transport is None:
            raise SipTransportError("not connected")
        try:
            self._transport.writer.write(data)
            await self._transport.writer.drain()
        except OSError as err:
            raise SipTransportError(f"обрыв записи: {err}") from err

    async def _final_response(self) -> SipMessage:
        """Wait for the reader task to hand over a final response."""
        try:
            async with asyncio.timeout(TIMER_F):
                return await self._responses.get()
        except TimeoutError as err:
            raise SipTransportError("регистратор не ответил вовремя") from err

    async def _close(self) -> None:
        """Close the socket and stop the reader, tolerating an already-gone one."""
        for deadline in list(self._branch_deadlines.values()):
            deadline.cancel()
        self._branch_deadlines.clear()

        keepalive, self._keepalive_task = self._keepalive_task, None
        reader, self._reader_task = self._reader_task, None
        for task in (keepalive, reader):
            if task is None:
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        transport, self._transport = self._transport, None
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
                foreign=tuple(self._foreign(bindings)),
                realm=challenge.realm if challenge else None,
                algorithm=challenge.algorithm if challenge else None,
                local=self._state.sent_by,
                received=params.get("received"),
                rport=params.get("rport"),
            )
        )
