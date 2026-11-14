import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, type Comparison, type Lap, type Session, type Window } from "../api";
import { MetricsTable } from "../components/MetricsTable";
import { TelemetryCharts, type ChartsHandle } from "../components/TelemetryCharts";
import { TrackMap } from "../components/TrackMap";
import { deltaClass, fmtDate, fmtDelta, fmtLap, fmtNum } from "../format";

const REFERENCES: { value: string; label: string }[] = [
  { value: "session_best", label: "Session best" },
  { value: "personal_best", label: "Personal best (this track)" },
  { value: "theoretical_best", label: "Theoretical best (best sectors)" },
  { value: "lap", label: "Another lap…" },
];

function lapLabel(l: Lap): string {
  const flags = [l.usable ? "" : l.valid === false ? "invalid" : l.issues[0] ?? "not usable"].filter(Boolean);
  return `Lap ${l.lap_number} · ${fmtLap(l.lap_time_ms)}${flags.length ? ` · ${flags.join(", ")}` : ""}`;
}

function pickDefaultLap(laps: Lap[]): Lap | undefined {
  const comparable = laps.filter((l) => l.comparable);
  if (comparable.length === 0) return undefined;
  const best = comparable
    .filter((l) => l.usable && l.lap_time_ms !== null)
    .sort((a, b) => a.lap_time_ms! - b.lap_time_ms!)[0];
  const newestFirst = [...comparable].reverse();
  return (
    newestFirst.find((l) => l.usable && l !== best) ??
    newestFirst.find((l) => l !== best) ??
    comparable[comparable.length - 1]
  );
}

export function LapAnalysis({ theme }: { theme: string }) {
  const [sessions, setSessions] = useState<Session[] | null>(null);
  const [sessionKey, setSessionKey] = useState<string>();
  const [laps, setLaps] = useState<Lap[]>([]);
  const [lapIndex, setLapIndex] = useState<number>();
  const [refKind, setRefKind] = useState("session_best");
  const [refLap, setRefLap] = useState<number>();
  const [data, setData] = useState<Comparison | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [hover, setHover] = useState<number | null>(null);
  const [zoomed, setZoomed] = useState(false);
  const charts = useRef<ChartsHandle>(null);

  useEffect(() => {
    api
      .sessions()
      .then((s) => {
        setSessions(s);
        if (s.length) setSessionKey(s[0].session_key);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  useEffect(() => {
    if (!sessionKey) return;
    api
      .laps(sessionKey)
      .then((r) => {
        setLaps(r.laps);
        setLapIndex(pickDefaultLap(r.laps)?.lap_index);
        const usable = r.laps.filter((l) => l.usable && l.comparable && l.lap_time_ms !== null);
        usable.sort((a, b) => a.lap_time_ms! - b.lap_time_ms!);
        setRefLap(usable[0]?.lap_index);
      })
      .catch((e: Error) => setError(e.message));
  }, [sessionKey]);

  useEffect(() => {
    if (!sessionKey || lapIndex === undefined) return;
    if (refKind === "lap" && refLap === undefined) return;
    let cancelled = false;
    setLoading(true);
    api
      .compare({
        session: sessionKey,
        lap: lapIndex,
        ref: refKind,
        ...(refKind === "lap" ? { ref_session: sessionKey, ref_lap: refLap } : {}),
      })
      .then((c) => {
        if (cancelled) return;
        setData(c);
        setError(null);
        setZoomed(false);
      })
      .catch((e: Error) => {
        if (cancelled) return;
        setError(e instanceof ApiError && e.status === 422 ? `Cannot compare: ${e.message}` : e.message);
      })
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [sessionKey, lapIndex, refKind, refLap]);

  const hoverFromMap = useCallback((i: number | null) => {
    setHover(i);
    charts.current?.showIndex(i);
  }, []);

  const zoomTo = (w: Window) => {
    const pad = 150;
    charts.current?.zoom(Math.max(0, w.start_m - pad), w.end_m + pad);
    setZoomed(true);
  };

  if (sessions && sessions.length === 0) {
    return (
      <div className="placeholder">
        <div className="card">
          <h1>No sessions yet</h1>
          <p className="muted">
            Record with <code>uv run pitwall capture run</code>, then <code>uv run pitwall ingest</code>. To explore
            first, run <code>uv run pitwall demo</code> for a clearly labelled synthetic session.
          </p>
        </div>
      </div>
    );
  }

  const session = sessions?.find((s) => s.session_key === sessionKey);
  const comparable = laps.filter((l) => l.comparable);
  return (
    <>
      <div className="filters" role="group" aria-label="Lap selection">
        <div className="field">
          <label htmlFor="session">Session</label>
          <select id="session" value={sessionKey ?? ""} onChange={(e) => setSessionKey(e.target.value)}>
            {(sessions ?? []).map((s) => (
              <option key={s.session_key} value={s.session_key}>
                {s.track_name} · {s.session_type_name} · {fmtDate(s.first_utc)}
                {s.synthetic ? " · synthetic" : ""}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="lap">Lap to analyse</label>
          <select id="lap" value={lapIndex ?? ""} onChange={(e) => setLapIndex(Number(e.target.value))}>
            {comparable.map((l) => (
              <option key={l.lap_index} value={l.lap_index}>
                {lapLabel(l)}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="ref">Compare with</label>
          <select id="ref" value={refKind} onChange={(e) => setRefKind(e.target.value)}>
            {REFERENCES.map((r) => (
              <option key={r.value} value={r.value}>
                {r.label}
              </option>
            ))}
          </select>
        </div>
        {refKind === "lap" && (
          <div className="field">
            <label htmlFor="reflap">Reference lap</label>
            <select id="reflap" value={refLap ?? ""} onChange={(e) => setRefLap(Number(e.target.value))}>
              {comparable.map((l) => (
                <option key={l.lap_index} value={l.lap_index}>
                  {lapLabel(l)}
                </option>
              ))}
            </select>
          </div>
        )}
      </div>

      {session?.synthetic && (
        <div className="banner" role="note">
          Synthetic demo data, generated by <code>pitwall demo</code>. Not real telemetry, and never mixed with your
          own laps.
        </div>
      )}
      {error && (
        <div className="error" role="alert">
          {error}
        </div>
      )}
      {laps.length > 0 && comparable.length === 0 && (
        <div className="error">This session has no complete flying lap to analyse (out-laps and incomplete laps are excluded).</div>
      )}

      {data && (
        <div className={loading ? "stale" : undefined}>
          <Summary data={data} />
          <div className="analysis">
            <section className="card" aria-label="Telemetry">
              <div className="charts-head">
                <div className="legend">
                  <span>
                    <span className="swatch-line" style={{ background: "var(--series-lap)" }} />
                    This lap (lap {data.lap.lap_number})
                  </span>
                  <span>
                    <span className="swatch-line" style={{ background: "var(--series-ref)" }} />
                    Reference: {data.reference.label}
                  </span>
                </div>
                <div className="toolbar">
                  <span>Drag across a chart to zoom</span>
                  {zoomed && (
                    <button
                      className="link-btn"
                      onClick={() => {
                        charts.current?.resetZoom();
                        setZoomed(false);
                      }}
                    >
                      Reset zoom
                    </button>
                  )}
                </div>
              </div>
              <Readout data={data} index={hover} />
              <TelemetryCharts ref={charts} data={data} theme={theme} onHover={setHover} />
            </section>
            <aside className="side">
              <section className="card" aria-label="Track map">
                <h2>Where time is gained and lost</h2>
                <TrackMap data={data} hoverIndex={hover} onHover={hoverFromMap} />
              </section>
              <section className="card" aria-label="Largest changes">
                <h2>Biggest 200 m stretches</h2>
                <WindowList title="Losing" rows={data.largest_losses} onZoom={zoomTo} />
                <WindowList title="Gaining" rows={data.largest_gains} onZoom={zoomTo} />
              </section>
              <section className="card" aria-label="Lap metrics">
                <h2>Lap metrics</h2>
                <MetricsTable lap={data.lap.metrics} reference={data.reference.metrics} definitions={data.metric_definitions} />
              </section>
            </aside>
          </div>
        </div>
      )}
    </>
  );
}

function Summary({ data }: { data: Comparison }) {
  return (
    <div className="summary">
      <div className="card tile">
        <div className="label">
          <span className="swatch-line" style={{ background: "var(--series-lap)" }} />
          This lap · lap {data.lap.lap_number}
        </div>
        <div className="value">{fmtLap(data.lap.lap_time_ms)}</div>
        <div className="sub">{data.lap.usable ? "usable lap" : data.lap.issues.join("; ") || "not usable"}</div>
      </div>
      <div className="card tile">
        <div className="label">
          <span className="swatch-line" style={{ background: "var(--series-ref)" }} />
          Reference
        </div>
        <div className="value">{fmtLap(data.reference.lap_time_ms)}</div>
        <div className="sub">{data.reference.label}</div>
      </div>
      <div className="card tile">
        <div className="label">Delta at the line</div>
        <div className={`value ${deltaClass(data.delta_s)}`}>{fmtDelta(data.delta_s)} s</div>
        <div className="sub">{data.delta_s > 0 ? "slower than the reference" : data.delta_s < 0 ? "faster than the reference" : "identical"}</div>
      </div>
      {data.sectors.map((s) => (
        <div className="card tile" key={s.sector}>
          <div className="label">Sector {s.sector}</div>
          <div className={`value ${deltaClass(s.delta_s)}`}>{fmtDelta(s.delta_s)} s</div>
          <div className="sub">
            {fmtLap(s.lap_ms)} vs {fmtLap(s.ref_ms)}
            {s.delta_source === "trace" ? " · from traces" : ""}
          </div>
        </div>
      ))}
    </div>
  );
}

function Readout({ data, index }: { data: Comparison; index: number | null }) {
  if (index === null) {
    return <div className="readout muted">Hover a chart or the map to read values at any point of the lap.</div>;
  }
  const t = data.trace;
  const pair = (key: string, digits: number, scale = 1) => {
    const a = t.lap[key]?.[index];
    const b = t.ref[key]?.[index];
    const f = (v: number | null | undefined) => (v === null || v === undefined ? "–" : fmtNum(v * scale, digits));
    return `${f(a)} / ${f(b)}`;
  };
  const delta = t.delta[index];
  return (
    <div className="readout" aria-live="off">
      <strong>{fmtNum(t.distance[index], 0)} m</strong> · Δ{" "}
      <span className={deltaClass(delta)}>{fmtDelta(delta)} s</span> · speed {pair("speed", 0)} km/h · throttle{" "}
      {pair("throttle", 0, 100)}% · brake {pair("brake", 0, 100)}% · gear {pair("gear", 0)} · steer {pair("steer", 2)}
      <span className="muted"> (this lap / reference)</span>
    </div>
  );
}

function WindowList(props: { title: string; rows: Window[]; onZoom: (w: Window) => void }) {
  return (
    <div style={{ marginTop: 10 }}>
      <div className="muted" style={{ fontSize: 12 }}>
        {props.title}
      </div>
      {props.rows.length === 0 ? (
        <div className="note">Nothing above 0.010 s.</div>
      ) : (
        <ol className="windows">
          {props.rows.map((w) => (
            <li key={`${w.start_m}-${w.end_m}`}>
              <span className={deltaClass(w.seconds)}>{fmtDelta(w.seconds)} s</span> between{" "}
              {fmtNum(w.start_m, 0)} and {fmtNum(w.end_m, 0)} m{" "}
              <button className="link-btn" onClick={() => props.onZoom(w)}>
                zoom
              </button>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
