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

const CARD_VERSION = "1.5.5";

// Stills cost one HTTP request every few seconds; a live stream costs a decoder and a
// socket for as long as it is open. With twenty doors on an account, "show me
// everything live" has to be a deliberate act with an end to it.
//
// Thirty seconds because the job is "where is that person standing" -- long enough to
// look along a row of doors, short enough that walking away from the tablet cannot
// leave twenty streams running. Press it again for another thirty; the setting raises
// it for anyone who wants longer.
const DEFAULT_LIVE_TIMEOUT = 30;

// How often a still is re-fetched.
//
// A minute, not ten seconds: the backend's picture is static -- measured, the same
// bytes ninety seconds apart -- so polling it hard is twenty-one pointless requests
// per tick, and it was actively harmful, overwriting a frame the user had just asked
// for. Anything genuinely new arrives either from the capture button or from a ring.
const STILL_REFRESH = 60;

// The sidebar page's address. One constant because three places have to agree on it:
// the panel itself, the link out of a tile, and the check that the page exists.
const PANEL_PATH = "loki";

// How long to wait for a forced snapshot before counting it as failed.
const SHOT_TIMEOUT = 15;

// How long the spinner stays up while a stream is being negotiated. Past this the
// card's own "video unavailable" banner is the honest answer, and a spinner that
// never stops is a lie told slowly.
const LIVE_WAIT = 20;

// Minimum tile width per size. The grid fills the card with as many columns of at
// least this width as fit, so the setting reads as "how big", not "how many" -- which
// is the thing that stays right when the same dashboard is opened on a phone.
const TILE_SIZES = { compact: 200, medium: 320, large: 460 };
const DEFAULT_TILE_SIZE = "medium";

// How long a real visit at the door lasts before the bar is empty. Not the
// integration's own CALL_TIMEOUT (120 s) -- that one is a safety net for a call whose
// end was never announced, and draining over two minutes would leave the bar looking
// full through the whole of an ordinary ring. Only the bar depends on this.
const DEFAULT_CALL_TIMEOUT = 30;

const ICON_OPEN = "mdi:lock-open-variant-outline";
const ICON_HANGUP = "mdi:phone-hangup-outline";
const ICON_LIVE = "mdi:video-outline";
const ICON_STOP = "mdi:video-off-outline";
const ICON_SHOT = "mdi:camera-outline";

const t = {
  open: "Открыть",
  live: "Вживую",
  stop: "Стоп",
  liveAll: "Все вживую",
  stopAll: "Остановить",
  shot: "Текущий кадр",
  shooting: "Обновляю…",
  shotHint:
    "Снять по одному кадру с каждого домофона. Дёшево: кадр вместо потока. "
    + "Обычная картинка на плитке приходит с сервера и не меняется — это статика.",
  shotBlocked: "Видеопоток недоступен — кадр снять неоткуда",
  shotDone: "Сняты текущие кадры",
  shotFail: "Кадры снять не удалось — нет видеопотока",
  shotNone: "На карточке нет камер",
  confirmLive: "Включить живое видео со всех камер? Это заметная нагрузка.",
  yes: "Да",
  no: "Нет",
  panelTitle: "Домофоны",
  backToAll: "Все домофоны",
  openPage: "открыть страницу домофона",
  doorGone: "Этот домофон не найден",
  ringing: "Вызов",
  unavailable:
    "Видеопоток недоступен. На плитках — статичная картинка с сервера, "
    + "не текущая обстановка.",
  pickDoor: "Выберите домофон в настройках карточки",
  noDoors: "Домофоны не найдены. Проверьте, что интеграция Loki настроена.",
  liveBlocked: "Видеохост недоступен — живое видео не откроется",
  opening: "Открываю…",
  hangup: "Завершить",
  hangingUp: "Завершаю…",
  hungUp: "Завершён",
  onCall: "Входящий вызов",
  connecting: "Подключаюсь…",
  layout: "Раскладка",
  layoutFull: "Обычная — большая картинка",
  layoutCompact: "Компактная — одна строка",
  callTimeout: "Сколько длится вызов, с",
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
  const out = {
    camera: cameraId,
    button: null,
    call: null,
    name: cameraId,
    loki: null,
  };
  if (!hass || !cameraId) return out;

  const state = hass.states[cameraId];
  if (state) out.name = state.attributes.friendly_name || cameraId;

  const registry = hass.entities || {};
  const deviceId = registry[cameraId] ? registry[cameraId].device_id : null;
  if (!deviceId) return out;
  out.device = deviceId;
  out.loki = lokiIdForDevice(hass, deviceId);

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

/** The numeric Loki device id behind a door, which the services take.
 *
 * It lives in the device registry identifier the integration set, so it survives every
 * rename an entity id does not.
 */
function lokiId(hass, door) {
  const device = hass && hass.devices ? hass.devices[door.device] : null;
  for (const [domain, value] of (device && device.identifiers) || []) {
    if (domain === "loki" && /^\d+$/.test(String(value))) return Number(value);
  }
  return null;
}

/** The numeric Loki device id behind a Home Assistant device, or null. */
function lokiIdForDevice(hass, deviceId) {
  const device = ((hass && hass.devices) || {})[deviceId];
  if (!device) return null;
  for (const [domain, value] of device.identifiers || []) {
    if (domain === "loki") return String(value);
  }
  return null;
}

/** The camera entity belonging to a numeric Loki device id, or null. */
function cameraForLokiId(hass, wanted) {
  const devices = (hass && hass.devices) || {};
  let deviceId = null;
  for (const [id, device] of Object.entries(devices)) {
    for (const [domain, value] of device.identifiers || []) {
      if (domain === "loki" && String(value) === String(wanted)) deviceId = id;
    }
  }
  if (!deviceId) return null;
  for (const [entityId, entry] of Object.entries((hass && hass.entities) || {})) {
    if (entry.device_id === deviceId && entityId.startsWith("camera.")) return entityId;
  }
  return null;
}

/** Whether the "Домофоны" page exists on this install.
 *
 * It is optional -- a question asked once during setup -- and a tile that led to a
 * page that is not there would be worse than a tile that does nothing.
 */
function panelReady(hass) {
  return Boolean(hass && hass.panels && hass.panels[PANEL_PATH]);
}

/** Go to a page inside Home Assistant, the way the frontend's own links do.
 *
 * pushState alone changes the address and nothing else: the router only redraws when
 * it hears `location-changed`.
 */
function goTo(path) {
  history.pushState(null, "", path);
  fire(window, "location-changed", { replace: false });
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
    // The cache-busting stamp currently on the picture's URL, and the refresh bucket
    // it belongs to. Held rather than recomputed per call: see _refreshStill.
    this._stamp = 0;
    this._bucket = -1;

    this._img = document.createElement("img");
    this._img.className = "loki-still";
    this._img.alt = "";
    this.el.appendChild(this._img);

    this._loader = el("div", "loki-loader", t.connecting);
    this._loader.hidden = true;
    this.el.appendChild(this._loader);
  }

  setHass(hass) {
    this._hass = hass;
    if (this._child) this._child.hass = hass;
    if (!this._live) this._refreshStill();
  }

  /** Re-read the picture Home Assistant currently holds for this camera. */
  reload() {
    if (this._live) return;
    this._refreshStill(true);
  }

  /** Fetch a new still right now, rather than waiting for the next tick.

   * Resolves to whether a picture actually arrived -- not whether a request was sent.
   * The difference matters: this integration's live video and its stills travel over
   * different protocols to different hosts, so "video is down" and "snapshots are
   * fine" are both routinely true at once, and a count of intentions would read as a
   * lie to anyone looking at the unavailable-video banner directly above it.
   */
  refreshNow() {
    return new Promise((resolve) => {
      if (this._live || this._img.hidden || !this._hass || !this._camera) {
        resolve(false);
        return;
      }
      let settled = false;
      const done = (ok) => {
        if (settled) return;
        settled = true;
        this._img.removeEventListener("load", onLoad);
        this._img.removeEventListener("error", onError);
        resolve(ok);
      };
      const onLoad = () => done(true);
      const onError = () => done(false);
      this._img.addEventListener("load", onLoad);
      this._img.addEventListener("error", onError);
      // A picture that never arrives must not leave the count hanging.
      window.setTimeout(() => done(false), SHOT_TIMEOUT * 1000);
      this._refreshStill(true);
    });
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
      this._waitForFrame(token);
    } catch (err) {
      this._live = false;
      this._showStill();
    }
  }

  /** Hold the spinner until the stream actually paints something.
   *
   * There is no event to listen for: media events do not bubble, and the video lives
   * inside somebody else's shadow root. So the video element is looked for by hand,
   * and the spinner comes down when it reports a frame -- or when the wait runs out,
   * because a spinner that never stops says less than an empty box.
   */
  _waitForFrame(token) {
    this._loader.hidden = false;
    const started = Date.now();
    const tick = () => {
      if (token !== this._token || !this._live) {
        this._loader.hidden = true;
        return;
      }
      const video = findVideo(this._child, 6);
      if (video && video.videoWidth > 0) {
        this._loader.hidden = true;
        return;
      }
      if (Date.now() - started > LIVE_WAIT * 1000) {
        this._loader.hidden = true;
        return;
      }
      window.setTimeout(tick, 250);
    };
    tick();
  }

  _showStill() {
    this._loader.hidden = true;
    this._img.hidden = false;
    this._refreshStill();
    this._startTimer();
  }

  _refreshStill(force) {
    if (this._img.hidden || !this._hass || !this._camera) return;
    const state = this._hass.states[this._camera];
    const picture = state && state.attributes.entity_picture;
    if (!picture) return;
    // entity_picture only changes when the access token rotates, a matter of minutes,
    // so the URL on its own would leave one frozen frame on screen. Only this URL is
    // safe to append to -- a signed path would fail its signature check.
    //
    // The unforced stamp is bucketed, so the periodic tick asks for the same URL until
    // the bucket rolls over and the browser can serve it from cache. A forced refresh
    // is unique, so it always goes to Home Assistant.
    //
    // The stamp is then KEPT until the bucket rolls over, and this is the whole point:
    // setHass runs an unforced refresh, Home Assistant pushes state constantly, and
    // recomputing the bucketed stamp would put the pre-capture URL back within about a
    // second -- straight out of the browser cache, and the frame somebody had just
    // asked for would flash up and vanish. Which is exactly what it did.
    const bucket = Math.floor(Date.now() / (STILL_REFRESH * 1000));
    if (force) {
      this._stamp = Date.now();
      this._bucket = bucket;
    } else if (bucket !== this._bucket) {
      this._stamp = bucket;
      this._bucket = bucket;
    }
    const src = `${picture}${picture.includes("?") ? "&" : "?"}_=${this._stamp}`;
    if (this._img.getAttribute("src") !== src) this._img.setAttribute("src", src);
  }

  _startTimer() {
    if (this._timer) return;
    this._timer = window.setInterval(
      () => this._refreshStill(),
      STILL_REFRESH * 1000
    );
  }

  _stopTimer() {
    if (!this._timer) return;
    window.clearInterval(this._timer);
    this._timer = null;
  }

  destroy() {
    this._token++;
    this._loader.hidden = true;
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
  /* These cards render into the light DOM, so this stylesheet applies to the whole
     document -- which is why the rule is spelled out element by element instead of a
     bare [hidden]. A global one did blank the entire dashboard: Home Assistant marks
     its own layout pieces hidden and relies on its CSS to show them again.

     The rule is needed at all because every selector below that sets display: flex
     outranks the user-agent's [hidden] { display: none }, so element.hidden = true
     silently does nothing -- which is how the live-view confirmation appeared unasked,
     and how an open button stayed on a door that has no lock to open. */
  .loki-media[hidden],
  .loki-still[hidden],
  .loki-live[hidden],
  .loki-bar[hidden],
  .loki-label[hidden],
  .loki-open[hidden],
  .loki-actions[hidden],
  .loki-round[hidden],
  .loki-round-wrap[hidden],
  .loki-grid[hidden],
  .loki-side[hidden],
  .loki-head[hidden],
  .loki-callbar[hidden],
  .loki-callrow[hidden],
  .loki-pill[hidden],
  .loki-live-badge[hidden],
  .loki-icon-btn[hidden],
  .loki-ringing[hidden],
  .loki-note[hidden],
  .loki-stamp[hidden],
  .loki-empty[hidden],
  .loki-pill[hidden],
  .loki-confirm[hidden] {
    display: none;
  }

  .loki-media {
    position: relative;
    overflow: hidden;
    aspect-ratio: 16 / 9;
    background: var(--secondary-background-color, #2a2a2a);
    border-radius: 10px;
  }
  .loki-still, .loki-live { display: block; width: 100%; height: 100%; }

  /* Between pressing "live" and the first frame there are a few seconds of nothing,
     and the composed card paints them white. On a wall of tiles that reads as a
     broken camera rather than as one that is still connecting. */
  .loki-loader {
    position: absolute;
    inset: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 10px;
    background: var(--secondary-background-color, #2a2a2a);
    color: var(--secondary-text-color, #9e9e9e);
    font-size: 13px;
  }
  .loki-loader[hidden] { display: none; }
  .loki-loader::before {
    content: "";
    width: 28px;
    height: 28px;
    border-radius: 50%;
    border: 3px solid currentColor;
    border-top-color: transparent;
    animation: loki-spin 0.9s linear infinite;
  }
  @keyframes loki-spin { to { transform: rotate(360deg); } }
  @media (prefers-reduced-motion: reduce) {
    .loki-loader::before { animation-duration: 3s; }
  }
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

  /* The same button on a card that has room for it: its own row under the picture,
     wide enough to hit without looking, and out of the way of the view it covers. */
  .loki-actions { display: flex; gap: 8px; padding: 10px 0 2px; }
  .loki-open-lg {
    flex: 1;
    justify-content: center;
    font-size: 15px;
    font-weight: 500;
    gap: 8px;
    padding: 12px 18px;
    min-height: 48px;
  }
  .loki-open-lg ha-icon {
    --mdc-icon-size: 22px;
    width: 22px;
    height: 22px;
  }

  /* Declining is round, smaller and set apart from opening. The two do opposite
     things, and a person reaching for one of them is usually in a hurry. */
  .loki-round-wrap {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 6px;
  }
  .loki-round {
    width: 56px;
    height: 56px;
    border: none;
    border-radius: 50%;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    color: #fff;
    background: var(--error-color, #db4437);
  }
  .loki-round[disabled] { opacity: 0.65; cursor: default; }
  .loki-round ha-icon { --mdc-icon-size: 26px; width: 26px; height: 26px; }
  .loki-round-label {
    font-size: 12px;
    color: var(--secondary-text-color, #727272);
  }

  /* The head: who is at the door, and how long they have been waiting. */
  .loki-head {
    display: flex;
    align-items: flex-start;
    gap: 12px;
    padding: 12px 12px 0;
  }
  .loki-title { flex: 1; min-width: 0; }
  .loki-name {
    font-size: 20px;
    font-weight: 500;
    line-height: 1.2;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .loki-sub {
    font-size: 13px;
    color: var(--secondary-text-color, #727272);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .loki-sub[hidden] { display: none; }

  .loki-callbar { padding: 8px 12px 0; }
  .loki-callrow {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }
  .loki-pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 13px;
    padding: 4px 10px;
    border-radius: 999px;
    color: var(--warning-color, #ffa600);
    background: color-mix(in srgb, var(--warning-color, #ffa600) 16%, transparent);
  }
  .loki-pill::before {
    content: "";
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: currentColor;
    /* Breathing, not blinking: a hard blink on a wall panel in a dark hallway is
       the sort of thing people end up covering with tape. */
    animation: loki-breathe 2s ease-in-out infinite;
  }
  @keyframes loki-breathe { 50% { opacity: 0.3; } }
  @media (prefers-reduced-motion: reduce) {
    .loki-pill::before { animation: none; }
  }
  .loki-timer {
    font-size: 15px;
    font-variant-numeric: tabular-nums;
    color: var(--secondary-text-color, #727272);
  }
  /* Draining rather than filling: what is left is the useful number, and the call
     ends by itself when it reaches zero. */
  .loki-progress {
    margin-top: 8px;
    height: 3px;
    border-radius: 2px;
    background: var(--divider-color, #e0e0e0);
    overflow: hidden;
  }
  .loki-progress > i {
    display: block;
    height: 100%;
    background: var(--warning-color, #ffa600);
    transition: width 1s linear;
  }

  .loki-live-badge {
    position: absolute;
    top: 8px;
    left: 8px;
    display: inline-flex;
    align-items: center;
    gap: 5px;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.04em;
    padding: 3px 8px;
    border-radius: 999px;
    color: #fff;
    background: var(--error-color, #db4437);
  }
  .loki-live-badge::before {
    content: "";
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: #fff;
  }

  /* Two columns once there is room; stacked when there is not, which is also the
     phone and the wall panel held upright. */
  .loki-grid { display: grid; gap: 12px; }
  .loki-side {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 12px;
    justify-content: center;
  }
  .loki-side .loki-actions { width: 100%; }
  @container (min-width: 520px) {
    .loki-grid { grid-template-columns: 1fr minmax(150px, 30%); align-items: center; }
  }

  /* One row: preview, who and how long, and the buttons. For a dashboard where the
     domofon is one line among many. */
  .loki-compact .loki-grid {
    grid-template-columns: 104px minmax(0, 1fr) auto;
    align-items: center;
  }
  .loki-compact .loki-head,
  .loki-compact .loki-callbar { display: none; }
  .loki-compact .loki-strip { display: block; min-width: 0; }
  .loki-compact .loki-side { flex-direction: row; gap: 8px; }
  .loki-compact .loki-side .loki-actions { width: auto; }
  .loki-compact .loki-round-label { display: none; }
  .loki-compact .loki-round { width: 48px; height: 48px; }
  .loki-compact .loki-open-lg {
    width: 48px;
    min-height: 48px;
    padding: 0;
    border-radius: 50%;
    flex: none;
  }
  .loki-compact .loki-open-lg span { display: none; }
  .loki-compact .loki-icon-btn { display: none; }

  .loki-strip { display: none; }
  .loki-strip-name {
    font-weight: 500;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .loki-strip-meta {
    font-size: 13px;
    color: var(--secondary-text-color, #727272);
  }

  /* A tile that leads to that card says so on hover, and answers the keyboard. */
  .loki-linked { cursor: pointer; }
  .loki-linked:focus-visible {
    outline: 2px solid var(--primary-color, #03a9f4);
    outline-offset: 2px;
  }

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
  .loki-stamp {
    border-left: 3px solid var(--success-color, #4caf50);
  }
  .loki-stamp.failed {
    border-left-color: var(--error-color, #db4437);
  }

  .loki-pill {
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
  .loki-pill ha-icon { --mdc-icon-size: 16px; width: 16px; height: 16px; }
  .loki-pill[disabled] { opacity: 0.4; cursor: default; }
  .loki-pill.on {
    color: var(--text-primary-color, #fff);
    background: currentColor;
    border-color: transparent;
  }
  /* Red, because it is the expensive one. The colour is the warning; the dialog is
     the brake. */
  .loki-pill.costly { color: var(--error-color, #db4437); }
  .loki-pill.costly.on {
    color: var(--text-primary-color, #fff);
    background: var(--error-color, #db4437);
  }

  .loki-confirm {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
    margin: 0 12px 10px;
    padding: 8px 12px;
    font-size: 13px;
    color: var(--primary-text-color);
    background: var(--secondary-background-color, rgba(127, 127, 127, 0.14));
    border-left: 3px solid var(--error-color, #db4437);
    border-radius: 8px;
  }
  .loki-confirm .loki-question { flex: 1; min-width: 180px; }
  .loki-confirm button {
    font: inherit;
    font-size: 13px;
    border-radius: 999px;
    padding: 4px 16px;
    cursor: pointer;
    border: 1px solid var(--divider-color, rgba(127, 127, 127, 0.4));
    background: none;
    color: var(--primary-text-color);
  }
  .loki-confirm button.danger {
    background: var(--error-color, #db4437);
    border-color: transparent;
    color: #fff;
  }
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
    this._autoLive = false;
  }

  setConfig(config) {
    // Deliberately not a throw. The picker renders a live preview from getStubConfig,
    // and on an install with no doors that would put a red error tile where the card
    // somebody came to add should be.
    this._config = { live_timeout: DEFAULT_LIVE_TIMEOUT, ...(config || {}) };
    this._autoLive = false;
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
    this._tickTimer(false);
    if (this._media) this._media.destroy();
  }

  _build() {
    const style = el("style");
    style.textContent = `${STYLE}
      .loki-body { padding: 12px; container-type: inline-size; }
    `;

    this._card = document.createElement("ha-card");
    const card = this._card;

    this._name = el("div", "loki-name");
    this._sub = el("div", "loki-sub");
    this._sub.hidden = true;
    const title = el("div", "loki-title");
    title.append(this._name, this._sub);
    this._header = el("div", "loki-head");
    this._header.appendChild(title);

    this._pill = el("span", "loki-pill", t.onCall);
    this._timer = el("span", "loki-timer");
    const callRow = el("div", "loki-callrow");
    callRow.append(this._pill, this._timer);
    this._bar = el("i");
    const progress = el("div", "loki-progress");
    progress.appendChild(this._bar);
    this._callBar = el("div", "loki-callbar");
    this._callBar.append(callRow, progress);
    this._callBar.hidden = true;

    // The compact row carries its own copy of the same two facts: the head is hidden
    // in that layout, and a card whose name nobody can read is not a card.
    this._stripName = el("div", "loki-strip-name");
    this._stripMeta = el("div", "loki-strip-meta");
    this._strip = el("div", "loki-strip");
    this._strip.append(this._stripName, this._stripMeta);

    this._media = new DoorMedia();

    this._ringing = el("div", "loki-ringing", t.ringing);
    this._ringing.hidden = true;

    this._liveBtn = iconButton(ICON_LIVE, t.live);
    this._liveBtn.addEventListener("click", () => this._toggleLive());

    this._shotBtn = iconButton(ICON_SHOT, t.shot);
    this._shotBtn.style.right = "42px";
    this._shotBtn.addEventListener("click", async () => {
      this._shotBtn.disabled = true;
      let ok = false;
      try {
        await this._hass.callService("loki", "capture_frame", {
          device_id: lokiId(this._hass, this._door),
        });
        this._media.reload();
        ok = true;
      } catch (err) {
        ok = false;
      }
      this._shotBtn.disabled = false;
      this._showStamp(ok ? 1 : 0, 1);
    });

    // Not the overlay strip the tiles use. This card is the one somebody opens to
    // look at the picture properly, and a button lying across the bottom of it covers
    // exactly the ground a visitor stands on. Here it gets a row of its own.
    this._openBtn = el("button", "loki-open loki-open-lg");
    this._openLabel = el("span", null, t.open);
    this._openBtn.append(icon(ICON_OPEN), this._openLabel);
    this._openBtn.addEventListener("click", () => this._open());

    // Only while a call is up: the rest of the time there is nothing to decline,
    // and a dead button next to a live one is worse than no button.
    this._hangupBtn = el("button", "loki-round");
    this._hangupBtn.appendChild(icon(ICON_HANGUP));
    this._hangupBtn.addEventListener("click", () => this._hangup());
    this._hangupLabel = el("span", "loki-round-label", t.hangup);
    this._hangupWrap = el("div", "loki-round-wrap");
    this._hangupWrap.append(this._hangupBtn, this._hangupLabel);
    this._hangupWrap.hidden = true;

    this._actions = el("div", "loki-actions");
    this._actions.appendChild(this._openBtn);

    this._liveBadge = el("div", "loki-live-badge", "LIVE");
    this._liveBadge.hidden = true;

    this._media.el.append(
      this._ringing,
      this._liveBadge,
      this._shotBtn,
      this._liveBtn
    );

    this._note = el("div", "loki-note", t.unavailable);
    this._note.hidden = true;

    this._stamp = el("div", "loki-note loki-stamp");
    this._stamp.hidden = true;

    this._side = el("div", "loki-side");
    this._side.append(this._actions, this._hangupWrap);

    this._grid = el("div", "loki-grid");
    this._grid.append(this._media.el, this._strip, this._side);

    const body = el("div", "loki-body");
    body.appendChild(this._grid);

    card.append(this._header, this._callBar, body, this._note, this._stamp);
    this.append(style, card);
    this._built = true;
  }

  _update() {
    const hass = this._hass;
    const camera = this._config.camera;
    if (!camera) {
      this._name.textContent = t.pickDoor;
      this._media.el.hidden = true;
      this._note.hidden = true;
      return;
    }
    this._media.el.hidden = false;

    const door = resolveDoor(hass, camera);
    this._door = door;
    const name = this._config.name || door.name;
    this._name.textContent = name;
    this._stripName.textContent = name;

    const area = doorArea(hass, door);
    this._sub.textContent = area;
    this._sub.hidden = !area;

    this._card.classList.toggle("loki-compact", this._config.layout === "compact");
    this._actions.hidden = !door.button;

    const call = door.call ? hass.states[door.call] : null;
    const ringing = Boolean(call && call.state === "on");
    // The badge on the picture said "Вызов" before there was a head to say it in.
    // Two of them is one too many.
    this._ringing.hidden = true;
    this._callBar.hidden = !ringing;
    this._hangupWrap.hidden = !ringing;
    this._since = ringing ? call.last_changed : null;
    this._tickTimer(ringing);

    const reachable = streamReachable(hass, this._config.stream_sensor);
    this._note.hidden = reachable;
    // Disabled rather than hidden: the button is the answer to "why can I not see
    // live video", and its tooltip says so. Letting it through would open an RTSP
    // connection already known to time out.
    this._shotBtn.disabled = !reachable;
    this._shotBtn.title = reachable ? t.shot : t.shotBlocked;
    this._liveBtn.disabled = !reachable;
    this._liveBtn.title = reachable ? (this._live ? t.stop : t.live) : t.liveBlocked;
    this._liveBtn.classList.toggle("on", this._live && reachable);
    this._liveBtn.firstChild.setAttribute(
      "icon",
      this._live && reachable ? ICON_STOP : ICON_LIVE
    );

    // ``live: true`` -- what /loki/<id>?live=1 sets, and what a wall panel woken by a
    // ring wants: the stream, not a still somebody has to walk over and tap. Once per
    // configuration, and never against an unreachable stream: after the timeout has
    // stopped it, or after somebody stopped it by hand, it stays stopped.
    if (this._config.live && !this._autoLive && reachable) {
      this._autoLive = true;
      this._toggleLive();
      return;
    }

    this._liveBadge.hidden = !(this._live && reachable);
    this._media.render(camera, this._live && reachable);
  }

  /** Keep the call timer running, and only while there is a call. */
  _tickTimer(on) {
    if (!on) {
      if (this._clock) {
        window.clearInterval(this._clock);
        this._clock = null;
      }
      return;
    }
    this._paint();
    if (!this._clock) this._clock = window.setInterval(() => this._paint(), 1000);
  }

  _paint() {
    const seconds = sinceSeconds(this._since);
    const text = asClock(seconds);
    this._timer.textContent = text;
    this._stripMeta.textContent = `${t.onCall} · ${text}`;
    const total = Number(this._config.call_timeout) || DEFAULT_CALL_TIMEOUT;
    const left = Math.max(0, Math.min(1, 1 - seconds / total));
    this._bar.style.width = `${(left * 100).toFixed(1)}%`;
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

  _showStamp(loaded, total) {
    const now = new Date().toLocaleTimeString("ru-RU");
    this._stamp.textContent = loaded
      ? `${t.shotDone} · ${now}`
      : `${t.shotFail} · ${now}`;
    this._stamp.classList.toggle("failed", Boolean(total) && !loaded);
    this._stamp.hidden = false;
    if (this._stampTimer) window.clearTimeout(this._stampTimer);
    this._stampTimer = window.setTimeout(() => {
      this._stamp.hidden = true;
    }, 8000);
  }

  async _open() {
    await pressOpen(this._hass, this._door, this._openBtn, this._openLabel);
  }

  async _hangup() {
    await pressHangup(
      this._hass,
      this._door,
      this._hangupBtn,
      this._hangupLabel
    );
  }
}

/* ------------------------------------------------------------ the wall card */

class LokiWallCard extends HTMLElement {
  static getConfigElement() {
    return document.createElement("loki-wall-card-editor");
  }

  static getStubConfig(hass) {
    return { cameras: findDoorCameras(hass), tile_size: DEFAULT_TILE_SIZE };
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
      tile_size: DEFAULT_TILE_SIZE,
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

  /** Roughly how many tiles fit across, for the size hints below. */
  _columnGuess() {
    const columns = Number(this._config.columns);
    if (columns) return Math.max(1, columns);
    const width = this.getBoundingClientRect().width || 900;
    const min =
      TILE_SIZES[this._config.tile_size] || TILE_SIZES[DEFAULT_TILE_SIZE];
    return Math.max(1, Math.floor(width / (min + 10)));
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
      .loki-actions { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
    `;

    const card = document.createElement("ha-card");

    const header = el("div", "loki-header");
    this._titleEl = el("div", "loki-title");
    const actions = el("div", "loki-actions");

    this._shotBtn = el("button", "loki-pill");
    this._shotIcon = icon(ICON_SHOT);
    this._shotLabel = el("span", null, t.shot);
    this._shotBtn.append(this._shotIcon, this._shotLabel);
    this._shotBtn.title = t.shotHint;
    this._shotBtn.addEventListener("click", () => this._refreshAll());

    this._liveBtn = el("button", "loki-pill costly");
    this._liveIcon = icon(ICON_LIVE);
    this._liveLabel = el("span", null, t.liveAll);
    this._liveBtn.append(this._liveIcon, this._liveLabel);
    this._liveBtn.addEventListener("click", () => this._askLive());

    actions.append(this._shotBtn, this._liveBtn);
    header.append(this._titleEl, actions);

    // Built once and shown on demand: a dialog imported from Home Assistant would be
    // a private API, and a native confirm() looks like a browser error.
    this._confirm = el("div", "loki-confirm");
    this._confirm.hidden = true;
    const question = el("div", "loki-question", t.confirmLive);
    const yes = el("button", "danger", t.yes);
    const no = el("button", null, t.no);
    yes.addEventListener("click", () => {
      this._confirm.hidden = true;
      this._startLive();
    });
    no.addEventListener("click", () => {
      this._confirm.hidden = true;
    });
    this._confirm.append(question, yes, no);
    this._confirmQuestion = question;

    this._note = el("div", "loki-note", t.unavailable);
    this._note.hidden = true;

    // A refreshed snapshot usually looks identical to the one it replaced -- the
    // camera may not have moved, and the backend keeps a frame for ten seconds.
    // Without a line saying so, the button reads as broken.
    this._stamp = el("div", "loki-note loki-stamp");
    this._stamp.hidden = true;

    this._grid = el("div", "loki-grid");
    this._empty = el("div", "loki-empty", t.noDoors);
    this._empty.hidden = true;

    card.append(
      header,
      this._confirm,
      this._note,
      this._stamp,
      this._empty,
      this._grid
    );
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

    this._shotBtn.hidden = cameras.length === 0;
    // A frame can only come from the stream, so when the stream is gone this button
    // has nothing to do -- exactly like the live one next to it.
    this._shotBtn.disabled = !reachable;
    this._shotBtn.title = reachable ? t.shotHint : t.shotBlocked;
    this._liveBtn.hidden = cameras.length === 0;
    this._liveBtn.disabled = !reachable;
    this._liveBtn.title = reachable ? "" : t.liveBlocked;
    this._liveBtn.classList.toggle("on", this._live && reachable);
    this._liveLabel.textContent = this._live && reachable ? t.stopAll : t.liveAll;
    this._liveIcon.setAttribute(
      "icon",
      this._live && reachable ? ICON_STOP : ICON_LIVE
    );
    if (!reachable) this._confirm.hidden = true;
    this._confirmQuestion.textContent = `${t.confirmLive} Камер: ${cameras.length}.`;

    // Width-driven by default: a fixed column count is either cramped on a phone or
    // absurdly stretched on a monitor. `columns` remains as an explicit override.
    const columns = Number(this._config.columns);
    // min(100%, …) so a narrow card shrinks the tile instead of overflowing rather
    // than pushing the page sideways. A wall of doors is for recognising a person, so
    // past a certain point more columns stop helping and start hiding faces.
    const minWidth =
      TILE_SIZES[this._config.tile_size] || TILE_SIZES[DEFAULT_TILE_SIZE];
    this._grid.style.gridTemplateColumns = columns
      ? `repeat(${Math.max(1, columns)}, minmax(0, 1fr))`
      : `repeat(auto-fill, minmax(min(100%, ${minWidth}px), 1fr))`;

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

      // Only a tile that leads somewhere looks and behaves like one: a pointer, a
      // stop on the tab order, and a name for whoever is listening rather than
      // looking.
      const linked = Boolean(door.loki) && panelReady(hass);
      tile.root.classList.toggle("loki-linked", linked);
      if (linked) {
        tile.root.tabIndex = 0;
        tile.root.setAttribute("role", "link");
        tile.root.setAttribute("aria-label", `${door.name} — ${t.openPage}`);
      } else {
        tile.root.removeAttribute("tabindex");
        tile.root.removeAttribute("role");
        tile.root.removeAttribute("aria-label");
      }

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

    // Tapping the picture opens that door on its own page. A tile is a thumbnail, and
    // the question a thumbnail raises -- who is that by the gate? -- wants a bigger
    // picture, not a squint. The buttons drawn on top keep doing their own job.
    const follow = (event) => {
      if (event.target.closest("button")) return;
      if (!tile.door || !tile.door.loki || !panelReady(this._hass)) return;
      goTo(`/${PANEL_PATH}/${tile.door.loki}`);
    };
    media.el.addEventListener("click", follow);
    media.el.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      // Space scrolls the page unless somebody says otherwise.
      event.preventDefault();
      follow(event);
    });

    return tile;
  }

  /** One real frame from every door, now.
   *
   * Goes through the integration rather than just re-requesting the picture: the
   * server's own still is static -- measured, the same bytes ninety seconds apart --
   * so asking for it again shows the same scene. Only the stream knows what is
   * actually out there, and one frame each is a fraction of the cost of watching.
   */
  async _refreshAll() {
    const tiles = [...this._tiles.values()].filter((tile) => tile.door);
    if (!tiles.length) {
      this._showStamp(0, 0);
      return;
    }
    this._shotBtn.disabled = true;
    this._shotLabel.textContent = t.shooting;

    const results = await Promise.all(
      tiles.map(async (tile) => {
        try {
          await this._hass.callService("loki", "capture_frame", {
            device_id: lokiId(this._hass, tile.door),
          });
        } catch (err) {
          return false;
        }
        tile.media.reload();
        return true;
      })
    );

    this._shotBtn.disabled = false;
    this._shotLabel.textContent = t.shot;
    this._showStamp(results.filter(Boolean).length, tiles.length);
  }

  _showStamp(loaded, total) {
    const now = new Date().toLocaleTimeString("ru-RU");
    if (!total) {
      this._stamp.textContent = t.shotNone;
    } else if (loaded) {
      this._stamp.textContent = `${t.shotDone}: ${loaded} из ${total} · ${now}`;
    } else {
      this._stamp.textContent = `${t.shotFail} · ${now}`;
    }
    this._stamp.classList.toggle("failed", Boolean(total) && !loaded);
    this._stamp.hidden = false;
    if (this._stampTimer) window.clearTimeout(this._stampTimer);
    this._stampTimer = window.setTimeout(() => {
      this._stamp.hidden = true;
    }, 8000);
  }

  /** Turning twenty streams on deserves a question, not a single tap. */
  _askLive() {
    if (this._live) {
      this._stopLive();
      return;
    }
    this._confirm.hidden = !this._confirm.hidden;
  }

  _startLive() {
    this._live = true;
    const seconds = Number(this._config.live_timeout) || 0;
    if (seconds > 0) {
      this._liveTimer = window.setTimeout(() => this._stopLive(), seconds * 1000);
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

/** Brief visual acknowledgement, so a press that changes nothing visible still lands. */
function flash(button, label, busyText, restore) {
  button.disabled = true;
  if (label && busyText !== undefined) label.textContent = busyText;
  window.setTimeout(() => {
    button.disabled = false;
    if (label && restore !== undefined) label.textContent = restore;
  }, 900);
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

/** The <video> inside a composed card, wherever its shadow roots have put it. */
function findVideo(root, depth) {
  if (!root || depth < 0) return null;
  if (root.tagName === "VIDEO") return root;
  const kids = [
    ...(root.shadowRoot ? root.shadowRoot.children : []),
    ...(root.children || []),
  ];
  for (const kid of kids) {
    const found = findVideo(kid, depth - 1);
    if (found) return found;
  }
  return null;
}

/** Seconds since an ISO timestamp, never negative. */
function sinceSeconds(since) {
  if (!since) return 0;
  return Math.max(0, Math.floor((Date.now() - Date.parse(since)) / 1000));
}

/** Seconds as mm:ss. */
function asClock(seconds) {
  const mm = String(Math.floor(seconds / 60)).padStart(2, "0");
  const ss = String(seconds % 60).padStart(2, "0");
  return `${mm}:${ss}`;
}

/** The area a door sits in, for the line under its name. */
function doorArea(hass, door) {
  const areas = (hass && hass.areas) || {};
  const devices = (hass && hass.devices) || {};
  const device = door.device ? devices[door.device] : null;
  const area = device && device.area_id ? areas[device.area_id] : null;
  return (area && area.name) || "";
}

/** Release our own branch of a call in progress.
 *
 * Not a rejection: the intercom goes on ringing the resident's phone, and that is
 * deliberate -- a global decline from here would cancel the call on every device at
 * once, including the phone of somebody who was about to answer.
 */
async function pressHangup(hass, door, button, label) {
  if (!hass || !door || button.disabled) return;
  const id = lokiId(hass, door);
  if (!id) return;
  const original = label.textContent;
  button.disabled = true;
  label.textContent = t.hangingUp;
  try {
    await hass.callService("loki", "hangup", { device_id: id });
    label.textContent = t.hungUp;
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
        tile_size: "Размер плиток",
        columns: "Плиток в ряд (пусто — по размеру)",
        live_timeout: "Сам выключить видео через, с",
        layout: t.layout,
        call_timeout: t.callTimeout,
      }[name] || name
    );
  }
}

class LokiDoorCardEditor extends LokiEditorBase {
  schema() {
    return [
      {
        name: "layout",
        selector: {
          select: {
            mode: "dropdown",
            options: [
              { value: "full", label: t.layoutFull },
              { value: "compact", label: t.layoutCompact },
            ],
          },
        },
      },
      {
        name: "call_timeout",
        selector: { number: { min: 5, max: 600, step: 5, mode: "box" } },
      },
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
        name: "tile_size",
        selector: {
          select: {
            mode: "dropdown",
            options: [
              { value: "compact", label: "Мелкие — больше в ряд" },
              { value: "medium", label: "Средние" },
              { value: "large", label: "Крупные — видно лица" },
            ],
          },
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

/* -------------------------------------------------------------- the panel */

/**
 * A whole sidebar page showing the wall of doors.
 *
 * Registered by the integration through ``panel_custom`` rather than by writing a
 * Lovelace dashboard: an integration has no supported way to create one, and reaching
 * into Lovelace's storage behind its back risks losing the user's own dashboards the
 * next time it saves.
 *
 * The panel is a thin frame around the same card the dashboard uses, so there is one
 * implementation of the wall and not two.
 */
class LokiPanel extends HTMLElement {
  constructor() {
    super();
    this._card = null;
    this._door = null;
    this._live = false;
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  /** panel_custom hands the panel its config here; we have nothing to read from it. */
  set panel(_panel) {}

  /**
   * ``/loki`` shows every door; ``/loki/<id>`` shows one.
   *
   * That second form is what a doorbell notification points at: tapping it should land
   * on the door that rang, with its picture and its open button, not on a wall of
   * twenty tiles to search through.
   */
  set route(route) {
    const path = (route && route.path) || "";
    const match = path.match(/(\d+)/);
    const door = match ? match[1] : null;
    // ``?live=1`` opens straight into the stream. The router hands us the path only,
    // so the query is read off the address bar -- which is where a panel woken by a
    // doorbell lands, and it should be showing the door by the time somebody looks.
    const live = new URLSearchParams(window.location.search).get("live") === "1";
    if (door === this._door && live === this._live) return;
    this._live = live;
    this._door = door;
    this._card = null;
    this.innerHTML = "";
    this._render();
  }

  set narrow(narrow) {
    if (this._narrow === narrow) return;
    this._narrow = narrow;
    if (this._door) return;
    // setConfig tears the card down and it only rebuilds when hass is set again, so
    // reconfiguring without re-feeding hass leaves an empty page. panel_custom sets
    // narrow after hass, which is exactly when that happens.
    if (this._card && this._hass) {
      this._card.setConfig(this._config());
      this._card.hass = this._hass;
    }
  }

  _config() {
    return {
      title: t.panelTitle,
      // A phone gets small tiles so a useful number fit; a desktop gets the default.
      tile_size: this._narrow ? "compact" : DEFAULT_TILE_SIZE,
    };
  }

  _render() {
    if (!this._hass) return;
    if (!this._card) this._build();
    this._card.hass = this._hass;
  }

  _buildOne() {
    const camera = cameraForLokiId(this._hass, this._door);
    const wrap = el("div", "loki-panel");

    const back = document.createElement("a");
    back.className = "loki-back";
    back.href = `/${PANEL_PATH}`;
    back.textContent = `← ${t.backToAll}`;
    wrap.appendChild(back);

    if (!camera) {
      wrap.appendChild(el("div", "loki-empty", t.doorGone));
      this.appendChild(wrap);
      // Something has to answer the hass setter; an inert stub keeps _render simple.
      this._card = { set hass(_v) {} };
      return wrap;
    }

    this._card = document.createElement("loki-door-card");
    this._card.setConfig({ camera, live: this._live });
    wrap.appendChild(this._card);
    return wrap;
  }

  _build() {
    const style = el("style");
    style.textContent = `
      .loki-panel { padding: 8px; box-sizing: border-box; }
      .loki-panel loki-wall-card, .loki-panel loki-door-card { display: block; }
      .loki-panel loki-door-card { max-width: 760px; margin: 0 auto; }
      .loki-back {
        display: inline-block;
        margin: 4px 8px 10px;
        font-size: 14px;
        color: var(--primary-color, #03a9f4);
        text-decoration: none;
      }
    `;
    if (this._door) {
      this.append(style, this._buildOne());
      return;
    }
    const wrap = el("div", "loki-panel");
    this._card = document.createElement("loki-wall-card");
    this._card.setConfig(this._config());
    wrap.appendChild(this._card);
    this.append(style, wrap);
  }

  disconnectedCallback() {
    // The card stops its own streams and timers; nothing else to unwind.
    if (this._card && this._card.disconnectedCallback) {
      this._card.disconnectedCallback();
    }
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

define("loki-panel", LokiPanel);
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
