# 1R1C identification — numerics

The model is deliberately the simplest one that can be explained and audited:

```
dT/dt = −a·(T − Text) + c_s·S + d + k·P
```

| Symbol | Meaning | Unit | How it is obtained |
|---|---|---|---|
| `a` | normalised heat loss (UA/C) | 1/h | fit on passive low-solar hours |
| `c_s` | solar gain | K/(W/m²)/h | fit on daylight passive hours, `a` and `d` fixed |
| `d` | net internal gains | K/h | with `a` in the same passive fit |
| `k` | heating gain | K/(h·kW) | algebraic energy balance on heating steps |

Discrete form (Euler, every term multiplied by `dt`):

```
T[k+1] = T[k] + dt·(−a·(T[k]−Text[k]) + c_s·S[k] + d + k·P[k])
```

## Why the naive estimator is wrong

Regressing `ΔT` on the *same* noisy temperature series it was differenced from biases `a` upward:
the difference and the regressor share the measurement noise, so `cov(Δn, n) = −σ²` inflates the
slope. Measured on synthetic data (a = 0.0065 /h, σ = 0.1 K):

| Estimator | bias on `a` |
|---|---|
| raw 15-min difference, regressor = start point | **+207 %** |
| 1-h difference, regressor = start point | **+64 %** |
| 1-h difference + 2 h smoothing | +1 % (±18 %) |
| 1-h difference + smoothing + **window-matched regressor** | −6 to −9 % (±15 %) |

## The estimator actually used

1. Work on the **hourly rollup**, never the raw 15-min series.
2. **Smooth** the indoor temperature (centred moving average, `smooth_hours`, default 2 h) *before*
   any differencing.
3. **Overlapping 1-h differences** with a **window-matched regressor** — the exact discretisation of
   `ΔT = −a·∫(T−Text)dt + d·Δt`:

   ```
   y[k] = (T[k+L] − T[k]) / L          with L = 1 h
   x[k] = mean(T − Text over [k, k+L])  — never the start point
   ```
4. Masks: heating steps (`P > 0`), the **post-heating grace period** (`tau_grace_minutes`, 30 min
   default — 2–3 h for a floor heating system), and outliers above `outlier_threshold` K/h.
5. `a` and `d` on **low-solar passive hours** (`S < 10 W/m²`, day *and* night). If `SE(a) > 30 %`,
   widen to `S < 50 W/m²`, then fall back to a joint passive fit — the choice is decided by
   synthetic tests, not by taste.
6. `c_s` on daylight passive hours with `a` and `d` **held fixed** (a joint fit is degenerate).
7. **A standard error is published for every parameter.** It is the only honest way to say "this
   number is solid" or "this number is noise".

## Why `k` is special

In a heated house the boiler runs *because* the room is cold: heating power and indoor temperature
are correlated by the controller, not only by physics. Regressing `ΔT` on `P` therefore gives a
biased (sometimes negative) `k`. Three routes, in order:

1. **Measured power** — algebraic balance on heating steps with known `a`, `c_s`, `d`:
   `k = (ΔT/dt + a·(T−Text) − c_s·S − d) / P`. No regression, no endogeneity.
   *Modulating heat pumps*: `P = state × rated_power` overestimates `k` by ×1.5 to ×2.5, so the
   result is published as `k_estimated_from_onoff` with a ±50 % band, or a real power sensor is required.
2. **Exogenous transients** — only windows where the heating was triggered by a setpoint change
   (decorrelated from the temperature), after checking `|corr(P, T−Text)| < 0.5`.
3. **Physics** — `k = COP / C`, with the COP from the datasheet and C from a full-cycle energy balance.

If all three fail, `k = None` and the reason is published. The service still forecasts in
`free_float` (no heating) and `observed_duty` (replay of the observed cycling).

## Identifiability, not optimism

- Degeneracy is detected with **`SE(β)/|β| > 0.5`** (or `SE(a) > 0.01 /h`) → parameters are marked
  `constrained` and automatic adoption is forbidden. A condition-number threshold was tried and
  rejected: it depends on the scale of the regressors (1729 on healthy data in raw units).
- **Data volume matters**: on a tight house (a ≈ 0.0065 /h) the theoretical `SE(a)` is ~33 % at 400
  days and ~70 % at 90 days in a night-only fit. Acceptance criteria are therefore expressed in terms
  of the published `SE`, and the primary quality metric is the **backtest RMSE**, not the parameter
  recovery.
- Two backtest metrics, never mixed: `rmse_free_float` (simulation without heating, compared **only
  to genuinely passive steps**) and `rmse_observed` (full model with the observed power imposed).

## Solar term without a solar sensor

`S` comes from a local irradiance sensor when available. Otherwise:

1. `/v1/forecast?past_days=N` (≤ 92 days, free) for the recent past — real model runs;
2. the ERA5 archive only beyond that — **it lags 2 to 5 days**, so using it for the bootstrap would
   leave the most recent days without irradiance.

A local sensor stays the best investment: the reanalysis grid (~25 km, ERA5-Land 9–11 km) cannot see
your shading.
