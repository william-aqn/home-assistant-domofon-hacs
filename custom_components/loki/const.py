"""Constants for the Loki integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from .protocol import build_user_agent

DOMAIN: Final = "loki"

# Shown in the config flow so the full terms are one click away from the checkbox.
DISCLAIMER_URL: Final = (
    "https://github.com/william-aqn/home-assistant-domofon-hacs/blob/main/DISCLAIMER.md"
)

# The upstream service endpoint. This is infrastructure, not branding -- it must
# match the operator's real host or nothing works.
DEFAULT_API_HOST: Final = "https://app.risan-service.ru"

# Composed the way Android composes it rather than pasted from a capture -- see
# protocol.build_user_agent. The official client never sets this header itself; the
# platform supplies it, so sending the platform's own shape is what "looking like the
# app" actually means.
DEFAULT_USER_AGENT: Final = build_user_agent()

DEFAULT_SCAN_INTERVAL: Final = timedelta(minutes=5)

# A call with no explicit end -- a missed SIP CANCEL, or a simulate_ring left hanging
# in testing -- must not pin a door's call_active sensor on forever. The blueprint's
# wait timeout is documented to match this.
CALL_TIMEOUT: Final = 120

# --- config entry keys -------------------------------------------------------

CONF_PHONE: Final = "phone"
CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_SIP: Final = "sip"
CONF_MASTER_FLG: Final = "master_flg"
CONF_MAX_PHONES: Final = "max_phones"
# When the refresh token was issued. Recorded so the age of a failing token can be
# logged; deliberately not used to guess *why* it failed.
CONF_AUTH_TIME: Final = "auth_time"

# --- SIP ---------------------------------------------------------------------
# Keys inside the ``sip`` object stored at entry.data[CONF_SIP]. Note CONF_SIP_USER
# is also the string "phone": harmless only while the SIP data stays nested.

CONF_SIP_URL: Final = "url"
CONF_SIP_USER: Final = "phone"
CONF_SIP_PASSWORD: Final = "password"

OPT_SIP_ENABLED: Final = "sip_enabled"
# Refuse to register while somebody else holds a binding on the account. The only
# check that prevents rather than detects, so it defaults on and turning it off is
# an informed decision the options flow spells out.
OPT_SIP_STRICT_GUARD: Final = "sip_strict_guard"

# --- phase 4 (Frigate), not yet wired ----------------------------------------

OPT_FRIGATE_ENABLED: Final = "frigate_enabled"
OPT_FRIGATE_TOPIC_PREFIX: Final = "frigate_topic_prefix"
OPT_FRIGATE_CAMERA_MAP: Final = "frigate_camera_map"
OPT_FACE_CORRELATION_WINDOW: Final = "face_correlation_window"

DEFAULT_FRIGATE_TOPIC_PREFIX: Final = "frigate"
DEFAULT_FACE_CORRELATION_WINDOW: Final = 30

EVENT_TYPE_FACE_RECOGNIZED: Final = "face_recognized"

# --- options keys ------------------------------------------------------------

OPT_SCAN_INTERVAL: Final = "scan_interval"
# Whether to put a "Домофоны" page in the sidebar. Asked once at setup, changeable in
# the options afterwards.
OPT_PANEL: Final = "sidebar_panel"

# How the «Домофоны» page is arranged: the order doors appear in, the ones put away,
# and how big the tiles are. Written by the page itself through a websocket command --
# see panel_layout.py -- and kept in the options because a layout that lives in one
# browser is a layout the phone does not have.
OPT_PANEL_ORDER: Final = "panel_order"
OPT_PANEL_HIDDEN: Final = "panel_hidden"
OPT_PANEL_TILE_SIZE: Final = "panel_tile_size"

# --- events ------------------------------------------------------------------

EVENT_LOKI: Final = "loki_event"

EVENT_TYPE_CALL_INCOMING: Final = "call_incoming"
EVENT_TYPE_CALL_ENDED: Final = "call_ended"
EVENT_TYPE_AUTH_FAILED: Final = "auth_failed"
EVENT_TYPE_SIP_TERMINAL: Final = "sip_terminal"

# --- repairs -----------------------------------------------------------------

ISSUE_REAUTH_UNRECOVERABLE: Final = "reauth_unrecoverable"
ISSUE_SIP_TERMINAL: Final = "sip_terminal"

# Matches HA's DoorbellEventType.RING. Imported as a literal rather than from
# homeassistant.components.event because that enum postdates our minimum version.
EVENT_RING: Final = "ring"

# --- services ----------------------------------------------------------------

SERVICE_OPEN_DOOR: Final = "open_door"
SERVICE_SIMULATE_RING: Final = "simulate_ring"
SERVICE_HANGUP: Final = "hangup"
SERVICE_CAPTURE: Final = "capture_frame"

ATTR_DEVICE_ID: Final = "device_id"

# --- stream layout -----------------------------------------------------------
#
# The media host serves the same channel two ways. The scheme and port carried in
# the API's own ``url`` field are unreliable (see models.normalize_stream), so we
# rebuild both forms from the hostname and channel id alone.

RTSP_PORT: Final = 8554
HLS_PORT: Final = 8888

# How long to wait when checking that a media host answers at all. Short on purpose:
# this runs on every poll and a firewalled host should be reported, not waited for.
MEDIA_PROBE_TIMEOUT: Final = 5.0
