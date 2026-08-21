"""Serve the dashboard cards from the integration itself.

The alternative is asking every user to install a second HACS repository and then add
a Lovelace resource by hand -- three chances to get it wrong before the first card
appears. Registering the module here means the cards show up in "Add card" as soon as
the integration is installed, and nothing has to be maintained in the dashboard's
resource list.

Both calls happen once per Home Assistant start, from ``async_setup``. That is not a
detail: ``async_register_static_paths`` adds a route each time it is called, so doing
this per config entry would stack duplicate routes on every reload.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

URL_BASE: Final = f"/{DOMAIN}_cards"
CARDS_FILE: Final = "loki-cards.js"

# Set once the module has been registered, so a second call is a no-op rather than a
# duplicate route.
DATA_REGISTERED: Final = f"{DOMAIN}_frontend_registered"


async def async_register_cards(hass: HomeAssistant, version: str) -> None:
    """Serve the card bundle and ask the frontend to load it.

    Failure is not fatal. Everything the integration does -- cameras, buttons,
    notifications, SIP -- works without a single card, and a headless install has no
    frontend to register with at all.
    """
    if hass.data.get(DATA_REGISTERED):
        return

    source = Path(__file__).parent / "www" / CARDS_FILE
    if not source.is_file():
        _LOGGER.warning("Файл карточек не найден: %s", source)
        return

    try:
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(
                    f"{URL_BASE}/{CARDS_FILE}",
                    str(source),
                    # Cached hard, and busted by the version in the query string
                    # below. Without a cache buster an upgrade leaves the browser
                    # running the previous card forever.
                    cache_headers=True,
                )
            ]
        )
    except (RuntimeError, ValueError) as err:
        _LOGGER.warning("Не удалось отдать файл карточек: %s", err)
        return

    hass.data[DATA_REGISTERED] = True

    try:
        from homeassistant.components.frontend import add_extra_js_url
    except ImportError:
        _LOGGER.debug("Фронтенд не установлен — карточки не регистрируются")
        return

    # es5=False puts it in extra_module_url, i.e. loaded as an ES module, which is
    # what a modern custom element needs.
    add_extra_js_url(hass, f"{URL_BASE}/{CARDS_FILE}?v={version}")
    _LOGGER.debug("Карточки Loki зарегистрированы (%s)", version)
