"""What "Перенастроить" is allowed to do, and what it must refuse.

The step ends in an SMS login either way, and both reasons to be there are real: the
same number renews a session (which is how a registrar that refused our SIP
credentials is answered), a different number moves the entry to another account. The
two are guarded by opposite Home Assistant helpers, and the flow used to hold the one
that forbids exactly what its own text invited.

**These tests need Home Assistant, which this project deliberately does not install**
-- neither locally nor in CI, because the fake registrar the other tests run against
needs sockets and ``pytest-socket`` takes them away. They skip themselves everywhere
until the harness is present. To actually run them, use a throwaway container:

    docker run --rm -v "$PWD":/repo -w /repo \\
        ghcr.io/home-assistant/home-assistant:stable sh -c \\
        "pip install -q pytest-homeassistant-custom-component &&
         python -m pytest tests/test_config_flow_reconfigure.py -q
         -o asyncio_mode=auto"
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.loki.const import DOMAIN

MINE = "+79990000001"
FREE = "+79990000002"
TAKEN = "+79990000003"

SIP = {"url": "registrar.example", "phone": "1000001", "password": "secret"}
SIP_ELSEWHERE = {"url": "registrar.example", "phone": "1000002", "password": "other"}


@pytest.fixture(autouse=True)
def _allow_custom_integration(enable_custom_integrations: Any) -> None:
    """The harness only loads custom_components when this fixture is requested."""


def _entry(phone: str, *, sip: dict[str, str] = SIP) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=phone,
        title=phone,
        data={
            "phone": phone,
            "refresh_token": "refresh-old",
            "sip": sip,
            "master_flg": True,
            "max_phones": 5,
        },
    )


def _client(*, sip: dict[str, str]) -> MagicMock:
    """A LokiClient that answers without a network."""
    client = MagicMock()
    client.request_sms = AsyncMock(return_value="provisional-token")
    client.confirm_sms = AsyncMock(
        return_value={
            "token": "access-new",
            "refresh": "refresh-new",
            "sip": sip,
            "master_flg": True,
            "max_phones": 5,
        }
    )
    client.refresh_token = "refresh-new"
    return client


async def _start(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    entry.add_to_hass(hass)
    return await entry.start_reconfigure_flow(hass)


@pytest.mark.asyncio
async def test_the_same_number_is_a_re_login_and_is_allowed(
    hass: HomeAssistant,
) -> None:
    """The path the "registrar refused" repair card sends people down.

    ``_abort_if_unique_id_configured`` on its own would refuse this: the number is
    already held -- by this very entry.
    """
    entry = _entry(MINE)
    result = await _start(hass, entry)

    with patch(
        "custom_components.loki.config_flow.LokiClient", return_value=_client(sip=SIP)
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"phone": MINE}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "sms"


@pytest.mark.asyncio
async def test_a_different_number_is_allowed_and_the_entry_follows_it(
    hass: HomeAssistant,
) -> None:
    """The thing the step's own title promises, and used to refuse.

    ``_abort_if_unique_id_mismatch`` aborted here, so the only way to move an account
    was to delete the entry -- which renames every entity in the house, because entity
    unique ids carry the entry id.
    """
    entry = _entry(MINE)
    result = await _start(hass, entry)

    with patch(
        "custom_components.loki.config_flow.LokiClient",
        return_value=_client(sip=SIP_ELSEWHERE),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"phone": FREE}
        )
        assert result["step_id"] == "sms"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"sms_code": "123456"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    # The number is the identity: data, unique id and title all have to move with it,
    # or every later flow judges this entry by the number it no longer uses.
    assert entry.data["phone"] == FREE
    assert entry.unique_id == FREE
    assert entry.title == FREE
    assert entry.data["sip"] == SIP_ELSEWHERE
    assert entry.data["refresh_token"] == "refresh-new"


@pytest.mark.asyncio
async def test_a_number_another_entry_holds_is_refused(hass: HomeAssistant) -> None:
    """One session per number: two entries on one would log each other out."""
    _entry(TAKEN).add_to_hass(hass)
    entry = _entry(MINE)
    result = await _start(hass, entry)

    with patch(
        "custom_components.loki.config_flow.LokiClient", return_value=_client(sip=SIP)
    ) as client:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"phone": TAKEN}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    # Refused before the SMS, not after: the code costs the user a message and twenty
    # minutes of validity.
    client.return_value.request_sms.assert_not_called()


@pytest.mark.asyncio
async def test_reauth_renews_the_session_and_leaves_the_account_alone(
    hass: HomeAssistant,
) -> None:
    """A reauth cannot be pointed at another number, and must not drift onto one.

    It never asks: ``async_step_reauth`` takes the number out of the entry and the
    confirm step shows an empty form. The mismatch guard at the end of the flow is the
    belt to those braces, so what is worth pinning here is the reachable half -- the
    session and the SIP credentials are renewed, the account is untouched.
    """
    entry = _entry(MINE)
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)

    with patch(
        "custom_components.loki.config_flow.LokiClient", return_value=_client(sip=SIP)
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["step_id"] == "sms"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"sms_code": "123456"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.unique_id == MINE
    assert entry.data["phone"] == MINE
    # The SIP block comes only from a completed login, so a reauth is also how it is
    # refreshed -- which is what a registrar that refused the old one needs.
    assert entry.data["refresh_token"] == "refresh-new"
    assert entry.data["sip"] == SIP


@pytest.mark.asyncio
async def test_the_same_number_keeps_the_sip_state(hass: HomeAssistant) -> None:
    """A re-login must not cost ten minutes of baseline.

    The SIP state belongs to an address-of-record. When the sign-in comes back with
    the same one, dropping it would make the client watch the account for ten minutes
    again before registering -- ten more minutes of silent doorbell, right after the
    re-login that was meant to end them.
    """
    entry = _entry(MINE)
    result = await _start(hass, entry)

    with (
        patch(
            "custom_components.loki.config_flow.LokiClient",
            return_value=_client(sip=SIP),
        ),
        patch("custom_components.loki.config_flow.SipStore") as store,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"phone": MINE}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"sms_code": "123456"}
        )
        await hass.async_block_till_done()

    assert result["reason"] == "reconfigure_successful"
    store.return_value.async_remove.assert_not_called()


@pytest.mark.asyncio
async def test_a_different_sip_identity_drops_the_sip_state(
    hass: HomeAssistant,
) -> None:
    """The opposite harm: state carried onto an account it was never earned on.

    ``first_registration_done`` would skip the baseline on an account nobody here has
    ever watched, and the remembered Contact URIs could have the client withdraw a
    binding that was never ours.
    """
    entry = _entry(MINE)
    result = await _start(hass, entry)

    with (
        patch(
            "custom_components.loki.config_flow.LokiClient",
            return_value=_client(sip=SIP_ELSEWHERE),
        ),
        patch("custom_components.loki.config_flow.SipStore") as store,
    ):
        store.return_value.async_remove = AsyncMock()
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"phone": FREE}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"sms_code": "123456"}
        )
        await hass.async_block_till_done()

    assert result["reason"] == "reconfigure_successful"
    store.return_value.async_remove.assert_called_once()
