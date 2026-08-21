"""The probe, proven against a registrar whose policy we control.

The case that matters is eviction: a registrar that keeps one contact per account will
silently drop the resident's phone when we register, and the phone will not notice for
minutes. The probe exists to find that out first, so these tests make a bench that
really does evict, and check the probe says so.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
from typing import Any

import pytest

from tests.fake_registrar import FakeRegistrar

_SPEC = importlib.util.spec_from_file_location(
    "sip_probe", Path(__file__).resolve().parents[1] / "scripts" / "sip_probe.py"
)
assert _SPEC and _SPEC.loader
sip_probe = importlib.util.module_from_spec(_SPEC)
sys.modules["sip_probe"] = sip_probe
_SPEC.loader.exec_module(sip_probe)

PASSWORD = "secret"
USER = "1009999"


async def _run_probe(
    registrar: FakeRegistrar, *, policy_test: bool = False, password: str = PASSWORD
) -> tuple[int, dict[str, Any]]:
    """Run the (blocking) probe against a running registrar without deadlocking."""
    port = await registrar.start()
    try:
        return await asyncio.to_thread(
            sip_probe.run, "127.0.0.1", USER, password, port, policy_test
        )
    finally:
        await registrar.stop()


# --------------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    ("row", "expected_uri", "expected_expires"),
    [
        ("<sip:a@b:5060;transport=tcp>;expires=60", "sip:a@b:5060;transport=tcp", "60"),
        # Bracket-less form is equally legal; mishandling it drops every parameter
        # and would report a healthy binding as somebody else's.
        ("sip:a@b;expires=120", "sip:a@b", "120"),
        ('<sip:a@b>;+sip.instance="<urn:uuid:x>";expires=30', "sip:a@b", "30"),
    ],
)
def test_parse_contact_handles_both_forms(
    row: str, expected_uri: str, expected_expires: str
) -> None:
    """Contact values come in two shapes and both must keep their parameters."""
    uri, params = sip_probe.parse_contact(row)

    assert uri == expected_uri
    assert params["expires"] == expected_expires


def test_split_commas_respects_quotes_and_brackets() -> None:
    """A single scanner is used everywhere; qop="auth,auth-int" must survive it."""
    assert sip_probe.split_commas('a="x,y", b=z') == ['a="x,y"', "b=z"]
    assert sip_probe.split_commas("<sip:a@b;x=1,2>;e=1, <sip:c@d>") == [
        "<sip:a@b;x=1,2>;e=1",
        "<sip:c@d>",
    ]


# --------------------------------------------------------------------- safety


@pytest.mark.asyncio
async def test_probe_sends_no_contact_and_changes_nothing() -> None:
    """The whole point: look at the bindings without touching them."""
    registrar = FakeRegistrar(password=PASSWORD, max_contacts=1)
    registrar.seed_foreign(1)
    before = list(registrar.bindings)

    code, result = await _run_probe(registrar)

    assert registrar.bindings == before, "the probe modified the binding table"
    assert registrar.wildcard_seen is False
    assert code == sip_probe.EXIT_CAUTION
    assert result["verdict"] == "one_contact"
    assert len(result["bindings_before"]) == 1
    assert result["bindings_before"][0]["looks_like_pjsua"] is True


@pytest.mark.asyncio
async def test_register_refuses_to_build_a_wildcard_contact() -> None:
    """A wildcard Contact with Expires: 0 wipes every binding on the account."""
    registrar = FakeRegistrar(password=PASSWORD)
    port = await registrar.start()
    try:
        wire = await asyncio.to_thread(sip_probe.Wire, "127.0.0.1", port)
        session = sip_probe.Session(wire, "127.0.0.1", port, USER, PASSWORD)
        with pytest.raises(sip_probe.ProbeError, match="wildcard"):
            session.register(contacts=["*"], expires=0)
        wire.close()
    finally:
        await registrar.stop()

    assert registrar.wildcard_seen is False


# --------------------------------------------------------------------- verdicts


@pytest.mark.asyncio
async def test_empty_account_is_caution_not_safe() -> None:
    """An empty list may just be a sleeping phone, so it is never a green light."""
    code, result = await _run_probe(FakeRegistrar(password=PASSWORD))

    assert code == sip_probe.EXIT_CAUTION
    assert result["verdict"] == "empty"


@pytest.mark.asyncio
async def test_several_coexisting_bindings_are_safe() -> None:
    """Two foreign bindings at once prove the registrar allows more than one."""
    registrar = FakeRegistrar(password=PASSWORD, max_contacts=5)
    registrar.seed_foreign(2)

    code, result = await _run_probe(registrar)

    assert code == sip_probe.EXIT_SAFE
    assert result["verdict"] == "multi_contact"


@pytest.mark.asyncio
async def test_wrong_password_reports_auth_and_stops() -> None:
    """Retrying a rejected credential is how an IP gets banned."""
    code, result = await _run_probe(FakeRegistrar(password=PASSWORD), password="wrong")

    assert code in (sip_probe.EXIT_AUTH, sip_probe.EXIT_REFUSED)
    assert result["verdict"] in ("auth", "forbidden")


@pytest.mark.asyncio
async def test_transport_failure_is_reported_not_raised() -> None:
    """A closed port is a normal answer: SIP is impossible here, REST still works."""
    code, result = await asyncio.to_thread(
        sip_probe.run, "127.0.0.1", USER, PASSWORD, 1, False
    )

    assert code == sip_probe.EXIT_TRANSPORT
    assert result["verdict"] == "transport"


# ----------------------------------------------------------------- policy test


@pytest.mark.asyncio
async def test_policy_test_detects_eviction() -> None:
    """The dangerous registrar: one contact per account, so we displace the phone."""
    registrar = FakeRegistrar(password=PASSWORD, max_contacts=1)
    registrar.seed_foreign(1)

    code, result = await _run_probe(registrar, policy_test=True)

    assert code == sip_probe.EXIT_UNSAFE
    assert result["verdict"] == "evicted"
    assert registrar.evictions == 1
    assert result["withdrew_own_contact"] is True
    # Whatever the outcome, we must leave nothing of ours behind.
    assert not any("127.0.0.1" in b.uri for b in registrar.bindings)


@pytest.mark.asyncio
async def test_policy_test_confirms_coexistence() -> None:
    """The permissive registrar: we register and the phone survives."""
    registrar = FakeRegistrar(password=PASSWORD, max_contacts=3)
    registrar.seed_foreign(1)

    code, result = await _run_probe(registrar, policy_test=True)

    assert code == sip_probe.EXIT_SAFE
    assert result["verdict"] == "coexists"
    assert registrar.evictions == 0
    # Our own contact is withdrawn again; only the resident's phone remains.
    assert len(registrar.bindings) == 1


@pytest.mark.asyncio
async def test_registrar_that_hides_bindings_is_flagged() -> None:
    """If bindings are invisible, the eviction guard is blind and SIP must not run."""
    registrar = FakeRegistrar(password=PASSWORD, max_contacts=1, report_bindings=False)
    registrar.seed_foreign(1)

    code, result = await _run_probe(registrar, policy_test=True)

    assert code == sip_probe.EXIT_ODD
    assert result["verdict"] in ("policy_test_pointless", "bindings_not_reported")


@pytest.mark.asyncio
async def test_nat_rewrite_is_reported() -> None:
    """Behind Docker the Contact must be rebuilt from received/rport, or no INVITE."""
    registrar = FakeRegistrar(password=PASSWORD, rewrite_contact=True)

    _, result = await _run_probe(registrar)

    assert result["nat"]["received"] == "203.0.113.9"
    assert result["nat"]["rport"] == "44444"


def test_cli_requires_acknowledgement_for_policy_test() -> None:
    """The destructive mode must never be reachable by accident."""
    with pytest.raises(SystemExit) as excinfo:
        sip_probe.main(["--url", "h", "--user", "u", "--policy-test"])

    assert excinfo.value.code == 2
