/**
 * Tests for the panel's data layer, in node, without a browser.
 *
 * This is where a chart actually goes wrong: a series projected onto the wrong
 * instant, a missing sample silently plotted as 0 °C, a real hole bridged, an
 * archived run drawn at the wrong age, the outdoor range sharing the indoor axis.
 * The options handed to ApexCharts are asserted too — a missing axis unit or a dash
 * that silently disappears is the kind of regression nobody notices for weeks.
 *
 *   node tests/js/chart_test.mjs
 */

import {
  APEXCHARTS_VERSION,
  ARCHIVED_PREFIX,

  COLORS,
  buildChartData,
  buildPanelOptions,
  buildPanels,
  parseHistoryRows,
  summarise,
  toMillis,
  toSeriesData,
} from "../../custom_components/thermal_model/frontend/thermal-model-chart.mjs";

const failures = [];
const check = (condition, message) => {
  if (!condition) failures.push(message);
};
const equal = (actual, expected, message) =>
  check(
    JSON.stringify(actual) === JSON.stringify(expected),
    `${message} (got ${JSON.stringify(actual)}, expected ${JSON.stringify(expected)})`,
  );

const NOW = Date.parse("2026-01-10T12:00:00Z");
const HOUR = 3600 * 1000;
const iso = (offsetHours) => new Date(NOW + offsetHours * HOUR).toISOString();

const history = {
  indoor: [
    [NOW - 8 * HOUR, 20.0],
    [NOW - 7 * HOUR, 19.8],
    [NOW - 6 * HOUR, 19.6],
    // real hole: six hours with no measurement at all
    [NOW - 1 * HOUR, 20.3],
    [NOW, 20.4],
  ],
  outdoor: [
    [NOW - 8 * HOUR, 5.0],
    [NOW - 7 * HOUR, 5.2],
    [NOW - 6 * HOUR, 5.4],
    [NOW - 1 * HOUR, 6.1],
    [NOW, 6.2],
  ],
};

const forecast = (scenario, values, archived) => ({
  scenario,
  issued_at: iso(0),
  horizon_hours: values.length - 1,
  weather_model: "best_match",
  params_digest: "digest",
  params_source: "manual_default",
  indoor_temperature_now: 20.4,
  outdoor_temperature_now: 6.1,
  stale: false,
  error: null,
  times: values.map((_, index) => iso(index)),
  values,
  weather_times: [0, 1, 2, 3, 4, 5].map((index) => iso(index)),
  weather_outdoor: [6, 5, 4, 4, 3, 3],
  archived_runs: archived,
});

const forecasts = {
  free_float: forecast(
    "free_float",
    Array.from({ length: 4 }, (_, index) => 20.4 - index * 0.5),
    [
      { issued_at: iso(-6), age_hours: 6, times: [-6, -5, -4].map(iso), values: [20.9, 20.7, 20.5] },
    ],
  ),
  // même entité, avec l'erreur mesurée publiée par le hold-out
  ...{},
  thermostat: forecast(
    "thermostat",
    Array.from({ length: 4 }, (_, index) => 21.0 - index * 0.1),
    [],
  ),
};
forecasts.free_float.uncertainty_k = 1.0;
forecasts.free_float.min_bias_k = 0.9;

// -- data -------------------------------------------------------------
const data = buildChartData({ history, forecasts, nowMs: NOW });

check(APEXCHARTS_VERSION === "4.7.0", `unexpected vendored version: ${APEXCHARTS_VERSION}`);
check(data.x.length >= 6, `axis too short: ${data.x.length}`);
check(
  data.x.every((value, index) => index === 0 || value > data.x[index - 1]),
  "axis timestamps are not strictly increasing",
);
check(data.x.every((value) => Number.isInteger(value)), "axis must be whole seconds");

const oldestSample = Math.min(
  ...history.indoor.map((point) => point[0]),
  ...history.outdoor.map((point) => point[0]),
  ...forecasts.free_float.archived_runs.flatMap((run) =>
    run.times.map((stamp) => Date.parse(stamp)),
  ),
);
check(
  data.x[0] === Math.round(oldestSample / 1000),
  "the axis must start at the oldest sample of any series",
);

const byId = Object.fromEntries(data.series.map((entry) => [entry.id, entry]));
check(Boolean(byId.measured), "measured series missing");
check(Boolean(byId.free_float), "free_float series missing");
check(Boolean(byId.outdoor_forecast), "outdoor forecast series missing");
equal(
  data.series.filter((entry) => entry.id === "outdoor_forecast").length,
  1,
  "the weather forecast must be added once, not once per scenario",
);
for (const entry of data.series) {
  equal(entry.values.length, data.x.length, `series ${entry.id} is not aligned on the axis`);
}

const measured = byId.measured;
check(
  measured.values.some((value) => value === null),
  "measured series should have nulls where the axis extends past it",
);
check(
  !measured.values.some((value) => value === 0),
  "a missing sample must be null, not 0 °C",
);
equal(
  measured.values[data.x.indexOf(Math.round(NOW / 1000))],
  20.4,
  "measured value at 'now' is wrong",
);

const holeStart = Math.round((NOW - 6 * HOUR) / 1000);
const holeEnd = Math.round((NOW - 1 * HOUR) / 1000);
const marksInsideHole = data.x.filter((stamp) => stamp > holeStart && stamp < holeEnd);
check(marksInsideHole.length >= 1, "an axis marker is missing inside a long gap");
check(
  marksInsideHole.some((stamp) => measured.values[data.x.indexOf(stamp)] === null),
  "the measured series must be null inside a real hole",
);

const archived = data.series.find((entry) => entry.id.includes(ARCHIVED_PREFIX) && !entry.band);
check(Boolean(archived), "archived run was not turned into a series");
equal(
  archived.values[data.x.indexOf(Math.round((NOW - 6 * HOUR) / 1000))],
  20.9,
  "archived run is shifted on the axis",
);
equal(archived.dash, [5, 4], "archived runs must be dashed");
equal(archived.scale, "y", "archived runs belong on the indoor scale");
equal(byId.outdoor_measured.scale, "out", "outdoor measured is on the wrong scale");
equal(byId.measured.color, COLORS.measured, "measured colour changed");

// -- timestamps -------------------------------------------------------
equal(toMillis(NOW), NOW, "millisecond epoch");
equal(toMillis(NOW / 1000), NOW, "second epoch (the compressed-history shape)");
equal(toMillis("2026-01-10T12:00:00Z"), NOW, "ISO string");
check(Number.isNaN(toMillis(null)), "null must be NaN");
check(Number.isNaN(toMillis("nonsense")), "garbage must be NaN");

const mixed = parseHistoryRows([
  { state: "19.6", last_changed: new Date(NOW - 5 * HOUR).toISOString() },
  ...Array.from({ length: 5 }, (_, index) => ({
    s: String(19.6 + index * 0.1),
    lu: (NOW - (4 - index) * HOUR) / 1000,
  })),
]);
equal(mixed.length, 6, "every history row must be parsed");
check(
  mixed.every(([ms]) => ms > 1.6e12),
  "a history timestamp landed in 1970 — seconds read as milliseconds",
);
equal(mixed[0][0], NOW - 5 * HOUR, "first history point must keep its instant");

// -- panels -----------------------------------------------------------
const { panels } = buildPanels({ history, forecasts, nowMs: NOW });
equal(panels.map((panel) => panel.id), ["indoor", "past", "outdoor"], "panel layout changed");
check(panels.every((panel) => panel.unit === "°C"), "every panel must carry its unit");

const panelById = Object.fromEntries(panels.map((panel) => [panel.id, panel]));
equal(
  panelById.indoor.series.map((entry) => entry.id).sort(),
  ["free_float", "free_float_band", "measured", "thermostat"],
  "the indoor panel must hold the current trajectories and their measured band",
);
check(
  !panelById.indoor.series.some((entry) => entry.scale === "out"),
  "the outdoor series must not share the indoor panel",
);
equal(
  panelById.past.series.filter(
    (entry) => entry.id.includes(ARCHIVED_PREFIX) && !entry.band,
  ).length,
  1,
  "the past panel must hold the archived runs",
);
check(
  panelById.past.series.some(
    (entry) => entry.band && entry.id.endsWith(":band"),
  ),
  "the past panel must show the band around the latest archived run",
);
check(
  panelById.past.series.some((entry) => entry.id === "measured"),
  "the past panel is useless without the measured reference",
);
equal(
  panelById.outdoor.series.map((entry) => entry.id).sort(),
  ["outdoor_forecast", "outdoor_measured"],
  "the outdoor panel must hold both outdoor series",
);
check(
  panels.every(
    (panel) => panel.series.filter((entry) => !entry.band).length <= 4,
  ),
  "a panel with more than four curves stops being readable (bands do not count: they are background)",
);

// -- ApexCharts options ------------------------------------------------
const band = data.series.find((entry) => entry.id === "free_float_band");
check(band !== undefined, "the measured uncertainty must produce a band series");
check(
  band.values.every((v) => v === null || (Array.isArray(v) && v[1] - v[0] === 2.0)),
  `the band must be value ± uncertainty (got ${JSON.stringify(band.values)})`,
);
const archivedBand = data.series.find((entry) => entry.band && entry.id.endsWith(":band"));
check(
  archivedBand !== undefined,
  "the latest archived run must carry a band for 'yesterday vs reality'",
);

const seriesData = toSeriesData(data.x, panelById.indoor.series);
check(seriesData.length === 4, "toSeriesData must keep every series");
const bandAsApex = toSeriesData(data.x, [band])[0];
check(
  bandAsApex.type === "rangeArea",
  "the band must use the native ApexCharts rangeArea type",
);
check(
  bandAsApex.data[0][1][1] - bandAsApex.data[0][1][0] === 2.0,
  "the rangeArea data points must be [x, [lo, hi]] pairs",
);
check(
  seriesData.every((entry) => entry.data.every(([ms]) => ms > 1.6e12)),
  "series points must be milliseconds",
);
check(
  seriesData.every((entry) =>
    entry.data.every(([, value]) =>
      Array.isArray(value)
        ? value.every((part) => Number.isFinite(part))
        : Number.isFinite(value),
    ),
  ),
  "nulls must be dropped, never plotted as a value (bands carry finite pairs)",
);

const options = buildPanelOptions(panelById.indoor, data.x, { nowMs: NOW });
equal(options.chart.type, "line", "chart type changed");
check(
  options.chart.group === undefined,
  "chart grouping was removed: it reported the first series for every colour",
);
equal(options.chart.animations.enabled, false, "animations would make the panel jump");
equal(options.yaxis.title.text, "°C", "the y axis must carry its unit");
equal(options.series.length, 4, "options must carry every series");
equal(
  options.series.filter((entry) => entry.type === "rangeArea").length,
  1,
  "the band must reach ApexCharts as a rangeArea",
);
equal(
  options.fill.opacity[options.series.findIndex((entry) => entry.type === "rangeArea")],
  0.15,
  "the band must be translucent",
);
equal(options.series[0].color, COLORS.measured, "series colours must be passed through");
equal(options.tooltip.shared, true, "the tooltip must list every series at once");
equal(options.annotations.xaxis[0].x, NOW, "the 'now' marker annotation is missing");
equal(options.legend.show, true, "the legend must be shown (it toggles series)");

const pastOptions = buildPanelOptions(panelById.past, data.x, { nowMs: NOW });
const dashIndex = panelById.past.series.findIndex((entry) => entry.dash);
check(dashIndex >= 0, "the past panel must contain a dashed series");
equal(pastOptions.stroke.dashArray[dashIndex], 5, "archived runs must be dashed");

// -- degenerate input -------------------------------------------------
const empty = buildChartData({ history: { indoor: [], outdoor: [] }, forecasts: {}, nowMs: NOW });
equal(empty.x.length, 0, "no data must produce an empty axis, not a fake one");
equal(empty.series.length, 0, "no data must produce no series");

const summary = summarise({ data, forecasts, nowMs: NOW });
equal(summary.current, 20.4, "summary current temperature is wrong");
check(summary.minimum !== null && summary.minimum <= 18.9, "summary minimum is wrong");

if (failures.length) {
  console.error("chart data tests failed:\n- " + failures.join("\n- "));
  process.exit(1);
}
console.log("chart data tests: ok");
