"""Config flow for Loki: phone number, then SMS confirmation."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from .api import (
    LokiApiError,
    LokiAuthError,
    LokiClient,
    LokiInvalidCode,
    LokiPhoneNotRegistered,
)
from .const import (
    CONF_MASTER_FLG,
    CONF_MAX_PHONES,
    CONF_PHONE,
    CONF_REFRESH_TOKEN,
    CONF_SIP,
    DISCLAIMER_URL,
    DOMAIN,
    OPT_PANEL,
    OPT_SCAN_INTERVAL,
    OPT_SIP_STRICT_GUARD,
)
from .coordinator import LokiConfigEntry
from .protocol import normalize_phone
from .reauth import async_record_auth_time
from .repairs import async_create_reauth_unrecoverable
from .sip_store import SipStore

_LOGGER = logging.getLogger(__name__)

CONF_SMS_CODE = "sms_code"
CONF_RESEND = "resend"
CONF_ACCEPT = "accept"
CONF_PANEL = "panel"

STEP_DISCLAIMER_SCHEMA = vol.Schema({vol.Required(CONF_ACCEPT, default=False): bool})

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PHONE): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEL, autocomplete="tel")
        )
    }
)

STEP_SMS_SCHEMA = vol.Schema(
    {
        # Optional, not Required: ticking "resend" has to be submittable on its own,
        # and voluptuous validates the schema before the step handler ever runs.
        # TEXT rather than NUMBER -- a number input renders a spinner and applies
        # locale formatting, neither of which belongs on a one-time code.
        vol.Optional(CONF_SMS_CODE, default=""): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT, autocomplete="one-time-code")
        ),
        vol.Optional(CONF_RESEND, default=False): bool,
    }
)


class LokiConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle initial setup and reauthentication."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialise flow state."""
        self._phone: str | None = None
        self._provisional_token: str | None = None
        self._entry_data: dict[str, Any] | None = None

    # -- initial setup --------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the terms of use and require an explicit acceptance.

        Deliberately the first step: the number the user is about to enter can log
        their operator app out, and that has to be read before it happens, not after.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            if user_input.get(CONF_ACCEPT):
                return await self.async_step_phone()
            errors[CONF_ACCEPT] = "disclaimer_not_accepted"

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_DISCLAIMER_SCHEMA,
            errors=errors,
            description_placeholders={"disclaimer_url": DISCLAIMER_URL},
        )

    async def async_step_phone(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the phone number and send the SMS."""
        errors: dict[str, str] = {}

        if user_input is not None:
            phone = normalize_phone(user_input[CONF_PHONE])
            if phone is None:
                errors[CONF_PHONE] = "invalid_phone"
            else:
                await self.async_set_unique_id(phone)
                self._abort_if_unique_id_configured()

                error = await self._async_send_sms(phone)
                if error:
                    errors["base"] = error
                else:
                    self._phone = phone
                    return await self.async_step_sms()

        return self.async_show_form(
            step_id="phone", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_sms(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm the SMS code, or resend it."""
        if self._phone is None:
            return self.async_abort(reason="reauth_failed")
        errors: dict[str, str] = {}

        if user_input is not None:
            if user_input.get(CONF_RESEND):
                if error := await self._async_send_sms(self._phone):
                    errors["base"] = error
            elif not (code := str(user_input.get(CONF_SMS_CODE) or "").strip()):
                errors[CONF_SMS_CODE] = "invalid_code"
            else:
                result = await self._async_confirm(code)
                if result is None:
                    # Credentials are good; one question left before the entry exists.
                    return await self.async_step_panel()
                if isinstance(result, str):
                    errors["base"] = result
                else:
                    return result

        return self.async_show_form(
            step_id="sms",
            data_schema=STEP_SMS_SCHEMA,
            errors=errors,
            description_placeholders={"phone": self._phone},
        )

    # -- changing the phone number --------------------------------------------

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Point this account at a different phone number.

        The number is the account: the refresh token, the SIP credentials and the list
        of doors all hang off it. Reauthentication deliberately refuses to change it --
        it exists to renew a session, not to swap accounts -- so this is the way.

        Everything keyed on the Loki device id survives, because that is global: the
        device cards, their names and their areas stay as they are.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            phone = normalize_phone(user_input[CONF_PHONE])
            if phone is None:
                errors[CONF_PHONE] = "invalid_phone"
            else:
                await self.async_set_unique_id(phone)
                # A second entry already using that number would collide on unique_id.
                self._abort_if_unique_id_mismatch(reason="already_configured")
                if error := await self._async_send_sms(phone):
                    errors["base"] = error
                else:
                    self._phone = phone
                    return await self.async_step_sms()

        entry = self._get_reconfigure_entry()
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
            description_placeholders={"phone": str(entry.data.get(CONF_PHONE, ""))},
        )

    # -- reauth ---------------------------------------------------------------

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication.

        Reached when the refresh token expires (it lasts about 90 days and is never
        rotated by the backend) or when the session is invalidated elsewhere.
        """
        self._phone = normalize_phone(entry_data.get(CONF_PHONE))
        if self._phone is None:
            # Entry data predates phone storage or was hand-edited; there is nothing
            # to reauthenticate against. Core only raises its own repair card once a
            # flow shows a form, so an abort here would otherwise be invisible
            # outside the log -- raise our own.
            entry = self._get_reauth_entry()
            async_create_reauth_unrecoverable(self.hass, entry.entry_id, entry.title)
            return self.async_abort(reason="reauth_failed")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Resend the SMS for the known phone number and confirm it."""
        if self._phone is None:
            return self.async_abort(reason="reauth_failed")

        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=vol.Schema({}),
                description_placeholders={"phone": self._phone},
            )

        if error := await self._async_send_sms(self._phone):
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=vol.Schema({}),
                errors={"base": error},
                description_placeholders={"phone": self._phone},
            )

        return await self.async_step_sms()

    # -- helpers --------------------------------------------------------------

    async def _async_send_sms(self, phone: str) -> str | None:
        """Trigger an SMS. Returns an error key, or None on success.

        Any failure clears the provisional token: ``authorize`` may already have minted
        a new one -- invalidating the previous -- before ``showPin`` failed, so the one
        we are holding can no longer be trusted.
        """
        client = LokiClient(async_get_clientsession(self.hass))
        try:
            self._provisional_token = await client.request_sms(phone)
        except LokiPhoneNotRegistered:
            self._provisional_token = None
            return "phone_not_registered"
        except (LokiApiError, LokiAuthError):
            self._provisional_token = None
            return "cannot_connect"
        except Exception:
            self._provisional_token = None
            _LOGGER.exception("Unexpected error requesting the SMS code")
            return "unknown"
        return None

    async def _async_confirm(self, sms_code: str) -> ConfigFlowResult | str | None:
        """Confirm the code and create or update the entry.

        Returns an error key on failure rather than raising, so the caller can render
        it against the form.
        """
        if self._phone is None:
            return "unknown"
        if not self._provisional_token:
            return "expired_token"

        client = LokiClient(async_get_clientsession(self.hass))
        try:
            session = await client.confirm_sms(
                self._provisional_token, sms_code.strip()
            )
        except LokiInvalidCode:
            return "invalid_code"
        except (LokiApiError, LokiAuthError):
            return "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected error confirming the SMS code")
            return "unknown"

        if client.refresh_token is None:
            return "invalid_code"

        # This is the only place a refresh token is ever minted, so it is the only
        # place its age can be stamped.
        data = async_record_auth_time(
            {
                CONF_PHONE: self._phone,
                CONF_REFRESH_TOKEN: client.refresh_token,
                # The SIP credentials are issued only by this endpoint -- a token
                # refresh does not return them -- so they must be persisted now.
                CONF_SIP: session.get("sip"),
                CONF_MASTER_FLG: session.get("master_flg"),
                CONF_MAX_PHONES: session.get("max_phones"),
            }
        )

        self._entry_data = data
        if self.source == SOURCE_RECONFIGURE:
            entry = self._get_reconfigure_entry()
            # The SIP state belongs to the old account: its instance id, its remembered
            # Contact URIs, its resolved doors. Carrying them over would let the client
            # claim a binding on the NEW account that was never ours -- the one harm
            # the whole SIP design exists to prevent -- so it goes.
            await SipStore(self.hass, entry.entry_id).async_remove()
            await self.async_set_unique_id(self._phone)
            self._abort_if_unique_id_mismatch(reason="already_configured")
            return self.async_update_reload_and_abort(entry, data=data)

        if self.source == SOURCE_REAUTH:
            # Re-assert the account identity: the entry is keyed on the phone number,
            # and a reauth must not quietly rebind it to a different account.
            await self.async_set_unique_id(self._phone)
            self._abort_if_unique_id_mismatch()
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data=data
            )

        # The entry is created by async_step_panel, after the last question.
        return None

    async def async_step_panel(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer a ready-made page before finishing.

        Asked here rather than left for later because the alternative is a dashboard
        the user has to build: the default "Обзор" panel is auto-generated and cannot
        be edited at all until they take it over, which is not obvious and is the first
        thing people get stuck on.
        """
        if user_input is not None:
            return self.async_create_entry(
                title=self._phone or "",
                data=self._entry_data or {},
                options={OPT_PANEL: bool(user_input.get(CONF_PANEL, True))},
            )

        return self.async_show_form(
            step_id="panel",
            data_schema=vol.Schema({vol.Required(CONF_PANEL, default=True): bool}),
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: LokiConfigEntry) -> LokiOptionsFlow:
        """Return the options flow."""
        return LokiOptionsFlow()


class LokiOptionsFlow(OptionsFlowWithReload):
    """Post-setup settings. Reloads the entry automatically on change."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        options = self.config_entry.options
        if user_input is not None:
            # Merged rather than replaced: the SIP switch writes OPT_SIP_ENABLED
            # straight into the options, and this form does not offer that field --
            # so saving user_input alone would silently switch SIP off.
            return self.async_create_entry(data={**options, **user_input})

        schema = vol.Schema(
            {
                # NumberSelector yields a float, which would be stored as 300.0.
                vol.Optional(
                    OPT_SCAN_INTERVAL, default=options.get(OPT_SCAN_INTERVAL, 300)
                ): vol.All(
                    NumberSelector(
                        NumberSelectorConfig(
                            min=60, max=3600, step=30, mode=NumberSelectorMode.BOX
                        )
                    ),
                    vol.Coerce(int),
                ),
                vol.Optional(
                    OPT_SIP_STRICT_GUARD,
                    default=options.get(OPT_SIP_STRICT_GUARD, True),
                ): bool,
                vol.Optional(OPT_PANEL, default=options.get(OPT_PANEL, False)): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
