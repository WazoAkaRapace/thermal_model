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

1. Work on the hourly window, never a single raw sample: overlapping 1-hour
   differences, with the driving term summed over the **steps** of the window
   (left endpoints only — using the mean of `span+1` points shifts the regressor
   and biases `a` by ~7 % on a pure Euler signal).
2. **No smoothing by default.** Smoothing was the intuitive fix for measurement
   noise, but it biases the slope of a curved signal. Measured on synthetic
   houses: 2 h smoothing gives +10 % on a noise-free 30-day house and −12 % on a
   90-day noisy one, while no smoothing gives +1 % and 0 %. The setting still
   exists for very noisy sensors.
3. **Instrumental variables** instead of plain least squares. The instrument is
   the *lagged* driving mean: correlated with the true driving temperature,
   uncorrelated with the noise of the current window. Plain least squares is
   biased here (errors in variables), by +60 % to +200 % depending on the lag.

   ```
   y[k]  = (T[k+L] − T[k]) / L                  # observed decay
   x[k]  = mean(T − Text over the L steps)      # driving term
   z[k]  = same mean over the previous window   # instrument
   β     = (ZᵀX)⁻¹ Zᵀy                          # → a = β₀, d = β₁
   ```
4. Masks: heating steps (`P > 0`), the **post-heating grace period**
   (`tau_grace_minutes`, 30 min default — 2–3 h for a floor heating system), and
   outliers above `outlier_threshold` K/h.
5. `a` and `d` on **low-solar passive hours** (`S < 10 W/m²`).
6. `c_s` on daylight passive hours with `a` and `d` **held fixed** (a joint fit is
   degenerate).
7. **A standard error is published for every parameter.**

## Two degeneracies, two different defences

**`(a, d)` trade off when the driving range is narrow.** If the outdoor
temperature barely moves, `y ≈ −a·x̄ + d` fits almost any `a`: the residuals stay
tiny, so the standard error looks reassuring while the extrapolation is wrong.
This is measured, not theoretical:

| data | relative spread of `T − Text` | error on `a` |
|---|---|---|
| 90 d with a seasonal trend | 0.12 | ~0 % |
| 60 d with a seasonal trend | 0.09 | +2 % |
| 30 d, flat outdoor temperature | 0.03 | **+41 %** |

So `driving_spread = σ(T−Text)/|mean|` is computed and published, and below
`min_driving_spread` (0.08) the result is marked **`constrained`** even when its
standard error is small. `constrained` means "the model predicts your house, but
you cannot claim to know how much of the decay is loss and how much is internal
gain" — which is the honest statement for a few weeks of autumn data.

**`k` is endogenous in a heated house.** See below.


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
