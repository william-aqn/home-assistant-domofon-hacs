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
  return {
    className: "",
    style: {},
    textContent: "",
    hidden: false,
    disabled: false,
    classList: { add() {}, remove() {}, toggle() {} },
    append() {},
    appendChild() {},
    addEventListener() {},
    removeEventListener() {},
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
    children: [],
    // The icon inside a button; the cards swap its `icon` attribute.
    firstChild: { setAttribute() {} },
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

/** Four doors, alphabetical by entity id -- the order the wall starts in. */
function doorsHass(extra) {
  const hass = { states: {}, entities: {}, devices: {}, panels: {}, ...extra };
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
  card._layout.move("camera.b", 0, card._available());
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
  layout.hide("camera.a", ["camera.a", "camera.b"]);
  const message = layout.message("medium");
  check(
    "absent doors keep their place in the stored lists",
    message.order.includes("camera.gone") && message.hidden.join() === "camera.away,camera.a",
    JSON.stringify(message)
  );
  // Restoring puts a door at the end of the wall even when nothing was ever
  // arranged -- not back into its alphabetical slot.
  const fresh = new Layout({ hidden: ["camera.a"] });
  fresh.restore("camera.a", ["camera.a", "camera.b", "camera.c"]);
  check(
    "a restored door lands after the others, not where the alphabet had it",
    fresh.arrange(["camera.a", "camera.b", "camera.c"]).active.join() === "camera.b,camera.c,camera.a"
  );

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
