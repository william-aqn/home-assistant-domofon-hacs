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
    if (defined[name]) throw new Error(`${name} defined twice`);
    defined[name] = cls;
  },
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

// eslint-disable-next-line no-eval
eval(fs.readFileSync(BUNDLE, "utf8"));

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

// A card that renders nothing must say why, or Lovelace shows an empty box.
let rejected = false;
try {
  new defined["loki-door-card"]().setConfig({});
} catch (err) {
  rejected = Boolean(err.message);
}
check("door card refuses a config with no door", rejected);

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
check(
  "wall card offers a multi-entity list",
  Boolean(camerasField && camerasField.selector.entity.multiple === true)
);

if (failures.length) {
  console.error(`\n${failures.length} check(s) failed`);
  process.exit(1);
}
console.log("\nall card checks passed");
