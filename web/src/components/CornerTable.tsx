import type { Consistency, SegmentResult } from "../api";
import { deltaClass, fmtDelta, fmtNum } from "../format";

function pointDelta(lap: number | null | undefined, ref: number | null | undefined, early: string, late: string) {
  if (lap === null || lap === undefined || ref === null || ref === undefined) return "–";
  const d = lap - ref;
  if (Math.abs(d) < 2) return "same";
  return `${fmtNum(Math.abs(d), 0)} m ${d < 0 ? early : late}`;
}

function pair(a: number | null | undefined, b: number | null | undefined) {
  return `${fmtNum(a ?? null, 0)} / ${fmtNum(b ?? null, 0)}`;
}

/** One row per segment: where the lap loses time (corners include the exit carried down the
 *  next straight) and the key driving points against the reference. */
export function CornerTable(props: {
  segments: SegmentResult[];
  consistency: Consistency | null;
  onSelect: (r: SegmentResult) => void;
  selected: string | null;
  definitions: Record<string, string>;
}) {
  const { segments, consistency, onSelect, selected, definitions } = props;
  const stats = new Map((consistency?.segments ?? []).map((s) => [s.id, s]));
  const total = segments.reduce((sum, r) => sum + r.total_s, 0);
  return (
    <>
      <div className="table-scroll">
        <table className="data corners">
          <thead>
            <tr>
              <th>Segment</th>
              <th className="num" title="Time lost (+) or gained (−) here; corners include the carried exit loss">
                Time Δ
              </th>
              <th className="num" title="Part of the corner's loss that happens on the next straight because of a slower exit">
                Exit carry
              </th>
              <th title={definitions.brake_onset_m}>Brake point</th>
              <th className="num" title={definitions.entry_speed_kmh}>Entry km/h</th>
              <th className="num" title={definitions.min_speed_kmh}>Min km/h</th>
              <th title={definitions.full_throttle_m}>Full throttle</th>
              <th className="num" title={definitions.exit_speed_kmh}>Exit km/h</th>
              <th className="num" title="Spread of this segment's time over the session's usable laps (standard deviation)">
                Spread
              </th>
            </tr>
          </thead>
          <tbody>
            {segments.map((r) => {
              const s = r.segment;
              const st = stats.get(s.id);
              const corner = s.kind !== "straight";
              return (
                <tr
                  key={s.id}
                  className={`${corner ? "corner-row" : "straight-row"}${selected === s.id ? " selected" : ""}`}
                  onClick={() => onSelect(r)}
                  tabIndex={0}
                  onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && onSelect(r)}
                  aria-label={`${s.name}: ${fmtDelta(r.total_s)} seconds`}
                >
                  <td>
                    <strong className={corner ? undefined : "muted"}>{s.name}</strong>
                    {s.kind !== "corner" && s.kind !== "straight" && <span className="muted"> · {s.kind}</span>}
                  </td>
                  <td className={`num ${deltaClass(r.total_s)}`}>{fmtDelta(r.total_s)}</td>
                  <td className={`num ${deltaClass(r.carried_s)}`}>{corner && r.carried_s ? fmtDelta(r.carried_s) : ""}</td>
                  <td>{corner ? pointDelta(r.lap?.brake_onset_m, r.ref?.brake_onset_m, "earlier", "later") : ""}</td>
                  <td className="num">{corner ? pair(r.lap?.entry_speed_kmh, r.ref?.entry_speed_kmh) : ""}</td>
                  <td className="num">{corner ? pair(r.lap?.min_speed_kmh, r.ref?.min_speed_kmh) : ""}</td>
                  <td>{corner ? pointDelta(r.lap?.full_throttle_m, r.ref?.full_throttle_m, "earlier", "later") : ""}</td>
                  <td className="num">{corner ? pair(r.lap?.exit_speed_kmh, r.ref?.exit_speed_kmh) : ""}</td>
                  <td className={`num${st?.inconsistent ? " loss" : ""}`}>
                    {st ? `±${st.std_s.toFixed(3)}` : "–"}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="note">
        Speeds: this lap / reference. Segments add up to {fmtDelta(total)} s, the lap delta. Click a row to zoom the
        charts and highlight it on the map.
        {consistency && consistency.laps >= 2
          ? ` Spread over ${consistency.laps} usable laps; red = more than ±0.100 s (inconsistent). Ideal lap from your best segments: ${fmtNum(consistency.ideal_lap_s, 3)} s.`
          : " Spread needs at least 2 usable laps in the session."}
      </p>
    </>
  );
}
