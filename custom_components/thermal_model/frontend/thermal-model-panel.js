/**
 * Thermal Model — sidebar panel.
 *
 * Several small charts, not one giant one: each panel answers one question, they
 * share the same time window and colours, and ApexCharts syncs tooltips, cursors and
 * zoom across panels that declare the same `chart.group` — so they read as one
 * instrument.
 *
 * The chart is ApexCharts (vendored, MIT — see VENDOR.md): tick values, shared
 * tooltip, axis titles with units, annotations and the legend are library features,
 * not code we maintain. Earlier attempts with a hand-rolled renderer and then a
 * minimal plotting library both broke on exactly those behaviours.
 *
 * Update contract (Home Assistant frontend docs, "The hass object"): a new `hass`
 * object is pushed on EVERY state change while unchanged parts keep their identity.
 * The panel therefore compares the states it uses by reference, coalesces renders
 * into one timer, updates text nodes only when their value changes, and skips the
 * chart update unless the data actually moved. Rendering on every push is what made
 * an earlier version flicker.
 */

import {
  APEXCHARTS_VERSION,
  ARCHIVED_PREFIX,
  buildPanelOptions,
  buildPanels,
  parseHistoryRows,
  summarise,
} from "./thermal-model-chart.mjs";

const HISTORY_REFRESH_MS = 5 * 60 * 1000;
const PAST_HOURS = 48;
const PANEL_ORDER = ["indoor", "past", "outdoor"];

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
  .panel-head { display: flex; align-items: baseline; gap: 8px; margin-bottom: 6px; flex-wrap: wrap; }
  .panel-title { font-size: 0.98rem; font-weight: 600; color: var(--primary-text-color); }
  .panel-unit {
    font-size: 0.72rem; font-weight: 600; letter-spacing: 0.04em;
    color: var(--secondary-text-color); border: 1px solid var(--divider-color);
    border-radius: 999px; padding: 1px 7px;
  }
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
  .chart { width: 100%; min-height: 220px; }
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
    this._charts = new Map(); // panel id -> ApexCharts instance
    this._cards = new Map();
    this._libraryPromise = null;
    this._shellBuilt = false;
    this._frame = null;
    this._seenStates = new Map();
    this._textCache = new Map();
  }

  set panel(panel) {
    this._panel = panel;
    this._scheduleRender(true);
  }

  set hass(hass) {
    this._hass = hass;
    const changed = this._collectChanges(hass);
    if (!changed) return;
    if (!this._history && !this._loading) this._loadHistory();
    this._scheduleRender();
  }

  connectedCallback() {
    this._historyTimer = window.setInterval(() => this._loadHistory(), HISTORY_REFRESH_MS);
    this._loadHistory();
    this._scheduleRender(true);
  }

  disconnectedCallback() {
    if (this._historyTimer) window.clearInterval(this._historyTimer);
    if (this._frame) clearTimeout(this._frame);
    for (const chart of this._charts.values()) {
      try {
        chart.destroy();
      } catch {
        /* already gone */
      }
    }
    this._charts.clear();
  }

  // -- change detection ------------------------------------------------
  _config() {
    return (this._panel && this._panel.config) || {};
  }

  _relevantIds() {
    const config = this._config();
    return [
      ...(config.indoor_entity ? [config.indoor_entity] : []),
      ...(config.source_outdoor_entity ? [config.source_outdoor_entity] : []),
      ...Object.values(config.scenarios || {}),
      ...Object.values(config.numbers || {}),
    ];
  }

  _collectChanges(hass) {
    const states = (hass && hass.states) || {};
    let changed = false;
    for (const entityId of this._relevantIds()) {
      const current = states[entityId];
      if (this._seenStates.get(entityId) !== current) {
        this._seenStates.set(entityId, current);
        changed = true;
      }
    }
    return changed;
  }

  // -- data -----------------------------------------------------------
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
        indoor: parseHistoryRows(result[config.indoor_entity]),
        outdoor: parseHistoryRows(result[config.source_outdoor_entity]),
      };
      this._historyError = null;
    } catch (err) {
      this._history = this._history || { indoor: [], outdoor: [] };
      this._historyError = String((err && err.message) || err);
    } finally {
      this._loading = false;
      this._scheduleRender(true);
    }
  }

  // -- ApexCharts ------------------------------------------------------
  async _ensureLibrary() {
    if (window.ApexCharts) return window.ApexCharts;
    if (!this._libraryPromise) {
      this._libraryPromise = new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = new URL("./apexcharts.min.js", import.meta.url).href;
        script.async = true;
        script.onload = () =>
          window.ApexCharts
            ? resolve(window.ApexCharts)
            : reject(new Error("ApexCharts vide après chargement"));
        script.onerror = () => reject(new Error("module ApexCharts introuvable"));
        document.head.appendChild(script);
      });
    }
    return this._libraryPromise;
  }

  _panelCard(panel) {
    let card = this._cards.get(panel.id);
    if (card) return card;

    card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `
      <div class="panel-head">
        <span class="panel-title">${escapeHtml(panel.title)}</span>
        <span class="panel-unit">${escapeHtml(panel.unit)}</span>
        <span class="sub">${escapeHtml(panel.subtitle || "")}</span>
      </div>
      <div class="chart"></div>`;
    const rank = PANEL_ORDER.indexOf(panel.id);
    const before = [...this._cards.entries()].find(([id]) => PANEL_ORDER.indexOf(id) > rank);
    this._chartHost.insertBefore(card, before ? before[1] : null);
    this._cards.set(panel.id, card);
    return card;
  }

  async _renderPanels(x, panels, nowMs) {
    if (!x.length || !panels.length) {
      if (this._charts.size === 0) {
        this._setChartHost(
          `<div class="card"><div class="empty">Aucune donnée à afficher pour l'instant.</div></div>`,
        );
      }
      return;
    }
    const Library = await this._ensureLibrary();
    const dark =
      window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;

    for (const panel of panels) {
      const card = this._panelCard(panel);
      const target = card.querySelector(".chart");
      const signature = panel.series.map((entry) => entry.id).join("|");
      const existing = this._charts.get(panel.id);

      if (existing && existing.__signature !== signature) {
        existing.destroy();
        this._charts.delete(panel.id);
        target.innerHTML = "";
      }

      const chart = this._charts.get(panel.id);
      const options = buildPanelOptions(panel, x, { nowMs, dark });

      if (chart) {
        // No animation, no full re-render: just the data and the "now" marker.
        await chart.updateSeries(options.series, false);
        continue;
      }

      target.innerHTML = "";
      const instance = new Library(target, options);
      await instance.render();
      instance.__signature = signature;
      this._charts.set(panel.id, instance);
    }
  }

  // -- shell ----------------------------------------------------------
  _buildShell() {
    this.shadowRoot.innerHTML = `
      <style>${CSS}</style>
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

        <div data-charts></div>

        <div class="card">
          <div class="sub" style="margin-bottom: 10px" data-params-info></div>
          <div class="params" data-params></div>
          <div class="foot" data-foot></div>
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
    this._chartHost = find("[data-charts]");
    this._cards = new Map();
    this._shellBuilt = true;
  }

  _setText(node, key, value) {
    if (!node) return;
    if (this._textCache.get(key) === value) return;
    this._textCache.set(key, value);
    node.textContent = value;
  }

  _setHtml(node, key, value) {
    if (!node) return;
    if (this._textCache.get(key) === value) return;
    this._textCache.set(key, value);
    node.innerHTML = value;
  }

  _setChartHost(html) {
    if (this._textCache.get("chartHost") === html) return;
    this._textCache.set("chartHost", html);
    this._chartHost.innerHTML = html;
  }

  _scheduleRender(force = false) {
    if (force) this._textCache.clear();
    if (this._frame) return;
    this._frame = setTimeout(() => {
      this._frame = null;
      this._update().catch((err) => {
        console.error("Thermal Model panel: render failed", err);
      });
    }, 16);
  }

  _update() {
    if (!this._hass) return Promise.resolve();
    if (!this._shellBuilt) this._buildShell();

    const config = this._config();
    const forecasts = this._forecasts();
    const reference = Object.values(forecasts)[0] || {};
    const nowMs = Date.now();

    const { x, panels, data } = buildPanels({
      history: this._history || { indoor: [], outdoor: [] },
      forecasts,
      nowMs,
    });
    const summary = summarise({ data, forecasts, nowMs });

    this._setText(this._nodes.title, "title", (this._panel && this._panel.title) || "Thermal Model");
    this._setText(this._nodes.engine, "engine", `· ApexCharts ${APEXCHARTS_VERSION}`);
    this._setText(
      this._nodes.now,
      "now",
      summary.current !== null && Number.isFinite(summary.current)
        ? `${summary.current.toFixed(1)} °C`
        : "—",
    );
    // innerHTML here: the value mixes a big number and a small annotation.
    // Every part is built by toFixed() on numbers — there is nothing to inject.
    this._setHtml(
      this._nodes.min,
      "min",
      (() => {
        const attrs = forecasts.free_float || {};
        const bias = Number.parseFloat(attrs.min_bias_k);
        if (
          summary.minimum !== null &&
          Number.isFinite(bias) &&
          Math.abs(bias) >= 0.05
        ) {
          return `${(summary.minimum - bias).toFixed(1)} °C <small>(${summary.minimum.toFixed(1)} − biais ${bias.toFixed(1)})</small>`;
        }
        return summary.minimum !== null ? `${summary.minimum.toFixed(1)} °C` : "—";
      })(),
    );
    this._setText(
      this._nodes.max,
      "max",
      summary.maximum !== null ? `${summary.maximum.toFixed(1)} °C` : "—",
    );
    this._setText(
      this._nodes.issued,
      "issued",
      reference.issued_at
        ? new Date(reference.issued_at).toLocaleTimeString(undefined, {
            hour: "2-digit",
            minute: "2-digit",
          })
        : "—",
    );
    this._setText(this._nodes.weather, "weather", reference.weather_model || "—");
    this._setText(this._nodes.badge, "badge", reference.stale ? "périmé" : "à jour");
    const badgeClass = `badge ${reference.stale ? "warn" : "ok"}`;
    if (this._textCache.get("badgeClass") !== badgeClass) {
      this._textCache.set("badgeClass", badgeClass);
      this._nodes.badge.className = badgeClass;
    }

    const banners = [];
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
    this._setHtml(this._nodes.banners, "banners", banners.join(""));

    const archived = data.series.filter((entry) => entry.id.includes(ARCHIVED_PREFIX));
    this._setText(
      this._nodes.foot,
      "foot",
      [
        "Trait plein : mesuré et prévu. Pointillés : prévisions émises plus tôt, à comparer avec le mesuré.",
        archived.length
          ? `prévisions passées : ${archived
              .map((entry) => entry.label.split("il y a ")[1])
              .join(", ")}`
          : "aucune prévision passée archivée pour l'instant",
        "survol, curseur et zoom synchronisés entre les graphiques · clic sur une légende pour masquer une série",
        `diagnostic : ${Object.keys(forecasts).length}/${scenarioEntries.length} scénario(s) · historique ${
          (this._history && this._history.indoor && this._history.indoor.length) || 0
        } point(s) · ${panels.length} graphique(s) · ${data.series.length} série(s) · largeur ${
          this._chartHost ? this._chartHost.clientWidth : 0
        }px`,
      ].join(" · "),
    );

    this._setHtml(
      this._nodes.paramsInfo,
      "paramsInfo",
      `Paramètres du modèle — source : <strong>${escapeHtml(
        reference.params_source || "—",
      )}</strong> · empreinte <code>${escapeHtml(reference.params_digest || "—")}</code>`,
    );
    this._renderParams(config);

    const signature = [
      x.length,
      x.length ? x[0] : 0,
      x.length ? x[x.length - 1] : 0,
      reference.issued_at || "",
      (this._history && this._history.indoor && this._history.indoor.length) || 0,
      panels.map((panel) => panel.series.map((entry) => entry.id).join(",")).join("|"),
    ].join("§");
    if (this._textCache.get("chartSignature") !== signature) {
      this._textCache.set("chartSignature", signature);
      return this._renderPanels(x, panels, nowMs).catch((err) => {
        this._setChartHost(
          `<div class="card"><div class="empty">Graphiques indisponibles :
            ${escapeHtml((err && err.message) || err)}</div></div>`,
        );
      });
    }
    return Promise.resolve();
  }

  _renderParams(config) {
    const keys = Object.keys(PARAM_LABELS).filter(
      (key) => config.numbers && config.numbers[key],
    );
    const signature = keys.join(",");
    if (this._textCache.get("paramKeys") !== signature || !this._nodes.params.childElementCount) {
      this._textCache.set("paramKeys", signature);
      this._nodes.params.innerHTML = keys
        .map(
          (key) => `<div class="param" data-key="${key}">
            <span class="name">${PARAM_LABELS[key]}</span>
            <button data-number="${key}" data-dir="down" aria-label="diminuer">−</button>
            <span class="val" data-val="${key}">—</span>
            <button data-number="${key}" data-dir="up" aria-label="augmenter">+</button>
          </div>`,
        )
        .join("");
      for (const button of this._nodes.params.querySelectorAll("[data-number]")) {
        button.addEventListener("click", () =>
          this._nudge(button.dataset.number, button.dataset.dir),
        );
      }
    }
    for (const key of keys) {
      const state = this._hass.states[config.numbers[key]];
      const value = state ? Number.parseFloat(state.state) : NaN;
      const field = this._nodes.params.querySelector(`[data-val="${key}"]`);
      if (field) {
        this._setText(
          field,
          `param:${key}`,
          Number.isFinite(value) ? value.toFixed(key === "c_s" ? 4 : 3) : "—",
        );
      }
    }
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

customElements.define("thermal-model-panel", ThermalModelPanel);
