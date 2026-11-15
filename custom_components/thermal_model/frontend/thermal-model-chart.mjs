/**
 * Pure data layer of the Thermal Model panel.
 *
 * Everything that can be wrong in a chart is here, away from the DOM: which
 * timestamps the axis contains, how a series is aligned onto it, which gaps are
 * real and which are only padding, what colours and dashes each series gets.
 * `thermal-model-panel.js` only feeds the result to uPlot.
 *
 * Kept free of DOM access so it can be imported and tested with node (see
 * tests/js/chart_test.mjs).
 */

export const UPLOT_VERSION = "1.6.32";

export const COLORS = {
  measured: "#03a9f4",
  outdoor_measured: "#78909c",
  outdoor_forecast: "#b0bec5",
  free_float: "#ff9800",
  thermostat: "#4caf50",
  observed_duty: "#9c27b0",
  fallback: "#607d8b",
};

export const LABELS = {
  measured: "Mesuré",
  outdoor_measured: "Extérieur mesuré",
  outdoor_forecast: "Extérieur prévu",
  free_float: "Sans chauffage",
  thermostat: "Thermostat",
  observed_duty: "Cyclage observé",
};

const HOUR_MS = 3600 * 1000;

/** Seconds, which is what uPlot wants on a time axis. */
const toSeconds = (ms) => Math.round(ms / 1000);

/**
 * Any timestamp Home Assistant sends, in milliseconds.
 *
 * This one line is the whole bug that made the chart look squashed: with
 * `minimal_response`, the history websocket returns the *first* row of an entity
 * as a full state object (ISO `last_changed`), then every following row as
 * `{s, lu}` where `lu` is an **epoch in seconds**. Feeding those straight to
 * `new Date()` reads them as milliseconds, so fifty measured points landed in
 * January 1970 and the real 48 h window was compressed into a vertical line at
 * the right edge of the plot.
 *
 * Anything below 1e11 cannot be a millisecond epoch (that would be 1973).
 */
export function toMillis(stamp) {
  if (stamp === null || stamp === undefined) return Number.NaN;
  if (typeof stamp === "number") {
    return Number.isFinite(stamp) ? (stamp < 1e11 ? Math.round(stamp * 1000) : stamp) : Number.NaN;
  }
  const parsed = new Date(stamp).getTime();
  return Number.isFinite(parsed) ? parsed : Number.NaN;
}

/**
 * Parse the history websocket payload into `[[ms, value]]`.
 *
 * Shape sent by Home Assistant with `minimal_response`: the first row of an
 * entity is a full state object (ISO `last_changed`), every following row is
 * `{s, lu}` with `lu` in **seconds**. `toMillis` normalises both; anything
 * unparsable is skipped rather than plotted as zero or as 1970.
 */
export function parseHistoryRows(rows) {
  if (!Array.isArray(rows)) return [];
  const points = [];
  for (const row of rows) {
    if (!row) continue;
    const raw = row.s !== undefined ? row.s : row.state;
    const stamp = row.lu !== undefined ? row.lu : row.lc !== undefined ? row.lc : row.last_changed;
    const value = Number.parseFloat(raw);
    const millis = toMillis(stamp);
    if (Number.isFinite(value) && Number.isFinite(millis)) points.push([millis, value]);
  }
  points.sort((a, b) => a[0] - b[0]);
  return points;
}

/** Sampling interval of a series, robust to a few long holes. */
function medianInterval(points) {
  if (!points || points.length < 3) return 0;
  const gaps = [];
  for (let index = 1; index < points.length; index += 1) {
    const delta = points[index][0] - points[index - 1][0];
    if (Number.isFinite(delta) && delta > 0) gaps.push(delta);
  }
  if (!gaps.length) return 0;
  gaps.sort((a, b) => a - b);
  return gaps[Math.floor(gaps.length / 2)];
}

/**
 * Where a series has a real hole, the axis needs a point *inside* the hole,
 * otherwise uPlot would draw a straight segment across it and a week of missing
 * data would look like a perfectly smooth house.
 *
 * The threshold is derived from the series' own sampling interval, so a 1-minute
 * sensor and an hourly one are both handled without configuration.
 */
function gapMarks(points) {
  const interval = medianInterval(points);
  if (!interval) return [];
  const threshold = Math.max(interval * 2.5, 15 * 60 * 1000);
  const marks = [];
  for (let index = 1; index < points.length; index += 1) {
    const previous = points[index - 1][0];
    const current = points[index][0];
    if (current - previous > threshold) marks.push(previous + threshold);
  }
  return marks;
}

/** Collect every timestamp exactly once, sorted. */
function buildTimestamps({ history, forecasts }) {
  const stamps = new Set();
  for (const point of history.indoor || []) stamps.add(point[0]);
  for (const point of history.outdoor || []) stamps.add(point[0]);
  for (const mark of gapMarks(history.indoor || [])) stamps.add(mark);
  for (const mark of gapMarks(history.outdoor || [])) stamps.add(mark);
  for (const attributes of Object.values(forecasts)) {
    for (const stamp of attributes.times || []) stamps.add(toMillis(stamp));
    for (const stamp of attributes.weather_times || []) stamps.add(toMillis(stamp));
    for (const run of attributes.archived_runs || []) {
      for (const stamp of run.times || []) stamps.add(toMillis(stamp));
    }
  }
  return [...stamps].filter((value) => Number.isFinite(value)).sort((a, b) => a - b);
}

/** Project [[ms, value]] onto the axis: null outside the samples, never 0. */
function alignPoints(points, index) {
  if (!points || !points.length) return null;
  const values = new Array(index.size).fill(null);
  let placed = 0;
  for (const [ms, value] of points) {
    const position = index.get(ms);
    if (position === undefined || !Number.isFinite(value)) continue;
    values[position] = value;
    placed += 1;
  }
  return placed ? values : null;
}

/**
 * Build the columns for uPlot.
 *
 * A null means "this series has no sample at that instant", which is *not* the
 * same as a missing measurement: `spanGaps` decides how it is drawn. Forecast
 * curves are hourly samples on an axis driven by the recorder, so they must span
 * their padding; the measured curve must not, or a real gap in the history would
 * be bridged silently.
 */
export function buildChartData({ history, forecasts, nowMs }) {
  const timestamps = buildTimestamps({ history, forecasts });
  const index = new Map(timestamps.map((ms, position) => [ms, position]));

  const series = [];

  const measuredPoints = [...(history.indoor || [])];
  const live = liveIndoor(forecasts);
  if (live !== null) {
    const last = measuredPoints[measuredPoints.length - 1];
    if (!last || nowMs - last[0] > 60 * 1000) measuredPoints.push([nowMs, live]);
  }
  const measured = alignPoints(measuredPoints, index);
  if (measured) {
    series.push({
      id: "measured",
      label: LABELS.measured,
      color: COLORS.measured,
      scale: "y",
      width: 2.4,
      values: measured,
    });
  }

  const outdoorMeasured = alignPoints(history.outdoor, index);
  if (outdoorMeasured) {
    series.push({
      id: "outdoor_measured",
      label: LABELS.outdoor_measured,
      color: COLORS.outdoor_measured,
      scale: "out",
      width: 1.4,
      values: outdoorMeasured,
    });
  }

  let weatherForecastAdded = false;
  for (const [scenario, attributes] of Object.entries(forecasts)) {
    const color = COLORS[scenario] || COLORS.fallback;
    const label = LABELS[scenario] || scenario;

    const points = (attributes.times || []).map((stamp, position) => [
      toMillis(stamp),
      attributes.values[position],
    ]);
    const values = alignPoints(points, index);
    if (values) {
      series.push({
        id: scenario,
        label,
        color,
        scale: "y",
        width: 2,
        values,
      });
    }

    for (const [position, run] of (attributes.archived_runs || []).entries()) {
      const archivedPoints = (run.times || []).map((stamp, pointIndex) => [
        toMillis(stamp),
        run.values[pointIndex],
      ]);
      const archivedValues = alignPoints(archivedPoints, index);
      if (!archivedValues) continue;
      series.push({
        id: `${scenario}:archived:${position}`,
        label: `${label} — prévu il y a ${Math.round(run.age_hours || 0)} h`,
        color,
        scale: "y",
        width: 1.1,
        dash: [5, 4],
        alpha: 0.7,
        values: archivedValues,
      });
    }

    if (!weatherForecastAdded && (attributes.weather_times || []).length) {
      const weatherPoints = attributes.weather_times.map((stamp, position) => [
        toMillis(stamp),
        attributes.weather_outdoor[position],
      ]);
      const weatherValues = alignPoints(weatherPoints, index);
      if (weatherValues) {
        weatherForecastAdded = true;
        series.push({
          id: "outdoor_forecast",
          label: LABELS.outdoor_forecast,
          color: COLORS.outdoor_forecast,
          scale: "out",
          width: 1.1,
          dash: [4, 4],
          alpha: 0.8,
          values: weatherValues,
        });
      }
    }
  }

  return {
    x: timestamps.map(toSeconds),
    series,
    span: { from: timestamps[0] ?? nowMs, to: timestamps[timestamps.length - 1] ?? nowMs },
  };
}

function liveIndoor(forecasts) {
  for (const attributes of Object.values(forecasts)) {
    const value = attributes.indoor_temperature_now;
    if (Number.isFinite(value)) return value;
  }
  return null;
}

/** Y range over the indoor series only, padded: the outdoor scale is separate. */
export function temperatureRange(series, scale) {
  const values = series
    .filter((entry) => entry.scale === scale)
    .flatMap((entry) => entry.values || [])
    .filter((value) => Number.isFinite(value));
  if (!values.length) return scale === "y" ? [10, 25] : [-5, 15];
  let min = Math.min(...values);
  let max = Math.max(...values);
  if (max - min < 1) {
    min -= 0.5;
    max += 0.5;
  }
  const padding = (max - min) * 0.08;
  return [min - padding, max + padding];
}

/**
 * uPlot series configuration, in the same order as `buildChartData().series`.
 * Index 0 is always uPlot's mandatory time series.
 */
export function buildSeriesConfig(series, { hoursToShow }) {
  const config = [
    {
      value: (self, raw) =>
        raw === null || raw === undefined
          ? "—"
          : new Date(raw * 1000).toLocaleString(undefined, {
              weekday: "short",
              hour: "2-digit",
              minute: "2-digit",
            }),
      spanGaps: false,
    },
  ];

  for (const entry of series) {
    config.push({
      label: entry.label,
      stroke: entry.color,
      width: entry.width,
      scale: entry.scale,
      dash: entry.dash,
      alpha: entry.alpha,
      // padding nulls (hourly samples on a finer axis) must not break the line;
      // the measured series keeps `spanGaps: false` so real holes stay visible
      spanGaps: entry.id !== "measured",
      points: { show: false },
      value: (self, raw) =>
        raw === null || raw === undefined ? "—" : `${raw.toFixed(1)} °C`,
    });
  }

  return config;
}

/** Axis configuration: time on the left, temperature on both sides. */
export function buildAxes(series) {
  const hasOutdoor = series.some((entry) => entry.scale === "out");
  const axes = [
    {
      stroke: "var(--secondary-text-color, #888)",
      grid: { stroke: "var(--divider-color, #e0e0e0)", width: 1 },
      ticks: { stroke: "var(--divider-color, #e0e0e0)" },
      font: "11px system-ui, sans-serif",
    },
    {
      scale: "y",
      stroke: "var(--secondary-text-color, #888)",
      grid: { stroke: "var(--divider-color, #e0e0e0)", width: 1 },
      ticks: { stroke: "var(--divider-color, #e0e0e0)" },
      font: "11px system-ui, sans-serif",
      size: 44,
      values: (self, splits) => splits.map((value) => `${value.toFixed(0)}°`),
    },
  ];
  if (hasOutdoor) {
    axes.push({
      scale: "out",
      side: 1,
      stroke: "var(--secondary-text-color, #888)",
      grid: { show: false },
      ticks: { show: false },
      font: "11px system-ui, sans-serif",
      size: 44,
      values: (self, splits) => splits.map((value) => `${value.toFixed(0)}°`),
    });
  }
  return axes;
}

/** Headline numbers shown above the chart. */
export function summarise({ data, forecasts, nowMs }) {
  const indoor = data.series.filter((entry) => entry.scale === "y");
  const values = indoor
    .flatMap((entry) => entry.values || [])
    .filter((value) => Number.isFinite(value));
  const nowSeries = data.series.find((entry) => entry.id === "measured");
  const nowPosition = data.x.indexOf(toSeconds(nowMs));
  const current =
    nowSeries && nowPosition >= 0 && Number.isFinite(nowSeries.values[nowPosition])
      ? nowSeries.values[nowPosition]
      : liveIndoor(forecasts);
  return {
    current,
    minimum: values.length ? Math.min(...values) : null,
    maximum: values.length ? Math.max(...values) : null,
    seriesCount: indoor.length,
    windowHours: data.x.length > 1 ? (data.x[data.x.length - 1] - data.x[0]) / 3600 : 0,
  };
}

export const ARCHIVED_PREFIX = ":archived:";
export const HOURS = HOUR_MS;
