# Known limits

Written down rather than discovered later. A model that lies is worse than no model.

## Identification

- **One zone, one sensor.** One config entry per zone; no multi-zone model. A sensor in a hallway is
  not a house.
- **Thermostat endogeneity.** In closed loop, `k` (heating gain) can be unidentifiable. The
  integration then publishes `k = None` with the reason and keeps forecasting in `free_float` and
  `observed_duty`. It never invents a value.
- **Modulating heaters.** Without a real power sensor, an inverter heat pump's power is approximated
  by `on/off × rated power`, which overestimates `k` by ×1.5 to ×2.5 (`k_estimated_from_onoff`,
  ±50 % band).
- **High-inertia emitters** (floor heating): the 1R1C model has one time constant, so it
  underestimates long memory. Fine for 24–48 h horizons, not beyond. Raise `tau_grace_minutes`
  (2–3 h) or the fit will attribute the emitter's restitution to the building.
- **Undeclared heat sources** pollute everything: a wood stove, a DHW tank heated inside the zone, a
  server rack. They appear as outliers and are dropped, which biases `d`.
- **Sensor placement.** A sensor that catches direct sun falsifies `c_s`. The integration cannot
  guess this.
- **Solar without a solar sensor.** Reanalysis irradiance does not see local shading; expect a biased
  `c_s` in a valley, near the sea, or under trees.

## Data

- **Two to five days of ERA5 lag.** Recent past irradiance comes from the forecast endpoint
  (`past_days`); the archive is only used beyond 92 days.
- **Recorder retention** (10 days by default) limits the bootstrap. The integration keeps its own
  SQLite store afterwards, so identification quality improves with time — but only if identification
  is actually run (`auto_identify_weekly` helps).
- **Long-term statistics cannot be built for climate attributes.** That is why the integration exposes
  mirror `sensor` entities with `state_class: measurement`; the model's own statistics live in its
  local database, not in Home Assistant's recorder.

## Storage

- The local SQLite database is written in **batches, at most one flush per minute**, in an executor —
  never on the event loop. On an SD card this is still real write load, mitigated by WAL,
  `synchronous=NORMAL`, hourly rollups and a monthly incremental `VACUUM`.
- Forecast trajectories are bulky: they are stored as compact binary payloads and thinned by
  retention tiers (all rows for 14 days, one per day up to 90 days, one per week beyond) — a JSON
  payload per cycle would cost 100+ MB per year.
- `PRAGMA wal_checkpoint(TRUNCATE)` runs before Home Assistant backups, so the WAL is not shipped
  inside them.

## Forecast

- **Without piloting, a forecast is a scenario.** `free_float` (no heating), `thermostat` (hysteresis
  emulation bounded by heating capacity) and `observed_duty` (replay of yesterday's cycling) are
  published as distinct entities, each labelled: the forecast depends on what the heating will do.
- **Weather sources disagree.** Several models are offered side by side, and a validation scoreboard
  keeps MAE per horizon band. The integration recommends; it never switches source by itself.
- A weather entity from another integration rarely exposes irradiance. Either the solar term is
  estimated from cloud cover and sun elevation (labelled `solar: estimated`), or it is disabled and
  said so.
