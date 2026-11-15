/**
 * The panel must not repaint on every `hass` push.
 *
 * Home Assistant hands a new `hass` object to the element on EVERY state change
 * (thousands per minute on this installation) while unchanged parts keep their
 * identity. A panel that re-renders unconditionally therefore flickers — this is
 * exactly what happened. The guard is tested here without a browser: `_update` is
 * replaced by a counter, and the `hass` setter must skip it when none of the
 * entities the panel displays changed.
 *
 *   node tests/js/panel_update_test.mjs
 */

import { copyFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const frontend = resolve(root, "custom_components/thermal_model/frontend");
// The panel is an ES module that imports the chart module relatively: copy both
// into a scratch directory under .mjs names so node loads them as modules.
const scratch = mkdtempSync(join(tmpdir(), "tm-panel-"));
const panelPath = join(scratch, "panel.mjs");
copyFileSync(join(frontend, "thermal-model-panel.js"), panelPath);
copyFileSync(join(frontend, "thermal-model-chart.mjs"), join(scratch, "thermal-model-chart.mjs"));

const failures = [];
const check = (condition, message) => {
  if (!condition) failures.push(message);
};

// -- stubs ------------------------------------------------------------
let defined = null;
const shadow = { innerHTML: "", querySelector: () => null, querySelectorAll: () => [] };
class FakeElement {
  attachShadow() {
    this.shadowRoot = shadow;
    return shadow;
  }
}
let timerQueue = [];
const sandbox = {
  HTMLElement: FakeElement,
  customElements: { define: (name, klass) => (defined = { name, klass }) },
  window: {
    addEventListener() {},
    removeEventListener() {},
    setInterval: () => 1,
    clearInterval() {},
    cancelAnimationFrame() {},
  },
  setTimeout: (callback) => {
    timerQueue.push(callback);
    return timerQueue.length;
  },
  clearTimeout: () => {},
  document: { head: { appendChild() {} }, createElement: () => new FakeElement() },
  Date,
  Number,
  Object,
  Math,
  Set,
  Array,
  String,
  console,
};
// globals are what the module touches at import time and while rendering
for (const [key, value] of Object.entries(sandbox)) {
  if (key !== "window" && key !== "document" && key !== "setTimeout" && key !== "clearTimeout") {
    globalThis[key] = value;
  }
}
globalThis.setTimeout = sandbox.setTimeout;
globalThis.clearTimeout = sandbox.clearTimeout;
globalThis.window = sandbox.window;
globalThis.document = sandbox.document;
await import(panelPath);

check(defined !== null, "the panel element was never defined");
if (!defined) {
  console.error(failures.join("\n"));
  process.exit(1);
}

const drainFrames = () => {
  // one coalescing timer per scheduled render
  const queue = timerQueue;
  timerQueue = [];
  for (const callback of queue) callback();
};

// -- fake state -------------------------------------------------------
const forecastState = {
  state: "20.4",
  attributes: { issued_at: "2026-11-15T15:00:00+00:00", values: [20.4, 20.3], times: [] },
};
const indoorState = { state: "20.4", attributes: {} };
const numberState = { state: "0.016", attributes: {} };

const baseStates = {
  "sensor.maison_indoor_temperature": indoorState,
  "sensor.temperature_moyenne_maison": indoorState,
  "sensor.ws2900_v2_01_18_outdoor_temperature": indoorState,
  "sensor.maison_forecast_free_float": forecastState,
  "number.maison_wall_loss_a": numberState,
};

const makeHass = (overrides = {}) => ({
  states: { ...baseStates, ...overrides },
  callWS: async () => ({}),
  callService: async () => {},
});

const panel = {
  title: "Maison",
  config: {
    entry_id: "test",
    indoor_entity: "sensor.maison_indoor_temperature",
    source_outdoor_entity: "sensor.ws2900_v2_01_18_outdoor_temperature",
    scenarios: { free_float: "sensor.maison_forecast_free_float" },
    numbers: { a: "number.maison_wall_loss_a" },
  },
};

// -- run --------------------------------------------------------------
const element = new defined.klass();
let renders = 0;
element._update = () => {
  renders += 1;
  return Promise.resolve();
};

element.panel = panel;
drainFrames();
const afterPanel = renders;
check(afterPanel === 1, `setting the panel must render once, got ${afterPanel}`);

element.hass = makeHass();
drainFrames();
const afterFirstHass = renders;
check(afterFirstHass === 2, `the first hass must render once, got ${renders - 1}`);

// 1. the same hass object pushed again: nothing changed
const same = makeHass();
element.hass = same;
drainFrames();
element.hass = same;
drainFrames();
check(renders === afterFirstHass, `an unchanged hass must not render (renders=${renders})`);

// 2. a NEW hass object whose relevant states are the same by identity: nothing changed
for (let index = 0; index < 30; index += 1) {
  element.hass = makeHass();
  drainFrames();
}
check(
  renders === afterFirstHass,
  `30 new hass objects with unchanged states must not render (renders=${renders})`,
);

// 3. many hass pushes inside one frame are coalesced into a single render
const changedForecast = { state: "20.6", attributes: { ...forecastState.attributes } };
for (let index = 0; index < 10; index += 1) {
  element.hass = makeHass({ "sensor.maison_forecast_free_float": changedForecast });
}
drainFrames();
check(
  renders === afterFirstHass + 1,
  `ten pushes in one frame must coalesce into one render, got ${renders - afterFirstHass}`,
);

// 4. a change on one of our entities does render
const nextForecast = { state: "20.7", attributes: { ...forecastState.attributes } };
element.hass = makeHass({ "sensor.maison_forecast_free_float": nextForecast });
drainFrames();
check(
  renders === afterFirstHass + 2,
  `a changed forecast entity must render once, got ${renders - afterFirstHass - 1}`,
);

if (failures.length) {
  console.error("panel update tests failed:\n- " + failures.join("\n- "));
  process.exit(1);
}
console.log("panel update tests: ok");
