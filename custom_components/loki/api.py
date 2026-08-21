"""HTTP client for the Loki cloud API.

Every endpoint is a POST carrying a JSON body -- there are no REST verbs, not even
for listings. Access tokens live about 20 minutes, so a 401 is routine rather than
exceptional and is handled transparently by refreshing once and replaying.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp
from yarl import URL

from .const import DEFAULT_API_HOST, DEFAULT_USER_AGENT
from .models import KEY_CAMERAS, KEY_DOORS, LokiDevice, parse_device_list
from .protocol import auth_hash
from .redact import describe_response, redact

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=30)

# Devices are listed by sending an empty body, which returns doors and cameras in
# one response. The typed form is kept as a fallback in case the backend ever stops
# accepting an empty body.
_DEVICE_LIST_OPTIONS = {
    "itemsPerPage": -1,
    "page": 1,
    "sortBy": [],
    "sortDesc": [],
}


class LokiError(Exception):
    """Base error for this integration.

    Carries the HTTP status and the server's own message when there was one, so a
    caller can record *what the backend actually said* rather than only our prose.
    That record is the only route to ever distinguishing a routine token expiry from
    a session taken over elsewhere -- today they are indistinguishable.
    """

    def __init__(
        self,
        message: str = "",
        *,
        status: int | None = None,
        server_message: str | None = None,
    ) -> None:
        """Initialise the error."""
        super().__init__(message)
        self.status = status
        self.server_message = server_message


class LokiApiError(LokiError):
    """The backend was unreachable or answered in a way we cannot use."""


class LokiBadRequest(LokiApiError):
    """HTTP 400. What it means depends on the endpoint, so callers classify it.

    Subclasses LokiApiError deliberately: an unclassified 400 must degrade to
    UpdateFailed, not escape the coordinator as an unhandled exception.
    """


class LokiAuthError(LokiError):
    """Credentials are no longer valid and only a new SMS login can fix it."""


class LokiPhoneNotRegistered(LokiError):
    """The phone number is not known to Loki."""


class LokiInvalidCode(LokiError):
    """The SMS code was rejected, or the provisional token had already expired."""


class LokiDeviceForbidden(LokiBadRequest):
    """This account may not operate that device."""


class LokiClient:
    """Talks to the Loki backend on behalf of one account."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        host: str = DEFAULT_API_HOST,
        access_token: str | None = None,
        refresh_token: str | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        """Initialise the client. The access token is optional and short-lived."""
        self._session = session
        self._host = host.rstrip("/")
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._user_agent = user_agent
        self._refresh_lock = asyncio.Lock()

    @property
    def access_token(self) -> str | None:
        """Current access token. Held in memory only -- it expires in ~20 min."""
        return self._access_token

    @property
    def refresh_token(self) -> str | None:
        """Long-lived refresh token. Never rotated by the backend; persisted once."""
        return self._refresh_token

    def image_url(self, device_id: int) -> str:
        """Thumbnail URL for a device.

        Built from the device id rather than the device's ``img`` field, because that
        field accumulates repeated "?a=<hash>" suffixes and becomes invalid.
        """
        return f"{self._host}/imgs/device/{device_id}.jpg"

    # -- authentication -------------------------------------------------------

    async def request_sms(self, phone: str) -> str:
        """Start a login: validate the phone, then trigger the SMS.

        Returns the provisional token that must be combined with the SMS code.
        """
        try:
            response = await self._post(
                "/api/auth/authorize", {"phone": phone}, auth=False
            )
        except (LokiBadRequest, LokiAuthError) as err:
            # The backend rejects an unknown number with a 4xx rather than an empty
            # token, so both shapes mean the same thing to the user.
            raise LokiPhoneNotRegistered(phone) from err

        token = response.get("token") if isinstance(response, dict) else None
        if not token:
            raise LokiPhoneNotRegistered(phone)

        # This second call is what actually sends the SMS.
        await self._post("/api/auth/showPin", {"phone": phone}, auth=False)
        # The backend acknowledges the request but says nothing about delivery, so
        # this records that we asked -- not that an SMS arrived.
        _LOGGER.debug("SMS requested for %s", redact({"phone": phone})["phone"])
        return str(token)

    async def confirm_sms(
        self, provisional_token: str, sms_code: str
    ) -> dict[str, Any]:
        """Finish a login. Returns the full session, including the SIP credentials.

        The ``sip`` object is issued here and nowhere else -- a token refresh does not
        return it -- so it must be persisted alongside the refresh token.
        """
        try:
            response = await self._post(
                "/api/auth/checkToken",
                {
                    "token": provisional_token,
                    "hash": auth_hash(provisional_token, sms_code),
                },
                auth=False,
            )
        except (LokiBadRequest, LokiAuthError) as err:
            # The backend does not distinguish "wrong code" from "the provisional
            # token expired"; both are recovered the same way, by resending.
            raise LokiInvalidCode(str(err)) from err

        if not isinstance(response, dict) or not response.get("token"):
            raise LokiInvalidCode("no token in checkToken response")

        self._access_token = str(response["token"])
        if refresh := response.get("refresh"):
            self._refresh_token = str(refresh)
        return response

    async def async_refresh_token(self) -> str:
        """Exchange the refresh token for a new access token.

        The backend returns only a new access token; the refresh token is never
        rotated, so there is nothing new to persist here.
        """
        if not self._refresh_token:
            raise LokiAuthError("no refresh token")

        # The stale access token is still sent as the bearer -- the official client
        # does the same, and the endpoint expects the header to be present.
        try:
            response = await self._post(
                "/api/auth/refreshToken",
                {"token": self._refresh_token},
                auth=True,
                allow_refresh=False,
            )
        except (LokiBadRequest, LokiAuthError) as err:
            # A rejected refresh token is unrecoverable: it is never rotated, so
            # there is nothing to retry with. Only a new SMS login fixes it, and
            # surfacing LokiAuthError is what starts the reauth flow.
            raise LokiAuthError(
                f"refresh rejected: {err}",
                status=err.status,
                server_message=err.server_message,
            ) from err

        token = response.get("token") if isinstance(response, dict) else None
        if not token:
            raise LokiAuthError("refresh rejected")

        self._access_token = str(token)
        return self._access_token

    # -- devices --------------------------------------------------------------

    async def async_get_devices(self) -> list[LokiDevice]:
        """List every door and camera the account can see."""
        response = await self._post("/api/device/list/", None)
        if isinstance(response, dict) and (
            KEY_DOORS in response or KEY_CAMERAS in response
        ):
            # Keyed on presence, not truthiness: an account with no devices yet is a
            # legitimate answer, and treating it as a failure would run the fallback
            # on every single poll forever.
            return parse_device_list(response)

        # Empty body returned nothing usable; fall back to the two typed calls the
        # reference client uses.
        doors, cameras = await asyncio.gather(
            self._post(
                "/api/device/list/",
                {"search": "", "type": "D", "options": _DEVICE_LIST_OPTIONS},
            ),
            self._post(
                "/api/device/list/",
                {"search": "", "type": "C", "options": _DEVICE_LIST_OPTIONS},
            ),
        )
        # Concatenated per key rather than dict.update: today each typed call answers
        # with only its own key, but a response carrying an empty counterpart key
        # would silently erase the other call's results.
        merged: dict[str, list[Any]] = {KEY_DOORS: [], KEY_CAMERAS: []}
        for part in (doors, cameras):
            if not isinstance(part, dict):
                continue
            for key in (KEY_DOORS, KEY_CAMERAS):
                if isinstance(entries := part.get(key), list):
                    merged[key].extend(entries)
        return parse_device_list(merged)

    async def async_get_device_by_sip_name(self, sip_uri: str) -> LokiDevice | None:
        """Resolve which door is calling, from the raw SIP remote URI.

        The lookup is server-side and the whole ``From`` value is passed verbatim --
        display name, angle brackets and all -- exactly as the official client does.

        The URI must be complete. Passing only the user part was measured against the
        live backend and resolved to a *different door*: the match is not exact, so a
        bare extension silently answers with somebody else's entrance. Since the
        result is used to decide which door to open, that is refused rather than
        risked.
        """
        if "sip:" not in sip_uri.lower():
            raise ValueError(
                "the full SIP URI is required: a bare extension resolves to the "
                "wrong door"
            )

        response = await self._post("/api/device/list/", {"name": sip_uri})
        if not isinstance(response, dict):
            return None
        devices = parse_device_list(response)
        if len(devices) == 1:
            return devices[0]
        if devices:
            # More than one match is not a menu to pick from: the list comes back
            # sorted by name, so taking the first would answer with whichever door
            # sorts earliest rather than with the one that is calling. Nothing here
            # can tell them apart, and the answer decides which door a notification
            # opens -- so this refuses rather than guesses.
            _LOGGER.warning(
                "Резолв двери по SIP URI вернул несколько устройств (%d); "
                "выбирать наугад нельзя",
                len(devices),
            )
        return None

    # -- actions --------------------------------------------------------------

    async def async_open_door(
        self, *, device_id: int | None = None, sip_uri: str | None = None
    ) -> None:
        """Open a door, addressed either by device id or by the calling SIP URI."""
        if device_id is not None:
            body: dict[str, Any] = {"device_id": int(device_id)}
        elif sip_uri:
            body = {"name": sip_uri}
        else:
            raise ValueError("either device_id or sip_uri is required")

        try:
            await self._post("/api/intercom/lockOpen", body)
        except LokiDeviceForbidden:
            raise
        except LokiBadRequest as err:
            # On this endpoint a 400 has exactly one meaning: the account is not
            # provisioned for that device.
            raise LokiDeviceForbidden(str(err)) from err

    async def async_get_snapshot(self, device_id: int) -> bytes:
        """Fetch the backend's periodically-refreshed still image for a device."""
        status, image = await self._raw_get_snapshot(device_id)

        # Stills go straight to the image host rather than through _post, so they
        # need their own refresh -- otherwise every snapshot 401s about twenty
        # minutes after startup and the camera silently serves a stale frame.
        if status == 401 and self._refresh_token:
            async with self._refresh_lock:
                await self.async_refresh_token()
            status, image = await self._raw_get_snapshot(device_id)

        if status == 401:
            raise LokiAuthError(f"snapshot for {device_id} returned 401")
        if status != 200 or image is None:
            raise LokiApiError(f"snapshot returned HTTP {status}")
        return image

    async def _raw_get_snapshot(self, device_id: int) -> tuple[int, bytes | None]:
        """Fetch one still without any retry logic."""
        headers = {
            "User-Agent": self._user_agent,
            "Authorization": f"Bearer {self._access_token or ''}",
        }
        try:
            async with self._session.get(
                URL(self.image_url(device_id)), headers=headers, timeout=_TIMEOUT
            ) as response:
                if response.status != 200:
                    return response.status, None
                return 200, await response.read()
        except TimeoutError as err:
            raise LokiApiError("snapshot timed out") from err
        except aiohttp.ClientError as err:
            raise LokiApiError(f"snapshot failed: {err}") from err

    # -- transport ------------------------------------------------------------

    async def _post(
        self,
        path: str,
        body: dict[str, Any] | None,
        *,
        auth: bool = True,
        allow_refresh: bool = True,
    ) -> Any:
        """POST a JSON body, refreshing the access token once on a 401."""
        status, payload = await self._raw_post(path, body, auth=auth)

        if status == 401 and auth and allow_refresh and self._refresh_token:
            # Snapshot the token we failed with *before* queueing on the lock: if a
            # concurrent caller already replaced it, our 401 is stale and we should
            # replay with the new token rather than mint yet another one. The backend
            # keeps one session per phone number, so surplus refreshes are a real risk.
            token_before = self._access_token
            async with self._refresh_lock:
                if self._access_token == token_before:
                    await self.async_refresh_token()
            status, payload = await self._raw_post(path, body, auth=auth)

        server_message = (
            str(payload.get("message"))
            if isinstance(payload, dict) and payload.get("message")
            else None
        )

        if status == 401:
            raise LokiAuthError(
                f"{path} returned 401", status=401, server_message=server_message
            )
        if status == 400:
            # Left unclassified on purpose: what a 400 means depends entirely on the
            # endpoint, so each caller narrows it.
            raise LokiBadRequest(
                server_message or "bad request",
                status=400,
                server_message=server_message,
            )
        if status != 200:
            raise LokiApiError(
                f"{path} returned HTTP {status}",
                status=status,
                server_message=server_message,
            )

        return payload

    async def _raw_post(
        self, path: str, body: dict[str, Any] | None, *, auth: bool
    ) -> tuple[int, Any]:
        """Issue one request without any retry logic."""
        headers = {
            "Content-Type": "application/json",
            "User-Agent": self._user_agent,
            # Always present. The bootstrap auth calls carry an EMPTY bearer, and
            # refreshToken carries the *stale* access token. ``auth`` only decides
            # whether a 401 is worth refreshing, so it does not gate the header.
            "Authorization": (
                f"Bearer {self._access_token or ''}" if auth else "Bearer "
            ),
        }

        # Serialised here rather than handed to aiohttp's ``json=`` so the exact bytes
        # are known. An empty body is meaningful: it is how the device list endpoint is
        # asked for doors and cameras at once.
        #
        # ensure_ascii=False matches the official client, which writes the body through
        # a writer using the platform charset and so puts raw UTF-8 on the wire rather
        # than \uXXXX escapes. It makes no difference to a phone number, but the device
        # lookup by SIP name carries a Cyrillic display name, and that is the one call
        # where being byte-identical to a client known to work is worth having.
        payload = (
            b""
            if body is None
            else json.dumps(body, ensure_ascii=False).encode("utf-8")
        )

        try:
            async with self._session.post(
                URL(f"{self._host}{path}"),
                headers=headers,
                data=payload,
                timeout=_TIMEOUT,
                # A deliberate difference from the official client, which leaves
                # redirects on. Java's HttpURLConnection does not re-POST on a 302
                # anyway -- it follows with a bodyless GET -- so following buys no
                # working behaviour, while it would send the bearer token to whatever
                # host the redirect names. The endpoint set is fixed; there is no
                # legitimate redirect to follow.
                allow_redirects=False,
            ) as response:
                text = await response.text()
                status = response.status
        except TimeoutError as err:
            raise LokiApiError(f"{path} timed out") from err
        except aiohttp.ClientError as err:
            raise LokiApiError(f"{path} failed: {err}") from err

        if not text:
            _LOGGER.debug("POST %s %s -> %s, empty body", path, redact(body), status)
            return status, None

        try:
            parsed = json.loads(text)
        except ValueError:
            _LOGGER.debug(
                "POST %s %s -> %s, non-JSON body (%d bytes)",
                path,
                redact(body),
                status,
                len(text),
            )
            return status, text

        _LOGGER.debug(
            "POST %s %s -> %s %s",
            path,
            redact(body),
            status,
            describe_response(parsed),
        )
        return status, parsed
