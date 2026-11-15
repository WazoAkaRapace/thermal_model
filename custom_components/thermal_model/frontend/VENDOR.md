# Vendored frontend dependency

**uPlot 1.6.32** — MIT, © 2022 Leon Sorokin.
Upstream: https://github.com/leeoniya/uPlot (npm package `uplot@1.6.32`).

| File | Upstream name | SHA-256 |
|---|---|---|
| `uplot.min.js` | `dist/uPlot.iife.min.js` | `19c8d4c6ad88929a79f4ae49d6f7161566dfd0ba3d15cc495e974f787eb78f1f` |
| `uplot.min.css` | `dist/uPlot.min.css` | `df630c6a8d6f8eeaff264b50f73ce5b114f646ffd9a0bb74f049b0a00135fa04` |

## Why this library, and why vendored

A time-series chart needs axes with time ticks, a shared cursor, a legend, zoom,
high-DPI handling and gap handling. Hand-rolling them is how a panel quietly
becomes worse than the thing it replaced.

uPlot is chosen because it is **51 KB raw / 22 KB gzipped** (ApexCharts is ~570 KB,
ECharts ~1 MB), it is purpose-built for time series, it has no dependency of its
own, and its API works without a build step — relevant because this integration
ships `requirements: []` and serves its own frontend.

Vendored rather than loaded from a CDN: the panel must work on an installation with
no internet access, and a third-party URL in an authenticated Home Assistant page is
a supply chain the user did not ask for. The version is pinned and the file is
served by the integration itself.

## Updating

```bash
npm pack uplot@<version>
tar xzf uplot-<version>.tgz
cp package/dist/uPlot.iife.min.js  custom_components/thermal_model/frontend/uplot.min.js
cp package/dist/uPlot.min.css      custom_components/thermal_model/frontend/uplot.min.css
cp package/LICENSE                 custom_components/thermal_model/frontend/uplot.LICENSE
sha256sum custom_components/thermal_model/frontend/uplot.*   # update this table
```

`tests/test_panel.py` checks that the files are present, that the vendored version
matches the one declared in the panel module, and that no remote URL appears in the
frontend sources.
