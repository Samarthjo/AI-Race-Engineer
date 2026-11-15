import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  ApiError,
  type BenchmarkData,
  type Finding,
  type Lap,
  type RefDriver,
  type RefOptions,
  type RefSummary,
  type SegmentBench,
  type Session,
} from "../api";
import { BenchmarkTable } from "../components/BenchmarkTable";
import { Findings } from "../components/Findings";
import { TelemetryCharts, type ChartsHandle } from "../components/TelemetryCharts";
import { TrackMap } from "../components/TrackMap";
import { deltaClass, fmtDate, fmtDelta, fmtLap, fmtNum } from "../format";

function refLabel(r: RefSummary): string {
  const part = r.session_part ? ` ${r.session_part}` : "";
  return `${r.driver} · ${r.session}${part} · lap ${r.lap_number} · ${fmtLap(r.lap_time_ms)}${r.synthetic ? " · synthetic" : ""}`;
}

function bestLap(laps: Lap[]): Lap | undefined {
  const usable = laps.filter((l) => l.comparable && l.usable && l.lap_time_ms !== null);
  usable.sort((a, b) => a.lap_time_ms! - b.lap_time_ms!);
  return usable[0] ?? laps.find((l) => l.comparable);
}

export function Benchmark({ theme }: { theme: string }) {
  const [sessions, setSessions] = useState<Session[] | null>(null);
  const [sessionKey, setSessionKey] = useState<string>();
  const [laps, setLaps] = useState<Lap[]>([]);
  const [lapIndex, setLapIndex] = useState<number>();
  const [options, setOptions] = useState<RefOptions | null>(null);
  const [refKey, setRefKey] = useState<string>();
  const [data, setData] = useState<BenchmarkData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [hover, setHover] = useState<number | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [zoomed, setZoomed] = useState(false);
  const [adding, setAdding] = useState(false);
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

  const loadOptions = useCallback((key: string, select?: string) => {
    api
      .references(key)
      .then((o) => {
        setOptions(o);
        const keep = select ?? o.stored[0]?.key;
        setRefKey(keep);
        if (o.stored.length === 0) setAdding(true);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  useEffect(() => {
    if (!sessionKey) return;
    setData(null);
    setOptions(null);
    setRefKey(undefined);
    api
      .laps(sessionKey)
      .then((r) => {
        setLaps(r.laps);
        setLapIndex(bestLap(r.laps)?.lap_index);
      })
      .catch((e: Error) => setError(e.message));
    loadOptions(sessionKey);
  }, [sessionKey, loadOptions]);

  useEffect(() => {
    if (!sessionKey || lapIndex === undefined || !refKey) return;
    let cancelled = false;
    setLoading(true);
    api
      .benchmark({ session: sessionKey, lap: lapIndex, ref: refKey })
      .then((b) => {
        if (cancelled) return;
        setData(b);
        setError(null);
        setSelected(null);
        setZoomed(false);
      })
      .catch((e: Error) => {
        if (cancelled) return;
        setData(null);
        setError(e instanceof ApiError && e.status === 422 ? `Cannot benchmark: ${e.message}` : e.message);
      })
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [sessionKey, lapIndex, refKey]);

  const zoomRange = (start: number, end: number) => {
    charts.current?.zoom(Math.max(0, start), end);
    setZoomed(true);
    document.getElementById("bench-telemetry")?.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  const selectSegment = (r: SegmentBench) => {
    setSelected(r.segment.id);
    zoomRange(r.segment.start_m - 60, r.segment.end_m + 80);
  };
  const showFinding = (f: Finding) => {
    setSelected(f.segment_id);
    zoomRange(f.evidence.start_m - 100, f.evidence.end_m + 100);
  };
  const hoverFromMap = useCallback((i: number | null) => {
    setHover(i);
    charts.current?.showIndex(i);
  }, []);

  if (sessions && sessions.length === 0) {
    return (
      <div className="placeholder">
        <div className="card">
          <h1>No sessions yet</h1>
          <p className="muted">
            Record and ingest a session first. To explore, <code>uv run pitwall demo</code> creates a synthetic session
            with synthetic reference laps.
          </p>
        </div>
      </div>
    );
  }

  const session = sessions?.find((s) => s.session_key === sessionKey);
  const comparable = laps.filter((l) => l.comparable);
  return (
    <>
      <div className="filters" role="group" aria-label="Benchmark selection">
        <div className="field">
          <label htmlFor="b-session">Session</label>
          <select id="b-session" value={sessionKey ?? ""} onChange={(e) => setSessionKey(e.target.value)}>
            {(sessions ?? []).map((s) => (
              <option key={s.session_key} value={s.session_key}>
                {s.track_name} · {s.session_type_name} · {fmtDate(s.first_utc)}
                {s.synthetic ? " · synthetic" : ""}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="b-lap">Your lap</label>
          <select id="b-lap" value={lapIndex ?? ""} onChange={(e) => setLapIndex(Number(e.target.value))}>
            {comparable.map((l) => (
              <option key={l.lap_index} value={l.lap_index}>
                Lap {l.lap_number} · {fmtLap(l.lap_time_ms)}
                {l.usable ? "" : " · not usable"}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="b-ref">Real F1 reference{options?.event ? ` · ${options.season} ${options.event}` : ""}</label>
          <select
            id="b-ref"
            value={refKey ?? ""}
            onChange={(e) => setRefKey(e.target.value || undefined)}
            disabled={!options?.stored.length}
          >
            {!options?.stored.length && <option value="">none stored yet</option>}
            {(options?.stored ?? []).map((r) => (
              <option key={r.key} value={r.key}>
                {refLabel(r)}
              </option>
            ))}
          </select>
        </div>
        {options?.event && (
          <div className="field">
            <label>&nbsp;</label>
            <button className="btn" onClick={() => setAdding((a) => !a)} aria-expanded={adding}>
              {adding ? "Close" : "Add a reference…"}
            </button>
          </div>
        )}
      </div>

      {session?.synthetic && (
        <div className="banner" role="note">
          Synthetic demo data: both your laps and the "real F1" reference laps (drivers PRO and FST) are generated by{" "}
          <code>pitwall demo</code>. Not real telemetry.
        </div>
      )}
      {options?.error && !options.event && (
        <div className="error" role="alert">
          {options.error}
        </div>
      )}
      {adding && sessionKey && options?.event && (
        <AddReference
          sessionKey={sessionKey}
          options={options}
          onAdded={(key) => {
            setAdding(false);
            loadOptions(sessionKey, key);
          }}
        />
      )}
      {error && (
        <div className="error" role="alert">
          {error}
        </div>
      )}
      {data?.registration.quality === "check" && (
        <div className="banner" role="note">
          The reference was placed on your track, but check it on the map: {data.registration.notes.join("; ")}.
        </div>
      )}

      {data && (
        <div className={loading ? "stale" : undefined}>
          <Summary data={data} />
          <div className="analysis">
            <div className="main-col">
              <section className="card" aria-label="Telemetry" id="bench-telemetry">
                <div className="charts-head">
                  <div className="legend">
                    <span>
                      <span className="swatch-line" style={{ background: "var(--series-lap)" }} />
                      Your lap {data.lap.lap_number}
                    </span>
                    <span>
                      <span className="swatch-line" style={{ background: "var(--series-ref)" }} />
                      {data.reference.driver} (real F1 data, about every {fmtNum(data.reference.capabilities.car_interval_s, 2)} s;
                      brake on/off; no steering)
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
                <TelemetryCharts ref={charts} data={data} theme={theme} onHover={setHover} />
              </section>
              <section className="card" aria-label="Corners against the reference">
                <h2>Corner by corner against {data.reference.driver}</h2>
                <Systematic data={data} />
                <BenchmarkTable
                  segments={data.segments}
                  onSelect={selectSegment}
                  selected={selected}
                  driver={data.reference.driver}
                />
              </section>
            </div>
            <aside className="side">
              <section className="card" aria-label="Findings">
                <h2>Findings vs {data.reference.driver}</h2>
                <Findings
                  findings={data.findings}
                  onShow={showFinding}
                  selected={selected}
                  emptyNote={`No corner differs from your systematic offset to ${data.reference.driver} by more than its threshold and its uncertainty.`}
                  footNote="Against real F1 data, after removing the systematic difference between the game car and the real car (median over the lap). Directional: the reference has about 4 samples a second and an on/off brake."
                />
                {data.strengths.length > 0 && (
                  <p className="note">
                    Better than your norm against {data.reference.driver}:{" "}
                    {data.strengths.map((s) => `${s.segment_name} (${fmtDelta(s.normalised_s)} s)`).join(", ")}.
                  </p>
                )}
              </section>
              <section className="card" aria-label="Track map">
                <h2>Where time goes vs {data.reference.driver}</h2>
                <TrackMap
                  data={data}
                  hoverIndex={hover}
                  onHover={hoverFromMap}
                  segments={data.track_model.segments}
                  selected={selected}
                  overlay={{ x: data.trace.ref_x, z: data.trace.ref_z, label: `${data.reference.driver}'s positions placed on your track` }}
                />
              </section>
              <section className="card" aria-label="Sectors">
                <h2>Sectors</h2>
                <SectorTable data={data} />
              </section>
              <section className="card" aria-label="Reference data">
                <h2>About the reference data</h2>
                <DataNotes data={data} />
              </section>
            </aside>
          </div>
        </div>
      )}
    </>
  );
}

function AddReference(props: { sessionKey: string; options: RefOptions; onAdded: (key: string) => void }) {
  const { sessionKey, options, onAdded } = props;
  const sessionsList = options.sessions?.length ? options.sessions : ["Qualifying"];
  const [refSession, setRefSession] = useState(sessionsList.includes("Qualifying") ? "Qualifying" : sessionsList[0]);
  const [drivers, setDrivers] = useState<RefDriver[] | null>(null);
  const [driver, setDriver] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setDrivers(null);
    setError(null);
    api
      .referenceDrivers(sessionKey, refSession)
      .then((r) => {
        if (cancelled) return;
        setDrivers(r.drivers);
        setDriver(r.drivers[0]?.driver);
      })
      .catch((e: Error) => !cancelled && setError(e.message));
    return () => {
      cancelled = true;
    };
  }, [sessionKey, refSession]);

  const fetchIt = async () => {
    if (!driver) return;
    setBusy(true);
    setError(null);
    try {
      const r = await api.fetchReference({ session: sessionKey, driver, ref_session: refSession });
      onAdded(r.key);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const best = drivers?.[0]?.lap_time_ms ?? null;
  return (
    <section className="card add-ref" aria-label="Add a reference lap">
      <h2>
        Add a reference: {options.season} {options.event}
      </h2>
      <div className="filters">
        <div className="field">
          <label htmlFor="r-session">Session</label>
          <select id="r-session" value={refSession} onChange={(e) => setRefSession(e.target.value)}>
            {sessionsList.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="r-driver">Driver · fastest valid lap</label>
          <select id="r-driver" value={driver ?? ""} onChange={(e) => setDriver(e.target.value)} disabled={!drivers?.length}>
            {!drivers && <option value="">loading…</option>}
            {(drivers ?? []).map((d) => (
              <option key={d.driver} value={d.driver}>
                {d.driver} · {fmtLap(d.lap_time_ms)}
                {best !== null && d.lap_time_ms !== null && d.lap_time_ms > best ? ` (+${((d.lap_time_ms - best) / 1000).toFixed(3)})` : ""}
                {d.session_part ? ` · ${d.session_part}` : ""}
                {d.team ? ` · ${d.team}` : ""}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label>&nbsp;</label>
          <button className="btn" onClick={fetchIt} disabled={!driver || busy}>
            {busy ? "Downloading…" : "Download and store"}
          </button>
        </div>
      </div>
      {error && (
        <div className="error" role="alert">
          {error}
        </div>
      )}
      <p className="note">
        Valid laps only: not deleted, timing accurate, timed and not an out or in lap. The files are downloaded once
        (TracingInsights archive of FastF1 data), checked against the official lap time and kept on this machine.
      </p>
    </section>
  );
}

function Summary({ data }: { data: BenchmarkData }) {
  const r = data.reference;
  const reg = data.registration;
  const t = data.systematic.time;
  return (
    <div className="summary">
      <div className="card tile">
        <div className="label">
          <span className="swatch-line" style={{ background: "var(--series-lap)" }} />
          Your lap {data.lap.lap_number}
        </div>
        <div className="value">{fmtLap(data.lap.lap_time_ms)}</div>
        <div className="sub">{data.session.track_name}</div>
      </div>
      <div className="card tile">
        <div className="label">
          <span className="swatch-line" style={{ background: "var(--series-ref)" }} />
          {r.driver_name ?? r.driver}
        </div>
        <div className="value">{fmtLap(r.lap_time_ms)}</div>
        <div className="sub">
          {r.season} {r.event} · {r.session}
          {r.session_part ? ` ${r.session_part}` : ""} · lap {r.lap_number}
          {r.team ? ` · ${r.team}` : ""}
        </div>
      </div>
      <div className="card tile">
        <div className="label">Delta at the line</div>
        <div className={`value ${deltaClass(data.delta_s)}`}>{fmtDelta(data.delta_s)} s</div>
        <div className="sub">{data.delta_s > 0 ? "slower than the reference" : "faster than the reference"}</div>
      </div>
      <div className="card tile">
        <div className="label">Systematic pace difference</div>
        <div className="value">
          {t.braking_corners?.pace_pct !== undefined ? `${fmtDelta(t.braking_corners.pace_pct, 1)} %` : "–"}
        </div>
        <div className="sub">
          braking corners; straights {t.straights?.pace_pct !== undefined ? `${fmtDelta(t.straights.pace_pct, 1)} %` : "–"}
        </div>
      </div>
      <div className="card tile">
        <div className="label">Placed on your track</div>
        <div className="value">{reg.quality === "good" ? "good" : "check"}</div>
        <div className="sub">
          {fmtNum(reg.median_m, 1)} m from your line (median) · start line {fmtNum(reg.start_offset_m, 1)} m
        </div>
      </div>
    </div>
  );
}

function Systematic({ data }: { data: BenchmarkData }) {
  const m = data.systematic.metrics;
  const parts: string[] = [];
  const bp = m.brake_onset_m?.offset;
  if (bp !== null && bp !== undefined) parts.push(`you brake ${fmtNum(Math.abs(bp), 0)} m ${bp < 0 ? "earlier" : "later"}`);
  const ms = m.min_speed_kmh?.offset;
  if (ms !== null && ms !== undefined) parts.push(`minimum speeds ${fmtDelta(ms, 1)} %`);
  const ft = m.full_throttle_m?.offset;
  if (ft !== null && ft !== undefined) parts.push(`full throttle ${fmtNum(Math.abs(ft), 0)} m ${ft < 0 ? "earlier" : "later"}`);
  return (
    <p className="note">
      The game car is not the real car. Across the lap (median over corners){parts.length ? `: ${parts.join(", ")}` : ""}.
      That part is treated as systematic; the <strong>normalised</strong> columns show what is left, corner by corner.
    </p>
  );
}

function SectorTable({ data }: { data: BenchmarkData }) {
  const ref = data.reference.sectors_on_your_lines_s;
  return (
    <>
      <table className="data">
        <thead>
          <tr>
            <th>Sector</th>
            <th className="num">You</th>
            <th className="num">{data.reference.driver}</th>
            <th className="num">Δ</th>
          </tr>
        </thead>
        <tbody>
          {data.sectors.map((s, i) => (
            <tr key={s.sector}>
              <td>S{s.sector}</td>
              <td className="num">{fmtLap(s.lap_ms)}</td>
              <td className="num">{ref ? ref[i].toFixed(3) : "–"}</td>
              <td className={`num ${deltaClass(s.delta_s)}`}>{fmtDelta(s.delta_s)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="note">
        On your game's sector lines (the reference's times there come from its trace). Its official sectors, on the
        real lines: {data.reference.official_sectors_ms.map((v) => fmtLap(v)).join(" · ")}.
      </p>
    </>
  );
}

function DataNotes({ data }: { data: BenchmarkData }) {
  const c = data.reference.capabilities;
  return (
    <>
      <p className="note">
        {c.car_samples} car samples, a median {fmtNum(c.car_interval_s, 2)} s apart (longest gap{" "}
        {fmtNum(c.car_max_gap_s, 2)} s); {c.pos_samples} positions. Brake is on/off. Values from the reference are
        estimated between its samples; their uncertainty is measured by recording your own lap the same way.
      </p>
      <p className="note">Not compared, because the reference data cannot show it:</p>
      <ul className="note plain">
        {data.not_assessed.map((n) => (
          <li key={n}>{n}</li>
        ))}
      </ul>
      {data.reference.sources.length > 0 && (
        <p className="note">
          Source:{" "}
          {data.reference.sources.map((s, i) => (
            <span key={i}>
              {i > 0 && ", "}
              <code>{(s.url ?? "").split("/").slice(-2).join("/")}</code>
            </span>
          ))}{" "}
          (stored with SHA-256, downloaded {fmtDate(data.reference.fetched_utc)}).
        </p>
      )}
    </>
  );
}
