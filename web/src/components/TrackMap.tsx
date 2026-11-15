import { useMemo, useRef } from "react";
import type { Series, SegmentDef, TraceView } from "../api";

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
  data: TraceView;
  hoverIndex: number | null;
  onHover: (i: number | null) => void;
  segments?: SegmentDef[];
  selected?: string | null;
  /** Another path in the same frame (e.g. a real F1 lap placed on your track), dashed. */
  overlay?: { x: Series; z: Series; label: string };
};

/** Short map label: "T1-T2" from "Variante del Rettifilo (T1-T2)", else "C3". */
export function shortLabel(seg: SegmentDef): string {
  const m = seg.name.match(/\(([^)]*T\d[^)]*)\)/);
  if (m) return m[1];
  return seg.name.length <= 4 ? seg.name : seg.id.toUpperCase();
}

export function TrackMap({ data, hoverIndex, onHover, segments = [], selected = null, overlay }: Props) {
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
    const nearest = (m: number) => {
      let best = pts[0];
      for (const p of pts) if (Math.abs(distance[p.i] - m) < Math.abs(distance[best.i] - m)) best = p;
      return best;
    };
    return {
      pts,
      index,
      segments,
      sectorMarks,
      nearest,
      viewBox: `${minX - pad} ${minY - pad} ${maxX - minX + 2 * pad} ${maxY - minY + 2 * pad}`,
      stroke: span * 0.014,
      font: span * 0.035,
    };
  }, [x, z, rate, distance, data.session.sector_starts_m]);

  const corners = useMemo(() => {
    if (!geo) return [];
    return segments
      .filter((s) => s.kind !== "straight")
      .map((s) => ({ seg: s, p: geo.nearest(s.apexes_m[0] ?? (s.start_m + s.end_m) / 2) }));
  }, [geo, segments]);

  const overlayPath = useMemo(() => {
    if (!overlay) return null;
    const parts: string[] = [];
    overlay.x.forEach((xv, i) => {
      const zv = overlay.z[i];
      if (xv !== null && zv !== null) parts.push(`${parts.length ? "L" : "M"}${xv.toFixed(1)} ${(-zv).toFixed(1)}`);
    });
    return parts.length > 1 ? parts.join(" ") : null;
  }, [overlay]);

  const highlight = useMemo(() => {
    if (!geo || !selected) return null;
    const seg = segments.find((s) => s.id === selected);
    if (!seg) return null;
    const pts = geo.pts.filter((p) => distance[p.i] >= seg.start_m && distance[p.i] <= seg.end_m);
    if (pts.length < 2) return null;
    return pts.map((p, k) => `${k ? "L" : "M"}${p.px.toFixed(1)} ${p.py.toFixed(1)}`).join(" ");
  }, [geo, segments, selected, distance]);

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
        {highlight && (
          <path
            d={highlight}
            fill="none"
            style={{ stroke: "var(--ink)", opacity: 0.35 }}
            strokeWidth={geo.stroke * 2.6}
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        )}
        {overlayPath && (
          <path
            d={overlayPath}
            fill="none"
            style={{ stroke: "var(--series-ref)" }}
            strokeWidth={geo.stroke * 0.45}
            strokeDasharray={`${geo.stroke * 1.6} ${geo.stroke * 1.1}`}
            strokeLinecap="round"
          />
        )}
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
        {corners.map(({ seg, p }) => (
          <text
            key={seg.id}
            x={p.px}
            y={p.py - geo.stroke * 1.8}
            fontSize={geo.font * (seg.id === selected ? 1.05 : 0.85)}
            fontWeight={seg.id === selected ? 700 : 500}
            textAnchor="middle"
            style={{ fill: seg.id === selected ? "var(--ink)" : "var(--ink-2)" }}
          >
            {shortLabel(seg)}
          </text>
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
        {overlay && <> Dashed orange line: {overlay.label}.</>}
      </div>
    </div>
  );
}
