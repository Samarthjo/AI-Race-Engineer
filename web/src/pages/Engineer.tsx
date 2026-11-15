import { useEffect, useMemo, useState } from "react";
import {
  api,
  ApiError,
  type Answer,
  type EngineerParams,
  type EngineerReport,
  type EngineerStatus,
  type Fact,
  type Lap,
  type Narrative,
  type NarrativePart,
  type RefSummary,
  type Session,
} from "../api";
import { fmtDate, fmtDelta, fmtLap } from "../format";

const REFERENCES: { value: string; label: string }[] = [
  { value: "session_best", label: "Session best" },
  { value: "personal_best", label: "Personal best (this track)" },
  { value: "theoretical_best", label: "Theoretical best (best sectors)" },
];

function pickLap(laps: Lap[]): Lap | undefined {
  const comparable = laps.filter((l) => l.comparable);
  const usable = comparable.filter((l) => l.usable && l.lap_time_ms !== null);
  const best = [...usable].sort((a, b) => a.lap_time_ms! - b.lap_time_ms!)[0];
  return [...usable].reverse().find((l) => l !== best) ?? comparable[comparable.length - 1];
}

/** Fact ids shown as small chips; hovering shows the measured sentence behind them. */
function Cites({ ids, facts }: { ids: string[]; facts: Map<string, Fact> }) {
  if (!ids.length) return null;
  return (
    <span className="cites">
      {ids.map((id) => (
        <span key={id} className="cite" title={facts.get(id)?.text ?? id} tabIndex={0}>
          {id}
        </span>
      ))}
    </span>
  );
}

export function Engineer() {
  const [sessions, setSessions] = useState<Session[] | null>(null);
  const [sessionKey, setSessionKey] = useState<string>();
  const [laps, setLaps] = useState<Lap[]>([]);
  const [lapIndex, setLapIndex] = useState<number>();
  const [ref, setRef] = useState("session_best");
  const [pros, setPros] = useState<RefSummary[]>([]);
  const [pro, setPro] = useState("");
  const [report, setReport] = useState<EngineerReport | null>(null);
  const [status, setStatus] = useState<EngineerStatus | null>(null);
  const [narrative, setNarrative] = useState<Narrative | null>(null);
  const [answers, setAnswers] = useState<Answer[]>([]);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState<"narrate" | "ask" | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .sessions()
      .then((s) => {
        setSessions(s);
        if (s.length) setSessionKey(s[0].session_key);
      })
      .catch((e: Error) => setError(e.message));
    api
      .engineerStatus()
      .then(setStatus)
      .catch(() => setStatus(null));
  }, []);

  useEffect(() => {
    if (!sessionKey) return;
    setReport(null);
    api
      .laps(sessionKey)
      .then((r) => {
        setLaps(r.laps);
        setLapIndex(pickLap(r.laps)?.lap_index);
      })
      .catch((e: Error) => setError(e.message));
    setPro("");
    api
      .references(sessionKey)
      .then((o) => setPros(o.stored))
      .catch(() => setPros([]));
  }, [sessionKey]);

  const params: EngineerParams | null =
    sessionKey && lapIndex !== undefined
      ? { session: sessionKey, lap: lapIndex, ref, ...(pro ? { pro } : {}) }
      : null;
  const paramKey = params ? JSON.stringify(params) : "";

  useEffect(() => {
    if (!params) return;
    let cancelled = false;
    setNarrative(null);
    setAnswers([]);
    api
      .engineerReport(params)
      .then((r) => {
        if (cancelled) return;
        setReport(r);
        setError(null);
      })
      .catch((e: Error) => {
        if (cancelled) return;
        setReport(null);
        setError(e instanceof ApiError && e.status === 422 ? `No report: ${e.message}` : e.message);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [paramKey]);

  const facts = useMemo(() => new Map((report?.facts ?? []).map((f) => [f.id, f])), [report]);

  const writeUp = async () => {
    if (!params) return;
    setBusy("narrate");
    try {
      setNarrative(await api.narrate(params));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const askIt = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!params || !question.trim()) return;
    setBusy("ask");
    try {
      const a = await api.ask({ ...params, question: question.trim() });
      setAnswers((prev) => [a, ...prev]);
      setQuestion("");
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(null);
    }
  };

  if (sessions && sessions.length === 0) {
    return (
      <div className="placeholder">
        <div className="card">
          <h1>No sessions yet</h1>
          <p className="muted">
            Record and ingest a session first, or run <code>uv run pitwall demo</code> to explore.
          </p>
        </div>
      </div>
    );
  }

  const session = sessions?.find((s) => s.session_key === sessionKey);
  const aiReady = !!status?.reachable && status.model_installed;
  return (
    <>
      <div className="filters" role="group" aria-label="Lap selection">
        <div className="field">
          <label htmlFor="e-session">Session</label>
          <select id="e-session" value={sessionKey ?? ""} onChange={(e) => setSessionKey(e.target.value)}>
            {(sessions ?? []).map((s) => (
              <option key={s.session_key} value={s.session_key}>
                {s.track_name} · {s.session_type_name} · {fmtDate(s.first_utc)}
                {s.synthetic ? " · synthetic" : ""}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="e-lap">Lap</label>
          <select id="e-lap" value={lapIndex ?? ""} onChange={(e) => setLapIndex(Number(e.target.value))}>
            {laps
              .filter((l) => l.comparable)
              .map((l) => (
                <option key={l.lap_index} value={l.lap_index}>
                  Lap {l.lap_number} · {fmtLap(l.lap_time_ms)}
                  {l.usable ? "" : " · not usable"}
                </option>
              ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="e-ref">Compare with</label>
          <select id="e-ref" value={ref} onChange={(e) => setRef(e.target.value)}>
            {REFERENCES.map((r) => (
              <option key={r.value} value={r.value}>
                {r.label}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="e-pro">Real F1 driver (optional)</label>
          <select id="e-pro" value={pro} onChange={(e) => setPro(e.target.value)} disabled={!pros.length}>
            <option value="">{pros.length ? "none" : "none stored (see Driver Benchmark)"}</option>
            {pros.map((r) => (
              <option key={r.key} value={r.key}>
                {r.driver} · {r.session} {r.session_part ?? ""} · {fmtLap(r.lap_time_ms)}
                {r.synthetic ? " · synthetic" : ""}
              </option>
            ))}
          </select>
        </div>
      </div>

      {session?.synthetic && (
        <div className="banner" role="note">
          Synthetic demo data, generated by <code>pitwall demo</code>. Not real telemetry.
        </div>
      )}
      {error && (
        <div className="error" role="alert">
          {error}
        </div>
      )}

      {report && (
        <div className="engineer">
          <section className="card" aria-label="Engineer's report">
            <h2>Engineer's report · measured</h2>
            <p className="headline">{report.headline}</p>
            <h3>Focus for the next lap</h3>
            {report.focus.length === 0 ? (
              <p className="note">Nothing specific: see the notes below.</p>
            ) : (
              <ol className="findings">
                {report.focus.map((f) => (
                  <li key={f.fact_id}>
                    <div className="finding-head">
                      <span className="corner-tag">{f.segment_name}</span>
                      <strong>{f.title}</strong>
                      {f.basis !== "self" && <span className="corner-tag">vs real F1 · estimate</span>}
                      <span className={`badge ${f.confidence}`}>{f.confidence}</span>
                    </div>
                    <div className="finding-try">
                      <span className="try-label">Try</span> {f.instruction}
                    </div>
                    <div className="finding-detail">
                      <span className="muted">Measured:</span> {f.measured}{" "}
                      {f.impact_s > 0.0005 && <span className="loss">{fmtDelta(f.impact_s)} s</span>}{" "}
                      <Cites ids={[f.fact_id]} facts={facts} />
                    </div>
                  </li>
                ))}
              </ol>
            )}
            {report.losses.length > 0 && (
              <>
                <h3>Where the time goes</h3>
                <ul className="plain-list">
                  {report.losses.map((x) => (
                    <li key={x.fact_id}>
                      <strong>{x.segment_name}</strong> <span className="loss">{fmtDelta(x.loss_s)} s</span>
                      {x.cause_titles.length > 0 && <span className="muted"> · {x.cause_titles.join(", ").toLowerCase()}</span>}{" "}
                      <Cites ids={[x.fact_id]} facts={facts} />
                    </li>
                  ))}
                </ul>
              </>
            )}
            {(
              [
                ["Strengths", report.strengths],
                ["Consistency", report.consistency],
                ["Notes", report.notes],
              ] as const
            ).map(
              ([title, ids]) =>
                ids.length > 0 && (
                  <div key={title}>
                    <h3>{title}</h3>
                    <ul className="plain-list">
                      {ids.map((id) => (
                        <li key={id}>
                          {facts.get(id)?.text} <Cites ids={[id]} facts={facts} />
                        </li>
                      ))}
                    </ul>
                  </div>
                ),
            )}
          </section>

          <div className="engineer-side">
          <section className="card" aria-label="AI write-up">
            <div className="card-head">
              <h2>AI write-up</h2>
              <AiStatus status={status} />
            </div>
            <p className="note">
              Your local model writes the report up in plain words. It only sees the measured facts above, and a check
              traces every number and corner it writes back to them; if it fails twice, the measured report is shown
              instead.
            </p>
            <button className="btn" onClick={writeUp} disabled={busy !== null || !aiReady}>
              {busy === "narrate" ? "Writing…" : "Write it up"}
            </button>
            {narrative && <NarrativeView n={narrative} facts={facts} />}
          </section>

          <section className="card" aria-label="Ask your engineer">
            <h2>Ask your engineer</h2>
            <form className="ask" onSubmit={askIt}>
              <input
                type="text"
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                placeholder="e.g. Where do I lose most time? How was this lap against lap 4?"
                aria-label="Your question"
                maxLength={500}
              />
              <button className="btn" type="submit" disabled={busy !== null || !aiReady || !question.trim()}>
                {busy === "ask" ? "Thinking…" : "Ask"}
              </button>
            </form>
            <p className="note">
              Answers come only from measured data (this lap, its corners, other laps of the session). Questions the
              data cannot answer (tyres, fuel, set-up, steering, past sessions) get an honest "can't tell".
            </p>
            {answers.map((a) => (
              <div key={a.narrative_id} className="answer">
                <div className="q">{a.question}</div>
                <div>{a.output.answer}</div>
                <div className="muted small">
                  <span className={`badge inline ${a.status === "grounded" ? "high" : ""}`}>{answerLabel(a.status)}</span>{" "}
                  <Cites ids={a.output.cites} facts={facts} />
                  {a.tool_calls.length > 0 && ` · looked up: ${a.tool_calls.map((c) => c.name).join(", ")}`}
                </div>
              </div>
            ))}
          </section>
          </div>
        </div>
      )}
    </>
  );
}

function answerLabel(s: Answer["status"]): string {
  return {
    grounded: "checked",
    refused: "data can't tell",
    fallback: "failed the check",
    unavailable: "no AI",
  }[s];
}

function AiStatus({ status }: { status: EngineerStatus | null }) {
  if (!status) return <span className="pill">AI status unknown</span>;
  if (!status.reachable)
    return (
      <span className="pill" title={status.error ?? ""}>
        <span className="dot" /> Ollama not running
      </span>
    );
  if (!status.model_installed)
    return (
      <span className="pill" title={status.error ?? ""}>
        <span className="dot" /> run: ollama pull {status.model}
      </span>
    );
  return (
    <span className="pill on">
      <span className="dot" /> {status.model} ready
    </span>
  );
}

function Part({ p, facts }: { p: NarrativePart; facts: Map<string, Fact> }) {
  return (
    <p>
      {p.title && <strong>{p.title}. </strong>}
      {p.text} <Cites ids={p.cites} facts={facts} />
    </p>
  );
}

function NarrativeView({ n, facts }: { n: Narrative; facts: Map<string, Fact> }) {
  const head = {
    grounded: "Checked: every number and corner traces to the measured facts.",
    fallback: "The AI text failed the fact check twice, so this is the measured report.",
    unavailable: `No AI available (${n.error ?? "unknown"}): this is the measured report.`,
  }[n.status];
  const rejected = n.log.flatMap((l) => l.violations.map((v) => `${v.where}: ${v.detail}`));
  return (
    <div className="narrative">
      <div className={n.status === "grounded" ? "check ok" : "check warn"} role="status">
        {head}
      </div>
      <Part p={n.output.summary} facts={facts} />
      {n.output.sections.map((s, i) => (
        <Part key={i} p={s} facts={facts} />
      ))}
      {n.output.focus.length > 0 && (
        <>
          <h3>Focus for the next lap</h3>
          <ol>
            {n.output.focus.map((f, i) => (
              <li key={i}>
                {f.text} <Cites ids={f.cites} facts={facts} />
              </li>
            ))}
          </ol>
        </>
      )}
      {rejected.length > 0 && (
        <details>
          <summary>What the check rejected ({rejected.length})</summary>
          <ul className="plain-list small">
            {rejected.map((r, i) => (
              <li key={i}>{r}</li>
            ))}
          </ul>
        </details>
      )}
      <div className="muted small">
        {n.provider} · {n.model} · prompt {n.prompt_version} · {n.attempts} attempt(s) · {n.duration_s.toFixed(1)} s
      </div>
    </div>
  );
}
