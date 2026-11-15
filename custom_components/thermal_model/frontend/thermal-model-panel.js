/**
 * Thermal Model — sidebar panel.
 *
 * The chart itself is uPlot (vendored, MIT, 51 KB — see VENDOR.md): axes with
 * time ticks, a shared cursor, a clickable legend, drag-to-zoom, high-DPI and gap
 * handling all come from the library instead of being re-implemented. This file
 * only owns what is specific to the integration: which series to feed it, the
 * header, the stale/history banners and the parameter chips.
 *
 * The data/axis plumbing lives in thermal-model-chart.mjs so it can be tested
 * with node, without a browser.
 */

import {
  UPLOT_VERSION,
  ARCHIVED_PREFIX,
  buildAxes,
  buildChartData,
  buildSeriesConfig,
  summarise,
  temperatureRange,
} from "./thermal-model-chart.mjs";

const CHART_HEIGHT = 360;
const HISTORY_REFRESH_MS = 5 * 60 * 1000;
const PAST_HOURS = 48;
const FUTURE_HOURS = 48;

const CSS = `
  :host { display: block; height: 100%; background: var(--primary-background-color); }
  .wrap { padding: 16px; box-sizing: border-box; height: 100%; overflow: auto; }
  .card {
    background: var(--card-background-color);
    border-radius: var(--ha-card-border-radius, 12px);
    border: 1px solid var(--divider-color);
    box-shadow: var(--ha-card-box-shadow, none);
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
  .banner { margin-top: 10px; padding: 8px 10px; border-radius: 8px;
    background: rgba(255, 152, 0, 0.12); font-size: 0.85rem; color: var(--primary-text-color); }
  .banner.err { background: rgba(244, 67, 54, 0.12); }
  .chart { width: 100%; }
  .foot { color: var(--secondary-text-color); font-size: 0.75rem; margin-top: 8px; }
  .params { display: flex; flex-wrap: wrap; gap: 10px; }
  .param { display: flex; align-items: center; gap: 6px; border: 1px solid var(--divider-color);
    border-radius: 10px; padding: 6px 8px; font-size: 0.8rem; }
  .param .name { color: var(--secondary-text-color); }
  .param .val { font-variant-numeric: tabular-nums; font-weight: 600; min-width: 58px; text-align: right; }
  .param button { width: 26px; height: 26px; border-radius: 50%; border: 1px solid var(--divider-color);
    background: none; color: var(--primary-text-color); cursor: pointer; font-size: 0.95rem;
    line-height: 1; font-family: inherit; }
  .param button:hover { background: var(--secondary-background-color); }
  .empty { color: var(--secondary-text-color); font-size: 0.9rem; padding: 24px 0; text-align: center; }
`;

const PARAM_LABELS = {
  a: "Déperditions (a)",
  c_s: "Apport solaire (c_s)",
  d: "Apports internes (d)",
  k: "Gain de chauffe (k)",
};
const PARAM_STEPS = { a: 0.002, c_s: 0.0005, d: 0.01, k: 0.05 };

const escapeHtml = (value) =>
  String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");

class ThermalModelPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._panel = null;
    this._history = null;
    this._historyError = null;
    this._historyTimer = null;
    this._loading = false;
    this._chart = null;
    this._signature = null;
    this._uplotPromise = null;
    this._shellBuilt = false;
  }

  set panel(panel) {
    this._panel = panel;
    this._update();
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._history && !this._loading) this._loadHistory();
    this._update();
  }

  connectedCallback() {
    this._onResize = () => {
      const width = this._chartTarget ? this._chartTarget.clientWidth : 0;
      if (width <= 0) return;
      if (this._chart && Math.abs(width - this._width) > 4) {
        this._width = width;
        this._chart.setSize({ width, height: CHART_HEIGHT });
      } else if (!this._chart) {
        // The first render happened while the element had no layout yet (Home
        // Assistant creates the element, sets its properties, then appends it).
        this._update();
      }
    };
    window.addEventListener("resize", this._onResize);
    // A window resize event is not enough: the panel is attached and laid out
    // after its properties are set, so the first chart can be built at width 0.
    if (typeof ResizeObserver === "function") {
      this._observer = new ResizeObserver(() => this._onResize());
    }
    this._historyTimer = window.setInterval(() => this._loadHistory(), HISTORY_REFRESH_MS);
    this._loadHistory();
    this._update();
    if (this._observer && this._chartTarget) this._observer.observe(this._chartTarget);
  }

  disconnectedCallback() {
    window.removeEventListener("resize", this._onResize);
    if (this._observer) {
      this._observer.disconnect();
      this._observer = null;
    }
    if (this._historyTimer) window.clearInterval(this._historyTimer);
    if (this._chart) {
      this._chart.destroy();
      this._chart = null;
    }
  }

  // -- data -----------------------------------------------------------
  _config() {
    return (this._panel && this._panel.config) || {};
  }

  _forecasts() {
    const forecasts = {};
    for (const [scenario, entityId] of Object.entries(this._config().scenarios || {})) {
      const state = this._hass && this._hass.states[entityId];
      if (state && state.attributes && state.attributes.values) {
        forecasts[scenario] = state.attributes;
      }
    }
    return forecasts;
  }

  async _loadHistory() {
    if (!this._hass || this._loading) return;
    const config = this._config();
    const entityIds = [config.indoor_entity, config.source_outdoor_entity].filter(Boolean);
    if (!entityIds.length) return;

    this._loading = true;
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
      this._history = {
        indoor: parseHistory(result[config.indoor_entity]),
        outdoor: parseHistory(result[config.source_outdoor_entity]),
      };
      this._historyError = null;
    } catch (err) {
      this._history = this._history || { indoor: [], outdoor: [] };
      this._historyError = String((err && err.message) || err);
    } finally {
      this._loading = false;
      this._update();
    }
  }

  // -- uPlot ----------------------------------------------------------
  async _ensureUplot() {
    if (window.uPlot) return window.uPlot;
    if (!this._uplotPromise) {
      this._uplotPromise = new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = new URL("./uplot.min.js", import.meta.url).href;
        script.async = true;
        script.onload = () =>
          window.uPlot ? resolve(window.uPlot) : reject(new Error("uPlot vide après chargement"));
        script.onerror = () => reject(new Error("module uPlot introuvable"));
        document.head.appendChild(script);
      });
    }
    return this._uplotPromise;
  }

  async _updateChart(data) {
    if (!data.x.length || !data.series.length) {
      this._chartTarget.innerHTML = `<div class="empty">Aucune donnée à afficher pour l'instant.</div>`;
      return;
    }

    // Building a chart at width 0 draws nothing and leaves nothing to look at:
    // wait for the ResizeObserver to report a real size.
    const measured = (this._chartTarget && this._chartTarget.clientWidth) || 0;
    if (measured <= 0 && !this._chart) return;

    const uPlot = await this._ensureUplot();
    const width = Math.max(320, measured || this._width || 900);
    this._width = width;

    const signature = data.series.map((entry) => entry.id).join("|");
    if (this._chart && signature !== this._signature) {
      this._chart.destroy();
      this._chart = null;
    }

    const columns = [data.x, ...data.series.map((entry) => entry.values)];
    if (this._chart) {
      this._chart.setData(columns);
      return;
    }

    this._chartTarget.innerHTML = "";
    const nowLine = {
      hooks: {
        draw: (u) => {
          const { ctx } = u;
          const position = u.valToPos(Date.now() / 1000, "x");
          if (!Number.isFinite(position)) return;
          ctx.save();
          ctx.strokeStyle = "#e53935";
          ctx.lineWidth = 1;
          ctx.setLineDash([5, 4]);
          ctx.beginPath();
          ctx.moveTo(position, u.bbox.top);
          ctx.lineTo(position, u.bbox.top + u.bbox.height);
          ctx.stroke();
          ctx.restore();
        },
      },
    };

    this._chart = new uPlot(
      {
        width,
        height: CHART_HEIGHT,
        padding: [10, 10, 0, 0],
        legend: { show: true, live: true },
        cursor: {
          drag: { setScale: true, x: true, y: false, dist: 8 },
          focus: { prox: 30 },
        },
        scales: {
          x: { time: true },
          y: { range: () => temperatureRange(data.series, "y") },
          out: { range: () => temperatureRange(data.series, "out") },
        },
        axes: buildAxes(data.series),
        series: buildSeriesConfig(data.series, { hoursToShow: PAST_HOURS + FUTURE_HOURS }),
      },
      columns,
      this._chartTarget,
      [nowLine],
    );
    this._signature = signature;

    // double-click restores the full window after a drag-zoom
    this._chartTarget.addEventListener("dblclick", () => {
      this._chart.setScale("x", { min: null, max: null });
    });
  }

  // -- shell ----------------------------------------------------------
  _buildShell() {
    this.shadowRoot.innerHTML = `
      <style>${CSS}</style>
      <link rel="stylesheet" href="${new URL("./uplot.min.css", import.meta.url).href}">
      <div class="wrap">
        <div class="card">
          <div class="row" style="justify-content: space-between">
            <div>
              <h1 data-title></h1>
              <div class="sub">Mesuré, prévisions et prévisions passées sur la même échelle
                <span data-engine></span></div>
            </div>
            <div class="row" style="gap: 14px">
              <div class="stat"><span class="k">Maintenant</span><span class="v" data-now>—</span></div>
              <div class="stat"><span class="k">Minimum prévu</span><span class="v" data-min>—</span></div>
              <div class="stat"><span class="k">Maximum prévu</span><span class="v" data-max>—</span></div>
              <div class="stat"><span class="k">Émis</span><span class="v" style="font-size:1rem" data-issued>—</span></div>
              <div class="stat"><span class="k">Modèle météo</span><span class="v" style="font-size:1rem" data-weather>—</span></div>
              <span class="badge ok" data-badge>à jour</span>
            </div>
          </div>
          <div data-banners></div>
        </div>

        <div class="card">
          <div class="chart" data-chart></div>
          <div class="foot" data-foot></div>
        </div>

        <div class="card">
          <div class="sub" style="margin-bottom: 10px" data-params-info></div>
          <div class="params" data-params></div>
        </div>
      </div>`;

    const find = (selector) => this.shadowRoot.querySelector(selector);
    this._nodes = {
      title: find("[data-title]"),
      engine: find("[data-engine]"),
      now: find("[data-now]"),
      min: find("[data-min]"),
      max: find("[data-max]"),
      issued: find("[data-issued]"),
      weather: find("[data-weather]"),
      badge: find("[data-badge]"),
      banners: find("[data-banners]"),
      foot: find("[data-foot]"),
      paramsInfo: find("[data-params-info]"),
      params: find("[data-params]"),
    };
    this._chartTarget = find("[data-chart]");
    this._shellBuilt = true;
  }

  _update() {
    if (!this._hass) return;
    if (!this._shellBuilt) this._buildShell();

    const config = this._config();
    const forecasts = this._forecasts();
    const reference = Object.values(forecasts)[0] || {};
    const nowMs = Date.now();

    const data = buildChartData({
      history: this._history || { indoor: [], outdoor: [] },
      forecasts,
      nowMs,
    });
    const summary = summarise({ data, forecasts, nowMs });

    this._nodes.title.textContent = (this._panel && this._panel.title) || "Thermal Model";
    this._nodes.engine.textContent = `· moteur uPlot ${UPLOT_VERSION}`;
    this._nodes.now.textContent =
      summary.current !== null && Number.isFinite(summary.current)
        ? `${summary.current.toFixed(1)} °C`
        : "—";
    this._nodes.min.textContent =
      summary.minimum !== null ? `${summary.minimum.toFixed(1)} °C` : "—";
    this._nodes.max.textContent =
      summary.maximum !== null ? `${summary.maximum.toFixed(1)} °C` : "—";
    this._nodes.issued.textContent = reference.issued_at
      ? new Date(reference.issued_at).toLocaleTimeString(undefined, {
          hour: "2-digit",
          minute: "2-digit",
        })
      : "—";
    this._nodes.weather.textContent = reference.weather_model || "—";
    this._nodes.badge.textContent = reference.stale ? "périmé" : "à jour";
    this._nodes.badge.className = `badge ${reference.stale ? "warn" : "ok"}`;

    const banners = [];
    // What the panel expected and did not find. Without this the page shows an
    // empty chart and nothing to act on; it is also the fastest way to diagnose a
    // registry mismatch after a rename.
    const missing = [];
    if (!config.entry_id) missing.push("configuration du panneau absente");
    if (!config.indoor_entity) {
      missing.push("capteur intérieur non fourni");
    } else if (!this._hass.states[config.indoor_entity]) {
      missing.push(`capteur introuvable : ${config.indoor_entity}`);
    }
    const scenarioEntries = Object.entries(config.scenarios || {});
    if (!scenarioEntries.length) missing.push("aucun scénario de prévision configuré");
    for (const [, entityId] of scenarioEntries) {
      if (!this._hass.states[entityId]) missing.push(`prévision introuvable : ${entityId}`);
    }
    if (missing.length) {
      banners.push(
        `<div class="banner err">Le panneau ne trouve pas tout ce qu'il attend : ${missing
          .map(escapeHtml)
          .join(" · ")}</div>`,
      );
    }
    if (reference.stale) {
      banners.push(
        `<div class="banner">⚠️ Météo indisponible : les dernières trajectoires connues sont affichées${
          reference.error ? ` (${escapeHtml(reference.error)})` : ""
        }.</div>`,
      );
    }
    if (this._historyError) {
      banners.push(
        `<div class="banner err">Historique indisponible : ${escapeHtml(this._historyError)}.
         La courbe mesurée peut manquer (intégration « history » désactivée ?).</div>`,
      );
    }
    this._nodes.banners.innerHTML = banners.join("");

    const archived = data.series.filter((entry) => entry.id.includes(ARCHIVED_PREFIX));
    this._nodes.foot.textContent = [
      "Trait plein : mesuré et prévu. Pointillés : prévisions émises plus tôt, à comparer avec le mesuré.",
      `${summary.seriesCount} trajectoire(s) intérieure(s)`,
      archived.length
        ? `prévisions passées : ${archived
            .map((entry) => entry.label.split("il y a ")[1])
            .join(", ")}`
        : "aucune prévision passée archivée pour l'instant",
      "glisser pour zoomer, double-clic pour revenir",
      `diagnostic : ${scenarioEntries.length ? Object.keys(forecasts).length : 0}/${
        scenarioEntries.length
      } scénario(s) · historique ${
        (this._history && this._history.indoor && this._history.indoor.length) || 0
      } point(s) · ${data.series.length} série(s) · ${
        this._chart ? "graphique prêt" : "graphique en attente"
      }${this._historyError ? " · historique en erreur" : ""} · largeur ${
        this._chartTarget ? this._chartTarget.clientWidth : 0
      }px${this._chart ? ` · toile ${this._chart.width}×${this._chart.height}` : ""}`,
    ].join(" · ");

    this._nodes.paramsInfo.innerHTML = `Paramètres du modèle — source : <strong>${escapeHtml(
      reference.params_source || "—",
    )}</strong> · empreinte <code>${escapeHtml(reference.params_digest || "—")}</code>`;
    this._nodes.params.innerHTML = Object.entries(PARAM_LABELS)
      .filter(([key]) => config.numbers && config.numbers[key])
      .map(([key, label]) => {
        const state = this._hass.states[config.numbers[key]];
        const value = state ? Number.parseFloat(state.state) : NaN;
        return `<div class="param">
            <span class="name">${label}</span>
            <button data-number="${key}" data-dir="down" aria-label="diminuer">−</button>
            <span class="val">${Number.isFinite(value) ? value.toFixed(key === "c_s" ? 4 : 3) : "—"}</span>
            <button data-number="${key}" data-dir="up" aria-label="augmenter">+</button>
          </div>`;
      })
      .join("");
    for (const button of this._nodes.params.querySelectorAll("[data-number]")) {
      button.addEventListener("click", () =>
        this._nudge(button.dataset.number, button.dataset.dir),
      );
    }

    this._updateChart(data).catch((err) => {
      this._chartTarget.innerHTML = `<div class="empty">Graphique indisponible :
        ${escapeHtml((err && err.message) || err)}</div>`;
    });
  }

  _nudge(key, direction) {
    const entityId = (this._config().numbers || {})[key];
    const state = this._hass && this._hass.states[entityId];
    if (!state) return;
    const current = Number.parseFloat(state.state);
    if (!Number.isFinite(current)) return;
    const step = PARAM_STEPS[key] || 0.01;
    const value = direction === "up" ? current + step : current - step;
    this._hass.callService("number", "set_value", {
      entity_id: entityId,
      value: Math.max(0, Number(value.toFixed(4))),
    });
  }
}

/**
 * The websocket returns a full state object first, then {s, lu} deltas. Both
 * shapes are accepted, and anything unparsable is skipped rather than plotted as
 * zero.
 */
function parseHistory(rows) {
  if (!Array.isArray(rows)) return [];
  const points = [];
  for (const row of rows) {
    if (!row) continue;
    const raw = row.s !== undefined ? row.s : row.state;
    const stamp =
      row.lu !== undefined ? row.lu : row.lc !== undefined ? row.lc : row.last_changed;
    const value = Number.parseFloat(raw);
    if (Number.isFinite(value) && stamp) points.push([new Date(stamp).getTime(), value]);
  }
  points.sort((a, b) => a[0] - b[0]);
  return points;
}

customElements.define("thermal-model-panel", ThermalModelPanel);
