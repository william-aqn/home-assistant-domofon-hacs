"""Serve the dashboard cards from the integration itself.

The alternative is asking every user to install a second HACS repository and then add
a Lovelace resource by hand -- three chances to get it wrong before the first card
appears. Registering the module here means the cards show up in "Add card" as soon as
the integration is installed, and nothing has to be maintained in the dashboard's
resource list.

Both calls happen once per Home Assistant start, from ``async_setup``. That is not a
detail: ``async_register_static_paths`` adds a route every time it is called, a repeat
registration of the same file raises outright, and a static path can never be taken
back afterwards -- aiohttp's router has no removal API at all.

``frontend`` is a hard dependency in the manifest rather than an ordering hint, because
``add_extra_js_url`` writes to a ``hass.data`` key that only frontend's own setup
creates. Without frontend the call raises ``KeyError`` -- not the ``ImportError`` a
guard here would plausibly be written to catch.

Two consequences worth knowing before somebody reports them as bugs: the cards do not
load in safe mode, because Home Assistant blanks the extra-module list there, and they
do not load under Home Assistant Cast, which never fetches the page they are injected
into.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Final

from homeassistant.components.frontend import add_extra_js_url
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
    #
    # es5=False puts the URL in extra_module_url, i.e. loaded as an ES module, which is
    # what a custom element needs.
    add_extra_js_url(hass, f"{URL_BASE}/{CARDS_FILE}?v={version}")
    _LOGGER.debug("Карточки Loki зарегистрированы (%s)", version)
