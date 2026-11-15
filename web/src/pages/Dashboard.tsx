import { useEffect, useState } from "react";
import { api, type Dashboard as DashboardData, type DashboardGroup } from "../api";
import { Sparkline } from "../components/LapTimeChart";
import { deltaClass, fmtDate, fmtDelta, fmtLap } from "../format";

const TREND_WORDS: Record<string, string> = {
  improved: "faster",
  worse: "slower",
  unchanged: "no clear change",
  "not enough data": "not enough data yet",
};

export function TrendBadge({ status, delta }: { status: string; delta: number | null }) {
  return (
    <span className={`trend trend-${status.replace(/ /g, "-")}`}>
      {status === "improved" ? "▼ " : status === "worse" ? "▲ " : ""}
      {TREND_WORDS[status] ?? status}
      {delta !== null && status !== "not enough data" && (
        <span className={status === "unchanged" ? "muted" : deltaClass(delta)}> {fmtDelta(delta)} s</span>
      )}
    </span>
  );
}

function GroupCard({ g, onOpen }: { g: DashboardGroup; onOpen: (id: string) => void }) {
  return (
    <article className="card group-card">
      <div className="card-head">
        <h3>
          {g.track_name} <span className="muted small">· {g.formula_name}</span>
        </h3>
        {g.synthetic && <span className="badge">synthetic</span>}
      </div>
      <div className="group-stats">
        <div>
          <div className="label">Best lap</div>
          <div className="value">{fmtLap(g.best_lap_ms)}</div>
          <div className="sub">{fmtDate(g.best.date)}</div>
        </div>
        <div>
          <div className="label">Latest session best</div>
          <div className="value">{fmtLap(g.latest_best_lap_ms)}</div>
          <div className="sub">{g.new_best_in_latest ? "new personal best" : fmtDate(g.last_date)}</div>
        </div>
        <div>
          <div className="label">Best lap per session</div>
          <Sparkline values={g.series.map((s) => s.best_lap_ms / 1000)} label={`Best lap per session at ${g.track_name}`} />
        </div>
      </div>
      <p className="small">
        {g.sessions === 1 ? "1 session" : `${g.sessions} sessions`} · {g.laps} usable laps · latest vs earlier:{" "}
        <TrendBadge status={g.trend.status} delta={g.trend.delta} />
      </p>
      {g.top_pattern && (
        <p className="small">
          <span className="corner-tag">Recurring</span> {g.top_pattern.segment_name}: {g.top_pattern.title.toLowerCase()} on{" "}
          {g.top_pattern.laps} of {g.top_pattern.of} compared laps
        </p>
      )}
      <button className="btn" onClick={() => onOpen(g.id)}>
        Open history
      </button>
    </article>
  );
}

export function Dashboard({ onOpenHistory }: { onOpenHistory: (group: string) => void }) {
  const [data, setData] = useState<DashboardData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .dashboard()
      .then(setData)
      .catch((e: Error) => setError(e.message));
  }, []);

  if (error) return <div className="error">Could not load the dashboard: {error}</div>;
  if (!data) return <p className="muted">Loading your history…</p>;

  const real = data.groups.filter((g) => !g.synthetic);
  const synthetic = data.groups.filter((g) => g.synthetic);
  const t = data.totals;
  const latest = data.latest_session;
  const recs = data.open_recommendations;

  return (
    <div className="dashboard">
      <div className="summary">
        <div className="card tile">
          <div className="label">Sessions</div>
          <div className="value">{t.real_sessions}</div>
          <div className="sub">
            your own{t.synthetic_sessions ? ` · ${t.synthetic_sessions} synthetic` : ""}
          </div>
        </div>
        <div className="card tile">
          <div className="label">Usable laps</div>
          <div className="value">{t.usable_laps}</div>
          <div className="sub">
            clean, complete laps{t.synthetic_usable_laps ? ` · ${t.synthetic_usable_laps} synthetic` : ""}
          </div>
        </div>
        <div className="card tile">
          <div className="label">Tracks</div>
          <div className="value">{t.tracks}</div>
          <div className="sub">with history{t.synthetic_tracks ? ` · ${t.synthetic_tracks} synthetic` : ""}</div>
        </div>
        <div className="card tile">
          <div className="label">Latest session</div>
          <div className="value small-value">{latest ? latest.track_name : "–"}</div>
          <div className="sub">
            {latest
              ? `${fmtDate(latest.date)} · best ${fmtLap(latest.best_usable_lap_ms)}${latest.synthetic ? " · synthetic" : ""}`
              : "record a session to begin"}
          </div>
        </div>
      </div>

      {data.cache.problems.length > 0 && (
        <div className="banner">
          {data.cache.problems.length} session(s) could not be analysed:{" "}
          {data.cache.problems.map((p) => `${p.session_key} (${p.reason})`).join("; ")}
        </div>
      )}

      {data.groups.length === 0 ? (
        <div className="card">
          <h2>No history yet</h2>
          <p className="muted">
            Record a session on the PS5 and run <code>uv run pitwall ingest</code>, or try{" "}
            <code>uv run pitwall demo</code> for a synthetic one.
          </p>
        </div>
      ) : (
        <>
          {recs.length > 0 && (
            <section className="card" aria-label="Work on next time">
              <h2>Work on next time</h2>
              <p className="note">
                From your latest session on each track: findings that came back on most laps compared with that
                session's best lap. Each is checked in your next session on its target measurement.
              </p>
              <ul className="findings">
                {recs.map((r) => (
                  <li key={`${r.group}-${r.segment_id}-${r.code}`}>
                    <div className="finding-head">
                      <span className="corner-tag">{r.track_name}</span>
                      <span className="corner-tag">{r.segment_name}</span>
                      <strong>{r.title}</strong>
                      {r.synthetic && <span className="badge">synthetic</span>}
                    </div>
                    <div className="finding-try">
                      <span className="try-label">Try</span> {r.suggestion ?? r.title}
                    </div>
                    <div className="finding-detail small">
                      <span className="muted">Seen on</span> {r.laps} of {r.of} laps ·{" "}
                      <span className="muted">target:</span> {r.metric_words} ({r.better} is better)
                    </div>
                  </li>
                ))}
              </ul>
            </section>
          )}
          {real.length > 0 && (
            <>
              <h2 className="section-title">Your tracks</h2>
              <div className="group-grid">
                {real.map((g) => (
                  <GroupCard key={g.id} g={g} onOpen={onOpenHistory} />
                ))}
              </div>
            </>
          )}
          {synthetic.length > 0 && (
            <>
              <h2 className="section-title">Synthetic data (demo and tests, never mixed with your laps)</h2>
              <div className="group-grid">
                {synthetic.map((g) => (
                  <GroupCard key={g.id} g={g} onOpen={onOpenHistory} />
                ))}
              </div>
            </>
          )}
        </>
      )}
    </div>
  );
}
