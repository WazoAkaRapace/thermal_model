# Thermal Model

A **generic** Home Assistant integration that identifies the thermal model of a house and forecasts
its indoor temperature.

- **Three entities in**: a heating entity (`climate`, `switch`, `water_heater` or `input_boolean`),
  an indoor temperature sensor, an outdoor temperature sensor.
- **A physical model out**: a 1R1C model identified from your own history, with published parameters
  and standard errors.
- **Forecasts out**: indoor temperature trajectories computed from weather forecasts, with **several
  weather models selectable** so you can compare them side by side.

**No numpy, no scipy, no external dependency at all.** The identification runs in pure Python
(four parameters, ordinary least squares — a few milliseconds for years of hourly data), so the
integration installs identically on any architecture (x86-64, aarch64, armv7, musl/Alpine).

> Scope: this integration **computes and publishes**. It does not drive your heating. A later
> milestone will expose the model to a piloting layer through services, but identification and
> forecasting come first — you cannot optimise what you cannot predict.

## Installation

1. HACS → Integrations → ⋮ → *Custom repositories* → add
   `https://github.com/WazoAkaRapace/thermal_model` as an **Integration**.
2. Install *Thermal Model*, restart Home Assistant.
3. Settings → Devices & services → *Add integration* → **Thermal Model**.

## Configuration

Everything is bound through the UI; no YAML, and no entity id is hardcoded.

| Step | What it asks | Notes |
|---|---|---|
| 1 | Name, latitude, longitude | Used for weather forecasts and past irradiance |
| 2 | Heating entity, indoor sensor, outdoor sensor | **This is the minimum to build a model** |
| 3 | Identification window, storage step, smoothing, retention | Sensible defaults; editable later in *Configure* |

Optional inputs make the model more accurate but are never required: a real heating power sensor,
the rated power and COP of the heat pump, a local irradiance sensor.

## How the model works

```
dT/dt = −a·(T − Text) + c_s·S + d + k·P
```

- `a` — normalised heat loss (UA/C), fitted on passive low-solar hours
- `c_s` — solar gain, fitted on daylight passive hours (`a`, `d` held fixed)
- `d` — net internal gains
- `k` — heating gain, obtained from an algebraic energy balance (never from a closed-loop
  regression: a thermostat makes it endogenous and biased)

Identification details, the estimator that avoids measurement-noise bias, the identifiability
diagnostics and the reasons a parameter can honestly come back as `unknown` are documented in
[`docs/`](docs/).

## Dashboard

A ready-to-import Lovelace dashboard ships in
[`dashboards/thermal_model.yaml`](dashboards/thermal_model.yaml). It plots 48 h of
**measured** indoor temperature, the 48 h **forecasts** of each scenario, and the
forecasts **issued 6 h / 12 h / 24 h ago** on the same axis — the only way to see
whether the model anticipates the house or merely agrees with itself. A second view
uses built-in cards only, so the dashboard is usable without any custom card.

See [`docs/dashboard.md`](docs/dashboard.md) for the import steps and the attributes
the cards read. `tests/test_dashboard.py` parses the YAML and fails if it references
an entity or attribute that does not exist.

## Status

| Milestone | Content | State |
|---|---|---|
| M1 | Integration skeleton, config flow, CI | ✅ |
| M2 | Local SQLite store, 1R1C identification | 🚧 core done, recorder reader pending |
| M3 | Forecast engine, multi-model weather | 🚧 Open-Meteo + 3 scenarios, one model at a time |
| M4 | `weather.*` entities, validation scoreboard, repairs | planned |
| M5 | Dashboards, HACS release | 🚧 bundled dashboard + entities, release pending |

## Development

```bash
python3.14 -m venv .venv          # Python 3.14 or use: uv venv --python 3.14 .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/python -m pytest -q          # test suite
.venv/bin/ruff check .                 # lint
```

The suite runs against a real Home Assistant test harness
(`pytest-homeassistant-custom-component`), not mocks of it. It is pinned to the
Home Assistant version people actually run — currently **2026.10.4** — which is
also the minimum declared in `hacs.json`: a newer minimum could not be honestly
claimed without testing it.

## Licence

MIT
