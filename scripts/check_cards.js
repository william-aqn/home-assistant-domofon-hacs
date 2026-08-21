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
const stubElement = () => ({
  className: "",
  style: {},
  textContent: "",
  hidden: false,
  disabled: false,
  classList: { add() {}, remove() {} },
  append() {},
  appendChild() {},
  addEventListener() {},
  setAttribute() {},
  getAttribute() {
    return null;
  },
  remove() {},
  insertBefore() {},
  children: [],
});

global.window = global;
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

if (failures.length) {
  console.error(`\n${failures.length} check(s) failed`);
  process.exit(1);
}
console.log("\nall card checks passed");
