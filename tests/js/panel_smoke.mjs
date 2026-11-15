/**
 * Smoke test for the panel module, run without a browser.
 *
 * `node --check` only proves the file parses. This runs the real render path with
 * a stubbed DOM and a fake `hass`, then asserts that the SVG it produces contains
 * what the panel promises: the measured line, one path per scenario, the archived
 * runs, the "now" marker and the parameter chips. A runtime error here would
 * otherwise only show up as a blank page in a browser.
 *
 *   node tests/js/panel_smoke.mjs
 */

import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const modulePath = resolve(root, "custom_components/thermal_model/frontend/thermal-model-panel.js");
const source = readFileSync(modulePath, "utf8");

const failures = [];
const check = (condition, message) => {
  if (!condition) failures.push(message);
};

// -- stubs ------------------------------------------------------------
let defined = null;
class FakeNode {
  constructor() {
    this.innerHTML = "";
  }
  querySelectorAll() {
    return [];
  }
  querySelector() {
    return null;
  }
  insertAdjacentHTML() {}
  addEventListener() {}
}
class FakeElement extends FakeNode {
  attachShadow() {
    this.shadowRoot = new FakeNode();
    return this.shadowRoot;
  }
}

const sandbox = {
  HTMLElement: FakeElement,
  customElements: {
    define: (name, klass) => {
      defined = { name, klass };
    },
  },
  window: { addEventListener() {}, removeEventListener() {}, setInterval: () => 1, clearInterval() {} },
  Date,
  Number,
  Object,
  Math,
  Set,
  Array,
  String,
  console,
};
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: modulePath });

check(defined !== null, "customElements.define was never called");
if (!defined) {
  console.error(failures.join("\n"));
  process.exit(1);
}
check(defined.name === "thermal-model-panel", `unexpected element name: ${defined.name}`);

// -- fake hass --------------------------------------------------------
const now = Date.now();
const iso = (offsetHours) => new Date(now + offsetHours * 3600 * 1000).toISOString();

const forecastAttributes = {
  issued_at: iso(0),
  scenario: "free_float",
  horizon_hours: 48,
  weather_model: "best_match",
  params_digest: "0.01600/0.00230/+0.0400/0.4900",
  params_source: "manual_default",
  indoor_temperature_now: 20.4,
  outdoor_temperature_now: 6.1,
  minimum: 18.1,
  minimum_at: iso(12),
  stale: false,
  error: null,
  times: Array.from({ length: 49 }, (_, index) => iso(index)),
  values: Array.from({ length: 49 }, (_, index) => 20.4 - index * 0.02),
  weather_times: Array.from({ length: 48 }, (_, index) => iso(index)),
  weather_outdoor: Array.from({ length: 48 }, () => 6.0),
  archived_runs: [
    {
      issued_at: iso(-6),
      age_hours: 6,
      times: Array.from({ length: 49 }, (_, index) => iso(index - 6)),
      values: Array.from({ length: 49 }, (_, index) => 20.9 - index * 0.02),
    },
    {
      issued_at: iso(-12),
      age_hours: 12,
      times: Array.from({ length: 49 }, (_, index) => iso(index - 12)),
      values: Array.from({ length: 49 }, (_, index) => 21.3 - index * 0.02),
    },
  ],
};

const hass = {
  states: {
    "sensor.home_indoor_temperature": { state: "20.4", attributes: {} },
    "sensor.indoor_temperature": { state: "20.4", attributes: {} },
    "sensor.outdoor_temperature": { state: "6.1", attributes: {} },
    "sensor.home_forecast_free_float": { state: "20.4", attributes: forecastAttributes },
    "sensor.home_forecast_thermostat": { state: "21.0", attributes: forecastAttributes },
    "number.home_wall_loss_a": { state: "0.016", attributes: {} },
    "number.home_solar_gain_c_s": { state: "0.0023", attributes: {} },
    "number.home_internal_gain_d": { state: "0.04", attributes: {} },
    "number.home_heating_gain_k": { state: "0.49", attributes: {} },
  },
  callWS: async () => ({
    "sensor.home_indoor_temperature": [
      { s: "20.1", lu: now - 3 * 3600 * 1000 },
      { s: "20.4", lu: now - 1 * 3600 * 1000 },
    ],
    "sensor.outdoor_temperature": [{ s: "6.1", lu: now - 2 * 3600 * 1000 }],
  }),
  callService: async () => {},
};

const panel = {
  title: "Home",
  config: {
    entry_id: "abc",
    indoor_entity: "sensor.home_indoor_temperature",
    source_indoor_entity: "sensor.indoor_temperature",
    source_outdoor_entity: "sensor.outdoor_temperature",
    scenarios: {
      free_float: "sensor.home_forecast_free_float",
      thermostat: "sensor.home_forecast_thermostat",
    },
    numbers: {
      a: "number.home_wall_loss_a",
      c_s: "number.home_solar_gain_c_s",
      d: "number.home_internal_gain_d",
      k: "number.home_heating_gain_k",
    },
  },
};

// -- render -----------------------------------------------------------
const element = new defined.klass();
element.panel = panel;
element._history = {
  indoor: [
    [now - 4 * 3600 * 1000, 20.0],
    [now - 2 * 3600 * 1000, 20.3],
  ],
  outdoor: [[now - 3 * 3600 * 1000, 5.4]],
};
element.hass = hass;

const html = element.shadowRoot.innerHTML;
check(html.includes("<svg"), "no SVG in the rendered panel");
check(html.includes("<path"), "no path drawn: the series produced no data");
check(html.includes("#03a9f4"), "the measured series colour is missing");
check(html.includes("#ff9800"), "the free-float forecast colour is missing");
check(html.includes("#4caf50"), "the thermostat forecast colour is missing");
check(html.includes('stroke-dasharray="4 4"'), "archived runs are not dashed");
check(html.includes("maintenant"), "the 'now' marker is missing");
check(html.includes("prévisions passées comparées"), "archived runs are not labelled");
check(html.includes("Déperditions"), "parameter chips are missing");
check(html.includes("best_match"), "the weather model is not shown");
check(html.includes("0.01600"), "the parameter fingerprint is not shown");

// the archived runs must actually be plotted twice (two entries in the fake data)
const dashedCount = (html.match(/stroke-dasharray="4 4"/g) || []).length;
check(dashedCount >= 2, `expected at least 2 dashed lines, found ${dashedCount}`);

// hiding a series must remove it from the chart (the phase after the first render)
element._hidden = new Set(["measured"]);
element.hass = hass; // the setter re-renders
const withoutMeasured = element.shadowRoot.innerHTML;
check(
  !withoutMeasured.includes('stroke="#03a9f4" stroke-width="2.6"'),
  "hiding the measured series did not remove its path"
);

if (failures.length) {
  console.error("panel smoke test failed:\n- " + failures.join("\n- "));
  process.exit(1);
}
console.log("panel smoke test: ok");
