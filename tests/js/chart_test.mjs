/**
 * Tests for the panel's data layer, in node, without a browser.
 *
 * This is where a chart actually goes wrong: a series projected onto the wrong
 * axis instant, a missing sample silently plotted as 0 °C, a real gap bridged by
 * spanGaps, a past forecast drawn over the current one. The uPlot options that
 * come out of these functions are asserted too, because a wrong `scale` puts the
 * outdoor temperature on the indoor axis and nobody notices for weeks.
 *
 *   node tests/js/chart_test.mjs
 */

import {
  ARCHIVED_PREFIX,
  COLORS,
  UPLOT_VERSION,
  buildAxes,
  buildChartData,
  buildPanels,
  buildSeriesConfig,
  summarise,
  parseHistoryRows,
  temperatureRange,
  toMillis,
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
      {
        issued_at: iso(-6),
        age_hours: 6,
        times: [-6, -5, -4].map(iso),
        values: [20.9, 20.7, 20.5],
      },
    ],
  ),
  thermostat: forecast(
    "thermostat",
    Array.from({ length: 4 }, (_, index) => 21.0 - index * 0.1),
    [],
  ),
};

// -- data -------------------------------------------------------------
const data = buildChartData({ history, forecasts, nowMs: NOW });

check(UPLOT_VERSION === "1.6.32", `unexpected vendored uPlot version: ${UPLOT_VERSION}`);
check(data.x.length >= 6, `axis too short: ${data.x.length}`);
check(
  data.x.every((value, index) => index === 0 || value > data.x[index - 1]),
  "axis timestamps are not strictly increasing",
);
check(data.x.every((value) => Number.isInteger(value)), "axis must be whole seconds for uPlot");
const oldestSample = Math.min(
  ...history.indoor.map((point) => point[0]),
  ...history.outdoor.map((point) => point[0]),
  ...forecasts.free_float.archived_runs.flatMap((run) => run.times.map((stamp) => Date.parse(stamp))),
);
check(
  data.x[0] === Math.round(oldestSample / 1000),
  "the axis must start at the oldest sample of any series",
);

const byId = Object.fromEntries(data.series.map((entry) => [entry.id, entry]));
check(Boolean(byId.measured), "measured series missing");
check(Boolean(byId.free_float), "free_float series missing");
check(Boolean(byId.thermostat), "thermostat series missing");
check(Boolean(byId.outdoor_measured), "outdoor measured series missing");
check(Boolean(byId.outdoor_forecast), "outdoor forecast series missing");
equal(
  data.series.filter((entry) => entry.id === "outdoor_forecast").length,
  1,
  "the weather forecast must be added once, not once per scenario",
);

for (const entry of data.series) {
  equal(entry.values.length, data.x.length, `series ${entry.id} is not aligned on the axis`);
}

// missing samples are null, never 0
const measured = byId.measured;
const measuredNulls = measured.values.filter((value) => value === null).length;
check(measuredNulls > 0, "measured series should have nulls where the axis extends past it");
check(
  !measured.values.some((value) => value === 0),
  "a missing sample must be null, not 0 °C",
);

// the live value is appended so the measured line reaches "now"
const nowPosition = data.x.indexOf(Math.round(NOW / 1000));
equal(measured.values[nowPosition], 20.4, "measured value at 'now' is wrong");

// a real hole must be visible: an axis point inside it where the series is null,
// otherwise uPlot draws a straight line across six hours of missing data
const holeStart = Math.round((NOW - 6 * HOUR) / 1000);
const holeEnd = Math.round((NOW - 1 * HOUR) / 1000);
const marksInsideHole = data.x.filter((stamp) => stamp > holeStart && stamp < holeEnd);
check(
  marksInsideHole.length >= 1,
  "an axis marker is missing inside a long gap: the hole would be drawn as a straight line",
);
check(
  marksInsideHole.some((stamp) => measured.values[data.x.indexOf(stamp)] === null),
  "the measured series must be null inside a real hole",
);

// archived run offset: its value belongs 6 h before now, not at now
const archived = data.series.find((entry) => entry.id.includes(ARCHIVED_PREFIX));
check(Boolean(archived), "archived run was not turned into a series");
equal(
  archived.values[data.x.indexOf(Math.round((NOW - 6 * HOUR) / 1000))],
  20.9,
  "archived run is shifted on the axis",
);
equal(archived.dash, [5, 4], "archived runs must be dashed");
equal(archived.scale, "y", "archived runs belong on the indoor scale");

// scales: outdoor series on their own scale
equal(byId.outdoor_measured.scale, "out", "outdoor measured is on the wrong scale");
equal(byId.free_float.scale, "y", "indoor forecast is on the wrong scale");
equal(byId.measured.color, COLORS.measured, "measured colour changed");

// -- series configuration ---------------------------------------------
const seriesConfig = buildSeriesConfig(data.series, { hoursToShow: 96 });
equal(seriesConfig.length, data.series.length + 1, "config must have a time series first");
check(typeof seriesConfig[0].value === "function", "the time axis needs a value formatter");
for (const [index, entry] of data.series.entries()) {
  equal(seriesConfig[index + 1].label, entry.label, "labels out of order");
  equal(seriesConfig[index + 1].scale, entry.scale, `scale lost for ${entry.id}`);
  equal(
    seriesConfig[index + 1].spanGaps,
    entry.id !== "measured",
    `spanGaps is wrong for ${entry.id}`,
  );
}
equal(
  seriesConfig.find((entry) => entry.label && entry.label.startsWith("Mesuré")).spanGaps,
  false,
  "the measured line must not bridge a real gap",
);

// -- axes -------------------------------------------------------------
const axes = buildAxes(data.series);
equal(axes.length, 3, "with an outdoor series there must be three axes (time, indoor, outdoor)");
equal(axes[1].scale, "y", "the left temperature axis must use the y scale");
equal(axes[2].scale, "out", "the right axis must use the out scale");
equal(axes[2].side, 1, "the outdoor axis belongs on the right");
equal(buildAxes(data.series.filter((entry) => entry.scale === "y")).length, 2, "outdoor axis shown without outdoor data");

// -- ranges and summary -----------------------------------------------
const [min, max] = temperatureRange(data.series, "y");
check(min < 19 && max > 21, `indoor range looks wrong: ${min}..${max}`);
const [outMin, outMax] = temperatureRange(data.series, "out");
check(outMin <= 3 && outMax >= 6, `outdoor range looks wrong: ${outMin}..${outMax}`);
check(min > outMax, "the two scales must not collapse into each other");

const summary = summarise({ data, forecasts, nowMs: NOW });
equal(summary.current, 20.4, "summary current temperature is wrong");
check(summary.minimum !== null && summary.minimum <= 18.9, "summary minimum is wrong");
check(summary.seriesCount >= 2, "summary should count the indoor trajectories");
check(summary.windowHours > 0, "summary window is empty");

// -- timestamps: the bug that squashed the chart ----------------------
// With minimal_response, the history websocket sends the first row as a full
// state (ISO last_changed) and every following row as {s, lu} with lu in
// **seconds**. Read as milliseconds, those points land in 1970 and the real 48 h
// window collapses into a vertical line at the right edge of the plot.
equal(toMillis(NOW), NOW, "millisecond epoch");
equal(toMillis(NOW / 1000), NOW, "second epoch (the compressed-history shape)");
equal(toMillis("2026-01-10T12:00:00Z"), NOW, "ISO string");
check(Number.isNaN(toMillis(null)), "null must be NaN");
check(Number.isNaN(toMillis("nonsense")), "garbage must be NaN");
equal(toMillis(1794900000.123), 1794900000123, "float seconds must round to ms");

const mixedHistory = {
  indoor: parseHistoryRows([
    // first row as Home Assistant sends it: a full state, ISO timestamp
    { state: "19.6", last_changed: new Date(NOW - 5 * HOUR).toISOString() },
    // then compressed rows, epoch in seconds
    ...Array.from({ length: 5 }, (_, index) => ({
      s: String(19.6 + index * 0.1),
      lu: (NOW - (4 - index) * HOUR) / 1000,
    })),
  ]),
  outdoor: [],
};
const mixed = buildChartData({ history: mixedHistory, forecasts, nowMs: NOW });

check(mixed.x.length > 0, "mixed history produced no axis");
check(
  mixed.x.every((stamp) => stamp > 1.6e9),
  `an axis point is not a 2020s timestamp (first: ${mixed.x[0]}) — seconds read as ms`,
);
const spanHours = (mixed.x[mixed.x.length - 1] - mixed.x[0]) / 3600;
check(
  spanHours < 200,
  `axis spans ${spanHours.toFixed(1)} h: the window is being stretched by a wrong epoch`,
);
const mixedSeries = Object.fromEntries(mixed.series.map((entry) => [entry.id, entry]));
equal(
  mixedSeries.measured.values.filter((value) => value !== null).length,
  6,
  "every history point must land on the axis",
);

// -- panels: several readable charts, not one giant one -----------------
const { panels } = buildPanels({ history, forecasts, nowMs: NOW });

equal(
  panels.map((panel) => panel.id),
  ["indoor", "past", "outdoor"],
  "panel layout changed",
);
check(
  panels.every((panel) => panel.unit === "°C"),
  "every panel must carry its unit",
);
for (const panel of panels) {
  check(panel.title.length > 0, `panel ${panel.id} has no title`);
  check(panel.series.length > 0, `panel ${panel.id} has no series`);
}

const panelById = Object.fromEntries(panels.map((panel) => [panel.id, panel]));
equal(
  panelById.indoor.series.map((entry) => entry.id).sort(),
  ["free_float", "measured", "thermostat"],
  "the indoor panel must hold the current trajectories only",
);
check(
  !panelById.indoor.series.some((entry) => entry.scale === "out"),
  "the outdoor series must not share the indoor panel",
);
equal(
  panelById.past.series.filter((entry) => entry.id.includes(ARCHIVED_PREFIX)).length,
  1,
  "the past panel must hold the archived runs",
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
  panels.every((panel) => panel.series.length <= 4),
  "a panel with more than four series stops being readable",
);

// -- axes carry their unit ---------------------------------------------
const indoorAxes = buildAxes(panelById.indoor.series, "°C");
equal(indoorAxes.length, 2, "an indoor-only panel needs two axes (time + temperature)");
equal(indoorAxes[1].label, "°C", "the temperature axis must be labelled with its unit");
equal(indoorAxes[1].values(null, [19, 20, 21]), ["19", "20", "21"], "tick formatting changed");

// -- degenerate input -------------------------------------------------
const empty = buildChartData({ history: { indoor: [], outdoor: [] }, forecasts: {}, nowMs: NOW });
equal(empty.x.length, 0, "no data must produce an empty axis, not a fake one");
equal(empty.series.length, 0, "no data must produce no series");

const noArchived = buildChartData({
  history,
  forecasts: { free_float: forecast("free_float", [20, 20.1], []) },
  nowMs: NOW,
});
check(
  !noArchived.series.some((entry) => entry.id.includes(ARCHIVED_PREFIX)),
  "archived series invented without archived runs",
);

if (failures.length) {
  console.error("chart data tests failed:\n- " + failures.join("\n- "));
  process.exit(1);
}
console.log("chart data tests: ok");
