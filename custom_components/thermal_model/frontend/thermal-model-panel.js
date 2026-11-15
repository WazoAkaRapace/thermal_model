/**
 * Thermal Model — sidebar panel.
 *
 * Shipped by the integration, no build step, no framework, no CDN: plain DOM and
 * inline SVG. It renders what the integration computes and nothing else:
 *
 *   - the measured indoor temperature (recorder history, last 48 h);
 *   - the current forecast of every enabled scenario (next 48 h);
 *   - the forecasts issued ~6/12/24 h ago, dashed, on the same axis.
 *
 * The last point is the whole reason this panel exists: a forecast that is never
 * compared with what happened is decoration.
 */

const PAST_HOURS = 48;
const FUTURE_HOURS = 48;
const HISTORY_REFRESH_MS = 5 * 60 * 1000;

const SCENARIO_META = {
  free_float: { label: "Sans chauffage", color: "#ff9800" },
  thermostat: { label: "Thermostat", color: "#4caf50" },
  observed_duty: { label: "Cyclage observé", color: "#9c27b0" },
};

const MEASURED_COLOR = "#03a9f4";
const OUTDOOR_COLOR = "#78909c";

const CSS = `
  :host { display: block; height: 100%; background: var(--primary-background-color); }
  .wrap { padding: 16px; box-sizing: border-box; height: 100%; overflow: auto; }
  .card {
    background: var(--card-background-color);
    border-radius: var(--ha-card-border-radius, 12px);
    box-shadow: var(--ha-card-box-shadow, none);
    border: 1px solid var(--divider-color);
    padding: 14px 16px;
    margin-bottom: 14px;
  }
  h1 { font-size: 1.35rem; margin: 0 0 4px; font-weight: 500; color: var(--primary-text-color); }
  .sub { color: var(--secondary-text-color); font-size: 0.85rem; }
  .row { display: flex; gap: 18px; flex-wrap: wrap; align-items: baseline; }
  .stat { display: flex; flex-direction: column; min-width: 92px; }
  .stat .k { font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--secondary-text-color); }
  .stat .v { font-size: 1.25rem; font-weight: 500; color: var(--primary-text-color); }
  .badge {
    display: inline-block; padding: 2px 8px; border-radius: 999px;
    font-size: 0.72rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em;
  }
  .badge.ok { background: rgba(76, 175, 80, 0.16); color: #2e7d32; }
  .badge.warn { background: rgba(255, 152, 0, 0.18); color: #e65100; }
  .badge.err { background: rgba(244, 67, 54, 0.16); color: #c62828; }
  .banner {
    margin-top: 10px; padding: 8px 10px; border-radius: 8px;
    background: rgba(255, 152, 0, 0.12); color: var(--primary-text-color); font-size: 0.85rem;
  }
  .banner.err { background: rgba(244, 67, 54, 0.12); }
  svg { width: 100%; display: block; touch-action: none; }
  .legend { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 6px; }
  .legend button {
    display: inline-flex; align-items: center; gap: 6px; cursor: pointer;
    background: none; border: 1px solid var(--divider-color); border-radius: 999px;
    padding: 3px 10px; font-size: 0.78rem; color: var(--primary-text-color);
    font-family: inherit;
  }
  .legend button[aria-pressed="false"] { opacity: 0.4; }
  .swatch { width: 10px; height: 10px; border-radius: 3px; display: inline-block; }
  .tooltip {
    position: absolute; pointer-events: none; z-index: 5;
    background: var(--card-background-color); border: 1px solid var(--divider-color);
    border-radius: 8px; padding: 8px 10px; font-size: 0.78rem;
    box-shadow: 0 2px 10px rgba(0,0,0,0.18); color: var(--primary-text-color);
    transform: translate(-50%, -105%); white-space: nowrap; display: none;
  }
  .tooltip .t { font-weight: 600; margin-bottom: 4px; }
  .tooltip .l { display: flex; justify-content: space-between; gap: 12px; }
  .chartbox { position: relative; }
  .params { display: flex; flex-wrap: wrap; gap: 10px; }
  .param {
    display: flex; align-items: center; gap: 6px; border: 1px solid var(--divider-color);
    border-radius: 10px; padding: 6px 8px; font-size: 0.8rem;
  }
  .param .name { color: var(--secondary-text-color); }
  .param .val { font-variant-numeric: tabular-nums; font-weight: 600; min-width: 56px; text-align: right; }
  .param button {
    width: 26px; height: 26px; border-radius: 50%; border: 1px solid var(--divider-color);
    background: none; color: var(--primary-text-color); cursor: pointer; font-size: 0.95rem;
    line-height: 1; font-family: inherit;
  }
  .param button:hover { background: var(--secondary-background-color); }
  .empty { color: var(--secondary-text-color); font-size: 0.9rem; padding: 20px 0; text-align: center; }
  .foot { color: var(--secondary-text-color); font-size: 0.75rem; margin-top: 10px; }
`;

const fmtTime = (date) =>
  date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
const fmtDay = (date) =>
  date.toLocaleDateString(undefined, { weekday: "short", day: "2-digit" });

class ThermalModelPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._panel = null;
    this._history = null; // { indoor: [[ms, value]], outdoor: [[ms, value]] }
    this._historyError = null;
    this._historyTimer = null;
    this._hidden = new Set();
    this._width = 900;
    this._hoverX = null;
    this._loadingHistory = false;
  }

  set panel(panel) {
    this._panel = panel;
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._history && !this._loadingHistory) {
      this._loadHistory();
    }
    this._render();
  }

  connectedCallback() {
    this._onResize = () => {
      const width = this._wrap ? this._wrap.clientWidth - 32 : this._width;
      if (width && Math.abs(width - this._width) > 4) {
        this._width = width;
        this._render();
      }
    };
    window.addEventListener("resize", this._onResize);
    this._historyTimer = window.setInterval(() => this._loadHistory(), HISTORY_REFRESH_MS);
    this._loadHistory();
  }

  disconnectedCallback() {
    window.removeEventListener("resize", this._onResize);
    if (this._historyTimer) window.clearInterval(this._historyTimer);
  }

  // -- data -----------------------------------------------------------
  _config() {
    return (this._panel && this._panel.config) || {};
  }

  _forecastAttributes() {
    const config = this._config();
    const out = {};
    for (const [scenario, entityId] of Object.entries(config.scenarios || {})) {
      const state = this._hass && this._hass.states[entityId];
      if (state) out[scenario] = state.attributes || {};
    }
    return out;
  }

  async _loadHistory() {
    if (!this._hass || this._loadingHistory) return;
    const config = this._config();
    const entityIds = [config.indoor_entity, config.source_outdoor_entity].filter(Boolean);
    if (!entityIds.length) return;

    this._loadingHistory = true;
    const end = Date.now();
    try {
      const result = await this._hass.callWS({
        type: "history/history_during_period",
        start_time: new Date(end - PAST_HOURS * 3600 * 1000).toISOString(),
        end_time: new Date(end).toISOString(),
        entity_ids: entityIds,
        minimal_response: true,
        no_attributes: true,
        significant_changes_only: false,
      });
      const parse = (rows) => {
        if (!rows) return [];
        const points = [];
        for (const row of rows) {
          const raw = row.s !== undefined ? row.s : row.state;
          const stamp = row.lu !== undefined ? row.lu : row.lc !== undefined ? row.lc : row.last_changed;
          const value = Number.parseFloat(raw);
          if (Number.isFinite(value) && stamp) {
            points.push([new Date(stamp).getTime(), value]);
          }
        }
        points.sort((a, b) => a[0] - b[0]);
        return points;
      };
      this._history = {
        indoor: parse(result[config.indoor_entity]),
        outdoor: parse(result[config.source_outdoor_entity]),
      };
      this._historyError = null;
    } catch (err) {
      this._history = this._history || { indoor: [], outdoor: [] };
      this._historyError = String(err && err.message ? err.message : err);
    } finally {
      this._loadingHistory = false;
      this._render();
    }
  }

  _liveIndoor() {
    const config = this._config();
    const state = this._hass && this._hass.states[config.indoor_entity];
    if (!state) return null;
    const value = Number.parseFloat(state.state);
    return Number.isFinite(value) ? value : null;
  }

  // -- scales ---------------------------------------------------------
  _domain() {
    const now = Date.now();
    return [now - PAST_HOURS * 3600 * 1000, now + FUTURE_HOURS * 3600 * 1000];
  }

  _series() {
    const forecasts = this._forecastAttributes();
    const history = this._history || { indoor: [], outdoor: [] };
    const series = [];

    const measured = history.indoor.slice();
    const live = this._liveIndoor();
    if (live !== null) {
      const last = measured[measured.length - 1];
      if (!last || Date.now() - last[0] > 60 * 1000) measured.push([Date.now(), live]);
    }
    series.push({
      id: "measured",
      label: "Mesuré",
      color: MEASURED_COLOR,
      width: 2.6,
      points: measured,
      axis: "in",
    });

    series.push({
      id: "outdoor_measured",
      label: "Extérieur mesuré",
      color: OUTDOOR_COLOR,
      width: 1.4,
      points: history.outdoor || [],
      axis: "out",
      dashed: false,
    });

    for (const [scenario, attributes] of Object.entries(forecasts)) {
      const meta = SCENARIO_META[scenario] || { label: scenario, color: "#607d8b" };
      const points = (attributes.times || []).map((stamp, index) => [
        new Date(stamp).getTime(),
        attributes.values[index],
      ]);
      series.push({
        id: scenario,
        label: meta.label,
        color: meta.color,
        width: 2,
        points,
        axis: "in",
      });

      const archived = attributes.archived_runs || [];
      archived.forEach((run, index) => {
        series.push({
          id: `${scenario}:archived:${index}`,
          label: `Prévu il y a ${Math.round(run.age_hours || 0)} h`,
          color: meta.color,
          width: 1.2,
          opacity: 0.45,
          dashed: true,
          points: (run.times || []).map((stamp, pointIndex) => [
            new Date(stamp).getTime(),
            run.values[pointIndex],
          ]),
          axis: "in",
        });
      });

      if (!series.some((entry) => entry.id === "outdoor_forecast")) {
        series.push({
          id: "outdoor_forecast",
          label: "Extérieur prévu",
          color: OUTDOOR_COLOR,
          width: 1.2,
          opacity: 0.6,
          dashed: true,
          axis: "out",
          points: (attributes.weather_times || []).map((stamp, index) => [
            new Date(stamp).getTime(),
            attributes.weather_outdoor[index],
          ]),
        });
      }
    }

    return series.filter((entry) => entry.points && entry.points.length);
  }

  // -- rendering ------------------------------------------------------
  _render() {
    if (!this.shadowRoot || !this._hass) return;

    const config = this._config();
    const forecasts = this._forecastAttributes();
    const reference = Object.values(forecasts)[0] || {};
    const series = this._series();
    const visible = series.filter((entry) => !this._hidden.has(entry.id) && entry.axis === "in");
    const visibleOutdoor = series.filter(
      (entry) => !this._hidden.has(entry.id) && entry.axis === "out"
    );

    const indoorPills = visible.filter((entry) => !entry.id.includes(":"));

    const width = Math.max(320, this._width);
    const height = 340;
    const padding = { top: 14, right: 14, bottom: 30, left: 44 };
    const [t0, t1] = this._domain();

    const values = [];
    for (const entry of series) {
      if (this._hidden.has(entry.id)) continue;
      for (const [, value] of entry.points) {
        if (Number.isFinite(value)) values.push(value);
      }
    }
    let min = values.length ? Math.min(...values) : 10;
    let max = values.length ? Math.max(...values) : 25;
    if (max - min < 2) {
      min -= 1;
      max += 1;
    }
    const pad = (max - min) * 0.08;
    min -= pad;
    max += pad;

    const x = (ms) =>
      padding.left +
      ((ms - t0) / (t1 - t0)) * (width - padding.left - padding.right);
    const y = (value) =>
      padding.top +
      (1 - (value - min) / (max - min)) * (height - padding.top - padding.bottom);

    // grid
    const parts = [];
    const yStep = max - min > 20 ? 5 : max - min > 8 ? 4 : 2;
    for (let value = Math.ceil(min / yStep) * yStep; value <= max; value += yStep) {
      parts.push(
        `<line x1="${padding.left}" x2="${width - padding.right}" y1="${y(value)}" y2="${y(value)}"
           stroke="var(--divider-color)" stroke-width="1" opacity="0.55"/>`,
        `<text x="${padding.left - 8}" y="${y(value) + 4}" text-anchor="end"
           font-size="11" fill="var(--secondary-text-color)">${value.toFixed(0)}°</text>`
      );
    }
    const xStepHours = width < 520 ? 12 : 6;
    const firstTick = Math.ceil(t0 / (xStepHours * 3600 * 1000)) * xStepHours * 3600 * 1000;
    for (let ms = firstTick; ms <= t1; ms += xStepHours * 3600 * 1000) {
      const date = new Date(ms);
      const label = date.getHours() === 0 ? fmtDay(date) : fmtTime(date);
      parts.push(
        `<line x1="${x(ms)}" x2="${x(ms)}" y1="${padding.top}" y2="${height - padding.bottom}"
           stroke="var(--divider-color)" stroke-width="1" opacity="0.28"/>`,
        `<text x="${x(ms)}" y="${height - padding.bottom + 16}" text-anchor="middle"
           font-size="11" fill="var(--secondary-text-color)">${label}</text>`
      );
    }

    const now = Date.now();
    parts.push(
      `<line x1="${x(now)}" x2="${x(now)}" y1="${padding.top}" y2="${height - padding.bottom}"
         stroke="var(--error-color, #e53935)" stroke-width="1.4" stroke-dasharray="5 4" opacity="0.9"/>`,
      `<text x="${x(now)}" y="${padding.top + 10}" text-anchor="middle" font-size="10"
         fill="var(--error-color, #e53935)">maintenant</text>`
    );

    const path = (entry) => {
      if (!entry.points.length) return "";
      let d = "";
      entry.points.forEach(([ms, value], index) => {
        if (!Number.isFinite(value)) return;
        d += `${index === 0 ? "M" : "L"}${x(ms).toFixed(1)},${y(value).toFixed(1)}`;
        d += " ";
      });
      return d.trim();
    };

    for (const entry of series) {
      if (this._hidden.has(entry.id)) continue;
      const d = path(entry);
      if (!d) continue;
      parts.push(
        `<path d="${d}" fill="none" stroke="${entry.color}" stroke-width="${entry.width}"
           stroke-linejoin="round" stroke-linecap="round"
           ${entry.dashed ? 'stroke-dasharray="4 4"' : ""}
           opacity="${entry.opacity !== undefined ? entry.opacity : 1}"/>`
      );
      const lastPoint = entry.points[entry.points.length - 1];
      if (lastPoint && indentChip(entry)) {
        parts.push(
          `<circle cx="${x(lastPoint[0]).toFixed(1)}" cy="${y(lastPoint[1]).toFixed(1)}" r="3"
             fill="${entry.color}"/>`
        );
      }
    }

    const hoverMarkup = this._renderHover(series, x, y, padding, height, width);

    const legend = series
      .filter((entry) => !entry.id.includes(":archived:"))
      .map(
        (entry) => `
        <button data-toggle="${entry.id}" aria-pressed="${!this._hidden.has(entry.id)}">
          <span class="swatch" style="background:${entry.color}"></span>${entry.label}
        </button>`
      )
      .join("");

    const parameters = this._renderParameters(config);

    const issued = reference.issued_at
      ? fmtTime(new Date(reference.issued_at)) +
        " " +
        (new Date(reference.issued_at).toDateString() === new Date().toDateString()
          ? ""
          : fmtDay(new Date(reference.issued_at)))
      : "—";

    const stale = reference.stale
      ? `<div class="banner">⚠️ Météo indisponible : les dernières trajectoires connues sont
         affichées${reference.error ? ` (${escapeHtml(reference.error)})` : ""}.</div>`
      : "";
    const historyProblem = this._historyError
      ? `<div class="banner err">Historique indisponible : ${escapeHtml(this._historyError)}.
         La courbe mesurée peut manquer (l'intégration « history » est peut-être désactivée).</div>`
      : "";
    const noData = series.length
      ? ""
      : `<div class="empty">Aucune donnée : vérifiez que les capteurs de prévision existent.</div>`;

    const minForecast = visible.length
      ? Math.min(...visible.flatMap((entry) => entry.points.map(([, value]) => value)))
      : null;

    // Which past forecasts are actually on the chart, oldest last: the user must
    // be able to tell a dashed curve from a mystery.
    const archivedSummary = series
      .filter((entry) => entry.id.includes(":archived:"))
      .map((entry) => entry.label.replace("Prévu il y a ", ""))
      .join(", ");

    this.shadowRoot.innerHTML = `
      <style>${CSS}</style>
      <div class="wrap">
        <div class="card">
          <div class="row" style="justify-content: space-between">
            <div>
              <h1>${escapeHtml(this._panel && this._panel.title ? this._panel.title : "Thermal Model")}</h1>
              <div class="sub">Mesuré, prévisions et prévisions passées sur la même échelle</div>
            </div>
            <div class="row" style="gap: 14px">
              <div class="stat"><span class="k">Maintenant</span>
                <span class="v">${this._liveIndoor() !== null ? this._liveIndoor().toFixed(1) + " °C" : "—"}</span></div>
              <div class="stat"><span class="k">Minimum prévu</span>
                <span class="v">${minForecast !== null ? minForecast.toFixed(1) + " °C" : "—"}</span></div>
              <div class="stat"><span class="k">Émis</span><span class="v" style="font-size:1rem">${issued}</span></div>
              <div class="stat"><span class="k">Modèle météo</span>
                <span class="v" style="font-size:1rem">${escapeHtml(reference.weather_model || "—")}</span></div>
              <span class="badge ${reference.stale ? "warn" : "ok"}">${reference.stale ? "périmé" : "à jour"}</span>
            </div>
          </div>
          ${stale}${historyProblem}
        </div>

        <div class="card">
          ${noData}
          <div class="chartbox">
            <svg viewBox="0 0 ${width} ${height}" height="${height}" role="img"
                 aria-label="Température intérieure mesurée et prévue">
              ${parts.join("")}
              ${hoverMarkup.lines}
            </svg>
            <div class="tooltip">${hoverMarkup.tooltip}</div>
          </div>
          <div class="legend">${legend}</div>
          <div class="foot">
            Trait plein : ce qui est arrivé (mesuré) et ce qui est prévu. Trait pointillé : prévision
            émise plus tôt, à comparer avec le mesuré. ${reference.horizon_hours || FUTURE_HOURS} h
            d'horizon, ${indoorPills.length} trajectoire(s) active(s)${
              archivedSummary ? ` · prévisions passées comparées : ${archivedSummary}` : ""
            }.
          </div>
        </div>

        <div class="card">
          <div class="sub" style="margin-bottom: 10px">
            Paramètres du modèle — source : <strong>${escapeHtml(reference.params_source || "—")}</strong>
            · empreinte <code>${escapeHtml(reference.params_digest || "—")}</code>
          </div>
          <div class="params">${parameters}</div>
        </div>
      </div>`;

    // interactions
    for (const button of this.shadowRoot.querySelectorAll("[data-toggle]")) {
      button.addEventListener("click", () => {
        const id = button.dataset.toggle;
        if (this._hidden.has(id)) this._hidden.delete(id);
        else this._hidden.add(id);
        this._render();
      });
    }
    for (const button of this.shadowRoot.querySelectorAll("[data-number]")) {
      button.addEventListener("click", () => this._nudge(button.dataset.number, button.dataset.dir));
    }
    const svg = this.shadowRoot.querySelector("svg");
    if (svg) {
      svg.addEventListener("pointermove", (event) => {
        const rect = svg.getBoundingClientRect();
        const relative = ((event.clientX - rect.left) / rect.width) * width;
        this._hoverX = relative;
        this._renderHoverOnly(series, x, y, padding, height, width);
      });
      svg.addEventListener("pointerleave", () => {
        this._hoverX = null;
        this._renderHoverOnly(series, x, y, padding, height, width);
      });
    }
    this._wrap = this.shadowRoot.querySelector(".wrap");
    if (typeof this._onResize === "function") this._onResize();
  }

  _renderHover(series, x, y, padding, height, width) {
    if (this._hoverX === null) return { lines: "", tooltip: "" };
    const [t0, t1] = this._domain();
    const ms = t0 + ((this._hoverX - padding.left) / (width - padding.left - padding.right)) * (t1 - t0);
    const rows = [];
    for (const entry of series) {
      if (this._hidden.has(entry.id) || !entry.points.length) continue;
      let best = null;
      for (const point of entry.points) {
        if (!best || Math.abs(point[0] - ms) < Math.abs(best[0] - ms)) best = point;
      }
      if (best && Math.abs(best[0] - ms) < 90 * 60 * 1000) {
        rows.push({ label: entry.label, color: entry.color, value: best[1] });
      }
    }
    const lines = `<line x1="${this._hoverX}" x2="${this._hoverX}" y1="${padding.top}"
                     y2="${height - padding.bottom}" stroke="var(--primary-text-color)"
                     stroke-width="1" opacity="0.35" pointer-events="none"/>`;
    const tooltip = `<div class="t">${new Date(ms).toLocaleString(undefined, {
      weekday: "short",
      hour: "2-digit",
      minute: "2-digit",
    })}</div>${rows
      .map(
        (row) =>
          `<div class="l"><span><span class="swatch" style="background:${row.color}"></span>
           ${escapeHtml(row.label)}</span><strong>${row.value.toFixed(1)} °C</strong></div>`
      )
      .join("")}`;
    return { lines, tooltip, x: this._hoverX };
  }

  _renderHoverOnly(series, x, y, padding, height, width) {
    const svg = this.shadowRoot.querySelector("svg");
    const tooltip = this.shadowRoot.querySelector(".tooltip");
    if (!svg || !tooltip) return;
    svg.querySelectorAll(".hoverline").forEach((node) => node.remove());
    const result = this._renderHover(series, x, y, padding, height, width);
    if (!result.lines) {
      tooltip.style.display = "none";
      return;
    }
    svg.insertAdjacentHTML("beforeend", result.lines.replace("<line", '<line class="hoverline"'));
    tooltip.innerHTML = result.tooltip;
    tooltip.style.display = "block";
    tooltip.style.left = `${(result.x / width) * 100}%`;
    tooltip.style.top = `${padding.top + 40}px`;
  }

  _renderParameters(config) {
    const labels = {
      a: "Déperditions (a)",
      c_s: "Apport solaire (c_s)",
      d: "Apports internes (d)",
      k: "Gain de chauffe (k)",
    };
    const steps = { a: 0.002, c_s: 0.0005, d: 0.01, k: 0.05 };
    return Object.entries(config.numbers || {})
      .filter(([key]) => key in labels)
      .map(([key, entityId]) => {
        const state = this._hass.states[entityId];
        const value = state ? Number.parseFloat(state.state) : NaN;
        return `<div class="param">
          <span class="name">${labels[key]}</span>
          <button data-number="${key}" data-dir="down" aria-label="diminuer">−</button>
          <span class="val">${Number.isFinite(value) ? value.toFixed(key === "c_s" ? 4 : 3) : "—"}</span>
          <button data-number="${key}" data-dir="up" aria-label="augmenter">+</button>
        </div>`;
      })
      .join("");
  }

  _nudge(key, direction) {
    const config = this._config();
    const entityId = (config.numbers || {})[key];
    const state = this._hass && this._hass.states[entityId];
    if (!state) return;
    const steps = { a: 0.002, c_s: 0.0005, d: 0.01, k: 0.05 };
    const step = steps[key] || 0.01;
    const current = Number.parseFloat(state.state);
    if (!Number.isFinite(current)) return;
    const value = direction === "up" ? current + step : current - step;
    this._hass.callService("number", "set_value", {
      entity_id: entityId,
      value: Math.max(0, Number(value.toFixed(4))),
    });
  }
}

function indentChip(entry) {
  return entry.id === "measured" || !entry.dashed;
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

customElements.define("thermal-model-panel", ThermalModelPanel);
