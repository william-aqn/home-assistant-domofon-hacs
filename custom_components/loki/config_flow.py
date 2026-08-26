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
from .models import sip_identity
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
        """Sign in by SMS again -- with the same number, or with a different one.

        Both uses are real and both end here. The same number is a plain re-login: it
        renews the session and re-reads the SIP credentials, which is precisely what a
        registrar that refused those credentials asks for, and what the repair card
        for that sends people to do. A different number moves this entry to another
        account -- and this is the only way to do that, because entity unique ids carry
        the entry id, so deleting the entry and adding it again renames every entity in
        the house.

        Neither of Home Assistant's two guards fits on its own, and taking one on faith
        breaks the other use. Measured against 2026.8.2:

        * ``_abort_if_unique_id_mismatch`` aborts the moment the number differs from
          this entry's own -- which is the very thing this step exists to allow. It was
          the call here, so the step refused every new number while its own text asked
          for one;
        * ``_abort_if_unique_id_configured`` aborts when *any* entry holds that number,
          this one included, so it would refuse the re-login instead.

        Hence the comparison in ``_abort_if_number_belongs_elsewhere``: the collision
        check is only ever asked about a number this entry does not already own.

        Everything keyed on the Loki device id survives either way, because that is
        global: the device cards, their names and their areas stay as they are.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            phone = normalize_phone(user_input[CONF_PHONE])
            if phone is None:
                errors[CONF_PHONE] = "invalid_phone"
            else:
                await self.async_set_unique_id(phone)
                # Checked before the SMS is sent, not after: the code costs the user a
                # message and twenty minutes of validity, and a flow that was going to
                # abort anyway should not spend either.
                self._abort_if_number_belongs_elsewhere()
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
            # Re-asserted here as well as in the step, because the two are minutes
            # apart -- an SMS is typed in between -- and another flow could have taken
            # the number in the meantime. Asserted before anything is destroyed: an
            # abort after the SIP state was already deleted would leave the entry as it
            # was except for the one part of it that cannot be rebuilt without ten
            # minutes of silence.
            await self.async_set_unique_id(self._phone)
            self._abort_if_number_belongs_elsewhere()
            await self._async_drop_sip_state_if_moved(entry, data)
            # The unique id and the title are the number, so both follow it. Without
            # this the entry would answer to the old number for every later flow --
            # a reauth would refuse itself as a mismatch, and a second entry could be
            # added for the number this one had just moved to.
            return self.async_update_reload_and_abort(
                entry, unique_id=self._phone, title=self._phone, data=data
            )

        if self.source == SOURCE_REAUTH:
            entry = self._get_reauth_entry()
            # Re-assert the account identity: the entry is keyed on the phone number,
            # and a reauth must not quietly rebind it to a different account.
            await self.async_set_unique_id(self._phone)
            self._abort_if_unique_id_mismatch()
            await self._async_drop_sip_state_if_moved(entry, data)
            return self.async_update_reload_and_abort(entry, data=data)

        # The entry is created by async_step_panel, after the last question.
        return None

    @callback
    def _abort_if_number_belongs_elsewhere(self) -> None:
        """Refuse a number another entry already holds; allow this entry's own.

        The service allows one session per number, so two entries on one number would
        take turns logging each other out. This entry keeping the number it already
        has is not that case -- it is a re-login, and the commonest reason to be here.
        """
        if self.unique_id != self._get_reconfigure_entry().unique_id:
            self._abort_if_unique_id_configured()

    async def _async_drop_sip_state_if_moved(
        self, entry: LokiConfigEntry, data: Mapping[str, Any]
    ) -> None:
        """Forget the stored SIP state if this sign-in moved us to another identity.

        That state belongs to an address-of-record, not to a config entry: the
        instance id the registrar knows this device by, the Contact URIs a restart
        uses to recognise its own leftover binding, the doors resolved on that
        account, and the record that the first registration has already been paid for.

        Both directions cost the doorbell, so the decision is made on the evidence
        rather than on which flow we happen to be in:

        * carried onto a *different* address-of-record, the remembered Contacts could
          have the client claim a binding that was never ours, and
          ``first_registration_done`` would skip the ten-minute baseline on an account
          it has never watched -- which may be the one the resident's phone sits on;
        * dropped on the *same* one, the next start pays that baseline again for
          nothing. Ten more minutes without a doorbell, immediately after a re-login
          whose whole purpose was to bring the doorbell back -- and a re-login is
          exactly what a registrar that refused our credentials asks for.

        A sign-in that returns no SIP block leaves the state alone: there is no new
        identity to compare against, and forgetting the old one buys nothing.
        """
        new = sip_identity(data)
        if new is None or new == sip_identity(entry.data):
            return
        _LOGGER.debug("SIP identity changed; the stored SIP state goes with it")
        await SipStore(self.hass, entry.entry_id).async_remove()

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
