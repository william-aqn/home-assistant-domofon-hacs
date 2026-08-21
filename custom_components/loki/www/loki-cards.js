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
 * **Stills are ours, live video is Home Assistant's.** A still is one `<img>` pointed
 * at the snapshot endpoint: cheap, reliable, and it gives us the layout we want.
 * Composing `picture-entity` for the still was tried and was worse on both counts --
 * it drew an empty box, and because the camera advertises a stream it dragged the
 * streaming machinery in behind it, which on an account whose video host is
 * unreachable means twenty doomed RTSP attempts and a wall of red in the log. Live
 * view still composes `picture-entity`, because HLS and WebRTC are genuinely hard and
 * already solved; it is simply no longer on the path you get by default.
 */

const CARD_VERSION = "0.4.0";

// Stills cost one HTTP request every few seconds; a live stream costs a decoder and a
// socket for as long as it is open. With twenty doors on an account, "show me
// everything live" has to be a deliberate act with an end to it.
//
// Thirty seconds because the job is "where is that person standing" -- long enough to
// look along a row of doors, short enough that walking away from the tablet cannot
// leave twenty streams running. Press it again for another thirty; the setting raises
// it for anyone who wants longer.
const DEFAULT_LIVE_TIMEOUT = 30;

// How often a still is re-fetched. The camera's own frame interval is 10 s, so asking
// faster only costs requests the backend answers from its own cache.
const STILL_REFRESH = 10;

const ICON_OPEN = "mdi:lock-open-variant-outline";
const ICON_LIVE = "mdi:video-outline";
const ICON_STOP = "mdi:video-off-outline";

const t = {
  open: "Открыть",
  live: "Вживую",
  stop: "Стоп",
  liveAll: "Все вживую",
  stopAll: "Остановить",
  ringing: "Вызов",
  unavailable: "Видео недоступно, показаны снимки",
  pickDoor: "Выберите домофон в настройках карточки",
  noDoors: "Домофоны не найдены. Проверьте, что интеграция Loki настроена.",
  liveBlocked: "Видеохост недоступен — живое видео не откроется",
  opening: "Открываю…",
  opened: "Открыто",
  failed: "Ошибка",
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
  if (!hass || !cameraId) return out;

  const state = hass.states[cameraId];
  if (state) out.name = state.attributes.friendly_name || cameraId;

  const registry = hass.entities || {};
  const deviceId = registry[cameraId] ? registry[cameraId].device_id : null;
  if (!deviceId) return out;
  out.device = deviceId;

  for (const [entityId, entry] of Object.entries(registry)) {
    if (entry.device_id !== deviceId || entry.platform !== "loki") continue;
    if (entityId.startsWith("button.")) out.button = entityId;
    if (entityId.startsWith("binary_sensor.")) out.call = entityId;
  }

  // The door's own name is nicer than the camera entity's, and it is what somebody
  // scanning a wall of tiles is actually trying to read.
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
 * the service device rather than by matching an entity id -- ids here are derived from
 * names, and names change.
 */
function findStreamSensor(hass) {
  const registry = (hass && hass.entities) || {};
  const devices = (hass && hass.devices) || {};
  for (const [entityId, entry] of Object.entries(registry)) {
    if (entry.platform !== "loki" || !entityId.startsWith("binary_sensor.")) continue;
    const device = devices[entry.device_id];
    if (device && device.entry_type === "service") return entityId;
  }
  return null;
}

/** False only when the integration positively reports the video host as down. */
function streamReachable(hass, configured) {
  const id = configured || findStreamSensor(hass);
  const state = id && hass ? hass.states[id] : null;
  return !(state && state.state === "off");
}

/** Every Loki camera that also has an open button, i.e. every door with a picture. */
function findDoorCameras(hass) {
  const registry = (hass && hass.entities) || {};
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
  node.dispatchEvent(new CustomEvent(type, { detail, bubbles: true, composed: true }));
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function icon(name) {
  const glyph = document.createElement("ha-icon");
  glyph.setAttribute("icon", name);
  return glyph;
}

/** A round icon button in the corner of a picture. */
function iconButton(name, label) {
  const button = el("button", "loki-icon-btn");
  button.title = label;
  button.setAttribute("aria-label", label);
  button.appendChild(icon(name));
  return button;
}

/** The gradient bar along the bottom of a picture: name on the left, open on the right. */
function pictureBar() {
  const bar = el("div", "loki-bar");
  const label = el("div", "loki-label");
  const open = el("button", "loki-open");
  const openLabel = el("span", null, t.open);
  open.append(icon(ICON_OPEN), openLabel);
  bar.append(label, open);
  return { bar, label, open, openLabel };
}

/* --------------------------------------------------------------- the picture */

/** One door's picture: our own snapshot by default, Home Assistant's live view on ask. */
class DoorMedia {
  constructor() {
    this.el = el("div", "loki-media");
    this._child = null;
    this._live = null;
    this._hass = null;
    this._camera = null;
    this._token = 0;
    this._timer = null;

    this._img = document.createElement("img");
    this._img.className = "loki-still";
    this._img.alt = "";
    this.el.appendChild(this._img);
  }

  setHass(hass) {
    this._hass = hass;
    if (this._child) this._child.hass = hass;
    if (!this._live) this._refreshStill();
  }

  async render(camera, live) {
    if (this._camera === camera && this._live === live) {
      if (!live) this._refreshStill();
      return;
    }
    this._camera = camera;
    this._live = live;
    // Guards an out-of-order await: two quick toggles must not leave the slower one's
    // element on screen.
    const token = ++this._token;

    if (this._child) {
      this._child.remove();
      this._child = null;
    }

    if (!live) {
      this._showStill();
      return;
    }

    this._stopTimer();

    let helpers = null;
    try {
      helpers = await window.loadCardHelpers();
    } catch (err) {
      helpers = null;
    }
    if (token !== this._token) return;

    if (!helpers || !camera) {
      this._live = false;
      this._showStill();
      return;
    }

    try {
      const child = helpers.createCardElement({
        type: "picture-entity",
        entity: camera,
        camera_image: camera,
        // "live" and "auto" are the only values picture-entity defines, and only
        // "live" reaches the streaming path -- which is the entire reason to compose
        // this card rather than draw the picture ourselves.
        camera_view: "live",
        show_name: false,
        show_state: false,
        tap_action: { action: "none" },
        hold_action: { action: "none" },
      });
      child.classList.add("loki-live");
      if (this._hass) child.hass = this._hass;
      this.el.insertBefore(child, this.el.firstChild);
      this._child = child;
      this._img.hidden = true;
    } catch (err) {
      this._live = false;
      this._showStill();
    }
  }

  _showStill() {
    this._img.hidden = false;
    this._refreshStill();
    this._startTimer();
  }

  _refreshStill() {
    if (this._img.hidden || !this._hass || !this._camera) return;
    const state = this._hass.states[this._camera];
    const picture = state && state.attributes.entity_picture;
    if (!picture) return;
    // entity_picture only changes when the access token rotates, a matter of minutes,
    // so the URL on its own would leave one frozen frame on screen. Only this URL is
    // safe to append to -- a signed path would fail its signature check.
    const stamp = Math.floor(Date.now() / (STILL_REFRESH * 1000));
    const src = `${picture}${picture.includes("?") ? "&" : "?"}_=${stamp}`;
    if (this._img.getAttribute("src") !== src) this._img.setAttribute("src", src);
  }

  _startTimer() {
    if (this._timer) return;
    this._timer = window.setInterval(() => this._refreshStill(), STILL_REFRESH * 1000);
  }

  _stopTimer() {
    if (!this._timer) return;
    window.clearInterval(this._timer);
    this._timer = null;
  }

  destroy() {
    this._token++;
    this._stopTimer();
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
    aspect-ratio: 16 / 9;
    background: var(--secondary-background-color, #2a2a2a);
    border-radius: 10px;
  }
  .loki-still, .loki-live { display: block; width: 100%; height: 100%; }
  .loki-still { object-fit: cover; }
  .loki-live ha-card {
    box-shadow: none; border: none; background: none; border-radius: 0;
  }

  .loki-bar {
    position: absolute;
    left: 0; right: 0; bottom: 0;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 8px;
    background: linear-gradient(transparent, rgba(0, 0, 0, 0.75));
  }
  .loki-label {
    flex: 1;
    min-width: 0;
    font-size: 13px;
    line-height: 1.25;
    color: #fff;
    text-shadow: 0 1px 3px rgba(0, 0, 0, 0.85);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .loki-open {
    flex: none;
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font: inherit;
    font-size: 12px;
    color: var(--text-primary-color, #fff);
    background: var(--primary-color, #03a9f4);
    border: none;
    border-radius: 999px;
    padding: 4px 10px 4px 7px;
    cursor: pointer;
    white-space: nowrap;
  }
  .loki-open ha-icon { --mdc-icon-size: 16px; width: 16px; height: 16px; }
  .loki-open[disabled] { opacity: 0.65; cursor: default; }

  .loki-icon-btn {
    position: absolute;
    top: 6px; right: 6px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 30px; height: 30px;
    color: #fff;
    background: rgba(0, 0, 0, 0.45);
    border: none;
    border-radius: 50%;
    cursor: pointer;
  }
  .loki-icon-btn ha-icon { --mdc-icon-size: 18px; width: 18px; height: 18px; }
  .loki-icon-btn:hover { background: rgba(0, 0, 0, 0.7); }
  .loki-icon-btn[disabled] { opacity: 0.3; cursor: default; }
  .loki-icon-btn.on { background: var(--primary-color, #03a9f4); }

  .loki-ringing {
    position: absolute;
    top: 6px; left: 6px;
    padding: 2px 9px;
    font-size: 11px;
    font-weight: 600;
    color: #fff;
    border-radius: 999px;
    background: var(--error-color, #db4437);
    animation: loki-pulse 1.2s ease-in-out infinite;
  }
  @keyframes loki-pulse { 50% { opacity: 0.4; } }

  .loki-note {
    margin: 0 12px 10px;
    padding: 5px 10px;
    font-size: 12px;
    color: var(--secondary-text-color);
    background: var(--secondary-background-color, rgba(127, 127, 127, 0.14));
    border-radius: 8px;
  }
  .loki-empty { padding: 16px; color: var(--secondary-text-color); }
`;

/* ------------------------------------------------------------ the door card */

class LokiDoorCard extends HTMLElement {
  static getConfigElement() {
    return document.createElement("loki-door-card-editor");
  }

  static getStubConfig(hass) {
    const doors = findDoorCameras(hass);
    return doors.length ? { camera: doors[0] } : {};
  }

  constructor() {
    super();
    this._config = {};
    this._hass = null;
    this._live = false;
    this._built = false;
    this._liveTimer = null;
  }

  setConfig(config) {
    // Deliberately not a throw. The picker renders a live preview from getStubConfig,
    // and on an install with no doors that would put a red error tile where the card
    // somebody came to add should be.
    this._config = { live_timeout: DEFAULT_LIVE_TIMEOUT, ...(config || {}) };
    if (this._built) {
      this._media.destroy();
      this.innerHTML = "";
      this._built = false;
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

  /** Sizing for sections views, which is what new dashboards use.

   * ``rows: "auto"`` rather than a number: the picture is 16:9 and the card knows its
   * own height far better than a guess does. Core's own cards use the same value.
   */
  getGridOptions() {
    return { columns: 12, rows: "auto", min_columns: 6, min_rows: 3 };
  }

  disconnectedCallback() {
    // A live stream left running behind a dashboard nobody is looking at is the one
    // thing a card like this must not do.
    this._stopLive();
    if (this._media) this._media.destroy();
  }

  _build() {
    const style = el("style");
    style.textContent = `${STYLE}
      .loki-body { padding: 0 12px 12px; }
      .card-header { padding-bottom: 8px; }
    `;

    const card = document.createElement("ha-card");
    this._header = el("div", "card-header");
    this._media = new DoorMedia();

    this._ringing = el("div", "loki-ringing", t.ringing);
    this._ringing.hidden = true;

    this._liveBtn = iconButton(ICON_LIVE, t.live);
    this._liveBtn.addEventListener("click", () => this._toggleLive());

    const parts = pictureBar();
    this._label = parts.label;
    this._openBtn = parts.open;
    this._openLabel = parts.openLabel;
    this._openBtn.addEventListener("click", () => this._open());

    this._media.el.append(this._ringing, this._liveBtn, parts.bar);

    this._note = el("div", "loki-note", t.unavailable);
    this._note.hidden = true;

    const body = el("div", "loki-body");
    body.appendChild(this._media.el);

    card.append(this._header, body, this._note);
    this.append(style, card);
    this._built = true;
  }

  _update() {
    const hass = this._hass;
    const camera = this._config.camera;
    if (!camera) {
      this._header.textContent = t.pickDoor;
      this._media.el.hidden = true;
      this._note.hidden = true;
      return;
    }
    this._media.el.hidden = false;

    const door = resolveDoor(hass, camera);
    this._door = door;
    this._header.textContent = this._config.name || door.name;
    // The name is already in the header; repeating it on the picture is noise.
    this._label.textContent = "";
    this._openBtn.hidden = !door.button;

    const call = door.call ? hass.states[door.call] : null;
    this._ringing.hidden = !(call && call.state === "on");

    const reachable = streamReachable(hass, this._config.stream_sensor);
    this._note.hidden = reachable;
    // Disabled rather than hidden: the button is the answer to "why can I not see
    // live video", and its tooltip says so. Letting it through would open an RTSP
    // connection already known to time out.
    this._liveBtn.disabled = !reachable;
    this._liveBtn.title = reachable ? (this._live ? t.stop : t.live) : t.liveBlocked;
    this._liveBtn.classList.toggle("on", this._live && reachable);
    this._liveBtn.firstChild.setAttribute(
      "icon",
      this._live && reachable ? ICON_STOP : ICON_LIVE
    );

    this._media.render(camera, this._live && reachable);
  }

  _toggleLive() {
    this._live = !this._live;
    if (this._live) {
      const seconds = Number(this._config.live_timeout) || 0;
      if (seconds > 0) {
        this._liveTimer = window.setTimeout(() => this._stopLive(), seconds * 1000);
      }
      if (this._hass) this._update();
    } else {
      this._stopLive();
    }
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
    await pressOpen(this._hass, this._door, this._openBtn, this._openLabel);
  }
}

/* ------------------------------------------------------------ the wall card */

class LokiWallCard extends HTMLElement {
  static getConfigElement() {
    return document.createElement("loki-wall-card-editor");
  }

  static getStubConfig(hass) {
    return { cameras: findDoorCameras(hass) };
  }

  constructor() {
    super();
    this._config = {};
    this._hass = null;
    this._live = false;
    this._tiles = new Map();
    this._built = false;
    this._liveTimer = null;
  }

  setConfig(config) {
    this._config = {
      title: "Домофоны",
      live_timeout: DEFAULT_LIVE_TIMEOUT,
      cameras: [],
      ...(config || {}),
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
    return 2 + Math.ceil(this._cameras().length / 3) * 3;
  }

  /** Sizing for sections views. A wall of doors wants the whole width.

   * ``"full"`` and not a number. The section grid is 24 columns wide here, so 12 --
   * which is what most of Home Assistant's own cards ask for -- lands at exactly half
   * the page. ``"full"`` is handled by a class of its own (``grid-column: 1 / -1``)
   * rather than by the column counter, so it stays right whatever the grid width is,
   * and a version that did not know the value would render this narrow rather than
   * broken.
   */
  getGridOptions() {
    return { columns: "full", rows: "auto", min_columns: 6, min_rows: 4 };
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
    const style = el("style");
    style.textContent = `${STYLE}
      .loki-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        padding: 12px 16px 8px;
      }
      .loki-header .loki-title {
        font-size: 18px;
        font-weight: 500;
        color: var(--ha-card-header-color, var(--primary-text-color));
      }
      .loki-live-all {
        font: inherit;
        font-size: 13px;
        display: inline-flex;
        align-items: center;
        gap: 6px;
        color: var(--primary-color, #03a9f4);
        background: none;
        border: 1px solid currentColor;
        border-radius: 999px;
        padding: 4px 12px;
        cursor: pointer;
        white-space: nowrap;
      }
      .loki-live-all ha-icon { --mdc-icon-size: 16px; width: 16px; height: 16px; }
      .loki-live-all[disabled] { opacity: 0.4; cursor: default; }
      .loki-live-all.on {
        color: var(--text-primary-color, #fff);
        background: var(--primary-color, #03a9f4);
        border-color: transparent;
      }
      .loki-grid { display: grid; gap: 10px; padding: 0 12px 12px; }
    `;

    const card = document.createElement("ha-card");

    const header = el("div", "loki-header");
    this._titleEl = el("div", "loki-title");
    this._liveBtn = el("button", "loki-live-all");
    this._liveIcon = icon(ICON_LIVE);
    this._liveLabel = el("span", null, t.liveAll);
    this._liveBtn.append(this._liveIcon, this._liveLabel);
    this._liveBtn.addEventListener("click", () => this._toggleLive());
    header.append(this._titleEl, this._liveBtn);

    this._note = el("div", "loki-note", t.unavailable);
    this._note.hidden = true;

    this._grid = el("div", "loki-grid");
    this._empty = el("div", "loki-empty", t.noDoors);
    this._empty.hidden = true;

    card.append(header, this._note, this._empty, this._grid);
    this.append(style, card);
    this._built = true;
  }

  _update() {
    const hass = this._hass;
    const cameras = this._cameras();
    const reachable = streamReachable(hass, this._config.stream_sensor);

    this._titleEl.textContent = this._config.title;
    this._empty.hidden = cameras.length > 0;
    this._note.hidden = reachable;

    this._liveBtn.hidden = cameras.length === 0;
    this._liveBtn.disabled = !reachable;
    this._liveBtn.title = reachable ? "" : t.liveBlocked;
    this._liveBtn.classList.toggle("on", this._live && reachable);
    this._liveLabel.textContent = this._live && reachable ? t.stopAll : t.liveAll;
    this._liveIcon.setAttribute(
      "icon",
      this._live && reachable ? ICON_STOP : ICON_LIVE
    );

    // Width-driven by default: a fixed column count is either cramped on a phone or
    // absurdly stretched on a monitor. `columns` remains as an explicit override.
    const columns = Number(this._config.columns);
    // min(100%, …) so a narrow card shrinks the tile instead of overflowing, and 210px
    // because a wall of doors is for recognising a person: past a certain point more
    // columns stop helping and start hiding faces.
    this._grid.style.gridTemplateColumns = columns
      ? `repeat(${Math.max(1, columns)}, minmax(0, 1fr))`
      : "repeat(auto-fill, minmax(min(100%, 210px), 1fr))";

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
      tile.label.textContent = door.name;
      tile.open.hidden = !door.button;

      const call = door.call ? hass.states[door.call] : null;
      tile.ringing.hidden = !(call && call.state === "on");

      const live = (this._live || tile.solo) && reachable;
      tile.liveBtn.disabled = !reachable;
      tile.liveBtn.title = reachable ? (live ? t.stop : t.live) : t.liveBlocked;
      tile.liveBtn.classList.toggle("on", live);
      tile.liveBtn.firstChild.setAttribute("icon", live ? ICON_STOP : ICON_LIVE);

      tile.media.setHass(hass);
      tile.media.render(camera, live);
    });
  }

  _buildTile(camera) {
    const media = new DoorMedia();
    const ringing = el("div", "loki-ringing", t.ringing);
    ringing.hidden = true;

    const liveBtn = iconButton(ICON_LIVE, t.live);
    const parts = pictureBar();
    media.el.append(ringing, liveBtn, parts.bar);

    const tile = {
      root: media.el,
      media,
      label: parts.label,
      open: parts.open,
      openLabel: parts.openLabel,
      ringing,
      liveBtn,
      solo: false,
      door: null,
    };

    liveBtn.addEventListener("click", () => {
      // One tile on its own: usually "I can see somebody on this thumbnail, show me
      // that one properly" -- without paying for twenty streams.
      tile.solo = !tile.solo;
      if (this._hass) this._update();
    });
    parts.open.addEventListener("click", () =>
      pressOpen(this._hass, tile.door, parts.open, parts.openLabel)
    );

    return tile;
  }

  _toggleLive() {
    this._live = !this._live;
    if (this._live) {
      const seconds = Number(this._config.live_timeout) || 0;
      if (seconds > 0) {
        this._liveTimer = window.setTimeout(() => this._stopLive(), seconds * 1000);
      }
      if (this._hass) this._update();
    } else {
      this._stopLive();
    }
  }

  _stopLive() {
    if (this._liveTimer) {
      window.clearTimeout(this._liveTimer);
      this._liveTimer = null;
    }
    if (!this._live) return;
    this._live = false;
    for (const tile of this._tiles.values()) tile.solo = false;
    if (this._hass && this._built) this._update();
  }

  _teardown() {
    // The DOM nodes go too, not just the bookkeeping. Switching dashboard tabs
    // disconnects the card and reconnects the same element without calling setConfig
    // again -- so a teardown that forgot the tiles but left them on screen meant the
    // next update built a second full set underneath the first. Twenty-one doors
    // became forty-two.
    for (const tile of this._tiles.values()) {
      tile.media.destroy();
      tile.root.remove();
    }
    this._tiles.clear();
  }
}

/* ------------------------------------------------------------------ opening */

/**
 * Press a door's open button and say what happened, on the button itself.
 *
 * A door opens out of sight of whoever pressed it, so silence is indistinguishable
 * from failure. The integration raises a real error when the service refuses, and that
 * has to reach the person standing at the dashboard.
 */
async function pressOpen(hass, door, button, label) {
  if (!hass || !door || !door.button || button.disabled) return;
  const original = label.textContent;
  button.disabled = true;
  label.textContent = t.opening;
  try {
    await hass.callService("button", "press", { entity_id: door.button });
    label.textContent = t.opened;
  } catch (err) {
    label.textContent = t.failed;
    fire(button, "hass-notification", {
      message: `${door.name}: ${(err && err.message) || t.failed}`,
    });
  } finally {
    window.setTimeout(() => {
      button.disabled = false;
      label.textContent = original;
    }, 2000);
  }
}

/* ------------------------------------------------------------------ editors */

/** Base editor: an ha-form driven by a schema, so nobody has to write YAML. */
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
        columns: "Плиток в ряд (пусто — по ширине)",
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
          entity: { multiple: true, filter: { integration: "loki", domain: "camera" } },
        },
      },
      {
        name: "columns",
        selector: { number: { min: 1, max: 8, step: 1, mode: "box" } },
      },
      {
        name: "live_timeout",
        selector: { number: { min: 0, max: 3600, step: 30, mode: "box" } },
      },
    ];
  }
}

/* ------------------------------------------------------------- registration */

// Idempotent on purpose. The module can legitimately be loaded twice -- once as a
// Lovelace resource and once as an extra module URL -- and a bare define() throws on
// the second, which aborts the rest of the module and takes the picker entries with
// it. Registering twice must be a no-op, not a failure.
const define = (name, cls) => {
  if (!customElements.get(name)) customElements.define(name, cls);
};

define("loki-door-card", LokiDoorCard);
define("loki-wall-card", LokiWallCard);
define("loki-door-card-editor", LokiDoorCardEditor);
define("loki-wall-card-editor", LokiWallCardEditor);

window.customCards = window.customCards || [];
const listed = new Set(window.customCards.map((card) => card.type));
const offer = (entry) => {
  if (!listed.has(entry.type)) window.customCards.push(entry);
};
[
  {
    type: "loki-door-card",
    name: "Loki — домофон",
    description:
      "Картинка с домофона, кнопка «Открыть» и отметка звонка. Живое видео — по кнопке.",
    preview: true,
    documentationURL: "https://github.com/william-aqn/home-assistant-domofon-hacs",
  },
  {
    type: "loki-wall-card",
    name: "Loki — все домофоны",
    description:
      "Плитки всех домофонов с кнопкой открытия на каждой, чтобы найти человека глазами.",
    preview: true,
    documentationURL: "https://github.com/william-aqn/home-assistant-domofon-hacs",
  },
].forEach(offer);

console.info(
  `%c LOKI CARDS %c ${CARD_VERSION} `,
  "color: white; background: #3f51b5; font-weight: 700;",
  "color: #3f51b5; background: white; font-weight: 700;"
);
