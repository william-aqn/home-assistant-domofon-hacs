"""Serve the dashboard cards from the integration itself.

The alternative is asking every user to install a second HACS repository and then add
a Lovelace resource by hand -- three chances to get it wrong before the first card
appears. Registering the module here means the cards show up in "Add card" as soon as
the integration is installed.

**Registered as a Lovelace resource, not only as an extra module URL.** This is the
part that took a while to get right. ``add_extra_js_url`` injects an ``import()`` into
the page that races the app: whenever the file has to come off the network -- a first
install, a version bump, a cleared cache -- Lovelace can reach the card before the
module has defined it, and renders "Custom element doesn't exist" instead. Home
Assistant retries after two seconds, which usually but not always saves it. Lovelace
*awaits* its resources before rendering any card, so the race cannot happen there.
The extra module URL stays as the fallback for the YAML resource mode, where the
resource list is the user's file and not ours to write to.

Registration happens once per Home Assistant run, from ``async_setup``. That is not a
detail: ``async_register_static_paths`` adds a route every time it is called, a repeat
registration of the same file raises outright, and a static path can never be taken
back -- aiohttp's router has no removal API at all.

``frontend`` is a hard dependency in the manifest rather than an ordering hint, because
``add_extra_js_url`` writes to a ``hass.data`` key that only frontend's own setup
creates. Without frontend the call raises ``KeyError`` -- not the ``ImportError`` a
guard here would plausibly be written to catch.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Final

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant, callback
from homeassistant.setup import async_when_setup

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

URL_BASE: Final = f"/{DOMAIN}_cards"
CARDS_FILE: Final = "loki-cards.js"
CARDS_URL: Final = f"{URL_BASE}/{CARDS_FILE}"

# The sidebar page. Registered through panel_custom rather than by creating a Lovelace
# dashboard: an integration has no supported way to create one, and writing into
# Lovelace's own storage behind its back would risk losing the user's dashboards the
# next time it saves its in-memory copy.
# Short and unlikely to be taken: a dashboard the user made themselves owns its own
# url_path, and panel_custom refuses to overwrite one. Losing that race means no page,
# so the path should not be a name somebody would plausibly pick for a dashboard.
PANEL_URL_PATH: Final = "loki"
PANEL_COMPONENT: Final = "loki-panel"
DATA_PANEL: Final = f"{DOMAIN}_panel_registered"

# Set once the module has been registered, so a second call is a no-op rather than a
# duplicate route.
DATA_REGISTERED: Final = f"{DOMAIN}_frontend_registered"


async def async_register_cards(hass: HomeAssistant, version: str) -> None:
    """Serve the card bundle and make sure the frontend loads it.

    A failure here is not fatal. Everything the integration does -- cameras, buttons,
    notifications, SIP -- works without a single card.
    """
    if hass.data.get(DATA_REGISTERED):
        return

    folder = Path(__file__).parent / "www"
    if not await hass.async_add_executor_job((folder / CARDS_FILE).is_file):
        _LOGGER.warning("Файл карточек не найден: %s", folder / CARDS_FILE)
        return

    try:
        # The folder, not the single file: registering one file twice raises, and this
        # shape stays correct if the bundle ever grows a second file.
        await hass.http.async_register_static_paths(
            [StaticPathConfig(URL_BASE, str(folder), cache_headers=True)]
        )
    except (RuntimeError, ValueError) as err:
        _LOGGER.warning("Не удалось отдать файл карточек: %s", err)
        return

    hass.data[DATA_REGISTERED] = True

    # The version is the only cache buster there is. The file is served with a month of
    # Cache-Control and no revalidation, so a card change that does not bump the
    # manifest version never reaches a browser that has already loaded it.
    url = f"{CARDS_URL}?v={version}"

    async def _register(hass: HomeAssistant, _component: str) -> None:
        if await _async_register_resource(hass, url):
            return
        # YAML resource mode, or Lovelace unavailable: fall back to the module URL and
        # accept the race. es5=False puts it in extra_module_url, i.e. loaded as an ES
        # module, which is what a custom element needs.
        add_extra_js_url(hass, url)
        _LOGGER.debug("Карточки Loki подключены как модуль (%s)", version)

    # Lovelace may well not be set up yet when this runs.
    async_when_setup(hass, "lovelace", _register)


async def _async_register_resource(hass: HomeAssistant, url: str) -> bool:
    """Add or update our Lovelace resource. False if that is not possible here."""
    try:
        from homeassistant.components.lovelace import LOVELACE_DATA
        from homeassistant.components.lovelace.resources import (
            ResourceStorageCollection,
        )
    except ImportError:
        return False

    data = hass.data.get(LOVELACE_DATA)
    resources = getattr(data, "resources", None)
    # In YAML resource mode the list is a file the user owns; writing to it is neither
    # possible nor ours to do.
    if not isinstance(resources, ResourceStorageCollection):
        return False

    try:
        await resources.async_get_info()  # loads the collection if it has not been

        existing: dict[str, Any] | None = None
        for item in resources.async_items():
            if str(item.get("url", "")).split("?")[0] == CARDS_URL:
                existing = item
                break

        if existing is None:
            await resources.async_create_item({"res_type": "module", "url": url})
            _LOGGER.debug("Карточки Loki добавлены как ресурс Lovelace")
        elif existing.get("url") != url:
            # Same file, new version: update in place rather than accumulating one
            # dead resource per release.
            await resources.async_update_item(existing["id"], {"url": url})
            _LOGGER.debug("Ресурс карточек Loki обновлён до %s", url)
    except Exception:
        _LOGGER.exception("Не удалось зарегистрировать ресурс Lovelace")
        return False
    return True


async def async_remove_resource(hass: HomeAssistant) -> None:
    """Take our Lovelace resource back out, when the last account is removed.

    The static route cannot be unregistered -- aiohttp has no API for it -- but the
    resource can, and leaving it behind means every dashboard keeps trying to load a
    file that will 404 after the next restart.
    """
    try:
        from homeassistant.components.lovelace import LOVELACE_DATA
        from homeassistant.components.lovelace.resources import (
            ResourceStorageCollection,
        )
    except ImportError:
        return

    resources = getattr(hass.data.get(LOVELACE_DATA), "resources", None)
    if not isinstance(resources, ResourceStorageCollection):
        return

    try:
        await resources.async_get_info()
        for item in list(resources.async_items()):
            if str(item.get("url", "")).split("?")[0] == CARDS_URL:
                await resources.async_delete_item(item["id"])
    except Exception:
        _LOGGER.exception("Не удалось убрать ресурс Lovelace")


async def async_register_panel(hass: HomeAssistant, version: str) -> None:
    """Add the Loki page to the sidebar, once."""
    if hass.data.get(DATA_PANEL):
        return
    try:
        from homeassistant.components import panel_custom
    except ImportError:
        return

    try:
        await panel_custom.async_register_panel(
            hass,
            frontend_url_path=PANEL_URL_PATH,
            webcomponent_name=PANEL_COMPONENT,
            sidebar_title="Домофоны",
            sidebar_icon="mdi:door-closed-lock",
            module_url=f"{CARDS_URL}?v={version}",
            embed_iframe=False,
            require_admin=False,
        )
    except ValueError as err:
        # Raised when the url path is already taken -- by a dashboard the user made
        # with the same name, most likely. Theirs wins.
        _LOGGER.warning("Не удалось добавить страницу «Домофоны»: %s", err)
        return
    hass.data[DATA_PANEL] = True
    _LOGGER.debug("Страница Loki добавлена в боковую панель")


@callback
def async_remove_panel(hass: HomeAssistant) -> None:
    """Take the page back out of the sidebar."""
    if not hass.data.pop(DATA_PANEL, None):
        return
    from homeassistant.components import frontend

    frontend.async_remove_panel(hass, PANEL_URL_PATH)
