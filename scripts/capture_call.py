#!/usr/bin/env python3
"""Register, wait for one real doorbell press, and record exactly what arrived.

Run this, then press the button on the intercom. It answers the way the integration
will -- 100 Trying, 180 Ringing, never a 2xx and never a 6xx -- and prints the whole
INVITE, because the ``From`` header is the one thing that cannot be guessed: the
backend resolves which door is calling from that string, and nothing else identifies
it. Afterwards it releases the branch and withdraws its own binding.

Reads its credentials from a Home Assistant config entry store:

  python scripts/capture_call.py --from-ha /config/.storage/core.config_entries
  python scripts/capture_call.py --from-ha ... --wait 300 --raw
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.loki.sip.client import (
    LokiSipClient,
    SipConfig,
    SipSnapshot,
    SipState,
)

# Printed as-is only when --raw is given: a captured INVITE carries the account's
# extension and public address.
_SENSITIVE = ("authorization", "proxy-authorization")


class Capture:
    """Prints what the client reports and remembers the first call."""

    def __init__(self, *, raw: bool) -> None:
        """Initialise the recorder."""
        self.raw = raw
        self.call: tuple[str, str] | None = None
        self.done = asyncio.Event()

    def on_state(self, state: SipState, detail: str | None) -> None:
        """Print each state change."""
        print(f"  {state.value}" + (f"  ({detail})" if detail else ""), flush=True)

    def on_snapshot(self, snapshot: SipSnapshot) -> None:
        """Print the binding list."""
        print(f"  привязок: {len(snapshot.bindings)}", flush=True)
        for binding in snapshot.bindings:
            tag = " [pjsip-style]" if binding.looks_like_pjsua else ""
            shown = binding.uri if self.raw else "<привязка>"
            print(f"      {shown} expires={binding.expires}{tag}", flush=True)

    def on_terminal(self, state: SipState, kind: str, detail: str) -> None:
        """Print a terminal failure and stop waiting."""
        print(f"\n  ОСТАНОВ: {state.value} / {kind}\n  {detail}", flush=True)
        self.done.set()

    async def on_incoming(self, call_id: str, remote_uri: str) -> bool:
        """Record the call and let the branch ring until the deadline."""
        print("\n=== ЗВОНОК ===", flush=True)
        print(f"  Call-ID  : {call_id}", flush=True)
        print(f"  From     : {remote_uri}", flush=True)
        self.call = (call_id, remote_uri)
        self.done.set()
        return True

    def on_call_end(self, call_id: str, reason: str) -> None:
        """Print how the call finished."""
        print(f"  вызов завершён: {reason}", flush=True)


def _credentials(path: Path) -> dict[str, str]:
    document = json.loads(path.read_text(encoding="utf-8"))
    for entry in document.get("data", {}).get("entries", []):
        if entry.get("domain") != "loki":
            continue
        sip = entry.get("data", {}).get("sip") or {}
        if sip.get("url") and sip.get("phone"):
            return sip
    raise SystemExit(f"no Loki entry with SIP credentials in {path}")


def _patch_tracing(client: LokiSipClient, capture: Capture) -> None:
    """Print every inbound request in full, which is the point of the exercise."""
    original = client._on_request

    async def traced(request):  # type: ignore[no-untyped-def]
        print(f"\n--- {request.start_line} ---", flush=True)
        for header in request.headers:
            if header.name in _SENSITIVE:
                print(f"  {header.name}: <вырезано>", flush=True)
            elif capture.raw or header.name not in ("contact", "to"):
                print(f"  {header.raw.decode('utf-8', 'replace')}", flush=True)
            else:
                print(f"  {header.name}: <вырезано>", flush=True)
        await original(request)

    client._on_request = traced  # type: ignore[method-assign]


async def main() -> int:
    """Register, wait for a call, then clean up."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-ha", type=Path, required=True)
    parser.add_argument("--wait", type=float, default=180.0)
    parser.add_argument(
        "--raw",
        action="store_true",
        help="print addresses and the extension unredacted",
    )
    args = parser.parse_args()

    sip = _credentials(args.from_ha)
    capture = Capture(raw=args.raw)
    client = LokiSipClient(
        SipConfig(
            host=sip["url"],
            user=sip["phone"],
            password=sip["password"],
            # The account was already shown to be empty by scripts/sip_probe.py; the
            # ten-minute baseline would only make a manual test unusable.
            require_baseline=False,
            register=True,
            first_registration_done=True,
            expires=120,
        ),
        capture,
    )
    _patch_tracing(client, capture)

    task = asyncio.create_task(client.async_run())
    print("Регистрируюсь…", flush=True)
    try:
        for _ in range(60):
            if client.state is SipState.REGISTERED or capture.done.is_set():
                break
            await asyncio.sleep(0.5)

        if client.state is not SipState.REGISTERED:
            print("\nЗарегистрироваться не удалось — звонок не поймать.", flush=True)
        else:
            print(
                f"\nГОТОВО. Нажмите кнопку домофона. Жду {args.wait:.0f} с…",
                flush=True,
            )
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(args.wait):
                    await capture.done.wait()
            if capture.call is None:
                print("\nЗвонок так и не пришёл.", flush=True)
    finally:
        print("\nСнимаю привязку…", flush=True)
        with contextlib.suppress(Exception):
            await client._withdraw_own_contact()
        await client.async_stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        print("Готово.", flush=True)

    return 0 if capture.call else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
