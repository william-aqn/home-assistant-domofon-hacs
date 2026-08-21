"""Repair flows for problems the user has to resolve by hand.

Home Assistant raises its own repair card whenever a reauth flow *starts*, and that
card opens the live flow when clicked -- so this module deliberately adds nothing for
the ordinary "sign in again" case; a second card would only double the badge.

What it covers is the gap: core creates its card only when the flow returns a form,
so a flow that aborts immediately produces no card at all. That happens when the
config entry has no phone number to re-authenticate against, and until now it was
visible only in the log.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import issue_registry as ir
import voluptuous as vol

from .const import DOMAIN, ISSUE_REAUTH_UNRECOVERABLE


class ReauthUnrecoverableFlow(RepairsFlow):
    """Guide the user through the only fix available: set the account up again."""

    def __init__(self, entry_id: str) -> None:
        """Initialise the flow."""
        self._entry_id = entry_id

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Start the flow."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Remove the unusable entry so the account can be added cleanly."""
        if user_input is None:
            return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}))

        # Returning anything other than an abort makes the repairs manager delete the
        # issue, and removing the entry takes any of core's own cards with it.
        if self.hass.config_entries.async_get_entry(self._entry_id):
            await self.hass.config_entries.async_remove(self._entry_id)
        return self.async_create_entry(data={})


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Build the repair flow for an issue id."""
    entry_id = str((data or {}).get("entry_id", ""))
    return ReauthUnrecoverableFlow(entry_id)


@callback
def async_create_reauth_unrecoverable(
    hass: HomeAssistant, entry_id: str, title: str
) -> None:
    """Raise a card for a reauth that cannot even be attempted."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"{ISSUE_REAUTH_UNRECOVERABLE}_{entry_id}",
        is_fixable=True,
        # Without this the issue's data -- and therefore the fix flow -- would not
        # survive a restart, which is exactly when the user is likely to see it.
        is_persistent=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_REAUTH_UNRECOVERABLE,
        translation_placeholders={"title": title},
        data={"entry_id": entry_id},
    )


@callback
def async_clear_reauth_unrecoverable(hass: HomeAssistant, entry_id: str) -> None:
    """Drop the card once the entry sets up again."""
    ir.async_delete_issue(
        hass, DOMAIN, f"{ISSUE_REAUTH_UNRECOVERABLE}_{entry_id}"
    )
