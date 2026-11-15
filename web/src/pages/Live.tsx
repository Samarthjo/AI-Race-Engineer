import { useEffect, useMemo, useRef, useState } from "react";
import { api, type LiveLapSummary, type LiveStatus, type LiveTrack, type Wheels } from "../api";
import { deltaClass, fmtDelta, fmtLap, fmtNum } from "../format";

/** Live telemetry over a WebSocket (up to 25 updates a second). Every value is shown with
 *  words and numbers; colour only repeats what the sign and the words already say. */

type Feed = {
  state: LiveStatus | null;
  connected: boolean;
  latencyMs: number | null; // packet arrival on this Mac -> shown on this page
};

function useLiveFeed(): Feed {
  const [feed, setFeed] = useState<Feed>({ state: null, connected: false, latencyMs: null });
  const latest = useRef<Feed>(feed);
  const frame = useRef<number | null>(null);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let closed = false;
    let retry = 500;
    let timer: number | undefined;
    const flush = () => {
      frame.current = null;
      setFeed({ ...latest.current });
    };
    const connect = () => {
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      const base = window.location.pathname.replace(/[^/]*$/, "");
      ws = new WebSocket(`${proto}://${window.location.host}${base}api/live/ws`);
      ws.onopen = () => {
        retry = 500;
        latest.current = { ...latest.current, connected: true };
        setFeed(latest.current);
      };
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data as string);
        if (msg.type !== "state") return;
        const data = msg.data as LiveStatus;
        const age = (data.last_packet_age_s ?? 0) * 1000;
        const transit = Math.max(0, Date.now() - (msg.sent_ms as number));
        latest.current = {
          state: data,
          connected: true,
          latencyMs: data.receiving ? Math.round(age + transit) : null,
        };
        if (frame.current === null) frame.current = requestAnimationFrame(flush);
      };
      ws.onclose = () => {
        latest.current = { ...latest.current, connected: false };
        setFeed(latest.current);
        if (!closed) timer = window.setTimeout(connect, (retry = Math.min(retry * 2, 5000)));
      };
    };
    connect();
    return () => {
      closed = true;
      window.clearTimeout(timer);
      if (frame.current !== null) cancelAnimationFrame(frame.current);
      ws?.close();
    };
  }, []);
  return feed;
}

export function Live() {
  const { state, connected, latencyMs } = useLiveFeed();
  const [options, setOptions] = useState<{ key: string; label: string }[]>([]);
  const [track, setTrack] = useState<LiveTrack | null>(null);
  const [demoBusy, setDemoBusy] = useState(false);
  const [openLap, setOpenLap] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const sessionUid = state?.session?.session_uid ?? null;
  const refKey = state?.reference?.key ?? "";
  const lapsDone = state?.laps?.length ?? 0;

  useEffect(() => {
    api
      .liveReferences()
      .then((r) => setOptions(r.options))
      .catch(() => setOptions([]));
  }, [sessionUid]);

  useEffect(() => {
    if (!sessionUid) {
      setTrack(null);
      return;
    }
    api
      .liveTrack()
      .then(setTrack)
      .catch(() => setTrack(null));
  }, [sessionUid, refKey, lapsDone]);

  const choose = async (key: string) => {
    try {
      await api.chooseLiveReference(key);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const demo = async (action: "start" | "stop") => {
    setDemoBusy(true);
    try {
      await api.liveDemo(action);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setDemoBusy(false);
    }
  };

  const s = state;
  const receiving = !!s?.receiving;
  return (
    <>
      <div className="live-bar" role="status">
        <span className={`pill ${receiving ? "on" : ""}`}>
          <span className="dot" aria-hidden /> {receiving ? "Receiving" : connected ? "Waiting for telemetry" : "Connecting…"}
        </span>
        {s?.session && (
          <span>
            <strong>{s.session.track_name}</strong> · {s.session.session_type_name}
          </span>
        )}
        <span className="muted">
          {fmtNum(s?.packets_per_s ?? 0, 0)} packets/s · loss {fmtNum(s?.loss_pct ?? 0, 1)} %
          {latencyMs !== null && ` · ${latencyMs} ms behind the car`}
        </span>
        <span className="spacer" />
        <label className="inline-field">
          Delta against{" "}
          <select value={s?.reference_choice ?? "auto"} onChange={(e) => choose(e.target.value)}>
            {(options.length ? options : [{ key: "auto", label: "Automatic" }]).map((o) => (
              <option key={o.key} value={o.key}>
                {o.label}
              </option>
            ))}
          </select>
        </label>
        <button className="btn" onClick={() => demo(s?.session?.synthetic && receiving ? "stop" : "start")} disabled={demoBusy}>
          {s?.session?.synthetic && receiving ? "Stop demo" : "Play demo"}
        </button>
      </div>

      {s?.session?.synthetic && (
        <div className="banner" role="note">
          Synthetic demo replay: made-up laps on a test track, not real telemetry. It is only compared with synthetic
          laps.
        </div>
      )}
      {(error || s?.error) && (
        <div className="error" role="alert">
          {error ?? s?.error}
        </div>
      )}
      {!receiving && !s?.session && (
        <div className="placeholder">
          <div className="card">
            <h1>No telemetry yet</h1>
            <p className="muted">
              Start recording with <code>uv run pitwall capture run</code> and drive: this page follows the car live. Or
              press <strong>Play demo</strong> to replay a synthetic session.
            </p>
          </div>
        </div>
      )}

      {s?.session && (
        <div className="live-grid">
          <div className="live-main">
            <DeltaCard s={s} />
            <InputsCard s={s} />
            <LapsCard laps={s.laps ?? []} open={openLap} onOpen={setOpenLap} />
          </div>
          <aside className="live-side">
            <section className="card" aria-label="Track position">
              <h2>On track</h2>
              <LiveMap track={track} x={s.x} z={s.z} />
            </section>
            <CarCard s={s} />
          </aside>
        </div>
      )}
    </>
  );
}

function DeltaCard({ s }: { s: LiveStatus }) {
  const d = s.delta_s;
  const ref = s.reference;
  return (
    <section className="card live-delta" aria-label="Live delta">
      <div className="delta-head">
        <div>
          <div className="label">Delta {ref ? `to ${ref.label}` : ""}</div>
          <div className={`delta-value ${deltaClass(d)}`}>{d === null || d === undefined ? "–" : `${fmtDelta(d)} s`}</div>
          <div className="sub">
            {d === null || d === undefined
              ? s.reference_note ?? (ref ? "starts at the line" : "no reference yet")
              : d > 0.0005
                ? "behind"
                : d < -0.0005
                  ? "ahead"
                  : "level"}
            {ref?.estimate ? " · real F1 lap: an estimate" : ""}
          </div>
        </div>
        <div className="delta-stats">
          <div>
            <div className="label">Lap {s.lap_number ?? "–"}</div>
            <div className="big">{fmtLap(s.current_lap_time_ms)}</div>
          </div>
          <div>
            <div className="label">Predicted</div>
            <div className="big">{fmtLap(s.predicted_lap_ms)}</div>
          </div>
          <div>
            <div className="label">Reference</div>
            <div className="big">{fmtLap(ref?.lap_time_ms)}</div>
          </div>
        </div>
      </div>
      <div className="sector-tiles">
        {[0, 1, 2].map((k) => {
          const v = s.sector_deltas?.[k];
          const now = (s.sector ?? 0) === k + 1;
          return (
            <div key={k} className={`sector-tile${now ? " now" : ""}`}>
              <span className="label">S{k + 1}{now ? " · now" : ""}</span>
              <span className={deltaClass(v)}>{v === null || v === undefined ? "–" : `${fmtDelta(v)} s`}</span>
            </div>
          );
        })}
      </div>
      {s.lap_invalid && <div className="note">This lap is invalid (track limits): it will not count as a clean lap.</div>}
      {(s.current_lap?.flashbacks ?? 0) > 0 && <div className="note">Flashback used on this lap: not a clean lap.</div>}
    </section>
  );
}

function Bar({ label, value, max = 1, unit = "%" }: { label: string; value: number | undefined; max?: number; unit?: string }) {
  const v = Math.max(0, Math.min(1, (value ?? 0) / max));
  return (
    <div className="vbar" aria-label={`${label} ${Math.round(v * 100)} percent`}>
      <div className="vbar-track">
        <div className="vbar-fill" style={{ height: `${v * 100}%` }} />
      </div>
      <div className="label">{label}</div>
      <div className="small">
        {unit === "%" ? `${Math.round(v * 100)} %` : `${fmtNum(value ?? 0, 1)} ${unit}`}
      </div>
    </div>
  );
}

function InputsCard({ s }: { s: LiveStatus }) {
  const steer = s.steer ?? 0;
  const rpmShare = s.max_rpm ? Math.min(1, (s.rpm ?? 0) / s.max_rpm) : 0;
  return (
    <section className="card" aria-label="Inputs">
      <h2>Car</h2>
      <div className="inputs">
        <Bar label="Throttle" value={s.throttle} />
        <Bar label="Brake" value={s.brake} />
        <div className="readouts">
          <div>
            <div className="label">Speed</div>
            <div className="huge">{s.speed_kmh ?? "–"}</div>
            <div className="small muted">km/h</div>
          </div>
          <div>
            <div className="label">Gear</div>
            <div className="huge">{s.gear === 0 ? "N" : s.gear === -1 ? "R" : (s.gear ?? "–")}</div>
            <div className="small muted">{s.drs ? "DRS open" : s.drs_allowed ? "DRS available" : "DRS closed"}</div>
          </div>
          <div className="wide">
            <div className="label">
              RPM {fmtNum(s.rpm ?? 0, 0)}
              {s.max_rpm ? ` / ${fmtNum(s.max_rpm, 0)}` : ""}
            </div>
            <div className="hbar" aria-label={`Engine at ${Math.round(rpmShare * 100)} percent of its maximum`}>
              <div className="hbar-fill" style={{ width: `${rpmShare * 100}%` }} />
            </div>
            <div className="label">Steering {steer < -0.02 ? "left" : steer > 0.02 ? "right" : "straight"}</div>
            <div className="steer" aria-label={`Steering ${Math.round(steer * 100)} percent`}>
              <div className="steer-mark" style={{ left: `${50 + steer * 50}%` }} />
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

const WHEEL_ORDER: (keyof Wheels)[] = ["fl", "fr", "rl", "rr"];

function CarCard({ s }: { s: LiveStatus }) {
  const ers = s.ers_store_mj ?? 0;
  return (
    <section className="card" aria-label="Tyres, fuel and energy">
      <h2>Tyres · fuel · energy</h2>
      <div className="tyres">
        {WHEEL_ORDER.map((w) => (
          <div key={w} className="tyre">
            <div className="label">{w.toUpperCase()}</div>
            <div>
              {fmtNum(s.tyre_surface_c?.[w], 0)} °C <span className="muted small">surface</span>
            </div>
            <div className="small">
              {fmtNum(s.tyre_inner_c?.[w], 0)} °C inner · {fmtNum(s.tyre_pressure_psi?.[w], 1)} psi
            </div>
            <div className="small">wear {fmtNum(s.tyre_wear_pct?.[w], 1)} %</div>
          </div>
        ))}
      </div>
      <p className="small">
        {s.tyre_compound ?? "–"} tyres, {s.tyre_age_laps ?? "–"} laps old · fuel {fmtNum(s.fuel_kg, 1)} kg (
        {fmtNum(s.fuel_laps, 1)} laps) · ERS {fmtNum(ers, 2)} MJ
      </p>
      <div className="hbar" aria-label={`ERS store ${fmtNum((ers / 4) * 100, 0)} percent`}>
        <div className="hbar-fill" style={{ width: `${Math.min(100, (ers / 4) * 100)}%` }} />
      </div>
    </section>
  );
}

function LiveMap({ track, x, z }: { track: LiveTrack | null; x?: number; z?: number }) {
  const geo = useMemo(() => {
    if (!track?.available || !track.x || !track.z) return null;
    const pts: [number, number][] = [];
    track.x.forEach((xv, i) => {
      const zv = track.z![i];
      if (xv !== null && zv !== null) pts.push([xv, -zv]);
    });
    if (pts.length < 2) return null;
    const xs = pts.map((p) => p[0]);
    const ys = pts.map((p) => p[1]);
    const minX = Math.min(...xs);
    const minY = Math.min(...ys);
    const span = Math.max(Math.max(...xs) - minX, Math.max(...ys) - minY) || 1;
    const pad = span * 0.06;
    return {
      d: pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(" "),
      viewBox: `${minX - pad} ${minY - pad} ${Math.max(...xs) - minX + 2 * pad} ${Math.max(...ys) - minY + 2 * pad}`,
      stroke: span * 0.012,
    };
  }, [track]);
  if (!geo) {
    return <p className="note">The map appears once a lap of this track is known (an ingested lap, or a clean lap now).</p>;
  }
  return (
    <div className="map">
      <svg viewBox={geo.viewBox} role="img" aria-label="Track map with the car's position">
        <path d={geo.d} fill="none" style={{ stroke: "var(--neutral)" }} strokeWidth={geo.stroke} strokeLinejoin="round" />
        {x !== undefined && z !== undefined && (
          <circle cx={x} cy={-z} r={geo.stroke * 1.6} style={{ fill: "var(--series-lap)", stroke: "var(--surface)" }} strokeWidth={geo.stroke * 0.5} />
        )}
      </svg>
    </div>
  );
}

function LapsCard(props: { laps: LiveLapSummary[]; open: number | null; onOpen: (n: number | null) => void }) {
  const laps = [...props.laps].reverse();
  return (
    <section className="card" aria-label="Laps of this session">
      <h2>Laps this session</h2>
      {laps.length === 0 ? (
        <p className="note">Completed laps appear here, each with a quick report a few seconds after the line.</p>
      ) : (
        <div className="table-scroll">
          <table className="data">
            <thead>
              <tr>
                <th>Lap</th>
                <th className="num">Time</th>
                <th className="num">Δ ref</th>
                <th className="num">S1 · S2 · S3 Δ</th>
                <th>Status</th>
                <th>Quick report</th>
              </tr>
            </thead>
            <tbody>
              {laps.map((l) => (
                <LapRow key={l.number} lap={l} open={props.open === l.number} onOpen={props.onOpen} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="note">
        Live results are provisional: <code>pitwall ingest</code> rebuilds every lap from the recording, and Lap Analysis
        shows the full analysis.
      </p>
    </section>
  );
}

function LapRow({ lap, open, onOpen }: { lap: LiveLapSummary; open: boolean; onOpen: (n: number | null) => void }) {
  const r = lap.report;
  return (
    <>
      <tr>
        <td>{lap.number}</td>
        <td className="num">{fmtLap(lap.lap_time_ms)}</td>
        <td className={`num ${deltaClass(lap.delta_s)}`}>{lap.delta_s === null ? "–" : fmtDelta(lap.delta_s)}</td>
        <td className="num small">{lap.sector_deltas.map((v) => (v === null ? "–" : fmtDelta(v))).join(" · ")}</td>
        <td>{lap.usable ? "clean" : <span className="muted">{lap.issues.join(", ")}</span>}</td>
        <td>
          {r === null ? (
            <span className="muted">{lap.usable ? "analysing…" : "–"}</span>
          ) : r.status === "ready" ? (
            <button className="link-btn" onClick={() => onOpen(open ? null : lap.number)}>
              {open ? "hide" : (r.focus?.[0]?.title ?? "no change needed")}
            </button>
          ) : (
            <span className="muted small">{r.reason}</span>
          )}
        </td>
      </tr>
      {open && r?.status === "ready" && (
        <tr className="report-row">
          <td colSpan={6}>
            <div className="small">{r.headline} <span className="muted">(provisional, vs {r.reference})</span></div>
            {(r.focus ?? []).length > 0 && (
              <ol className="plain-list small">
                {r.focus!.map((f) => (
                  <li key={f.fact_id}>
                    <strong>{f.segment_name}</strong> · {f.title}: <span className="muted">{f.measured}</span>{" "}
                    <span className="try-label">Try</span> {f.instruction}
                  </li>
                ))}
              </ol>
            )}
          </td>
        </tr>
      )}
    </>
  );
}
