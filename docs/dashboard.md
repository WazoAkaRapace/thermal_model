# The bundled dashboard

`dashboards/thermal_model.yaml` is a ready-to-import Lovelace dashboard that
shows, on the same time axis:

- the **measured** indoor temperature (48 h of history, straight from the
  recorder);
- the **current forecasts**, one line per scenario (48 h ahead);
- the **forecasts issued earlier** (~6 h, ~12 h, ~24 h ago), archived in SQLite,
  so the model's drift is visible instead of being claimed;
- the **outdoor** temperature, measured against the weather model actually used.

## Why archived runs and not just "the forecast"

A forecast that is never checked is decoration. Plotting the run issued 12 hours
ago next to the thermometer answers the only question that matters: *does this
model anticipate my house, or does it merely agree with itself?* That is why
every cycle is written to a local SQLite file instead of being kept in memory.

## Import

1. **Paramètres → Tableaux de bord → Ajouter un tableau de bord**, choose *Vide*
   (not "Nouveau tableau de bord à partir de zéro" if you already use YAML mode).
2. Open the dashboard, **Modifier**, then the three-dot menu →
   **Éditeur de code brut**, and paste the content of
   `dashboards/thermal_model.yaml`.
3. If your config entry is not named `Home`, replace the `home_` prefix of every
   entity (`sensor.home_forecast_free_float`, `number.home_wall_loss_a`, ...) with
   the slug of your Thermal Model device.

The first view needs [apexcharts-card](https://github.com/RomRider/apexcharts-card)
(HACS). The second view, *Repli natif*, uses only built-in cards and works with
no custom card installed.

## Entities the dashboard relies on

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

`tests/test_dashboard.py` parses this YAML and fails if any entity id, attribute
or template reference does not exist on a live config entry — a renamed entity
would otherwise turn the dashboard into "Entity not available" with no error
anywhere.

## Attributes read by the cards

On each `forecast_*` sensor:

- `times` / `values` — the trajectory (`values[0]` is now, then one point per hour);
- `weather_times` / `weather_outdoor` — the weather the run was built on;
- `archived_runs` — up to three earlier runs (`issued_at`, `age_hours`, `times`, `values`);
- `minimum` / `minimum_at`, `horizon_hours`, `weather_model`, `issued_at`;
- `params_digest`, `params_source`, `stale`, `error`.

`stale` goes to `true` when the weather provider failed but previous trajectories
were kept: the chart keeps drawing the last known run instead of blinking empty,
and the markdown card says why.

## What these curves do *not* say yet

The four model parameters are exposed as numbers and **default to a reference
house** (`params_source: manual_default`). Until identification runs on your own
history, the trajectories show how the model behaves, not what your house will
do. The number entities exist so the dashboard is honest about it: the markdown
card prints the parameter source and the fingerprint of the set in use.
