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

export const APEXCHARTS_VERSION = "4.7.0";
// Panels deliberately do NOT share an ApexCharts `chart.group`: group sync expects
// the same series on every chart, and with 3/4/2 series the shared tooltip reported
// the first series three times. Coherence comes from the shared time window, the
// shared colours and the same axis unit — not from a sync that lies.

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

/** "il y a 25 min" / "il y a 6 h": an archived run of 20 minutes is not "0 h". */
function formatAge(ageHours) {
  const hours = Number.isFinite(ageHours) ? ageHours : 0;
  if (hours < 1) return `${Math.max(1, Math.round(hours * 60))} min`;
  return `${Math.round(hours)} h`;
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
      // The model's own measured error, as a native ApexCharts rangeArea band:
      // the honest reading of the trajectory is "somewhere in here".
      const band = Number.parseFloat(attributes.uncertainty_k);
      if (scenario === "free_float" && Number.isFinite(band) && band > 0) {
        series.push({
          id: "free_float_band",
          label: "Incertitude",
          color,
          scale: "y",
          width: 0,
          band: true,
          values: values.map((v) => (v === null ? null : [v - band, v + band])),
        });
      }
    }

    for (const [position, run] of (attributes.archived_runs || []).entries()) {
      const archivedPoints = (run.times || []).map((stamp, pointIndex) => [
        toMillis(stamp),
        run.values[pointIndex],
      ]);
      const archivedValues = alignPoints(archivedPoints, index);
      if (!archivedValues) continue;
      // Band around the LATEST archived run only: the "yesterday vs reality"
      // reading is about that run, and three overlapping bands are ink, not
      // information. The band is the model's current measured error.
      const currentBand = Number.parseFloat(attributes.uncertainty_k);
      if (position === 0 && Number.isFinite(currentBand) && currentBand > 0) {
        series.push({
          id: `${scenario}:archived:${position}:band`,
          label: "Incertitude",
          color: COLORS[scenario] || COLORS.fallback,
          scale: "y",
          width: 0,
          band: true,
          values: archivedValues.map((v) =>
            v === null ? null : [v - currentBand, v + currentBand],
          ),
        });
      }
      series.push({
        id: `${scenario}:archived:${position}`,
        label: `${label} — prévu il y a ${formatAge(run.age_hours)}`,
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
/**
 * Panels are built on shared `x` (seconds); ApexCharts wants `[[ms, value]]` per
 * series. A `null` means "no sample here" and is simply dropped rather than drawn
 * as zero.
 */
export function toSeriesData(x, series) {
  return series.map((entry) => {
    const data = [];
    for (let index = 0; index < x.length; index += 1) {
      const value = entry.values[index];
      if (entry.band) {
        if (!Array.isArray(value)) continue;
        data.push([x[index] * 1000, value]);
        continue;
      }
      if (value === null || value === undefined || !Number.isFinite(value)) continue;
      data.push([x[index] * 1000, value]);
    }
    const out = {
      name: entry.label,
      data,
      color: entry.color,
      dash: Boolean(entry.dash),
    };
    if (entry.band) out.type = "rangeArea";
    return out;
  });
}

/**
 * ApexCharts options for one panel.
 *
 * Everything the panel used to hand-roll is a first-class option here: the y axis
 * title carries the unit, tick labels are numbers, the tooltip lists every series at
 * the hovered instant (the axis values stay put while it is open), the "now" marker
 * is an annotation, and the legend toggles a series on click.
 */
export function buildPanelOptions(panel, x, { nowMs, dark = false } = {}) {
  const series = toSeriesData(x, panel.series);
  const textColor = dark ? "#b0b0b0" : "#5f6368";
  const gridColor = dark ? "#333" : "#e0e0e0";

  return {
    chart: {
      type: "line",
      height: 220,
      id: panel.id,
      background: "transparent",
      fontFamily: "inherit",
      parentHeightOffset: 0,
      animations: { enabled: false },
      toolbar: { show: false },
      zoom: { enabled: true, type: "x" },
      sparkline: { enabled: false },
    },
    series,
    colors: series.map((entry) => entry.color),
    fill: {
      opacity: panel.series.map((entry) => (entry.band ? 0.15 : 1)),
    },
    stroke: {
      curve: "smooth",
      width: panel.series.map((entry) => entry.width || 2),
      dashArray: panel.series.map((entry) => (entry.dash ? 5 : 0)),
    },
    markers: { size: 0, hover: { size: 4 }, strokeWidth: 0 },
    dataLabels: { enabled: false },
    grid: { borderColor: gridColor, strokeDashArray: 3, padding: { left: 4, right: 8 } },
    xaxis: {
      type: "datetime",
      labels: {
        style: { colors: textColor, fontSize: "11px" },
        datetimeUTC: false,
      },
      axisBorder: { color: gridColor },
      axisTicks: { color: gridColor },
      tooltip: { enabled: false },
    },
    yaxis: {
      title: {
        text: panel.unit,
        style: { color: textColor, fontSize: "11px", fontWeight: 600 },
      },
      labels: {
        style: { colors: textColor, fontSize: "11px" },
        formatter: (value) => (Number.isFinite(value) ? value.toFixed(0) : ""),
      },
    },
    legend: {
      show: true,
      position: "bottom",
      horizontalAlign: "left",
      fontSize: "11px",
      labels: { colors: textColor },
      markers: { size: 5 },
      itemMargin: { horizontal: 8, vertical: 0 },
      onItemClick: { toggleDataSeries: true },
    },
    tooltip: {
      shared: true,
      intersect: false,
      theme: dark ? "dark" : "light",
      x: { format: "ddd HH:mm" },
      y: {
        formatter: (value) => {
          if (Array.isArray(value)) {
            return `${value[0].toFixed(1)} – ${value[1].toFixed(1)} ${panel.unit}`;
          }
          return Number.isFinite(value) ? `${value.toFixed(1)} ${panel.unit}` : "";
        },
      },
    },
    annotations: {
      xaxis: [
        {
          x: nowMs,
          borderColor: "#e53935",
          strokeDashArray: 4,
          label: {
            text: "maintenant",
            borderWidth: 0,
            style: { background: "#e53935", color: "#fff", fontSize: "10px" },
          },
        },
      ],
    },
    noData: { text: "—", style: { color: textColor, fontSize: "12px" } },
  };
}

export function buildPanels({ history, forecasts, nowMs, primaryScenario, maxArchived = 3 }) {
  const data = buildChartData({ history, forecasts, nowMs });
  const indoor = data.series.filter((entry) => entry.scale === "y");
  const outdoor = data.series.filter((entry) => entry.scale === "out");
  const measured = indoor.filter((entry) => entry.id === "measured");
  const current = indoor.filter((entry) => !entry.id.includes(ARCHIVED_PREFIX));
  // One scenario only: comparing three runs of one model is a reading;
  // comparing six runs of two models in one plot is a tangle.
  const primary = primaryScenario || Object.keys(forecasts)[0];
  const archivedAll = indoor.filter(
    (entry) => entry.id.includes(ARCHIVED_PREFIX) && entry.id.startsWith(`${primary}:`),
  );
  // The slice limits *runs*, not bands: a translucent rangeArea around the
  // latest run is background, not a fourth curve to read.
  const archivedRuns = archivedAll.filter((entry) => !entry.band).slice(0, maxArchived);
  const keptRunIds = new Set(archivedRuns.map((entry) => entry.id));
  const archived = [
    ...archivedRuns,
    ...archivedAll.filter(
    (entry) => entry.band && keptRunIds.has(entry.id.replace(/:band$/, "")),
  ),
  ];

  const panels = [];
  if (current.length) {
    panels.push({
      id: "indoor",
      title: "Température intérieure",
      subtitle: "mesuré et trajectoires prévues",
      unit: "°C",
      series: current,
    });
  }
  if (archived.length && measured.length) {
    panels.push({
      id: "past",
      title: "Prévision d'hier contre réalité",
      subtitle: "pointillés : ce que le modèle annonçait quelques heures plus tôt",
      unit: "°C",
      series: [...measured, ...archived],
    });
  }
  if (outdoor.length) {
    panels.push({
      id: "outdoor",
      title: "Extérieur",
      subtitle: "mesuré et prévision météo utilisée",
      unit: "°C",
      series: outdoor,
    });
  }
  return { x: data.x, panels, data, primaryScenario };
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
