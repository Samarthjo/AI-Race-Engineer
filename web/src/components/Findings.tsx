import type { Finding } from "../api";
import { fmtDelta, fmtNum } from "../format";

/** Measured findings, biggest time impact first. The suggestion is kept visibly separate
 *  from the measurement. */
export function Findings(props: {
  findings: Finding[];
  onShow: (f: Finding) => void;
  selected: string | null;
  emptyNote?: string;
  footNote?: string;
}) {
  const { findings, onShow, selected, emptyNote, footNote } = props;
  if (findings.length === 0) {
    return (
      <p className="note">
        {emptyNote ??
          "No findings: no measured difference above the thresholds. Either this lap matches the reference corner by corner, or the differences are within measurement resolution."}
      </p>
    );
  }
  return (
    <>
      <ol className="findings">
        {findings.map((f, i) => (
          <li key={`${f.code}-${f.segment_id}-${i}`} className={selected === f.segment_id ? "selected" : undefined}>
            <div className="finding-head">
              <span className="corner-tag">{f.segment_name}</span>
              <strong>{f.title}</strong>
              {f.basis === "pro_normalised" && <span className="corner-tag">vs real F1 · normalised</span>}
              <span className={`badge ${f.confidence}`}>{f.confidence}</span>
            </div>
            <div className="finding-impact">
              <span className={f.time_impact_s > 0.0005 ? "loss" : ""}>
                {f.time_impact_s > 0.0005 ? `${fmtDelta(f.time_impact_s)} s` : "event"}
              </span>
              <span className="muted"> · {f.phases.map((p) => p.replace("_", " ")).join(" + ")}</span>
            </div>
            <div className="finding-detail">{f.detail}</div>
            {f.suggestion && (
              <div className="finding-try">
                <span className="try-label">Try</span> {f.suggestion}
              </div>
            )}
            <button className="link-btn" onClick={() => onShow(f)}>
              show on charts ({fmtNum(f.evidence.start_m, 0)}–{fmtNum(f.evidence.end_m, 0)} m)
            </button>
          </li>
        ))}
      </ol>
      <p className="note">
        {footNote ?? "Measured against the reference lap."} Impacts of findings in the same corner can overlap, so
        don't add them up; the corner table shows each corner's total.
      </p>
    </>
  );
}
