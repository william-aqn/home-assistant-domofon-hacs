"""The arrangement of the «Домофоны» page, kept where every device can see it.

The page starts as every camera in alphabetical order, which is right for nobody: an
account can carry twenty doors, and the two that matter are wherever the alphabet put
them. So the page can be rearranged -- reordered, and with the doors nobody watches
put away -- and the arrangement belongs to the installation rather than to the browser
that made it. A layout stored in one tablet's local storage is a layout the phone does
not have.

It lives in the config entry's options, which is the one place an integration already
owns that survives restarts and reaches every session. Written through a websocket
command rather than a service: this is the card talking to its own integration, not
something anybody would call from an automation, and the actions list is short enough
without it.

Nothing here validates that the entity ids still exist. A door removed from the
account leaves a stale id in the order, the page skips it, and the day it comes back
it appears exactly where it was left. Pruning would be tidier and would silently lose
that.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
import voluptuous as vol

from .const import DOMAIN, OPT_PANEL_HIDDEN, OPT_PANEL_ORDER, OPT_PANEL_TILE_SIZE

TILE_SIZES = ("compact", "medium", "large")


@callback
def _entry(hass: HomeAssistant, entry_id: str | None) -> ConfigEntry | None:
    """The entry a request means: the named one, or the only one there is."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if entry_id is not None:
        return next((entry for entry in entries if entry.entry_id == entry_id), None)
    # One account is the ordinary case, and making the page ask which one would be
    # asking a question it cannot answer.
    return entries[0] if len(entries) == 1 else None


@callback
def layout_of(entry: ConfigEntry) -> dict[str, Any]:
    """The stored arrangement, in the shape the page expects.

    ``tile_size`` is None until somebody has chosen one: the page picks its own
    default from the screen it is on -- small tiles on a phone, medium elsewhere --
    and a stored "medium" would override that without anyone having asked for it.

    ``entry_id`` names the entry the arrangement belongs to, so the page can send it
    back on save instead of relying on "the only one" still being the only one.
    """
    options = entry.options
    return {
        "entry_id": entry.entry_id,
        "order": list(options.get(OPT_PANEL_ORDER) or []),
        "hidden": list(options.get(OPT_PANEL_HIDDEN) or []),
        "tile_size": options.get(OPT_PANEL_TILE_SIZE) or None,
    }


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/panel_layout/get",
        vol.Optional("entry_id"): str,
    }
)
@callback
def ws_get(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Report the stored arrangement."""
    entry = _entry(hass, msg.get("entry_id"))
    if entry is None:
        # Not an error: a page opened before the integration finished loading, or on
        # an install with two accounts and no way to guess. The page falls back to
        # showing everything, which is the state it started in -- and with no
        # entry_id it knows there is nothing to edit, so it offers no pencil.
        connection.send_result(
            msg["id"],
            {"entry_id": None, "order": [], "hidden": [], "tile_size": None},
        )
        return
    connection.send_result(msg["id"], layout_of(entry))


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/panel_layout/set",
        vol.Optional("entry_id"): str,
        vol.Required("order"): [str],
        vol.Required("hidden"): [str],
        vol.Required("tile_size"): vol.In(TILE_SIZES),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_set(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Store a new arrangement.

    Admin only: it changes what everybody in the household sees, and a page is a
    poor place to discover that a guest account could rearrange it.
    """
    entry = _entry(hass, msg.get("entry_id"))
    if entry is None:
        connection.send_error(msg["id"], "not_found", "нет подходящей записи Loki")
        return

    # Merged, not replaced: the options carry the SIP switch and everything else the
    # settings form owns, and replacing the dict wholesale would quietly turn them off.
    hass.config_entries.async_update_entry(
        entry,
        options={
            **entry.options,
            OPT_PANEL_ORDER: list(msg["order"]),
            OPT_PANEL_HIDDEN: list(msg["hidden"]),
            OPT_PANEL_TILE_SIZE: msg["tile_size"],
        },
    )
    connection.send_result(msg["id"], layout_of(entry))


@callback
def async_register_layout_api(hass: HomeAssistant) -> None:
    """Register the two commands the page uses. Idempotent by registration order."""
    websocket_api.async_register_command(hass, ws_get)
    websocket_api.async_register_command(hass, ws_set)
