"""Making "you must sign in again" impossible to miss.

Home Assistant already raises a repair card when a reauth flow starts, so this module
does *not* duplicate it. It covers the two gaps that card leaves:

* the flow can abort before any card exists (an entry with no phone number to
  re-authenticate), leaving the failure visible only in the log;
* a card in the UI does not reach someone who is not at home, and an integration
  cannot push to a phone by itself -- only the user's own automation can, so it needs
  an event to trigger on.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import (
    CONF_AUTH_TIME,
    CONF_PHONE,
    DOMAIN,
    EVENT_LOKI,
    EVENT_TYPE_AUTH_FAILED,
)

if TYPE_CHECKING:
    from .api import LokiAuthError
    from .coordinator import LokiConfigEntry

_LOGGER = logging.getLogger(__name__)

# Entry ids already announced. The coordinator raises on *every* refresh once the
# token is dead, so without a latch a five-minute poll interval would fire this
# event 288 times a day -- and the user's automation would send 288 notifications.
_AUTH_LATCH = f"{DOMAIN}_auth_failed_latch"


@callback
def async_token_age_days(entry: LokiConfigEntry) -> float | None:
    """Days since this account last completed an SMS login, if known.

    Recorded as an observation only. It is tempting to use the age to tell "the token
    simply expired" apart from "someone signed in elsewhere with this number", but the
    token's real lifetime has never been measured -- guessing would mean accusing the
    user of a session takeover on every routine expiry.
    """
    raw = entry.data.get(CONF_AUTH_TIME)
    if not raw:
        return None
    parsed = dt_util.parse_datetime(str(raw))
    if parsed is None:
        return None
    delta: timedelta = dt_util.utcnow() - parsed
    return round(delta.total_seconds() / 86400, 1)


@callback
def async_fire_auth_failed(
    hass: HomeAssistant, entry: LokiConfigEntry, err: LokiAuthError
) -> None:
    """Announce, once, that this account needs a new SMS login.

    This is the only channel that can reach someone away from home: the repair card
    requires them to be looking at Home Assistant, and this integration cannot send a
    push itself -- notification services belong to devices, not to integrations. The
    shipped blueprint turns this event into a notification.
    """
    latch: set[str] = hass.data.setdefault(_AUTH_LATCH, set())
    if entry.entry_id in latch:
        return
    latch.add(entry.entry_id)

    age = async_token_age_days(entry)
    _LOGGER.warning(
        "Account %s needs a new SMS login (token age: %s days, status: %s). "
        "Open Settings -> Devices & services to sign in again",
        entry.title,
        age if age is not None else "unknown",
        err.status,
    )

    hass.bus.async_fire(
        EVENT_LOKI,
        {
            "type": EVENT_TYPE_AUTH_FAILED,
            "entry_id": entry.entry_id,
            "title": entry.title,
            "phone": entry.data.get(CONF_PHONE),
            "age_days": age,
            "status": err.status,
            "detail": err.server_message or str(err),
        },
    )


@callback
def async_clear_auth_failed(hass: HomeAssistant, entry: LokiConfigEntry) -> None:
    """Re-arm the announcement after a successful sign-in.

    Deliberately not cleared on unload: while the account stays broken, a restart
    should announce it again rather than quietly silencing the alarm.
    """
    latch: set[str] | None = hass.data.get(_AUTH_LATCH)
    if latch is not None:
        latch.discard(entry.entry_id)


@callback
def async_record_auth_time(data: dict) -> dict:
    """Stamp a config entry payload with the moment its refresh token was issued.

    Called from the only place a refresh token is ever minted: a completed SMS login.
    """
    return {**data, CONF_AUTH_TIME: datetime.isoformat(dt_util.utcnow())}
