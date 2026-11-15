# Vendored frontend dependency

**ApexCharts 4.7.0** — MIT, © 2018 ApexCharts.
Upstream: https://github.com/apexcharts/apexcharts.js (npm package `apexcharts@4.7.0`).

| File | Upstream name | SHA-256 |
|---|---|---|
| `apexcharts.min.js` | `dist/apexcharts.min.js` | `c46de876c375aab3fbc23d82418f7d77251403335808983d2b832d4a38481948` |

## Why this library, and why vendored

The panel needs what every chart needs and what a hand-written SVG renderer gets
wrong: axis tick values that stay put while the cursor moves, a shared tooltip
listing the series at the hovered instant, axis titles with units, annotations (the
"now" marker), a clickable legend, synchronised cursors and zoom across panels, and
gap handling. Each of those was a bug in the previous hand-rolled and minimal-library
attempts.

ApexCharts is the library Home Assistant users already have through
`apexcharts-card`, so the rendering and the interactions are the ones they expect.
It costs 576 KB raw / 153 KB gzipped — accepted deliberately over uPlot's 22 KB
because the point is to stop maintaining chart behaviours ourselves.

Vendored rather than loaded from a CDN: the panel must work on an installation with
no internet access, and a third-party URL inside an authenticated Home Assistant page
is a supply chain the user did not ask for. The version is pinned, the file is served
by the integration itself, and `tests/test_panel.py` checks that the version declared
in the code matches this table.

## Updating

```bash
npm pack apexcharts@<version>
tar xzf apexcharts-<version>.tgz
cp package/dist/apexcharts.min.js  custom_components/thermal_model/frontend/apexcharts.min.js
cp package/LICENSE                 custom_components/thermal_model/frontend/apexcharts.LICENSE
sha256sum custom_components/thermal_model/frontend/apexcharts.min.js   # update this table
```
