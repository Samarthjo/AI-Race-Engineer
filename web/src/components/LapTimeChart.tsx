import { useEffect, useMemo, useRef, useState } from "react";
import type { HistorySession } from "../api";
import { fmtDate, fmtLap } from "../format";

/** Lap times per session (one hue, marks carry identity): every usable lap as a dot, the
 *  session median as a line, the session best ringed, your best overall as a dashed line.
 *  Lower is faster. Laps far off the scale (a spin, a big mistake) are drawn at the top edge
 *  as a triangle so they do not squash everything else; the tooltip gives the real time. */

const HEIGHT = 260;
const M = { top: 14, right: 18, bottom: 38, left: 64 };

type Hover = { x: number; y: number; lines: string[] };

function quantile(sorted: number[], q: number): number {
  if (!sorted.length) return NaN;
  const pos = (sorted.length - 1) * q;
  const lo = Math.floor(pos);
  const hi = Math.ceil(pos);
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo);
}

function niceStep(span: number): number {
  const steps = [0.05, 0.1, 0.2, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60];
  return steps.find((s) => span / s <= 6) ?? 60;
}

function fmtTick(v: number, step: number): string {
  const digits = step < 0.1 ? 2 : step < 1 ? 1 : 0;
  const minutes = Math.floor(v / 60);
  const seconds = v - minutes * 60;
  const sec = seconds.toFixed(digits).padStart(digits ? 3 + digits : 2, "0");
  return minutes > 0 ? `${minutes}:${sec}` : seconds.toFixed(digits);
}

function shortDate(utc: string | null): string {
  if (!utc) return "";
  const d = new Date(utc);
  return Number.isNaN(d.getTime()) ? utc.slice(0, 10) : d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

export function LapTimeChart({ sessions }: { sessions: HistorySession[] }) {
  const box = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(640);
  const [hover, setHover] = useState<Hover | null>(null);

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => setWidth(Math.max(320, Math.floor(entry.contentRect.width))));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const geo = useMemo(() => {
    const all = sessions.flatMap((s) => s.lap_times_s).sort((a, b) => a - b);
    const min = all[0];
    const q = quantile(all, 0.85);
    const cap = Math.min(all[all.length - 1], q + Math.max(0.5, q - min));
    const pad = Math.max(0.05, (cap - min) * 0.08);
    const lo = min - pad;
    const hi = cap + pad;
    const step = niceStep(hi - lo);
    const ticks: number[] = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) ticks.push(Number(v.toFixed(3)));
    const innerW = width - M.left - M.right;
    const innerH = HEIGHT - M.top - M.bottom;
    const band = innerW / Math.max(1, sessions.length);
    const y = (v: number) => M.top + innerH * (1 - (Math.min(v, hi) - lo) / (hi - lo));
    const xc = (i: number) => M.left + band * (i + 0.5);
    const labelEvery = Math.max(1, Math.ceil(56 / band));
    return { min, cap, lo, hi, ticks, step, innerW, innerH, band, y, xc, labelEvery };
  }, [sessions, width]);

  if (!sessions.length) return null;
  const { min, cap, ticks, step, innerW, band, y, xc, labelEvery } = geo;
  const best = sessions.map((s) => Math.min(...s.lap_times_s));
  const median = sessions.map((s) => s.median_s);
  const medianPath = median.map((v, i) => `${i ? "L" : "M"}${xc(i).toFixed(1)},${y(v).toFixed(1)}`).join("");

  const dots = sessions.flatMap((s, i) => {
    const n = s.lap_times_s.length;
    const spread = Math.min(7, (band * 0.55) / Math.max(1, n));
    const bestIdx = s.lap_times_s.indexOf(best[i]);
    return s.lap_times_s.map((t, k) => ({
      key: `${s.session_key}-${k}`,
      x: xc(i) + (k - (n - 1) / 2) * spread,
      y: y(t),
      t,
      off: t > cap + 1e-9,
      best: k === bestIdx,
      session: s,
      lap: k + 1,
    }));
  });

  const show = (x: number, yy: number, lines: string[]) => setHover({ x, y: yy, lines });

  return (
    <div className="laptime-chart" ref={box}>
      <div className="legend" aria-hidden>
        <span>
          <svg width="14" height="10">
            <circle cx="7" cy="5" r="4" className="lt-dot" />
          </svg>
          each lap
        </span>
        <span>
          <svg width="22" height="10">
            <line x1="1" y1="5" x2="21" y2="5" className="lt-median" />
            <circle cx="11" cy="5" r="4" className="lt-median-dot" />
          </svg>
          session median
        </span>
        <span>
          <svg width="14" height="12">
            <circle cx="7" cy="6" r="5" className="lt-best" />
          </svg>
          session best
        </span>
        <span>
          <svg width="22" height="10">
            <line x1="1" y1="5" x2="21" y2="5" className="lt-pb" />
          </svg>
          your best
        </span>
      </div>
      <svg
        width={width}
        height={HEIGHT}
        role="img"
        aria-label={`Lap times over ${sessions.length} sessions; best ${fmtLap(Math.round(min * 1000))}. The sessions table below has the same numbers.`}
        onMouseLeave={() => setHover(null)}
      >
        {ticks.map((t) => (
          <g key={t}>
            <line x1={M.left} x2={M.left + innerW} y1={y(t)} y2={y(t)} className="lt-grid" />
            <text x={M.left - 8} y={y(t)} className="lt-axis" textAnchor="end" dominantBaseline="middle">
              {fmtTick(t, step)}
            </text>
          </g>
        ))}
        <line x1={M.left} x2={M.left + innerW} y1={y(min)} y2={y(min)} className="lt-pb" />
        <text x={M.left + innerW} y={y(min) - 6} className="lt-axis" textAnchor="end">
          your best {fmtLap(Math.round(min * 1000))}
        </text>
        {sessions.map((s, i) =>
          i % labelEvery === 0 || i === sessions.length - 1 ? (
            <text key={s.session_key} x={xc(i)} y={HEIGHT - M.bottom + 18} className="lt-axis" textAnchor="middle">
              {shortDate(s.date)}
            </text>
          ) : null,
        )}
        <text x={M.left} y={HEIGHT - 4} className="lt-axis">
          lower is faster · one column per session
        </text>
        {sessions.length > 1 && <path d={medianPath} className="lt-median" />}
        {dots.map((d) =>
          d.off ? (
            <path
              key={d.key}
              d={`M${d.x - 4},${d.y + 4}L${d.x + 4},${d.y + 4}L${d.x},${d.y - 3}Z`}
              className="lt-off"
            />
          ) : (
            <circle key={d.key} cx={d.x} cy={d.y} r={4} className="lt-dot" />
          ),
        )}
        {dots
          .filter((d) => d.best)
          .map((d) => (
            <circle key={`b-${d.key}`} cx={d.x} cy={d.y} r={6.5} className="lt-best" />
          ))}
        {median.map((v, i) => (
          <circle key={`m-${i}`} cx={xc(i)} cy={y(v)} r={4} className="lt-median-dot" />
        ))}
        {/* hit targets, larger than the marks */}
        {dots.map((d) => (
          <circle
            key={`h-${d.key}`}
            cx={d.x}
            cy={d.y}
            r={8}
            className="lt-hit"
            onMouseEnter={() =>
              show(d.x, d.y, [
                fmtDate(d.session.date),
                `${d.best ? "Session best · " : ""}${fmtLap(Math.round(d.t * 1000))}${d.off ? " (off the scale)" : ""}`,
              ])
            }
          />
        ))}
        {median.map((v, i) => (
          <rect
            key={`hm-${i}`}
            x={xc(i) - 6}
            y={y(v) - 6}
            width={12}
            height={12}
            className="lt-hit"
            onMouseEnter={() =>
              show(xc(i), y(v), [
                fmtDate(sessions[i].date),
                `Median ${fmtLap(Math.round(v * 1000))} over ${sessions[i].laps} laps`,
              ])
            }
          />
        ))}
      </svg>
      {hover && (
        <div
          className="lt-tip"
          style={{
            left: Math.min(Math.max(hover.x, 90), width - 90),
            top: Math.max(hover.y - 12, 0),
          }}
          role="status"
        >
          {hover.lines.map((l) => (
            <div key={l}>{l}</div>
          ))}
        </div>
      )}
    </div>
  );
}

/** A small single-series line (best lap per session); like the big chart, faster is lower. */
export function Sparkline({ values, label }: { values: number[]; label: string }) {
  const w = 132;
  const h = 34;
  if (values.length < 2) return <span className="muted small">one session so far</span>;
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const span = hi - lo || 1;
  const x = (i: number) => 4 + ((w - 8) * i) / (values.length - 1);
  const y = (v: number) => 4 + (h - 8) * (1 - (v - lo) / span);
  const d = values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
  const last = values.length - 1;
  return (
    <svg width={w} height={h} role="img" aria-label={label} className="sparkline">
      <path d={d} className="lt-median" />
      <circle cx={x(last)} cy={y(values[last])} r={4} className="lt-median-dot" />
    </svg>
  );
}
