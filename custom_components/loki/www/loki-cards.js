/**
 * Loki dashboard cards.
 *
 * Two of them, both configured by picking a door from a dropdown -- no YAML. The
 * integration serves this file itself and registers it as a frontend module, so
 * there is no Lovelace resource for anyone to add or to forget.
 *
 * Written as plain custom elements with no build step and no dependencies. A card
 * that needs bundling is a card that stops working the day its toolchain does, and
 * everything here is either standard DOM or an element Home Assistant already ships.
 *
 * The camera picture itself is not reimplemented: both cards compose Home Assistant's
 * own `picture-entity` through `loadCardHelpers`. That is what makes the live view
 * work at all -- HLS, WebRTC and go2rtc negotiation live behind that card -- and it
 * means an upgrade that changes how streaming works changes nothing here.
 */

const CARD_VERSION = "0.1.1";

// Stills cost one HTTP request every few seconds; a live stream costs a decoder and a
// socket for as long as it is open. With twenty doors on an account, "show me
// everything live" has to be a deliberate act with an end to it.
const DEFAULT_LIVE_TIMEOUT = 180;

// How often the fallback snapshot is re-fetched. Matches the camera's own frame
// interval: asking faster only costs the backend requests it will answer from cache.
const FALLBACK_REFRESH = 10;

const t = {
  open: "Открыть",
  live: "Смотреть вживую",
  stop: "Остановить видео",
  liveAll: "Показать все вживую",
  stopAll: "Остановить все",
  ringing: "Вызов",
  unavailable: "Живое видео недоступно — показан снимок",
  noCamera: "У этой двери нет камеры",
  pickDoor: "Выберите домофон в настройках карточки",
  noDoors: "Домофоны не найдены. Проверьте, что интеграция Loki настроена.",
  opening: "Открываю…",
  opened: "Открыто",
  failed: "Не удалось",
};

/* ------------------------------------------------------------------ helpers */

/**
 * Everything one door owns, worked out from its camera entity.
 *
 * Only the camera is stored in the card config. The open button and the call sensor
 * are found through the device they share, so renaming an entity cannot silently
 * detach the button from the picture above it.
 */
function resolveDoor(hass, cameraId) {
  const out = { camera: cameraId, button: null, call: null, name: cameraId };

  const state = hass.states[cameraId];
  if (state) {
    out.name = state.attributes.friendly_name || cameraId;
  }

  const registry = hass.entities || {};
  const deviceId = registry[cameraId] ? registry[cameraId].device_id : null;
  if (!deviceId) {
    return out;
  }
  out.device = deviceId;

  for (const [entityId, entry] of Object.entries(registry)) {
    if (entry.device_id !== deviceId || entry.platform !== "loki") continue;
    if (entityId.startsWith("button.")) out.button = entityId;
    if (entityId.startsWith("binary_sensor.")) out.call = entityId;
  }

  // The door's own name is nicer than the camera entity's, and it is what the person
  // looking at a wall of tiles is trying to read.
  const device = hass.devices ? hass.devices[deviceId] : null;
  if (device && (device.name_by_user || device.name)) {
    out.name = device.name_by_user || device.name;
  }
  return out;
}

/**
 * The account-level "can the video host be reached at all" sensor.
 *
 * It hangs off the account device rather than any door, so it is found by looking for
 * the service device rather than by matching an entity id -- ids are derived from
 * names here, and names change.
 */
function findStreamSensor(hass) {
  const registry = hass.entities || {};
  const devices = hass.devices || {};
  for (const [entityId, entry] of Object.entries(registry)) {
    if (entry.platform !== "loki" || !entityId.startsWith("binary_sensor.")) continue;
    const device = devices[entry.device_id];
    if (device && device.entry_type === "service") return entityId;
  }
  return null;
}

/** Every Loki camera that also has an open button, i.e. every door with a picture. */
function findDoorCameras(hass) {
  const registry = hass.entities || {};
  const byDevice = {};
  for (const [entityId, entry] of Object.entries(registry)) {
    if (entry.platform !== "loki" || !entry.device_id) continue;
    const bucket = (byDevice[entry.device_id] = byDevice[entry.device_id] || {});
    if (entityId.startsWith("camera.")) bucket.camera = entityId;
    if (entityId.startsWith("button.")) bucket.button = entityId;
  }
  return Object.values(byDevice)
    .filter((bucket) => bucket.camera && bucket.button)
    .map((bucket) => bucket.camera)
    .sort();
}

function fire(node, type, detail) {
  node.dispatchEvent(
    new CustomEvent(type, { detail, bubbles: true, composed: true })
  );
}

/* --------------------------------------------------------------- the picture */

/**
 * One camera picture, still or live, with an overlay we own.
 *
 * The picture is a composed `picture-entity`; the overlay is ours. Composing rather
 * than embedding an <img> is deliberate: the live path is where all the difficulty
 * is, and Home Assistant already solves it.
 */
class DoorMedia {
  constructor() {
    this.el = document.createElement("div");
    this.el.className = "loki-media";
    this._child = null;
    this._live = null;
    this._hass = null;
    this._camera = null;
    this._token = 0;
    this._fallbackTimer = null;

    this._fallback = document.createElement("img");
    this._fallback.className = "loki-fallback";
    this._fallback.alt = "";
    this._fallback.hidden = true;
    this.el.appendChild(this._fallback);
  }

  setHass(hass) {
    this._hass = hass;
    if (this._child) this._child.hass = hass;
    this._refreshFallback();
  }

  /** Render this camera, live or not. Rebuilds only when something actually changes. */
  async render(camera, live) {
    if (this._camera === camera && this._live === live) {
      this._refreshFallback();
      return;
    }
    this._camera = camera;
    this._live = live;

    // Guards against an out-of-order await: two quick toggles must not leave the
    // slower one's element on screen.
    const token = ++this._token;

    let helpers = null;
    try {
      helpers = await window.loadCardHelpers();
    } catch (err) {
      helpers = null;
    }
    if (token !== this._token) return;

    if (this._child) {
      this._child.remove();
      this._child = null;
    }

    if (!helpers || !camera) {
      this._useFallback();
      return;
    }

    try {
      const child = helpers.createCardElement({
        type: "picture-entity",
        entity: camera,
        camera_image: camera,
        // "auto" and "live" are the only values picture-entity defines.
        // "image" happens to work because everything that is not "live" is
        // treated as a still, but that is an accident to lean on, not a
        // contract.
        camera_view: live ? "live" : "auto",
        show_name: false,
        show_state: false,
        // The card's own tap opens more-info, which fights with our buttons.
        tap_action: { action: "none" },
        hold_action: { action: "none" },
      });
      child.classList.add("loki-picture");
      if (this._hass) child.hass = this._hass;
      this.el.insertBefore(child, this.el.firstChild);
      this._child = child;
      this._fallback.hidden = true;
      if (this._fallbackTimer) {
        window.clearInterval(this._fallbackTimer);
        this._fallbackTimer = null;
      }
    } catch (err) {
      this._useFallback();
    }
  }

  /**
   * Last resort: the plain snapshot endpoint.
   *
   * Reached only if card helpers are unavailable or picture-entity refuses the
   * config. A door that shows a still picture is far better than a card that shows
   * an error, because the snapshot keeps working even when the video host does not.
   */
  _useFallback() {
    this._fallback.hidden = false;
    this._refreshFallback();
    this._startFallbackTimer();
  }

  _refreshFallback() {
    if (this._fallback.hidden || !this._hass || !this._camera) return;
    const state = this._hass.states[this._camera];
    const picture = state && state.attributes.entity_picture;
    if (!picture) return;
    // A camera's entity_picture only changes when its access token rotates, which is
    // a matter of minutes -- so the URL alone would leave a frozen frame on screen.
    // The cache buster is what makes this a live-ish snapshot rather than a photo of
    // whenever the card happened to load.
    const stamp = Math.floor(Date.now() / (FALLBACK_REFRESH * 1000));
    const src = `${picture}${picture.includes("?") ? "&" : "?"}_=${stamp}`;
    if (this._fallback.getAttribute("src") !== src) {
      this._fallback.setAttribute("src", src);
    }
  }

  /** Keep the snapshot moving while the composed card is unavailable. */
  _startFallbackTimer() {
    if (this._fallbackTimer) return;
    this._fallbackTimer = window.setInterval(
      () => this._refreshFallback(),
      FALLBACK_REFRESH * 1000
    );
  }

  destroy() {
    this._token++;
    if (this._fallbackTimer) {
      window.clearInterval(this._fallbackTimer);
      this._fallbackTimer = null;
    }
    if (this._child) {
      this._child.remove();
      this._child = null;
    }
    this._camera = null;
    this._live = null;
  }
}

/* ------------------------------------------------------------- shared styles */

const STYLE = `
  .loki-media {
    position: relative;
    overflow: hidden;
    background: var(--secondary-background-color, #222);
    border-radius: var(--ha-card-border-radius, 12px);
    min-height: 90px;
  }
  .loki-picture, .loki-fallback { display: block; width: 100%; }
  .loki-picture { --ha-card-border-radius: 0; }
  .loki-picture ha-card {
    box-shadow: none;
    border: none;
    background: none;
    border-radius: 0;
  }
  .loki-fallback { object-fit: cover; }

  .loki-overlay {
    position: absolute;
    left: 0; right: 0; bottom: 0;
    display: flex;
    gap: 8px;
    align-items: center;
    justify-content: flex-end;
    padding: 8px;
    background: linear-gradient(transparent, rgba(0, 0, 0, 0.55));
    pointer-events: none;
  }
  .loki-overlay > * { pointer-events: auto; }

  .loki-btn {
    font: inherit;
    font-size: 14px;
    color: #fff;
    background: rgba(0, 0, 0, 0.55);
    border: 1px solid rgba(255, 255, 255, 0.25);
    border-radius: 8px;
    padding: 6px 14px;
    cursor: pointer;
    backdrop-filter: blur(2px);
  }
  .loki-btn:hover { background: rgba(0, 0, 0, 0.72); }
  .loki-btn[disabled] { opacity: 0.6; cursor: default; }
  .loki-btn.primary {
    background: var(--primary-color, #03a9f4);
    border-color: transparent;
  }
  .loki-btn.busy { opacity: 0.7; }

  .loki-ribbon {
    position: absolute;
    top: 0; left: 0; right: 0;
    padding: 4px 10px;
    font-size: 12px;
    color: #fff;
    background: rgba(180, 30, 30, 0.85);
    text-align: center;
  }
  .loki-ringing {
    position: absolute;
    top: 8px; left: 8px;
    padding: 3px 10px;
    font-size: 12px;
    font-weight: 600;
    color: #fff;
    border-radius: 999px;
    background: var(--error-color, #db4437);
    animation: loki-pulse 1.2s ease-in-out infinite;
  }
  @keyframes loki-pulse { 50% { opacity: 0.45; } }

  .loki-title { font-weight: 500; }
  .loki-empty {
    padding: 16px;
    color: var(--secondary-text-color);
  }
`;

/* ------------------------------------------------------------ the door card */

class LokiDoorCard extends HTMLElement {
  static getConfigElement() {
    return document.createElement("loki-door-card-editor");
  }

  static getStubConfig(hass) {
    const doors = findDoorCameras(hass);
    return { camera: doors[0] || "" };
  }

  constructor() {
    super();
    this._config = null;
    this._hass = null;
    this._live = false;
    this._built = false;
    this._liveTimer = null;
  }

  setConfig(config) {
    // Deliberately not a throw. The card picker renders a live preview from
    // getStubConfig, and on an install with no doors yet that would put a red
    // error tile in the picker instead of the card the user is looking for.
    this._config = { live_timeout: DEFAULT_LIVE_TIMEOUT, ...config };
    if (this._built) {
      this._media.destroy();
      this._built = false;
      this.innerHTML = "";
    }
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._built) this._build();
    this._media.setHass(hass);
    this._update();
  }

  getCardSize() {
    return 4;
  }

  /** Sizing for sections views, which is what new dashboards use. */
  getGridOptions() {
    return { columns: 12, rows: 4, min_columns: 6, min_rows: 3 };
  }

  disconnectedCallback() {
    // Leaving a live stream running behind a dashboard nobody is looking at is the
    // one thing a card like this must not do.
    this._stopLive();
    if (this._media) this._media.destroy();
  }

  _build() {
    const style = document.createElement("style");
    style.textContent = STYLE;

    const card = document.createElement("ha-card");
    this._media = new DoorMedia();

    this._ribbon = document.createElement("div");
    this._ribbon.className = "loki-ribbon";
    this._ribbon.textContent = t.unavailable;
    this._ribbon.hidden = true;

    this._ringing = document.createElement("div");
    this._ringing.className = "loki-ringing";
    this._ringing.textContent = t.ringing;
    this._ringing.hidden = true;

    const overlay = document.createElement("div");
    overlay.className = "loki-overlay";

    this._liveBtn = document.createElement("button");
    this._liveBtn.className = "loki-btn";
    this._liveBtn.addEventListener("click", () => this._toggleLive());

    this._openBtn = document.createElement("button");
    this._openBtn.className = "loki-btn primary";
    this._openBtn.textContent = t.open;
    this._openBtn.addEventListener("click", () => this._open());

    overlay.append(this._liveBtn, this._openBtn);
    this._media.el.append(this._ribbon, this._ringing, overlay);

    const header = document.createElement("div");
    header.className = "card-header loki-title";
    this._header = header;

    card.append(header, this._media.el);
    this.append(style, card);
    this._built = true;
  }

  _update() {
    const hass = this._hass;
    if (!this._config.camera) {
      this._header.textContent = t.pickDoor;
      this._media.el.hidden = true;
      return;
    }
    this._media.el.hidden = false;
    const door = resolveDoor(hass, this._config.camera);
    this._door = door;

    this._header.textContent = this._config.name || door.name;
    this._openBtn.hidden = !door.button;
    this._liveBtn.textContent = this._live ? t.stop : t.live;

    const call = door.call ? hass.states[door.call] : null;
    this._ringing.hidden = !(call && call.state === "on");

    const sensorId = this._config.stream_sensor || findStreamSensor(hass);
    const sensor = sensorId ? hass.states[sensorId] : null;
    // Only ever a claim that video is DOWN. An unknown sensor says nothing, and a
    // red banner over a working picture is worse than no banner at all.
    this._ribbon.hidden = !(sensor && sensor.state === "off");

    this._media.render(this._config.camera, this._live);
  }

  _toggleLive() {
    this._live = !this._live;
    if (this._live) {
      const seconds = Number(this._config.live_timeout) || 0;
      if (seconds > 0) {
        this._liveTimer = window.setTimeout(() => this._stopLive(), seconds * 1000);
      }
    } else {
      this._stopLive();
    }
    if (this._hass) this._update();
  }

  _stopLive() {
    if (this._liveTimer) {
      window.clearTimeout(this._liveTimer);
      this._liveTimer = null;
    }
    if (!this._live) return;
    this._live = false;
    if (this._hass && this._built) this._update();
  }

  async _open() {
    await pressOpen(this._hass, this._door, this._openBtn);
  }
}

/* ------------------------------------------------------------ the wall card */

class LokiWallCard extends HTMLElement {
  static getConfigElement() {
    return document.createElement("loki-wall-card-editor");
  }

  static getStubConfig(hass) {
    return { cameras: findDoorCameras(hass), columns: 2 };
  }

  constructor() {
    super();
    this._config = null;
    this._hass = null;
    this._live = false;
    this._tiles = new Map();
    this._built = false;
    this._liveTimer = null;
  }

  setConfig(config) {
    this._config = {
      title: "Домофоны",
      columns: 2,
      live_timeout: DEFAULT_LIVE_TIMEOUT,
      cameras: [],
      ...config,
    };
    if (this._built) {
      this._teardown();
      this.innerHTML = "";
      this._built = false;
    }
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._built) this._build();
    this._update();
  }

  getCardSize() {
    const count = this._cameras().length;
    return 2 + Math.ceil(count / (Number(this._config.columns) || 2)) * 4;
  }

  /** Sizing for sections views. A wall of doors wants the full width. */
  getGridOptions() {
    const rows = Math.ceil(
      this._cameras().length / (Number(this._config.columns) || 2)
    );
    return {
      columns: 'full',
      rows: Math.max(4, rows * 4 + 1),
      min_columns: 6,
      min_rows: 4,
    };
  }

  disconnectedCallback() {
    this._stopLive();
    this._teardown();
  }

  /** Configured cameras, or every door when the card has not been narrowed down. */
  _cameras() {
    const configured = this._config && this._config.cameras;
    if (configured && configured.length) return configured;
    return this._hass ? findDoorCameras(this._hass) : [];
  }

  _build() {
    const style = document.createElement("style");
    style.textContent = `${STYLE}
      .loki-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        padding: 12px 16px;
      }
      .loki-header .loki-title { font-size: 18px; }
      .loki-grid {
        display: grid;
        gap: 10px;
        padding: 0 12px 12px;
      }
      .loki-tile { display: flex; flex-direction: column; gap: 6px; }
      .loki-tile .loki-name {
        font-size: 14px;
        color: var(--primary-text-color);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .loki-tile .loki-btn {
        color: var(--text-primary-color, #fff);
        background: var(--primary-color, #03a9f4);
        border-color: transparent;
        width: 100%;
      }
      .loki-solo {
        font-size: 14px;
        background: rgba(0, 0, 0, 0.55);
        border: 1px solid rgba(255, 255, 255, 0.25);
        color: #fff;
      }
    `;

    const card = document.createElement("ha-card");

    const header = document.createElement("div");
    header.className = "loki-header";
    this._titleEl = document.createElement("div");
    this._titleEl.className = "loki-title";
    this._liveBtn = document.createElement("button");
    this._liveBtn.className = "loki-btn primary";
    this._liveBtn.addEventListener("click", () => this._toggleLive());
    header.append(this._titleEl, this._liveBtn);

    this._ribbon = document.createElement("div");
    this._ribbon.className = "loki-ribbon";
    this._ribbon.style.position = "static";
    this._ribbon.textContent = t.unavailable;
    this._ribbon.hidden = true;

    this._grid = document.createElement("div");
    this._grid.className = "loki-grid";

    this._empty = document.createElement("div");
    this._empty.className = "loki-empty";
    this._empty.textContent = t.noDoors;
    this._empty.hidden = true;

    card.append(header, this._ribbon, this._empty, this._grid);
    this.append(style, card);
    this._built = true;
  }

  _update() {
    const hass = this._hass;
    const cameras = this._cameras();

    this._titleEl.textContent = this._config.title;
    this._liveBtn.textContent = this._live ? t.stopAll : t.liveAll;
    this._liveBtn.hidden = cameras.length === 0;
    this._empty.hidden = cameras.length > 0;

    const columns = Math.max(1, Number(this._config.columns) || 2);
    this._grid.style.gridTemplateColumns = `repeat(${columns}, minmax(0, 1fr))`;

    const sensorId = this._config.stream_sensor || findStreamSensor(hass);
    const sensor = sensorId ? hass.states[sensorId] : null;
    this._ribbon.hidden = !(sensor && sensor.state === "off");

    // Drop tiles for cameras that are no longer configured, so their streams stop.
    for (const [camera, tile] of this._tiles) {
      if (!cameras.includes(camera)) {
        tile.media.destroy();
        tile.root.remove();
        this._tiles.delete(camera);
      }
    }

    cameras.forEach((camera, index) => {
      let tile = this._tiles.get(camera);
      if (!tile) {
        tile = this._buildTile(camera);
        this._tiles.set(camera, tile);
      }
      // Keep DOM order in step with the configured order, which the editor lets the
      // user rearrange.
      if (this._grid.children[index] !== tile.root) {
        this._grid.insertBefore(tile.root, this._grid.children[index] || null);
      }

      const door = resolveDoor(hass, camera);
      tile.door = door;
      tile.name.textContent = door.name;
      tile.open.hidden = !door.button;

      const call = door.call ? hass.states[door.call] : null;
      tile.ringing.hidden = !(call && call.state === "on");

      tile.media.setHass(hass);
      tile.media.render(camera, this._live);
    });
  }

  _buildTile(camera) {
    const root = document.createElement("div");
    root.className = "loki-tile";

    const media = new DoorMedia();

    const ringing = document.createElement("div");
    ringing.className = "loki-ringing";
    ringing.textContent = t.ringing;
    ringing.hidden = true;

    const overlay = document.createElement("div");
    overlay.className = "loki-overlay";
    const solo = document.createElement("button");
    solo.className = "loki-btn loki-solo";
    solo.textContent = t.live;
    overlay.appendChild(solo);
    media.el.append(ringing, overlay);

    const name = document.createElement("div");
    name.className = "loki-name";

    const open = document.createElement("button");
    open.className = "loki-btn";
    open.textContent = t.open;

    const tile = { root, media, name, open, ringing, solo, door: null };

    solo.addEventListener("click", () => {
      // One tile live on its own: the usual case is "I can see somebody on this
      // thumbnail, show me that one properly" without paying for twenty streams.
      tile.solo.textContent = tile.soloLive ? t.live : t.stop;
      tile.soloLive = !tile.soloLive;
      media.render(camera, this._live || tile.soloLive);
    });
    open.addEventListener("click", () => pressOpen(this._hass, tile.door, open));

    root.append(media.el, name, open);
    return tile;
  }

  _toggleLive() {
    this._live = !this._live;
    if (this._live) {
      const seconds = Number(this._config.live_timeout) || 0;
      if (seconds > 0) {
        this._liveTimer = window.setTimeout(() => this._stopLive(), seconds * 1000);
      }
    } else {
      this._stopLive();
    }
    if (this._hass) this._update();
  }

  _stopLive() {
    if (this._liveTimer) {
      window.clearTimeout(this._liveTimer);
      this._liveTimer = null;
    }
    if (!this._live) return;
    this._live = false;
    if (this._hass && this._built) this._update();
  }

  _teardown() {
    for (const tile of this._tiles.values()) tile.media.destroy();
    this._tiles.clear();
  }
}

/* ------------------------------------------------------------------ opening */

/**
 * Press a door's open button and say what happened, on the button itself.
 *
 * A door opens out of sight of whoever pressed it, so silence is indistinguishable
 * from failure. The integration raises a real error when the service refuses, and
 * that has to reach the person standing at the dashboard.
 */
async function pressOpen(hass, door, button) {
  if (!hass || !door || !door.button || button.disabled) return;
  const label = button.textContent;
  button.disabled = true;
  button.classList.add("busy");
  button.textContent = t.opening;
  try {
    await hass.callService("button", "press", { entity_id: door.button });
    button.textContent = t.opened;
  } catch (err) {
    button.textContent = t.failed;
    fire(button, "hass-notification", {
      message: `${door.name}: ${(err && err.message) || t.failed}`,
    });
  } finally {
    window.setTimeout(() => {
      button.disabled = false;
      button.classList.remove("busy");
      button.textContent = label;
    }, 2000);
  }
}

/* ------------------------------------------------------------------ editors */

/**
 * Base editor: an ha-form driven by a schema, so the door is a dropdown and the
 * entity list is the same picker Home Assistant uses everywhere else.
 */
class LokiEditorBase extends HTMLElement {
  setConfig(config) {
    this._config = config || {};
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _render() {
    if (!this._hass || !this._config) return;
    if (!this._form) {
      this._form = document.createElement("ha-form");
      this._form.addEventListener("value-changed", (event) => {
        event.stopPropagation();
        fire(this, "config-changed", { config: event.detail.value });
      });
      this.appendChild(this._form);
    }
    this._form.hass = this._hass;
    this._form.schema = this.schema();
    this._form.data = this._config;
    this._form.computeLabel = (item) => this.label(item.name);
  }

  label(name) {
    return (
      {
        camera: "Домофон",
        cameras: "Какие домофоны показывать",
        name: "Заголовок (необязательно)",
        title: "Заголовок",
        columns: "Плиток в ряд",
        live_timeout: "Сам выключить видео через, с",
      }[name] || name
    );
  }
}

class LokiDoorCardEditor extends LokiEditorBase {
  schema() {
    return [
      {
        name: "camera",
        required: true,
        selector: { entity: { filter: { integration: "loki", domain: "camera" } } },
      },
      { name: "name", selector: { text: {} } },
      {
        name: "live_timeout",
        selector: { number: { min: 0, max: 3600, step: 30, mode: "box" } },
      },
    ];
  }
}

class LokiWallCardEditor extends LokiEditorBase {
  schema() {
    return [
      { name: "title", selector: { text: {} } },
      {
        name: "cameras",
        selector: {
          entity: {
            multiple: true,
            filter: { integration: "loki", domain: "camera" },
          },
        },
      },
      {
        name: "columns",
        selector: { number: { min: 1, max: 6, step: 1, mode: "box" } },
      },
      {
        name: "live_timeout",
        selector: { number: { min: 0, max: 3600, step: 30, mode: "box" } },
      },
    ];
  }
}

/* ------------------------------------------------------------- registration */

customElements.define("loki-door-card", LokiDoorCard);
customElements.define("loki-wall-card", LokiWallCard);
customElements.define("loki-door-card-editor", LokiDoorCardEditor);
customElements.define("loki-wall-card-editor", LokiWallCardEditor);

window.customCards = window.customCards || [];
window.customCards.push(
  {
    type: "loki-door-card",
    name: "Loki — домофон",
    description:
      "Картинка с домофона, кнопка «Открыть» и предупреждение, когда живое видео недоступно.",
    preview: true,
    documentationURL:
      "https://github.com/william-aqn/home-assistant-domofon-hacs",
  },
  {
    type: "loki-wall-card",
    name: "Loki — все домофоны",
    description:
      "Плитки всех домофонов с кнопкой открытия под каждой. По кнопке — живое видео сразу со всех.",
    preview: true,
    documentationURL:
      "https://github.com/william-aqn/home-assistant-domofon-hacs",
  }
);

console.info(
  `%c LOKI CARDS %c ${CARD_VERSION} `,
  "color: white; background: #3f51b5; font-weight: 700;",
  "color: #3f51b5; background: white; font-weight: 700;"
);
