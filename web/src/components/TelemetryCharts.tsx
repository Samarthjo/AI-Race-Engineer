import { forwardRef, useEffect, useImperativeHandle, useRef } from "react";
import uPlot from "uplot";
import type { Comparison, Series } from "../api";

/** Synchronised telemetry charts on one distance axis (small multiples, one y-axis each).
 *  Blue = this lap, orange = reference. Crosshair and zoom (drag) are shared by all charts. */

type Spec = {
  key: string;
  title: string;
  height: number;
  lap: (c: Comparison) => Series;
  ref?: (c: Comparison) => Series;
  range?: [number, number];
  stepped?: boolean;
  delta?: boolean;
  tick: (v: number) => string;
};

const pct = (s: Series | undefined): Series => (s ?? []).map((v) => (v === null ? null : v * 100));

export const SPECS: Spec[] = [
  {
    key: "delta",
    title: "Time delta to reference (s) · above zero = behind",
    height: 130,
    lap: (c) => c.trace.delta,
    delta: true,
    tick: (v) => v.toFixed(2),
  },
  {
    key: "speed",
    title: "Speed (km/h)",
    height: 190,
    lap: (c) => c.trace.lap.speed,
    ref: (c) => c.trace.ref.speed,
    tick: (v) => v.toFixed(0),
  },
  {
    key: "throttle",
    title: "Throttle (%)",
    height: 95,
    lap: (c) => pct(c.trace.lap.throttle),
    ref: (c) => pct(c.trace.ref.throttle),
    range: [0, 100],
    tick: (v) => v.toFixed(0),
  },
  {
    key: "brake",
    title: "Brake (%)",
    height: 95,
    lap: (c) => pct(c.trace.lap.brake),
    ref: (c) => pct(c.trace.ref.brake),
    range: [0, 100],
    tick: (v) => v.toFixed(0),
  },
  {
    key: "gear",
    title: "Gear",
    height: 95,
    lap: (c) => c.trace.lap.gear,
    ref: (c) => c.trace.ref.gear,
    range: [0, 8.5],
    stepped: true,
    tick: (v) => (Number.isInteger(v) ? String(v) : ""),
  },
  {
    key: "steer",
    title: "Steering (−1 full left · +1 full right)",
    height: 95,
    lap: (c) => c.trace.lap.steer,
    ref: (c) => c.trace.ref.steer,
    range: [-1.05, 1.05],
    tick: (v) => v.toFixed(1),
  },
];

const X_AXIS_BAND = 30;

export type ChartsHandle = {
  showIndex(i: number | null): void;
  zoom(min: number, max: number): void;
  resetZoom(): void;
};

type Props = {
  data: Comparison;
  theme: string;
  onHover: (i: number | null) => void;
};

function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function deltaFill(u: uPlot, loss: string, gain: string): CanvasRenderingContext2D["fillStyle"] {
  const top = u.bbox.top;
  const h = u.bbox.height;
  if (!h) return loss;
  const zero = u.valToPos(0, "y", true);
  const f = Math.min(1, Math.max(0, (zero - top) / h));
  const g = u.ctx.createLinearGradient(0, top, 0, top + h);
  g.addColorStop(0, loss);
  g.addColorStop(f, loss);
  g.addColorStop(f, gain);
  g.addColorStop(1, gain);
  return g;
}

function drawAnnotations(
  u: uPlot,
  sectorStarts: number[],
  zeroLine: boolean,
  lineColor: string,
  labelColor: string,
  labels: boolean,
) {
  const ctx = u.ctx;
  const { left, top, width, height } = u.bbox;
  const dpr = window.devicePixelRatio || 1;
  ctx.save();
  ctx.strokeStyle = lineColor;
  ctx.lineWidth = dpr;
  ctx.fillStyle = labelColor;
  ctx.font = `${11 * dpr}px system-ui, -apple-system, sans-serif`;
  sectorStarts.forEach((d, i) => {
    const xp = u.valToPos(d, "x", true);
    if (xp < left || xp > left + width) return;
    ctx.beginPath();
    ctx.moveTo(xp, top);
    ctx.lineTo(xp, top + height);
    ctx.stroke();
    if (labels) ctx.fillText(`S${i + 2}`, xp + 4 * dpr, top + 12 * dpr);
  });
  if (zeroLine) {
    const yp = u.valToPos(0, "y", true);
    ctx.beginPath();
    ctx.moveTo(left, yp);
    ctx.lineTo(left + width, yp);
    ctx.stroke();
  }
  ctx.restore();
}

export const TelemetryCharts = forwardRef<ChartsHandle, Props>(function TelemetryCharts(
  { data, theme, onHover },
  ref,
) {
  const wrap = useRef<HTMLDivElement>(null);
  const holders = useRef<(HTMLDivElement | null)[]>([]);
  const plots = useRef<uPlot[]>([]);
  const hoverRef = useRef(onHover);
  hoverRef.current = onHover;

  useImperativeHandle(
    ref,
    () => ({
      showIndex(i) {
        const x = data.trace.distance;
        for (const p of plots.current) {
          if (i === null) p.setCursor({ left: -10, top: -10 }, false);
          else p.setCursor({ left: p.valToPos(x[i], "x"), top: 8 }, false);
        }
      },
      zoom(min, max) {
        plots.current[0]?.setScale("x", { min, max });
      },
      resetZoom() {
        const x = data.trace.distance;
        plots.current[0]?.setScale("x", { min: x[0], max: x[x.length - 1] });
      },
    }),
    [data],
  );

  useEffect(() => {
    const colors = {
      lap: cssVar("--series-lap"),
      ref: cssVar("--series-ref"),
      ink: cssVar("--ink"),
      muted: cssVar("--muted"),
      grid: cssVar("--grid"),
      axis: cssVar("--axis"),
      fillGain: cssVar("--fill-gain"),
      fillLoss: cssVar("--fill-loss"),
    };
    const x = data.trace.distance;
    const width = Math.max(320, wrap.current?.clientWidth ?? 800);
    const syncKey = `pitwall-${Math.random().toString(36).slice(2)}`;
    const sectorStarts = data.session.sector_starts_m ?? [];
    const created: uPlot[] = [];
    let syncing = false;

    SPECS.forEach((spec, i) => {
      const holder = holders.current[i];
      if (!holder) return;
      const last = i === SPECS.length - 1;
      const series: uPlot.Series[] = [{ label: "Distance" }];
      const ys: Series[] = [];
      const stepped = spec.stepped ? uPlot.paths.stepped!({ align: 1 }) : undefined;
      if (spec.delta) {
        series.push({
          label: "Delta",
          stroke: colors.ink,
          width: 1.5,
          fill: (u) => deltaFill(u, colors.fillLoss, colors.fillGain),
          fillTo: 0,
          points: { show: false },
        });
        ys.push(spec.lap(data));
      } else {
        // Reference drawn first, so "this lap" stays visible where the two overlap.
        if (spec.ref) {
          series.push({ label: "Reference", stroke: colors.ref, width: 2, paths: stepped, points: { show: false } });
          ys.push(spec.ref(data));
        }
        series.push({ label: "This lap", stroke: colors.lap, width: 2, paths: stepped, points: { show: false } });
        ys.push(spec.lap(data));
      }
      const yScale: uPlot.Scale = spec.range
        ? { range: spec.range }
        : spec.delta
          ? {
              range: (_u, min, max) => {
                const lo = Math.min(min ?? 0, 0);
                const hi = Math.max(max ?? 0, 0);
                const pad = Math.max((hi - lo) * 0.1, 0.01);
                return [lo - pad, hi + pad];
              },
            }
          : {};
      const opts: uPlot.Options = {
        width,
        height: spec.height + (last ? X_AXIS_BAND : 0),
        series,
        scales: { x: { time: false }, y: yScale },
        legend: { show: false },
        cursor: {
          y: false, // a vertical hairline finds the distance; no horizontal rule
          sync: { key: syncKey },
          drag: { x: true, y: false },
          points: { size: 7 },
        },
        axes: [
          {
            stroke: colors.muted,
            grid: { stroke: colors.grid, width: 1 },
            ticks: { stroke: colors.grid, width: 1, size: last ? 4 : 0 },
            size: last ? X_AXIS_BAND : 2,
            font: "11px system-ui, -apple-system, sans-serif",
            values: last
              ? (_u, splits) => splits.map((v) => `${Math.round(v).toLocaleString()} m`)
              : (_u, splits) => splits.map(() => ""),
          },
          {
            stroke: colors.muted,
            grid: { stroke: colors.grid, width: 1 },
            ticks: { show: false },
            size: 48,
            font: "11px system-ui, -apple-system, sans-serif",
            values: (_u, splits) => splits.map((v) => spec.tick(v)),
          },
        ],
        hooks: {
          setCursor: [(u) => hoverRef.current(u.cursor.idx ?? null)],
          setScale: [
            (u, key) => {
              if (key !== "x" || syncing) return;
              syncing = true;
              const { min, max } = u.scales.x;
              for (const other of created) {
                if (other !== u && min !== undefined && max !== undefined) {
                  other.setScale("x", { min, max });
                }
              }
              syncing = false;
            },
          ],
          draw: [
            (u) =>
              drawAnnotations(u, sectorStarts, !!spec.delta, colors.axis, colors.muted, !!spec.delta),
          ],
        },
      };
      created.push(new uPlot(opts, [x, ...ys] as uPlot.AlignedData, holder));
    });
    plots.current = created;

    const observer = new ResizeObserver(() => {
      const w = wrap.current?.clientWidth;
      if (!w) return;
      created.forEach((p, i) =>
        p.setSize({ width: w, height: SPECS[i].height + (i === SPECS.length - 1 ? X_AXIS_BAND : 0) }),
      );
    });
    if (wrap.current) observer.observe(wrap.current);
    return () => {
      observer.disconnect();
      created.forEach((p) => p.destroy());
      plots.current = [];
    };
  }, [data, theme]);

  return (
    <div ref={wrap}>
      {SPECS.map((spec, i) => (
        <div className="chart" key={spec.key} aria-label={spec.title} role="img">
          <div className="chart-title">
            <span>{spec.title}</span>
          </div>
          <div ref={(el) => (holders.current[i] = el)} />
        </div>
      ))}
    </div>
  );
});
