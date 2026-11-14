# Pitwall web UI

React + TypeScript + [uPlot](https://github.com/leeoniya/uPlot), built with Vite.

The production build is written to `../src/pitwall/api/static/` and committed, so
`uv run pitwall serve` works on the Mac without Node.js. Only rebuild after changing files in
this folder.

## Develop

Needs Node.js 22+.

```bash
npm ci
```

Run the API with some data (e.g. after `uv run pitwall demo`) in one terminal:

```bash
uv run pitwall serve
```

and the UI dev server (hot reload, proxies `/api` to port 8000) in another:

```bash
npm run dev
```

## Build (commit the result)

```bash
npm run build
```

CI rebuilds the UI and fails if `src/pitwall/api/static/` differs from the committed build.

## Conventions

- Colours come from CSS variables in `src/styles.css` (dark default, light via
  `data-theme="light"`). Blue = this lap, orange = reference (validated colour-blind safe in
  both modes); diverging blue/grey/red for time gained/lost.
- Charts share one distance axis, one y-axis each (no dual axes), a synced crosshair and zoom.
