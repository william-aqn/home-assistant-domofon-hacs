"""The join between the SIP client and Home Assistant.

Everything Home-Assistant-shaped lives here so that ``sip/`` stays a plain asyncio
library with no framework imports -- which is what lets the whole protocol be tested
against a fake registrar without a Home Assistant instance anywhere in sight.

The bridge owns three things:

* the client's **lifecycle**, as a background task that the switch can start and stop
  without reloading the entry (the one-tap rollback the risk register asks for);
* the **translation of a ring into a door**, which is a backend lookup and never a
  guess, because the answer decides which door a notification will open;
* the **state a person sees**, both the diagnostic sensor and the repair card raised
  when the client stops for good.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)

from .api import LokiApiError, LokiAuthError, LokiClient
from .call import CallManager, CallUpdate, signal_call_update
from .const import (
    CONF_SIP,
    CONF_SIP_PASSWORD,
    CONF_SIP_URL,
    CONF_SIP_USER,
    DOMAIN,
    EVENT_LOKI,
    EVENT_TYPE_SIP_TERMINAL,
    OPT_SIP_STRICT_GUARD,
)
from .coordinator import LokiConfigEntry
from .models import LokiDevice
from .repairs import async_clear_sip_terminal, async_create_sip_terminal
from .sip.client import LokiSipClient, SipConfig, SipSnapshot, SipState
from .sip.registration import RegistrationState
from .sip_store import SipStore

_LOGGER = logging.getLogger(__name__)

# How long the door lookup may take before we give up and decline the branch. Short
# for two reasons: the caller is standing at a door listening to ringback, and this
# runs inline on the SIP client's single reader -- so a slow lookup also delays the
# CANCEL that arrives when the caller gives up. The 180 Ringing has already gone out
# by this point, so the caller hears ringback throughout.
RESOLVE_TIMEOUT = 8.0

# Which terminal states survive a restart. Eviction and an unverifiable registrar are
# decisions about the *account*, and repeating the manoeuvre that produced them would
# repeat the harm. Being blocked is not: it only means somebody else was registered at
# the time, and looking again changes nothing -- so a restart is allowed to re-check,
# and the doorbell comes back on its own once the account is free.
LATCHED_ACROSS_RESTARTS: frozenset[SipState] = frozenset(
    {SipState.EVICTED, SipState.FAILED}
)


def _latched(stored: str) -> SipState | None:
    """The terminal state a stored value names, if it is one we honour on restart.

    Anything else -- an unknown value from a hand-edited file, or a state that stopped
    being latched when the rules changed -- is ignored rather than trusted, because
    the cost of ignoring it is one more probe and the cost of honouring it wrongly is
    a doorbell that never comes back.
    """
    try:
        state = SipState(stored)
    except ValueError:
        return None
    return state if state in LATCHED_ACROSS_RESTARTS else None


def signal_sip_update(entry_id: str) -> str:
    """Dispatcher signal fired when the SIP client's state changes."""
    return f"{DOMAIN}_sip_update_{entry_id}"


@callback
def sip_credentials(entry: LokiConfigEntry) -> tuple[str, str, str] | None:
    """The host, user and password for this account, or None if it has none.

    Issued only by the SMS login -- a token refresh does not return them -- so an
    entry created before this data was stored simply has no SIP, and says so rather
    than failing to load.
    """
    raw = entry.data.get(CONF_SIP)
    if not isinstance(raw, Mapping):
        return None
    host = str(raw.get(CONF_SIP_URL) or "").strip()
    user = str(raw.get(CONF_SIP_USER) or "").strip()
    password = str(raw.get(CONF_SIP_PASSWORD) or "")
    if not host or not user or not password:
        return None
    return host, user, password


class SipBridge:
    """Runs the SIP client for one config entry and reports what it does."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: LokiConfigEntry,
        client: LokiClient,
        call_manager: CallManager,
        store: SipStore,
    ) -> None:
        """Initialise the bridge. Nothing starts until async_start."""
        self.hass = hass
        self.entry = entry
        self._api = client
        self._calls = call_manager
        self._store = store

        self.state: SipState = SipState.DISABLED
        self.detail: str | None = None
        self.snapshot: SipSnapshot | None = None

        self._client: LokiSipClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._unsub_calls: Any = None
        # call_id -> Loki device id, for the calls we announced.
        self._active: dict[str, int] = {}

    # ------------------------------------------------------------- lifecycle

    @property
    def available(self) -> bool:
        """Whether this account has SIP credentials at all."""
        return sip_credentials(self.entry) is not None

    @property
    def running(self) -> bool:
        """Whether the client task is alive."""
        return self._task is not None and not self._task.done()

    async def async_start(self) -> None:
        """Start the SIP client, unless it is already running or cannot run."""
        if self.running:
            return

        credentials = sip_credentials(self.entry)
        if credentials is None:
            self._publish(SipState.DISABLED, "учётные данные SIP не выданы")
            return

        stored = self._store.state
        if stored.terminal is not None:
            if (latched := _latched(stored.terminal)) is not None:
                # Honouring the latch is the whole point of persisting it: a restart
                # must not quietly retry a manoeuvre we already decided was unsafe.
                # Clearing it is a deliberate act -- switch SIP off and on again.
                self._publish(latched, stored.terminal_detail)
                return
            # A latch we no longer honour. Dropped rather than left behind, so the
            # stored state does not go on contradicting what the sensor says.
            self.entry.async_create_task(
                self.hass, self.async_reset_terminal(), f"{DOMAIN}_sip_unlatch"
            )

        host, user, password = credentials
        config = SipConfig(
            host=host,
            user=user,
            password=password,
            register=True,
            strict_guard=bool(self.entry.options.get(OPT_SIP_STRICT_GUARD, True)),
            # Ten quiet minutes before the very first registration is a real cost, and
            # it buys the evidence that the account is genuinely unused. Paid once.
            require_baseline=not stored.first_registration_done,
            first_registration_done=stored.first_registration_done,
        )
        registration = RegistrationState(
            host=host, user=user, instance_id=stored.instance_id
        )
        # Hand back the Contact URIs a previous process registered with, so its
        # leftover binding is recognised as ours and withdrawn rather than mistaken
        # for another device. Without this a restart blocks itself out of its own
        # account for as long as the old binding takes to expire.
        registration.adopt_prior_contacts(stored.fresh_contacts())
        self._client = LokiSipClient(config, self, state=registration)
        # Reaching this line means we are about to try again; a card from a previous
        # attempt would otherwise sit there contradicting the sensor.
        async_clear_sip_terminal(self.hass, self.entry.entry_id)
        # Subscribed only while the client runs: this is the path by which a hangup or
        # an opened door releases our SIP branch. Released first because a client
        # that ended on its own leaves `running` False with the previous
        # subscription still live, and a second one would decline every branch twice.
        self._unsubscribe_calls()
        self._unsub_calls = async_dispatcher_connect(
            self.hass, signal_call_update(self.entry.entry_id), self._on_call_update
        )
        self._task = self.entry.async_create_background_task(
            self.hass, self._run(), f"{DOMAIN}_sip_{self.entry.entry_id}"
        )

    async def async_stop(self) -> None:
        """Stop the client and release everything it holds."""
        self._unsubscribe_calls()

        client, self._client = self._client, None
        task, self._task = self._task, None

        try:
            if client is not None:
                await client.async_stop()
            if task is not None:
                task.cancel()
                # gather(return_exceptions=True) and not contextlib.suppress:
                # _run re-raises CancelledError, which derives from
                # BaseException, so suppress(Exception) would let it escape and
                # abandon everything below -- including the platform unload of
                # the caller. gather absorbs the task's cancellation while still
                # propagating our own if somebody cancels us.
                await asyncio.gather(task, return_exceptions=True)
        finally:
            # A call we announced is over as far as Home Assistant is concerned:
            # the socket carrying it is gone. Leaving it would pin the door's
            # sensor on until CALL_TIMEOUT with nothing behind it.
            for call_id, device_id in list(self._active.items()):
                self._calls.async_end_call(
                    device_id, reason="sip_stopped", call_id=call_id
                )
            self._active.clear()
            if self.state is not SipState.DISABLED and not self._store.state.terminal:
                self._publish(SipState.DISABLED, None)

    @callback
    def _unsubscribe_calls(self) -> None:
        """Drop the dispatcher subscription, if we hold one."""
        if self._unsub_calls is not None:
            self._unsub_calls()
            self._unsub_calls = None

    async def _run(self) -> None:
        """Run the client, surviving anything it throws."""
        assert self._client is not None
        try:
            await self._client.async_run()
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Клиент SIP остановился с ошибкой")
            self._publish(SipState.FAILED, "внутренняя ошибка, см. журнал")

    # ---------------------------------------------------------- SipEvents in

    @callback
    def on_state(self, state: SipState, detail: str | None) -> None:
        """The client's state machine moved."""
        self._publish(state, detail)
        if state is SipState.REGISTERED:
            self.entry.async_create_task(
                self.hass, self._async_record_registration(), f"{DOMAIN}_sip_registered"
            )

    async def _async_record_registration(self) -> None:
        """Persist what a restart will need to recognise this registration.

        Runs on every renewal, not only on the first REGISTERED, because the
        stored contact carries a timestamp and a stale one is discarded. With a
        single write at start-up, any restart after CONTACT_TTL of uptime threw
        the URI away and the client blocked itself out of its own account --
        which is every real restart, and no test session short enough to notice.

        The contact is written before the baseline flag: the reverse order can
        persist "baseline already paid" without the URI that makes the next
        start safe, if the task is cancelled between the two.
        """
        if self._client is not None and (uri := self._client.contact_uri):
            await self._store.async_record_contact(uri)
        await self._store.async_mark_registered()

    @callback
    def on_snapshot(self, snapshot: SipSnapshot) -> None:
        """A fresh look at the account's bindings."""
        self.snapshot = snapshot
        async_dispatcher_send(self.hass, signal_sip_update(self.entry.entry_id))

    @callback
    def on_terminal(self, state: SipState, kind: str, detail: str) -> None:
        """The client stopped for good; a person has to decide what happens next."""
        _LOGGER.error("SIP остановлен окончательно (%s): %s", kind, detail)
        self._publish(state, detail)
        if state in LATCHED_ACROSS_RESTARTS:
            self.entry.async_create_task(
                self.hass,
                self._store.async_latch_terminal(str(state), detail),
                f"{DOMAIN}_sip_latch",
            )
        async_create_sip_terminal(
            self.hass, self.entry.entry_id, self.entry.title, str(state), detail
        )
        self.hass.bus.async_fire(
            EVENT_LOKI,
            {
                "type": EVENT_TYPE_SIP_TERMINAL,
                "entry_id": self.entry.entry_id,
                "state": str(state),
                "kind": kind,
                "detail": detail,
            },
        )

    async def on_incoming(self, call_id: str, remote_uri: str) -> bool:
        """Announce a ring. False releases the SIP branch immediately."""
        device = await self._async_resolve(remote_uri)
        if device is None:
            _LOGGER.warning(
                "Входящий вызов, но дверь определить не удалось; вызов отклонён"
            )
            return False

        call = self._calls.async_start_call(device, sip_uri=remote_uri, call_id=call_id)
        if call is None:
            # The entry is unloading. Declining is the honest answer: nothing here is
            # going to answer the call.
            return False

        self._active[call_id] = device.id
        return True

    @callback
    def on_call_end(self, call_id: str, reason: str) -> None:
        """Our SIP branch finished, so clear the matching Home Assistant call."""
        device_id = self._active.pop(call_id, None)
        if device_id is None:
            return
        self._calls.async_end_call(device_id, reason=reason, call_id=call_id)

    # --------------------------------------------------------- HA -> SIP out

    @callback
    def _on_call_update(self, update: CallUpdate) -> None:
        """Release our SIP branch when the call ends on the Home Assistant side.

        Without this, pressing "hangup" -- or the blueprint opening the door -- would
        clear the sensor while our branch stayed on 180 Ringing, and a forking proxy
        holds back the other branches' final responses for as long as that lasts.
        """
        if update.kind != "end" or self._client is None:
            return
        if self._active.pop(update.call_id, None) is None:
            return
        self.entry.async_create_task(
            self.hass,
            self._async_decline(update.call_id, update.reason or "ended"),
            f"{DOMAIN}_sip_decline",
        )

    async def _async_decline(self, call_id: str, reason: str) -> None:
        """Send our own 486 for a call Home Assistant has finished with."""
        client = self._client
        if client is None:
            return
        try:
            await client.async_end_call(call_id, reason)
        except Exception:
            _LOGGER.exception("Не удалось завершить SIP-ветку")

    # ------------------------------------------------------------- resolving

    async def _async_resolve(self, remote_uri: str) -> LokiDevice | None:
        """Which door is calling.

        Answered by the backend, never guessed: matching a display name against the
        device list is how the wrong entrance gets opened, and the answer to this
        question is what a notification's "open" button acts on.

        Successful answers are remembered, so a ring during a brief API outage still
        reaches the right door instead of being declined.
        """
        devices = self.entry.runtime_data.coordinator.data

        try:
            async with asyncio.timeout(RESOLVE_TIMEOUT):
                device = await self._api.async_get_device_by_sip_name(remote_uri)
        except (LokiApiError, LokiAuthError, TimeoutError, ValueError) as err:
            _LOGGER.warning("Не удалось определить дверь по SIP URI: %s", err)
            device = None
        else:
            if device is not None:
                # Written in the background: this is the ring path, and the caller is
                # standing at the door waiting for it. A disk write is not allowed to
                # sit between the INVITE and the notification.
                self.entry.async_create_task(
                    self.hass,
                    self._store.async_remember(remote_uri, device.id),
                    f"{DOMAIN}_sip_remember",
                )
                # The coordinator's copy carries the stream URL and the area, and its
                # entities are the ones a notification will reference.
                return devices.get(device.id, device)

        remembered = self._store.state.resolved.get(remote_uri)
        if remembered is None:
            return None
        _LOGGER.debug("Дверь взята из запомненных резолвов")
        return devices.get(remembered)

    # ------------------------------------------------------------- reporting

    @callback
    def _publish(self, state: SipState, detail: str | None) -> None:
        """Record the state and wake the sensor."""
        self.state = state
        self.detail = detail
        async_dispatcher_send(self.hass, signal_sip_update(self.entry.entry_id))

    async def async_reset_terminal(self) -> None:
        """Forget a latched failure, on an explicit request from a person."""
        await self._store.async_clear_terminal()
        async_clear_sip_terminal(self.hass, self.entry.entry_id)
