# Frontends: the bundled panel (and an optional dashboard)

## The panel — the integrated one

The integration ships its **own panel**, registered in the sidebar the first time a
config entry loads. No import, no YAML, no entity ids to copy: it is part of the
integration, so it appears on every installation and keeps following the entities
wherever the registry moves them.

- `/thermal-model` in the sidebar, title = the config entry title;
- the module is served **by the integration** at
  `/{domain}/frontend/{version}/thermal-model-panel.js`, authenticated, with
  `Content-Type: text/javascript` and `Cache-Control: no-cache`, and the version in
  the path so a browser never mixes an old module with new ones after an update;
- the charts are drawn by **ApexCharts** (vendored, MIT, 576 KB raw / 153 KB
  gzipped): axis tick values, axis titles with units, the shared tooltip, the "now"
  annotation, the clickable legend and zoom are library features. Nothing is fetched
  from a CDN and `requirements` stays empty.

### Chart library

| Candidate | Verdict |
|---|---|
| **ApexCharts** | **chosen**: the library HA users already have through apexcharts-card, so the interactions are the expected ones |
| uPlot (~22 KB gzipped) | tried first for its size, and it cost three bugs in a row (axis values vanishing under the cursor, a hand-written tooltip, a custom draw hook for the "now" line) |
| ECharts (~1 MB) | too heavy for a panel served to every user, mobile included |
| hand-rolled SVG | what this replaced: every basic feature had to be written and tested by hand |

ApexCharts is vendored in `frontend/` with its licence and a SHA-256 table in
[`VENDOR.md`](../custom_components/thermal_model/frontend/VENDOR.md); the version is
declared once, in `thermal-model-chart.mjs`, and a test checks the two agree.

Panels do **not** share an ApexCharts `chart.group`: group sync expects the same
series on every chart, and with 3/4/2 series the shared tooltip reported the first
series three times. Coherence comes from the shared time window, the shared colours
and the same axis unit.

### What it draws

One time axis, last 48 h and next 48 h:

- the **measured** indoor temperature, from the recorder history;
- the **current forecast** of every enabled scenario (solid);
- the **forecasts issued ~6/12/24 h ago** (dashed, in their scenario's colour,
  listed in the caption), to compare with what actually happened;
- the **outdoor** temperature, measured and forecast, toggleable;
- a `maintenant` marker (drawn as a uPlot plugin, since annotations are not core),
  the built-in cursor legend giving every visible value under the pointer, a legend
  that hides a series on click, drag-to-zoom with double-click to reset, and live
  parameter chips (`a`, `c_s`, `d`, `k`) with ± buttons that write to the number
  entities.

### How it degrades

| Situation | What the panel does |
|---|---|
| Weather provider down | keeps the previous trajectories, shows the `périmé` badge and the reason |
| Recorder / `history` not loaded | draws the forecast anyway and says the measured curve is unavailable |
| No forecast entity yet | says so instead of showing an empty frame |
| Entity renamed by the user | nothing to fix: ids are resolved server-side from the entity registry and handed to the panel |

## The YAML dashboard — optional

`dashboards/thermal_model.yaml` is still shipped, for people who prefer native
Lovelace cards or want the curves inside their own dashboard. Its first view needs
[apexcharts-card](https://github.com/RomRider/apexcharts-card); the second uses
built-in cards only. Import through **Paramètres → Tableaux de bord → Ajouter →
Modifier → Éditeur de code brut**, and replace the `home_` prefix if your entry is
not called *Home*.

The panel exists precisely because that file is the problem: a dashboard full of
string identifiers that has to be imported, adapted and kept in sync.

## Entities both frontends rely on

| Entity | What it is |
|---|---|
| `sensor.<device>_indoor_temperature` | mirror of the bound indoor thermometer |
| `sensor.<device>_forecast_free_float` | trajectory with the heating off |
| `sensor.<device>_forecast_thermostat` | trajectory with a hysteresis thermostat |
| `number.<device>_wall_loss_a` | `a`, heat loss per hour |
| `number.<device>_solar_gain_c_s` | `c_s`, solar gain |
| `number.<device>_internal_gain_d` | `d`, internal gains, K/h |
| `number.<device>_heating_gain_k` | `k`, temperature rise per kWh |
| `number.<device>_scenario_setpoint` | setpoint used by the thermostat scenario |
| `number.<device>_heating_capacity` | power of the heating when on, kW |
| `number.<device>_forecast_horizon` | simulated horizon, hours |

### Attributes read by both frontends

On each `forecast_*` sensor:

- `times` / `values` — the trajectory (`values[0]` is now, then one point per hour);
- `weather_times` / `weather_outdoor` — the weather the run was built on;
- `archived_runs` — up to three earlier runs (`issued_at`, `age_hours`, `times`, `values`);
- `minimum` / `minimum_at`, `horizon_hours`, `weather_model`, `issued_at`;
- `params_digest`, `params_source`, `stale`, `error`.

`stale` goes to `true` when the weather provider failed but previous trajectories
were kept: the chart keeps drawing the last known run instead of blinking empty.

### Why archived runs and not just "the forecast"

A forecast that is never checked is decoration. Plotting the run issued 12 hours
ago next to the thermometer answers the only question that matters: *does this
model anticipate my house, or does it merely agree with itself?* That is why every
cycle is written to a local SQLite file instead of being kept in memory.

## Tests that keep the frontends honest

- `tests/test_panel.py` — the sidebar entry exists, every frontend file is served
  with a JS/CSS content type and `no-cache`, the served config carries the **real**
  entity ids, path traversal is refused, unload removes the panel, the vendored
  library has its licence and a version that matches the code, no remote URL
  appears in the sources, and both modules parse (`node --check`).
- `tests/js/chart_test.mjs` — the data layer, in node: axis alignment, a missing
  sample staying `null` instead of `0 °C`, a six-hour hole in the recorder history
  getting an axis marker so the line breaks instead of being drawn across, archived
  runs placed 6 h ago rather than at now, dashes, per-series scales (outdoor must
  not land on the indoor axis), and the y ranges.
- `tests/test_dashboard.py` — parses the YAML dashboard and fails on any entity id,
  attribute or template reference that does not exist on a live entry.

## What these curves do *not* say yet

The four model parameters default to a reference house
(`params_source: manual_default`). Until identification runs on your own history,
the trajectories show how the model behaves, not what your house will do — which is
why both frontends print the parameter source and the fingerprint of the set in
use instead of hiding it.
