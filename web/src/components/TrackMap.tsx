import { useMemo, useRef } from "react";
import type { Comparison } from "../api";

/** Track map from the lap's own world positions, coloured by where time is gained or lost
 *  against the reference (smoothed delta slope, seconds per 100 m). Diverging scale:
 *  blue = gaining, grey = about equal, red = losing. */

const THRESHOLDS = [0.005, 0.02, 0.06]; // s per 100 m
const COLORS: Record<number, string> = {
  [-3]: "var(--gain-3)",
  [-2]: "var(--gain-2)",
  [-1]: "var(--gain-1)",
  0: "var(--neutral)",
  1: "var(--loss-1)",
  2: "var(--loss-2)",
  3: "var(--loss-3)",
};

function bin(rate: number | null): number {
  if (rate === null) return 0;
  const a = Math.abs(rate);
  const level = a < THRESHOLDS[0] ? 0 : a < THRESHOLDS[1] ? 1 : a < THRESHOLDS[2] ? 2 : 3;
  return rate > 0 ? level : -level;
}

type Props = {
  data: Comparison;
  hoverIndex: number | null;
  onHover: (i: number | null) => void;
};

export function TrackMap({ data, hoverIndex, onHover }: Props) {
  const svgRef = useRef<SVGSVGElement>(null);
  const frame = useRef<number | null>(null);
  const { x, z, loss_rate: rate, distance } = data.trace;

  const geo = useMemo(() => {
    const pts: { i: number; px: number; py: number }[] = [];
    x.forEach((xv, i) => {
      const zv = z[i];
      if (xv !== null && zv !== null) pts.push({ i, px: xv, py: -zv });
    });
    if (pts.length < 2) return null;
    const xs = pts.map((p) => p.px);
    const ys = pts.map((p) => p.py);
    const minX = Math.min(...xs);
    const maxX = Math.max(...xs);
    const minY = Math.min(...ys);
    const maxY = Math.max(...ys);
    const span = Math.max(maxX - minX, maxY - minY) || 1;
    const pad = span * 0.06;
    const segments: { level: number; d: string }[] = [];
    let current = bin(rate[pts[0].i]);
    let d = `M${pts[0].px.toFixed(1)} ${pts[0].py.toFixed(1)}`;
    for (let k = 1; k < pts.length; k++) {
      const p = pts[k];
      const level = bin(rate[p.i]);
      d += ` L${p.px.toFixed(1)} ${p.py.toFixed(1)}`;
      if (level !== current) {
        segments.push({ level: current, d });
        current = level;
        d = `M${p.px.toFixed(1)} ${p.py.toFixed(1)}`;
      }
    }
    segments.push({ level: current, d });
    const index = new Map(pts.map((p) => [p.i, p]));
    const sectorMarks = (data.session.sector_starts_m ?? []).map((m, k) => {
      let best = pts[0];
      for (const p of pts) if (Math.abs(distance[p.i] - m) < Math.abs(distance[best.i] - m)) best = p;
      return { label: `S${k + 2}`, p: best };
    });
    return {
      pts,
      index,
      segments,
      sectorMarks,
      viewBox: `${minX - pad} ${minY - pad} ${maxX - minX + 2 * pad} ${maxY - minY + 2 * pad}`,
      stroke: span * 0.014,
      font: span * 0.035,
    };
  }, [x, z, rate, distance, data.session.sector_starts_m]);

  if (!geo) return <p className="muted">No position data for this lap.</p>;

  const onMove = (e: React.PointerEvent<SVGSVGElement>) => {
    const svg = svgRef.current;
    if (!svg) return;
    const ctm = svg.getScreenCTM();
    if (!ctm) return;
    const pt = new DOMPoint(e.clientX, e.clientY).matrixTransform(ctm.inverse());
    if (frame.current !== null) cancelAnimationFrame(frame.current);
    frame.current = requestAnimationFrame(() => {
      let best = geo.pts[0];
      let bestD = Infinity;
      for (const p of geo.pts) {
        const dd = (p.px - pt.x) ** 2 + (p.py - pt.y) ** 2;
        if (dd < bestD) {
          bestD = dd;
          best = p;
        }
      }
      onHover(best.i);
    });
  };

  const hover = hoverIndex !== null ? geo.index.get(hoverIndex) : undefined;
  const start = geo.pts[0];
  return (
    <div className="map">
      <svg
        ref={svgRef}
        viewBox={geo.viewBox}
        role="img"
        aria-label="Track map coloured by time gained or lost against the reference"
        onPointerMove={onMove}
        onPointerLeave={() => onHover(null)}
      >
        {geo.segments.map((s, k) => (
          <path
            key={k}
            d={s.d}
            fill="none"
            style={{ stroke: COLORS[s.level] }}
            strokeWidth={geo.stroke}
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        ))}
        <circle cx={start.px} cy={start.py} r={geo.stroke * 0.9} style={{ fill: "var(--ink)" }} />
        <text x={start.px + geo.stroke * 1.6} y={start.py - geo.stroke * 1.2} fontSize={geo.font} style={{ fill: "var(--ink-2)" }}>
          Start / finish
        </text>
        {geo.sectorMarks.map((m) => (
          <g key={m.label}>
            <circle cx={m.p.px} cy={m.p.py} r={geo.stroke * 0.7} style={{ fill: "var(--muted)" }} />
            <text x={m.p.px + geo.stroke * 1.4} y={m.p.py + geo.font * 0.35} fontSize={geo.font * 0.9} style={{ fill: "var(--muted)" }}>
              {m.label}
            </text>
          </g>
        ))}
        {hover && (
          <circle
            cx={hover.px}
            cy={hover.py}
            r={geo.stroke * 1.25}
            style={{ fill: "var(--series-lap)", stroke: "var(--surface)" }}
            strokeWidth={geo.stroke * 0.45}
          />
        )}
      </svg>
      <div className="map-legend" aria-label="Map colour scale">
        <span>Gaining</span>
        <span className="ramp">
          {[-3, -2, -1, 0, 1, 2, 3].map((l) => (
            <span key={l} style={{ background: COLORS[l] }} />
          ))}
        </span>
        <span>Losing</span>
      </div>
      <div className="note">
        Time vs reference per 100 m: grey under {THRESHOLDS[0]} s, then {THRESHOLDS[0]}–{THRESHOLDS[1]},{" "}
        {THRESHOLDS[1]}–{THRESHOLDS[2]}, over {THRESHOLDS[2]} s. Map from your lap's world positions.
      </div>
    </div>
  );
}
