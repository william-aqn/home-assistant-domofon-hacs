/**
 * Load the dashboard cards against a DOM shim and assert they register.
 *
 * The cards ship as a plain module with no build step, so nothing else would notice a
 * syntax error or a throw at import time -- and a card that throws while loading takes
 * every other custom card on the dashboard down with it, because they share one module
 * graph. This runs in CI next to the Python tests.
 *
 * It is a smoke test, not a rendering test: it proves the file loads, defines its
 * elements, offers both entries to the card picker, and that each card accepts or
 * refuses a config as intended. Rendering is verified against a real Home Assistant.
 *
 *   node scripts/check_cards.js
 */

const fs = require("node:fs");
const path = require("node:path");

const BUNDLE = path.join(
  __dirname,
  "..",
  "custom_components",
  "loki",
  "www",
  "loki-cards.js"
);

const defined = {};

/** Enough of an element for the module to build its DOM without a browser. */
const stubElement = () => {
  const attrs = {};
  const classes = new Set();
  const listeners = {};
  return {
    className: "",
    style: {},
    textContent: "",
    hidden: false,
    disabled: false,
    // Classes are kept, not dropped: whether the picture stands alone in its row
    // is said with one, and that is a thing worth checking.
    classList: {
      add(...names) {
        names.forEach((name) => classes.add(name));
      },
      remove(...names) {
        names.forEach((name) => classes.delete(name));
      },
      toggle(name, force) {
        const on = force === undefined ? !classes.has(name) : Boolean(force);
        if (on) classes.add(name);
        else classes.delete(name);
        return on;
      },
      contains(name) {
        return classes.has(name);
      },
    },
    // Children are kept so a check can reach a button the card built but did not
    // keep a name for -- the two in the confirmation strip.
    append(...nodes) {
      for (const node of nodes) if (node) this.children.push(node);
    },
    appendChild(node) {
      if (node) this.children.push(node);
      return node;
    },
    // Listeners are kept, not dropped. A check that calls the handler by hand
    // proves the handler and never the wire, and the wire is a whole line of
    // product code: reverting one arrow function can remove a feature entirely
    // with every check still green. That happened to be measured, not imagined.
    addEventListener(type, fn) {
      (listeners[type] = listeners[type] || []).push(fn);
    },
    removeEventListener(type, fn) {
      listeners[type] = (listeners[type] || []).filter((each) => each !== fn);
    },
    /** What a browser delivers on a press. The handlers read the modifier keys. */
    dispatchEvent(event) {
      const type = (event && event.type) || "";
      for (const fn of [...(listeners[type] || [])]) fn(event);
      return true;
    },
    // Attributes are remembered rather than dropped: the picture's src is one, and
    // whether it survives a state update is a thing worth checking.
    setAttribute(name, value) {
      attrs[name] = String(value);
    },
    getAttribute(name) {
      return name in attrs ? attrs[name] : null;
    },
    removeAttribute(name) {
      delete attrs[name];
    },
    remove() {},
    insertBefore() {},
    // What the panel hands the card it creates; kept so the check can read it.
    setConfig(config) {
      this.config = config;
    },
    children: [],
    // The icon inside a button; the cards swap its `icon` attribute. The real first
    // child, not a dummy that throws the write away: the swap is a line of product
    // code like any other and has to be readable back.
    get firstChild() {
      return this.children[0] || { setAttribute() {}, getAttribute: () => null };
    },
  };
};

global.window = global;
// A refresh timer must not hold the process open once the checks are done. Node keeps
// running while one is pending; a browser simply throws the page away.
const realSetInterval = global.setInterval;
global.setInterval = (fn, ms) => {
  const handle = realSetInterval(fn, ms);
  if (handle && handle.unref) handle.unref();
  return handle;
};
// The same for the one-shot kind, which is what stops live video. A check that
// leaves one armed would stall the whole run by the length of that timeout -- half
// a minute at the default -- and still report that everything passed.
const realSetTimeout = global.setTimeout;
global.setTimeout = (fn, ms) => {
  const handle = realSetTimeout(fn, ms);
  if (handle && handle.unref) handle.unref();
  return handle;
};
global.customElements = {
  define: (name, cls) => {
    // Throws exactly like a browser does, which is the point: the bundle may be
    // loaded twice (Lovelace resource plus extra module URL) and must survive it.
    if (defined[name]) throw new Error(`${name} already defined`);
    defined[name] = cls;
  },
  get: (name) => defined[name],
};
global.document = { createElement: stubElement };
global.CustomEvent = class {
  constructor(type, options) {
    this.type = type;
    Object.assign(this, options);
  }
};
global.HTMLElement = class {
  append() {}
  appendChild() {}
  addEventListener() {}
  dispatchEvent() {}
};

const failures = [];

function check(what, ok, detail) {
  if (ok) {
    console.log(`ok   ${what}`);
  } else {
    console.log(`FAIL ${what}${detail ? ` — ${detail}` : ""}`);
    failures.push(what);
  }
}

const SOURCE = fs.readFileSync(BUNDLE, "utf8");
// eslint-disable-next-line no-eval
eval(SOURCE);

// Loading twice is a real scenario, not a hypothetical: the bundle is registered both
// as a Lovelace resource and as an extra module URL, and on a bare define() the second
// load throws and takes the picker entries down with it.
let secondLoad = "ok";
try {
  // eslint-disable-next-line no-eval
  eval(SOURCE);
} catch (err) {
  secondLoad = err.message;
}
check("bundle survives being loaded twice", secondLoad === "ok", secondLoad);
check(
  "a second load does not duplicate the picker entries",
  (global.customCards || []).length === 2,
  String((global.customCards || []).length)
);

const EXPECTED = [
  "loki-door-card",
  "loki-wall-card",
  "loki-door-card-editor",
  "loki-wall-card-editor",
];
for (const name of EXPECTED) {
  check(`defines ${name}`, Boolean(defined[name]));
}

const picker = (global.customCards || []).map((card) => card.type);
check(
  "both cards offered in the picker",
  picker.includes("loki-door-card") && picker.includes("loki-wall-card"),
  picker.join(", ")
);
check(
  "picker entries carry a name and a description",
  (global.customCards || []).every((card) => card.name && card.description)
);

// A card with no door must still construct: the picker renders a live preview
// from getStubConfig, and a throw there shows a red error tile instead of the
// card the user came to add. It says what to do in its header instead.
let survived = true;
try {
  new defined["loki-door-card"]().setConfig({});
} catch (err) {
  survived = false;
}
check("door card survives a config with no door", survived);

// The wall card has a sensible default -- every door -- so an empty config is fine.
let accepted = true;
try {
  new defined["loki-wall-card"]().setConfig({});
} catch (err) {
  accepted = false;
}
check("wall card accepts an empty config", accepted);

for (const name of ["loki-door-card-editor", "loki-wall-card-editor"]) {
  const editor = new defined[name]();
  const schema = editor.schema();
  check(
    `${name} builds a schema`,
    Array.isArray(schema) && schema.length > 0 && schema.every((row) => row.name)
  );
  check(
    `${name} labels every field`,
    schema.every((row) => editor.label(row.name) !== row.name),
    schema
      .filter((row) => editor.label(row.name) === row.name)
      .map((row) => row.name)
      .join(", ")
  );
}

// The door picker must be narrowed to this integration, or the dropdown lists every
// camera in the house and the whole point of the preset is lost.
const doorSchema = new defined["loki-door-card-editor"]().schema();
const cameraField = doorSchema.find((row) => row.name === "camera");
check(
  "door field is filtered to loki cameras",
  Boolean(
    cameraField &&
      cameraField.selector.entity.filter.integration === "loki" &&
      cameraField.selector.entity.filter.domain === "camera"
  )
);

const wallSchema = new defined["loki-wall-card-editor"]().schema();
const camerasField = wallSchema.find((row) => row.name === "cameras");
for (const card of ['loki-door-card', 'loki-wall-card']) {
  const instance = new defined[card]();
  instance.setConfig({});
  const grid = instance.getGridOptions();
  check(
    `${card} reports grid options for sections views`,
    Boolean(grid && grid.columns && grid.rows)
  );
}

check(
  "wall card offers a multi-entity list",
  Boolean(camerasField && camerasField.selector.entity.multiple === true)
);

// A frame somebody asked for has to survive the next state update. `setHass` runs an
// unforced refresh and Home Assistant pushes state constantly, so a stamp recomputed
// on every call put the pre-capture URL back within about a second -- and the browser
// served that one from cache. The captured frame flashed up and vanished.
{
  const hass = {
    states: {
      "camera.x": {
        state: "idle",
        attributes: { friendly_name: "Дверь", entity_picture: "/api/camera_proxy/x?t=1" },
      },
    },
    entities: {
      "camera.x": { device_id: "dev", platform: "loki" },
      "button.x": { device_id: "dev", platform: "loki" },
    },
    devices: { dev: { identifiers: [["loki", "1"]], name: "Дверь" } },
    panels: {},
  };
  const card = new defined["loki-door-card"]();
  card.setConfig({ camera: "camera.x" });
  card.hass = hass;
  const media = card._media;
  const bucketed = media._img.getAttribute("src");
  media.reload();
  const forced = media._img.getAttribute("src");
  // What Home Assistant does, over and over, for reasons that have nothing to do
  // with this camera.
  for (let i = 0; i < 5; i++) card.hass = hass;
  const after = media._img.getAttribute("src");
  check("the capture button changes the picture URL", Boolean(forced) && bucketed !== forced);
  check("a captured frame survives the state updates that follow", forced === after);
  media.destroy();
}

// The column beside the picture holds the open button and, during a call, the
// hang-up button. A plain camera has neither, and keeping the column gave its
// picture 70% of the card with a blank strip next to it.
{
  const hass = {
    states: {
      "camera.door": {
        state: "idle",
        attributes: { friendly_name: "Дверь", entity_picture: "/api/camera_proxy/door" },
      },
      "camera.yard": {
        state: "idle",
        attributes: { friendly_name: "Двор", entity_picture: "/api/camera_proxy/yard" },
      },
      "camera.gate": {
        state: "idle",
        attributes: { friendly_name: "Калитка", entity_picture: "/api/camera_proxy/gate" },
      },
      "binary_sensor.gate": { state: "on", last_changed: new Date().toISOString() },
    },
    entities: {
      "camera.door": { device_id: "door", platform: "loki" },
      "button.door": { device_id: "door", platform: "loki" },
      "camera.yard": { device_id: "yard", platform: "loki" },
      // A door with no lock to open, ringing: the hang-up button needs the column.
      "camera.gate": { device_id: "gate", platform: "loki" },
      "binary_sensor.gate": { device_id: "gate", platform: "loki" },
    },
    devices: {
      door: { identifiers: [["loki", "1"]], name: "Дверь" },
      yard: { identifiers: [["loki", "2"]], name: "Двор" },
      gate: { identifiers: [["loki", "3"]], name: "Калитка" },
    },
    panels: {},
  };
  const show = (camera) => {
    const card = new defined["loki-door-card"]();
    card.setConfig({ camera });
    card.hass = hass;
    const alone = card._side.hidden && card._grid.classList.contains("loki-alone");
    const beside = !card._side.hidden && !card._grid.classList.contains("loki-alone");
    card.disconnectedCallback();
    return { alone, beside };
  };
  check("a door keeps the column for its open button", show("camera.door").beside);
  check("a plain camera's picture takes the whole row", show("camera.yard").alone);
  check(
    "a door with no lock keeps the column while a call is up",
    show("camera.gate").beside
  );
}

// The one-door page hands the card its height and asks it to fill the page; a
// dashboard card asks for nothing and keeps its own 16:9.
{
  const hass = {
    states: {
      "camera.x": {
        state: "idle",
        attributes: { friendly_name: "Дверь", entity_picture: "/api/camera_proxy/x" },
      },
    },
    entities: {
      "camera.x": { device_id: "dev", platform: "loki" },
      "button.x": { device_id: "dev", platform: "loki" },
    },
    devices: { dev: { identifiers: [["loki", "1"]], name: "Дверь" } },
    panels: { loki: {} },
  };
  const fills = (config) => {
    const card = new defined["loki-door-card"]();
    card.setConfig(config);
    card.hass = hass;
    const on = card._card.classList.contains("loki-fill");
    card.disconnectedCallback();
    return on;
  };
  check("a dashboard card does not try to fill its host", !fills({ camera: "camera.x" }));
  check("a card asked to fill the page says so on itself", fills({ camera: "camera.x", fill: true }));

  const panel = new defined["loki-panel"]();
  panel._hass = hass;
  panel._door = "1";
  const wrap = panel._buildOne();
  const config = panel._card && panel._card.config;
  check(
    "the one-door page is the card: it asks the card to fill the page",
    wrap.className.split(" ").includes("loki-one") &&
      Boolean(config) &&
      config.camera === "camera.x" &&
      config.fill === true,
    JSON.stringify({ className: wrap.className, config })
  );
}

// Every class that lays itself out with flex or grid must also say what [hidden] means
// for it. The cards live in the light DOM, where a blanket `[hidden] { display: none }`
// would blank Home Assistant's own layout -- so the rule is spelled out per class, and
// a class added without one silently ignores `element.hidden = true`. That has bitten
// twice: a confirmation strip that appeared unasked, and an open button that stayed on
// a door with no lock to open.
const STYLE_BLOCK = SOURCE.match(/const STYLE = `([\s\S]*?)`;/);
check("the stylesheet is where this check expects it", Boolean(STYLE_BLOCK));
if (STYLE_BLOCK) {
  const css = STYLE_BLOCK[1];
  const missing = [];
  for (const [, name, body] of css.matchAll(/\.(loki-[a-z-]+)\s*\{([^}]*)\}/g)) {
    if (!/display:\s*(?:inline-)?(?:flex|grid)/.test(body)) continue;
    if (!css.includes(`.${name}[hidden]`)) missing.push(name);
  }
  check(
    "every flex/grid class says what [hidden] means for it",
    missing.length === 0,
    missing.join(", ")
  );
}

// ---- the page's arrangement -------------------------------------------------

/** Four doors, alphabetical by entity id -- the order the wall starts in -- and,
 * when asked, plain cameras: a picture with no open button on the device. */
function doorsHass(extra, plain = []) {
  const hass = { states: {}, entities: {}, devices: {}, panels: {}, ...extra };
  for (const id of plain) {
    hass.states[`camera.${id}`] = {
      state: "idle",
      attributes: {
        friendly_name: id.toUpperCase(),
        entity_picture: `/api/camera_proxy/camera.${id}?t=1`,
      },
    };
    hass.entities[`camera.${id}`] = { device_id: `dev-${id}`, platform: "loki" };
    hass.devices[`dev-${id}`] = {
      identifiers: [["loki", String(id.charCodeAt(0))]],
      name: id.toUpperCase(),
    };
  }
  for (const id of ["a", "b", "c", "d"]) {
    hass.states[`camera.${id}`] = {
      state: "idle",
      attributes: {
        friendly_name: id.toUpperCase(),
        entity_picture: `/api/camera_proxy/camera.${id}?t=1`,
      },
    };
    hass.entities[`camera.${id}`] = { device_id: `dev-${id}`, platform: "loki" };
    hass.entities[`button.${id}`] = { device_id: `dev-${id}`, platform: "loki" };
    hass.devices[`dev-${id}`] = {
      identifiers: [["loki", String(id.charCodeAt(0))]],
      name: id.toUpperCase(),
    };
  }
  return hass;
}

// ---- Shift on the wall's live button ----------------------------------------
// Thirty seconds is right for a glance along a row of doors and wrong for standing
// and watching, and Shift is the press that means "and leave it on". Everything here
// goes through the buttons themselves rather than through the methods behind them:
// the one line that connects the press to the method is product code too, and
// reverting it removes the whole feature without failing a thing.
{
  const Wall = defined["loki-wall-card"];
  const walls = doorsHass();
  const press = (node, shift) =>
    node.dispatchEvent({ type: "click", shiftKey: Boolean(shift) });
  // The strip is built as question, «Да», «Нет»; the affirmative is the red one and
  // the other button is the only child with no class of its own.
  const yesOf = (wall) => wall._confirm.children.find((node) => node.className === "danger");
  const noOf = (wall) => wall._confirm.children.find((node) => node.className === "");
  const make = (config) => {
    const wall = new Wall();
    wall.setConfig({ live_timeout: 30, ...config });
    wall.hass = walls;
    return wall;
  };

  const run = (shift) => {
    const wall = make();
    const out = { idleTitle: wall._liveBtn.title };
    press(wall._liveBtn, shift);
    out.asked = wall._confirm.hidden === false;
    out.question = wall._confirmQuestion.textContent;
    press(yesOf(wall), false);
    out.timer = wall._liveTimer !== null;
    out.label = wall._liveLabel.textContent;
    out.title = wall._liveBtn.title;
    out.aria = wall._liveBtn.getAttribute("aria-label");
    press(wall._liveBtn, false);
    out.stopped =
      wall._live === false && wall._liveTimer === null && wall._noTimer === false;
    wall._teardown();
    return out;
  };

  const plain = run(false);
  const held = run(true);

  check("a plain press asks first and arms the timer", plain.asked && plain.timer);
  check(
    "the idle button says the key exists -- the only place it is discoverable",
    plain.idleTitle.includes("Shift") && plain.idleTitle === held.idleTitle,
    plain.idleTitle
  );
  check(
    "Shift asks a question that warns about the part that is new",
    held.asked
      && held.question.includes("не выключать само")
      && !plain.question.includes("не выключать само"),
    held.question
  );
  check("Shift leaves the wall live with no timer behind it", held.timer === false);
  check(
    "a wall left on says so on the button, in the glyph and in words",
    held.label.includes("\u221e")
      && !plain.label.includes("\u221e")
      && String(held.aria).includes("не выключится")
      && plain.aria === null,
    `${plain.label} | ${held.label} | ${held.aria}`
  );
  check(
    "the two live tooltips differ, and only the timed one offers the key",
    plain.title !== held.title
      && plain.title.includes("Shift")
      && !held.title.includes("Shift"),
    `${plain.title} | ${held.title}`
  );
  check(
    "a plain press stops it and puts the answer back for the next one",
    plain.stopped && held.stopped
  );

  // The question is the last place to add "and leave it on" to a press that started
  // without the key held -- and «Нет» has to forget the answer either way.
  const late = make();
  press(late._liveBtn, false);
  press(yesOf(late), true);
  check(
    "Shift on the confirmation's yes starts an unlimited run too",
    late._live === true
      && late._liveTimer === null
      && late._liveLabel.textContent === held.label,
    late._liveLabel.textContent
  );
  press(late._liveBtn, false);
  late._teardown();

  const backedOut = make();
  press(backedOut._liveBtn, true);
  press(noOf(backedOut), false);
  check(
    "backing out of the Shift question forgets it",
    backedOut._confirm.hidden === true
      && backedOut._noTimer === false
      && backedOut._live === false
  );
  backedOut._teardown();

  // Shift on a wall already live is "take the timer off", not "stop": the streams
  // are open already, and stopping them is what the plain press is for.
  const onTheFly = make();
  press(onTheFly._liveBtn, false);
  press(yesOf(onTheFly), false);
  const armed = onTheFly._liveTimer !== null;
  press(onTheFly._liveBtn, true);
  check(
    "Shift on a live wall cancels the timer instead of stopping the video, and says so",
    armed
      && onTheFly._live === true
      && onTheFly._liveTimer === null
      && onTheFly._liveLabel.textContent === held.label
      && onTheFly._liveBtn.title === held.title,
    `${onTheFly._liveLabel.textContent} | ${onTheFly._liveBtn.title}`
  );
  press(onTheFly._liveBtn, false);
  check("and a plain press still stops it", onTheFly._live === false);
  onTheFly._teardown();

  // The question on its own: a repeat of the same press takes it back, a press that
  // changes the key rewords it rather than dismissing it.
  const asking = make();
  const seen = [];
  const left = [];
  for (const shift of [false, true, true, false, false]) {
    press(asking._liveBtn, shift);
    seen.push(
      asking._confirm.hidden
        ? "hidden"
        : asking._confirmQuestion.textContent.includes("не выключать само")
          ? "noTimer"
          : "plain"
    );
    left.push(asking._noTimer);
  }
  check(
    "the question follows the key: it rewords on a change and closes on a repeat",
    seen.join() === "plain,noTimer,hidden,plain,hidden",
    seen.join()
  );
  // The third press is the one that matters here: a Shift question taken back with
  // Shift. The plain presses reset the flag on their own way in, so an assertion at
  // the end of the run would pass whether or not anything was ever retracted.
  check(
    "a question taken back leaves no answer standing",
    left.join() === "false,true,false,false,false",
    left.join()
  );
  asking._teardown();

  // A wall held open with Shift has no timer to end it, so the video host going
  // away must end it instead: the button that would stop it is disabled while the
  // host is down, and before 1.9.0 the timeout was what cleared this state.
  const gone = {
    ...walls,
    states: { ...walls.states, "binary_sensor.stream": { state: "off" } },
  };
  const outage = make({ stream_sensor: "binary_sensor.stream" });
  press(outage._liveBtn, true);
  press(yesOf(outage), false);
  const wasHeld = outage._live === true && outage._liveTimer === null;
  outage.hass = gone;
  check(
    "an unlimited wall does not sit out the video host going away",
    wasHeld
      && outage._live === false
      && outage._noTimer === false
      && outage._liveBtn.disabled === true,
    `${outage._live} | ${outage._noTimer}`
  );
  outage._teardown();

  // The mark has to follow the timer, not the key: Lovelace calls setConfig on the
  // element it already has, so the setting can change underneath a running stream.
  const edited = make({ live_timeout: 0 });
  press(edited._liveBtn, false);
  press(yesOf(edited), false);
  const wasMarked = edited._liveLabel.textContent === held.label;
  edited.setConfig({ live_timeout: 30 });
  edited.hass = walls;
  check(
    "a stream that will never stop keeps its mark when the setting changes under it",
    wasMarked && edited._liveLabel.textContent === held.label,
    edited._liveLabel.textContent
  );
  edited._teardown();

  const reversed = make({ live_timeout: 30 });
  press(reversed._liveBtn, false);
  press(yesOf(reversed), false);
  const wasTimed = reversed._liveTimer !== null;
  reversed.setConfig({ live_timeout: 0 });
  reversed.hass = walls;
  check(
    "and a timed stream does not borrow the mark from a setting it never used",
    wasTimed
      && reversed._liveTimer !== null
      && reversed._liveLabel.textContent === plain.label,
    reversed._liveLabel.textContent
  );
  press(reversed._liveBtn, false);
  reversed._teardown();

  // «Сам выключить видео через» = 0 is the same unlimited wall by another door. The
  // mark belongs to the fact, not to the key -- otherwise the one wall that really
  // will still be streaming in an hour is the one with nothing on screen saying so.
  const forever = make({ live_timeout: 0 });
  const foreverIdle = forever._liveBtn.title;
  press(forever._liveBtn, false);
  const foreverQuestion = forever._confirmQuestion.textContent;
  press(yesOf(forever), false);
  check(
    "a wall configured never to stop wears the same mark as one held with Shift",
    forever._liveTimer === null
      && forever._liveLabel.textContent === held.label
      && forever._liveBtn.title === held.title
      && foreverQuestion === held.question
      && foreverIdle === "",
    `${forever._liveLabel.textContent} | ${forever._liveBtn.title} | idle="${foreverIdle}"`
  );
  press(forever._liveBtn, false);
  forever._teardown();
}

// ---- Shift on one door's camera button ---------------------------------------
// The same key on the other card, and it has to behave the same way: the door opened
// from a notification and the wall glanced at along a row are the same person on the
// same tablet ten seconds apart. No question here -- one stream is not twenty -- so
// the press acts at once, and the LIVE badge is what says the stream will not stop.
{
  const Door = defined["loki-door-card"];
  const doors = doorsHass();
  // The same account with its video host reported down -- a routine event, not a
  // hypothetical: the media host is a different machine from the API and is blocked
  // whole under a VPN.
  const away = {
    ...doors,
    states: { ...doors.states, "binary_sensor.stream": { state: "off" } },
  };
  const press = (node, shift) =>
    node.dispatchEvent({ type: "click", shiftKey: Boolean(shift) });
  const door = (config, hass) => {
    const card = new Door();
    card.setConfig({ camera: "camera.a", live_timeout: 30, ...config });
    card.hass = hass || doors;
    return card;
  };

  const watch = (shift) => {
    const card = door();
    const out = { idleTitle: card._liveBtn.title };
    press(card._liveBtn, shift);
    out.live = card._live;
    out.timer = card._liveTimer !== null;
    out.badge = card._liveBadge.textContent;
    out.title = card._liveBtn.title;
    out.name = card._liveBtn.getAttribute("aria-label");
    out.icon = card._liveBtn.firstChild.getAttribute("icon");
    press(card._liveBtn, false);
    out.stopped =
      card._live === false && card._liveTimer === null && card._noTimer === false;
    out.iconBack = card._liveBtn.firstChild.getAttribute("icon");
    card.disconnectedCallback();
    return out;
  };

  const plain = watch(false);
  const held = watch(true);

  check("a plain press on a door still arms the timer", plain.live && plain.timer);
  check(
    "the door's idle tooltip carries the name and the key",
    plain.idleTitle.includes("Вживую") && plain.idleTitle.includes("Shift"),
    plain.idleTitle
  );
  check("Shift leaves one door live with no timer behind it", held.live && !held.timer);
  check(
    "the LIVE badge is what says the stream will not stop by itself",
    held.badge.includes("\u221e") && !plain.badge.includes("\u221e"),
    `${plain.badge} | ${held.badge}`
  );
  check(
    "the door's button says it in words too, and keeps its name short",
    String(held.name).includes("не выключится")
      && plain.name === "Стоп"
      && !held.title.includes("Shift")
      && plain.title.includes("Shift"),
    `${plain.name} / ${plain.title} | ${held.name} / ${held.title}`
  );
  check("a plain press stops the door and forgets the key", plain.stopped && held.stopped);
  check(
    "the camera icon becomes a stop icon while the stream is up, and back after",
    plain.icon === "mdi:video-off-outline"
      && held.icon === "mdi:video-off-outline"
      && plain.iconBack === "mdi:video-outline",
    `${plain.icon} -> ${plain.iconBack}`
  );

  // A stream with no timer behind it must not outlive the host it comes from: the
  // button that would stop it is disabled while the host is away, and before 1.9.0
  // the timeout was what cleared this state.
  const outage = door({ stream_sensor: "binary_sensor.stream" });
  press(outage._liveBtn, true);
  const wasHeld = outage._live === true && outage._liveTimer === null;
  outage.hass = away;
  check(
    "an unlimited stream does not sit out the video host going away",
    wasHeld
      && outage._live === false
      && outage._noTimer === false
      && outage._liveBtn.disabled === true
      && outage._liveBadge.hidden === true,
    `${outage._live} | ${outage._noTimer}`
  );
  outage.disconnectedCallback();

  // Shift on a door already live takes the timer off rather than stopping it.
  const onTheFly = door();
  press(onTheFly._liveBtn, false);
  const armed = onTheFly._liveTimer !== null;
  press(onTheFly._liveBtn, true);
  check(
    "Shift on a live door cancels the timer instead of stopping the video, and says so",
    armed
      && onTheFly._live === true
      && onTheFly._liveTimer === null
      && onTheFly._liveBadge.textContent === held.badge
      && onTheFly._liveBtn.title === held.title,
    `${onTheFly._liveBadge.textContent} | ${onTheFly._liveBtn.title}`
  );
  press(onTheFly._liveBtn, false);
  check("and a plain press still stops the door", onTheFly._live === false);
  onTheFly.disconnectedCallback();

  // The same wall by another door: the setting at 0 needs no key at all.
  const forever = door({ live_timeout: 0 });
  const foreverIdle = forever._liveBtn.title;
  press(forever._liveBtn, false);
  check(
    "a door configured never to stop wears the same mark as one held with Shift",
    forever._liveTimer === null
      && forever._liveBadge.textContent === held.badge
      && forever._liveBtn.title === held.title
      && foreverIdle === "Вживую",
    `${forever._liveBadge.textContent} | ${forever._liveBtn.title} | idle="${foreverIdle}"`
  );
  forever.disconnectedCallback();

  // The same for one door: the badge follows the timer, not the setting read a
  // paint later, in both directions.
  const edited = door({ live_timeout: 0 });
  press(edited._liveBtn, false);
  const wasMarked = edited._liveBadge.textContent === held.badge;
  edited.setConfig({ camera: "camera.a", live_timeout: 30 });
  edited.hass = doors;
  check(
    "a door that will never stop keeps its mark when the setting changes under it",
    wasMarked && edited._liveBadge.textContent === held.badge,
    edited._liveBadge.textContent
  );
  edited.disconnectedCallback();

  const reversed = door({ live_timeout: 30 });
  press(reversed._liveBtn, false);
  const wasTimed = reversed._liveTimer !== null;
  reversed.setConfig({ camera: "camera.a", live_timeout: 0 });
  reversed.hass = doors;
  check(
    "and a timed door does not borrow the mark from a setting it never used",
    wasTimed
      && reversed._liveTimer !== null
      && reversed._liveBadge.textContent === plain.badge,
    reversed._liveBadge.textContent
  );
  reversed.disconnectedCallback();

  // ``live: true`` is the door's own page and a wall panel woken by a ring. Nobody
  // was standing there to hold a key, so it keeps the timeout it was configured with
  // -- an unlimited stream must never be something a notification can start.
  const opened = door({ live: true });
  check(
    "a page that opens straight into the stream keeps its timeout",
    opened._live === true
      && opened._liveTimer !== null
      && opened._noTimer === false
      && opened._liveBadge.textContent === plain.badge,
    opened._liveBadge.textContent
  );
  opened.disconnectedCallback();

  // ...and it must not turn one off either. setConfig keeps _live but clears
  // _autoLive, so the gate fires again at a stream that is already running.
  const reconfigured = door({ live: true });
  press(reconfigured._liveBtn, true);
  const heldOpen = reconfigured._live === true && reconfigured._liveTimer === null;
  reconfigured.setConfig({ camera: "camera.a", live_timeout: 30, live: true, name: "Вход" });
  reconfigured.hass = doors;
  check(
    "editing an unrelated option does not throw away a stream held open",
    heldOpen && reconfigured._live === true && reconfigured._liveTimer === null,
    `${reconfigured._live} | ${reconfigured._liveTimer}`
  );
  reconfigured.disconnectedCallback();
}

/** A store that answers at once and remembers what it was asked to keep. */
function fakeStore(layout) {
  const store = {
    saved: [],
    load: async () => layout,
    save: async (_hass, message) => {
      store.saved.push(message);
      return message;
    },
  };
  return store;
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

async function checkLayout() {
  const Wall = defined["loki-wall-card"];
  const admin = doorsHass({ user: { is_admin: true } });

  // Stored: c before a, b put away, d never mentioned -- so d goes last.
  const card = new Wall();
  card.setConfig({});
  card.layoutStore = fakeStore({
    entry_id: "e1",
    order: ["camera.c", "camera.a"],
    hidden: ["camera.b"],
    tile_size: null,
  });
  card.hass = admin;
  check(
    "nothing is drawn while the arrangement is still loading",
    card._arranged().pending === true
  );
  await settle();
  let { active, hidden } = card._arranged();
  check(
    "stored order wins, unknown doors go last, put-away doors are off the wall",
    active.join() === "camera.c,camera.a,camera.d" && hidden.join() === "camera.b",
    `${active.join()} | ${hidden.join()}`
  );
  check("an admin with a store gets the pencil", card._editBtn.hidden === false);
  check(
    "the tile size falls back to the card's own when none was chosen",
    card._tileSize() === "medium"
  );
  check(
    "put-away tiles are not built outside the editor",
    !card._tiles.has("camera.b")
  );

  card._setEditing(true);
  check(
    "the editor swaps the pencil for done and the sizes",
    card._editBtn.hidden && !card._doneBtn.hidden && !card._sizes.hidden
  );
  const tileA = card._tiles.get("camera.a");
  check(
    "no door opens from the editor",
    tileA.open.hidden === true && tileA.putAwayBtn.hidden === false && tileA.handle.hidden === false
  );
  const tileB = card._tiles.get("camera.b");
  check(
    "a put-away tile offers bring back and nothing else",
    Boolean(tileB)
      && tileB.restore.hidden === false
      && tileB.open.hidden === true
      && tileB.putAwayBtn.hidden === true
      && tileB.handle.hidden === true
  );

  card._putAway("camera.d");
  ({ active, hidden } = card._arranged());
  check(
    "a put-away door leaves the wall and joins the end of the put-away list",
    active.join() === "camera.c,camera.a" && hidden.join() === "camera.b,camera.d",
    `${active.join()} | ${hidden.join()}`
  );
  card._bringBack("camera.b");
  ({ active, hidden } = card._arranged());
  check(
    "a door brought back goes to the end of the wall, before the put-away ones",
    active.join() === "camera.c,camera.a,camera.b" && hidden.join() === "camera.d",
    `${active.join()} | ${hidden.join()}`
  );
  card._layout.move("camera.b", 0, card._roster());
  card._update();
  check(
    "moving a door puts it at the index asked for",
    card._arranged().active.join() === "camera.b,camera.c,camera.a",
    card._arranged().active.join()
  );
  card._chooseSize("large");
  check("choosing a size applies it", card._tileSize() === "large");
  await settle();
  const { saved } = card._store;
  const last = saved[saved.length - 1];
  check(
    "every change is written back, with the entry it belongs to",
    saved.length === 3
      && Boolean(last)
      && last.entry_id === "e1"
      && last.tile_size === "large"
      && last.order.join() === "camera.b,camera.c,camera.a"
      && last.hidden.join() === "camera.d",
    JSON.stringify(saved)
  );

  card._setEditing(false);
  check(
    "leaving the editor takes the put-away tiles off the wall",
    !card._tiles.has("camera.d") && card._tiles.has("camera.b")
  );

  // Ids of doors the account does not report right now survive a change: a hidden
  // door that is away for a day must come back still hidden.
  const Layout = card._layout.constructor;
  const layout = new Layout({ order: ["camera.gone", "camera.a"], hidden: ["camera.away"] });
  layout.hide("camera.a", { doors: ["camera.a", "camera.b"], cameras: [] });
  const message = layout.message("medium");
  check(
    "absent doors keep their place in the stored lists",
    message.order.includes("camera.gone") && message.hidden.join() === "camera.away,camera.a",
    JSON.stringify(message)
  );
  // Restoring puts a door at the end of the wall even when nothing was ever
  // arranged -- not back into its alphabetical slot.
  const fresh = new Layout({ hidden: ["camera.a"] });
  const three = { doors: ["camera.a", "camera.b", "camera.c"], cameras: [] };
  fresh.restore("camera.a", three);
  check(
    "a restored door lands after the others, not where the alphabet had it",
    fresh.arrange(three).active.join() === "camera.b,camera.c,camera.a"
  );

  // Plain cameras -- a picture with no lock behind it -- are on the roster but put
  // away until somebody brings one out, below the doors put away on purpose.
  const mixed = doorsHass({ user: { is_admin: true } }, ["x", "y"]);
  const yard = new Wall();
  yard.setConfig({});
  yard.layoutStore = fakeStore({ entry_id: "e1", order: [], hidden: ["camera.d"], tile_size: null });
  yard.hass = mixed;
  await settle();
  let got = yard._arranged();
  check(
    "plain cameras start put away, after the doors somebody put away",
    got.active.join() === "camera.a,camera.b,camera.c"
      && got.hidden.join() === "camera.d,camera.x,camera.y",
    `${got.active.join()} | ${got.hidden.join()}`
  );
  yard._setEditing(true);
  yard._bringBack("camera.y");
  yard._setEditing(false);
  got = yard._arranged();
  const tileY = yard._tiles.get("camera.y");
  check(
    "a plain camera brought out joins the end of the wall, with no open button",
    got.active.join() === "camera.a,camera.b,camera.c,camera.y"
      && got.hidden.join() === "camera.d,camera.x"
      && Boolean(tileY) && tileY.open.hidden === true && tileY.liveBtn.hidden === false,
    `${got.active.join()} | ${got.hidden.join()}`
  );
  yard._setEditing(true);
  yard._putAway("camera.y");
  got = yard._arranged();
  check(
    "a plain camera put away again lists with the doors put away, ahead of the rest",
    got.active.join() === "camera.a,camera.b,camera.c"
      && got.hidden.join() === "camera.d,camera.y,camera.x",
    `${got.active.join()} | ${got.hidden.join()}`
  );
  yard._setEditing(false);
  await settle();
  check(
    "the stored lists carry the camera, so the choice survives a reload",
    yard._store.saved.length === 2 && yard._store.saved[1].hidden.join() === "camera.d,camera.y",
    JSON.stringify(yard._store.saved)
  );
  const doorsOnly = new Wall();
  doorsOnly.setConfig({});
  doorsOnly.hass = mixed;
  check(
    "a dashboard card without a store shows doors only",
    doorsOnly._arranged().active.join() === "camera.a,camera.b,camera.c,camera.d"
  );
  yard._teardown();
  doorsOnly._teardown();

  // No pencil on a dashboard card, for anyone but an admin, or when the integration
  // could not say which entry to write.
  const plain = new Wall();
  plain.setConfig({});
  plain.hass = admin;
  check("a dashboard card has no pencil", plain._editBtn.hidden === true);

  const guest = new Wall();
  guest.setConfig({});
  guest.layoutStore = fakeStore({ entry_id: "e1", order: [], hidden: [], tile_size: null });
  guest.hass = doorsHass({ user: { is_admin: false } });
  await settle();
  check(
    "a guest sees the wall but gets no pencil",
    guest._editBtn.hidden === true && guest._arranged().active.length === 4
  );

  const nowhere = new Wall();
  nowhere.setConfig({});
  nowhere.layoutStore = fakeStore({ entry_id: null, order: [], hidden: [], tile_size: null });
  nowhere.hass = admin;
  await settle();
  check(
    "no pencil when the integration cannot say which entry to write",
    nowhere._editBtn.hidden === true && nowhere._arranged().active.length === 4
  );

  // A store that fails still yields a page: everything, alphabetical, no pencil.
  const broken = new Wall();
  broken.setConfig({});
  const realWarn = console.warn;
  console.warn = () => {};
  broken.layoutStore = {
    load: async () => {
      throw new Error("no");
    },
    save: async () => {},
  };
  broken.hass = admin;
  await settle();
  console.warn = realWarn;
  check(
    "a store that fails leaves the wall whole and uneditable",
    broken._arranged().active.length === 4 && broken._editBtn.hidden === true
  );

  for (const each of [card, plain, guest, nowhere, broken]) each._teardown();
}

checkLayout()
  .catch((err) => {
    check("layout checks run to the end", false, (err && err.stack) || String(err));
  })
  .then(() => {
    if (failures.length) {
      console.error(`\n${failures.length} check(s) failed`);
      process.exit(1);
    }
    console.log("\nall card checks passed");
  });
