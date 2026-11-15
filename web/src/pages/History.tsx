import { useEffect, useState } from "react";
import {
  api,
  ApiError,
  type Change,
  type DashboardGroup,
  type Recommendation,
  type RefSummary,
  type TrackHistory,
} from "../api";
import { LapTimeChart } from "../components/LapTimeChart";
import { deltaClass, fmtDate, fmtDelta, fmtLap, fmtNum } from "../format";
import { TrendBadge } from "./Dashboard";

const OUTCOME_LABEL: Record<Recommendation["outcome"], string> = {
  improved: "✓ improved",
  worse: "✗ went the wrong way",
  unchanged: "= no clear change",
  inconclusive: "? too few laps to judge",
  open: "… next session",
};

function groupLabel(g: DashboardGroup): string {
  return `${g.track_name} · ${g.formula_name}${g.synthetic ? " · synthetic" : ""} (${g.sessions} sessions)`;
}

function metricValue(v: number | null | undefined, unit: string): string {
  if (v === null || v === undefined) return "–";
  return unit ? `${fmtNum(v, unit === "m" || unit === "km/h" ? 0 : 1)} ${unit}` : fmtNum(v, 2);
}

function ChangeCell({ c, compact = false }: { c: Change; compact?: boolean }) {
  if (c.status === "not enough data") return <span className="muted">not enough laps</span>;
  return (
    <span>
      <TrendBadge status={c.status} delta={c.delta} />
      {!compact && <span className="muted small"> ± {fmtNum(c.sigma, 3)}</span>}
    </span>
  );
}

export function History({ initialGroup }: { initialGroup?: string }) {
  const [groups, setGroups] = useState<DashboardGroup[] | null>(null);
  const [group, setGroup] = useState<string | undefined>(initialGroup);
  const [pros, setPros] = useState<RefSummary[]>([]);
  const [pro, setPro] = useState("");
  const [view, setView] = useState<TrackHistory | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .dashboard()
      .then((d) => {
        setGroups(d.groups);
        setGroup((g) => g ?? (d.groups.find((x) => !x.synthetic) ?? d.groups[0])?.id);
      })
      .catch((e: Error) => setError(e.message));
  }, []);

  useEffect(() => {
    if (initialGroup) setGroup(initialGroup);
  }, [initialGroup]);

  useEffect(() => {
    if (!group) return;
    let cancelled = false;
    setBusy(true);
    api
      .history(group, pro)
      .then((v) => {
        if (cancelled) return;
        setView(v);
        setError(null);
      })
      .catch((e: Error) => {
        if (cancelled) return;
        setView(null);
        setError(e instanceof ApiError && e.status === 404 ? `No history: ${e.message}` : e.message);
      })
      .finally(() => !cancelled && setBusy(false));
    return () => {
      cancelled = true;
    };
  }, [group, pro]);

  const bestSession = view?.group.best.session_key;
  useEffect(() => {
    if (!bestSession) return;
    api
      .references(bestSession)
      .then((o) => setPros(o.stored))
      .catch(() => setPros([]));
  }, [bestSession]);

  if (groups && groups.length === 0)
    return (
      <div className="card">
        <h2>No history yet</h2>
        <p className="muted">Ingest a session (or run the demo) and the history builds itself.</p>
      </div>
    );

  const corners = view?.corners;
  const weakMin = Number(view?.thresholds.weak_min_s ?? 0.02);
  const changed = view?.segment_trends.filter((s) => s.status === "improved" || s.status === "worse") ?? [];

  return (
    <div className="history">
      <div className="filters">
        <div className="field">
          <label htmlFor="hist-group">Track and car</label>
          <select id="hist-group" value={group ?? ""} onChange={(e) => setGroup(e.target.value)}>
            {(groups ?? []).map((g) => (
              <option key={g.id} value={g.id}>
                {groupLabel(g)}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="hist-pro">Real-F1 comparison (optional)</label>
          <select id="hist-pro" value={pro} onChange={(e) => setPro(e.target.value)}>
            <option value="">None</option>
            {pros.map((r) => (
              <option key={r.key} value={r.key}>
                {r.driver_name ?? r.driver} · {r.season} {r.event} {r.session} · {fmtLap(r.lap_time_ms)}
              </option>
            ))}
          </select>
        </div>
        {busy && <span className="muted small">Loading…</span>}
      </div>

      {error && <div className="error">{error}</div>}
      {view && (
        <>
          {view.group.synthetic && (
            <div className="banner">Synthetic data: made up for the demo and tests, never mixed with your laps.</div>
          )}
          {view.excluded_laps > 0 && (
            <div className="banner">
              {view.excluded_laps} laps were analysed with an older version of this track's corners and are left out.
            </div>
          )}
          <div className="summary">
            <div className="card tile">
              <div className="label">Best lap</div>
              <div className="value">{fmtLap(view.group.best_lap_ms)}</div>
              <div className="sub">
                {fmtDate(view.group.best.date)} · lap {view.group.best.lap_number}
              </div>
            </div>
            <div className="card tile">
              <div className="label">Ideal lap</div>
              <div className="value">{corners?.ideal_lap_s ? fmtLap(Math.round(corners.ideal_lap_s * 1000)) : "–"}</div>
              <div className="sub">your best segments, any session</div>
            </div>
            <div className="card tile">
              <div className="label">Typical lap now</div>
              <div className="value">{corners?.typical_lap_s ? fmtLap(Math.round(corners.typical_lap_s * 1000)) : "–"}</div>
              <div className="sub">median of your last {corners?.recent_laps ?? 0} laps</div>
            </div>
            <div className="card tile">
              <div className="label">Latest session vs earlier</div>
              <div className="value small-value">
                <TrendBadge status={view.trend.status} delta={view.trend.delta} />
              </div>
              <div className="sub">
                {view.trend.status === "not enough data"
                  ? view.trend.reason
                  : `median ${fmtLap(Math.round((view.trend.after ?? 0) * 1000))} vs ${fmtLap(
                      Math.round((view.trend.before ?? 0) * 1000),
                    )} · counts from ${fmtNum(view.trend.threshold, 3)} s`}
              </div>
            </div>
          </div>
          {(view.trend.caveats ?? []).map((c) => (
            <div className="banner" key={c}>
              {c}
            </div>
          ))}

          <section className="card" aria-label="Lap times over sessions">
            <h2>Lap times per session</h2>
            <LapTimeChart sessions={view.sessions} />
            <div className="table-scroll">
              <table className="data">
                <thead>
                  <tr>
                    <th>Session</th>
                    <th>Laps</th>
                    <th>Best</th>
                    <th>Median</th>
                    <th>Spread</th>
                    <th>Ideal</th>
                    <th>Conditions</th>
                  </tr>
                </thead>
                <tbody>
                  {[...view.sessions].reverse().map((s) => (
                    <tr key={s.session_key}>
                      <td>
                        {fmtDate(s.date)}
                        {s.label ? <span className="muted"> · {s.label}</span> : null}
                      </td>
                      <td>{s.laps}</td>
                      <td>{fmtLap(s.best_lap_ms)}</td>
                      <td>{fmtLap(Math.round(s.median_s * 1000))}</td>
                      <td>{s.spread_s === null ? "–" : `± ${s.spread_s.toFixed(3)}`}</td>
                      <td>{s.ideal_s === null ? "–" : fmtLap(Math.round(s.ideal_s * 1000))}</td>
                      <td className="muted">
                        {s.weather}
                        {s.track_temp_c !== null ? ` · ${s.track_temp_c} °C` : ""}
                        {s.team_id !== null ? ` · team ${s.team_id}` : ""}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>

          <div className="history-grid">
            <section className="card" aria-label="Time left at each corner">
              <h2>Where the time is left</h2>
              <p className="note">
                Each corner with the straight after it (a slow exit costs its time there): your last{" "}
                {corners?.recent_laps} laps against your best there, on average (mistakes included) and as a median.
              </p>
              {corners?.status !== "ok" ? (
                <p className="muted">Not enough laps yet (needs {corners?.min_laps}).</p>
              ) : (
                <div className="table-scroll">
                  <table className="data">
                    <thead>
                      <tr>
                        <th>Corner</th>
                        <th>Mean gap</th>
                        <th>Median gap</th>
                        <th>Spread</th>
                        <th />
                      </tr>
                    </thead>
                    <tbody>
                      {corners.corners.map((c) => {
                        const weak = corners.weakest.some((w) => w.segment_id === c.segment_id);
                        const strong = corners.strongest.some((w) => w.segment_id === c.segment_id);
                        return (
                          <tr key={c.segment_id}>
                            <td title={c.zone_name}>{c.segment_name}</td>
                            <td className={c.mean_gap_s >= weakMin ? "loss" : undefined}>{fmtDelta(c.mean_gap_s)} s</td>
                            <td>{fmtDelta(c.median_gap_s)} s</td>
                            <td>{c.spread_s === null ? "–" : `± ${c.spread_s.toFixed(3)}`}</td>
                            <td>
                              {weak && <span className="corner-tag">most time left</span>}
                              {strong && <span className="corner-tag">closest to your best</span>}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </section>

            <section className="card" aria-label="Recurring patterns">
              <h2>Recurring patterns</h2>
              <p className="note">
                Findings against each session's best lap that keep coming back: on at least{" "}
                {Math.round(Number(view.thresholds.pattern_share) * 100)}% of your latest {view.patterns.compared_laps} compared
                laps.
              </p>
              {view.patterns.status !== "ok" ? (
                <p className="muted">Not enough laps yet (needs {view.patterns.min_laps} compared laps).</p>
              ) : view.patterns.patterns.length === 0 ? (
                <p className="muted">Nothing recurring above the thresholds.</p>
              ) : (
                <ul className="findings">
                  {view.patterns.patterns.map((p) => (
                    <li key={`${p.segment_id}-${p.code}`}>
                      <div className="finding-head">
                        <span className="corner-tag">{p.segment_name}</span>
                        <strong>{p.title}</strong>
                      </div>
                      <div className="finding-detail small">
                        {p.laps} of {p.of} laps ({Math.round(p.share * 100)}%) · in {p.sessions} of {p.of_sessions} sessions ·{" "}
                        <span className="loss">{fmtDelta(p.mean_impact_s)} s</span> each time on average
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </div>

          <section className="card" aria-label="Recommendations and outcomes">
            <h2>Recommendations and outcomes</h2>
            <p className="note">
              After each session, the findings seen on most of its laps become up to three recommendations with a target
              measurement; the next session on this track shows whether it moved the right way (more than its minimum change
              and two standard errors), with at least {view.thresholds.evaluation_min_laps} laps on each side.
            </p>
            {view.recommendations.length === 0 ? (
              <p className="muted">None yet: they are made after a session with recurring findings.</p>
            ) : (
              <div className="table-scroll">
                <table className="data">
                  <thead>
                    <tr>
                      <th>After session</th>
                      <th>Corner</th>
                      <th>Recommendation</th>
                      <th>Target</th>
                      <th>Before → next session</th>
                      <th>Outcome</th>
                      <th>Time there</th>
                    </tr>
                  </thead>
                  <tbody>
                    {[...view.recommendations].reverse().map((r) => (
                      <tr key={`${r.session_key}-${r.segment_id}-${r.code}`}>
                        <td>{fmtDate(r.session_date)}</td>
                        <td>{r.segment_name}</td>
                        <td className="wrap" title={r.suggestion ?? ""}>
                          {r.title} <span className="muted small">({r.laps} of {r.of} laps)</span>
                        </td>
                        <td className="muted wrap">
                          {r.metric_words}, {r.better} is better
                        </td>
                        <td>
                          {r.metric_change && r.metric_change.status !== "not enough data"
                            ? `${metricValue(r.metric_change.before, r.unit)} → ${metricValue(r.metric_change.after, r.unit)}`
                            : "–"}
                        </td>
                        <td className={`outcome outcome-${r.outcome}`}>{OUTCOME_LABEL[r.outcome]}</td>
                        <td>{r.segment_time ? <ChangeCell c={r.segment_time} compact /> : "–"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="card" aria-label="Latest session per corner">
            <h2>Latest session vs earlier, per corner and straight</h2>
            {view.segment_trends.length === 0 ? (
              <p className="muted">Needs at least two sessions.</p>
            ) : (
              <>
                <p className="note">
                  {changed.length} changed by more than {fmtNum(Number(view.thresholds.segment_min_change_s), 2)} s and two
                  standard errors; the others are unchanged or have too few laps.
                </p>
                <div className="table-scroll">
                  <table className="data">
                    <thead>
                      <tr>
                        <th>Segment</th>
                        <th>Earlier (median)</th>
                        <th>Latest (median)</th>
                        <th>Change</th>
                      </tr>
                    </thead>
                    <tbody>
                      {view.segment_trends.map((s) => (
                        <tr key={s.segment_id} className={s.kind === "straight" ? "muted-row" : undefined}>
                          <td>{s.segment_name}</td>
                          <td>{s.before === null ? "–" : `${s.before.toFixed(3)} s`}</td>
                          <td>{s.after === null ? "–" : `${s.after.toFixed(3)} s`}</td>
                          <td>
                            <ChangeCell c={s} />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </section>

          {view.pro && (
            <section className="card" aria-label="Against real F1">
              <h2>Your best lap here against real F1 (estimates)</h2>
              {view.pro.error !== undefined ? (
                <div className="error">{view.pro.error}</div>
              ) : (
                <>
                  <p className="note">
                    {view.pro.driver}, {view.pro.season} {view.pro.event} {view.pro.session} (
                    {fmtLap(view.pro.ref_lap_time_ms)}) against your best lap ({fmtLap(view.pro.lap.lap_time_ms)}), after
                    the systematic pace difference is taken out; {view.pro.rule}.
                  </p>
                  <div className="history-grid">
                    {(
                      [
                        ["Weaker than your usual offset", view.pro.weakest],
                        ["Stronger than your usual offset", view.pro.strongest],
                      ] as const
                    ).map(([title, items]) => (
                      <div key={title}>
                        <h3>{title}</h3>
                        {items.length === 0 ? (
                          <p className="muted">None beyond the uncertainty.</p>
                        ) : (
                          <ul className="plain-list">
                            {items.map((c) => (
                              <li key={c.segment_id}>
                                <strong>{c.segment_name}</strong>{" "}
                                <span className={deltaClass(c.normalised_s)}>about {fmtDelta(c.normalised_s)} s</span>{" "}
                                <span className="muted">± {c.sigma_s.toFixed(3)}</span>
                              </li>
                            ))}
                          </ul>
                        )}
                      </div>
                    ))}
                  </div>
                </>
              )}
            </section>
          )}
        </>
      )}
    </div>
  );
}
