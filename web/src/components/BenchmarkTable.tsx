import type { Confidence, MetricCompare, SegmentBench } from "../api";
import { deltaClass, fmtDelta, fmtNum } from "../format";

/** Corner table against a real F1 lap: what was measured, what is systematic (game vs real
 *  car, median over the lap) and what is left (normalised), each with its uncertainty and a
 *  confidence label. Signs and words carry the meaning; colour only repeats it. */

function Badge({ c }: { c: Confidence }) {
  if (c !== "high" && c !== "medium") return null;
  return <span className={`badge inline ${c}`}>{c}</span>;
}

function signed(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "–";
  const r = Number(v.toFixed(digits));
  if (r === 0) return (0).toFixed(digits);
  return `${r > 0 ? "+" : "−"}${Math.abs(r).toFixed(digits)}`;
}

function Metric({ m, title }: { m: MetricCompare | undefined; title: string }) {
  if (!m || m.delta === null) {
    return (
      <td className="num muted" title={m?.note ?? `${title}: not measurable here`}>
        –
      </td>
    );
  }
  const tip =
    `${title}: you ${fmtNum(m.lap, 0)} ${m.unit}, reference about ${fmtNum(m.ref, 0)} ±${fmtNum(m.ref_sigma, 0)} ${m.unit}` +
    (m.offset !== null ? `; systematic ${signed(m.offset)} ${m.unit}` : "") +
    (m.z !== null ? `; ${fmtNum(m.z, 1)} standard deviations` : "");
  return (
    <td className="num" title={tip}>
      <span>{signed(m.delta)}</span>
      {m.normalised !== null && (
        <>
          {" → "}
          <strong>{signed(m.normalised)}</strong>
        </>
      )}
      <span className="muted"> ±{fmtNum(m.sigma, 0)}</span> <Badge c={m.confidence} />
    </td>
  );
}

export function BenchmarkTable(props: {
  segments: SegmentBench[];
  onSelect: (r: SegmentBench) => void;
  selected: string | null;
  driver: string;
}) {
  const { segments, onSelect, selected, driver } = props;
  return (
    <>
      <div className="table-scroll">
        <table className="data corners bench">
          <thead>
            <tr>
              <th>Segment</th>
              <th className="num" title={`Time lost (+) or gained (−) against ${driver} in this segment, ± 1 standard deviation`}>
                Time Δ
              </th>
              <th className="num" title="Expected from the systematic pace difference (median time ratio over braking corners, or over straights)">
                Systematic
              </th>
              <th className="num" title="Measured minus systematic: the driver-specific part">
                Normalised
              </th>
              <th className="num" title="Brake point: − earlier, + later (m). Measured → normalised ± 1 sd">
                Brake point m
              </th>
              <th className="num" title="Minimum speed: − slower (km/h). Measured → normalised ± 1 sd">
                Min speed km/h
              </th>
              <th className="num" title="Full throttle point: − earlier, + later (m). Measured → normalised ± 1 sd">
                Full throttle m
              </th>
              <th className="num" title="Exit speed: − slower (km/h). Measured → normalised ± 1 sd">
                Exit km/h
              </th>
            </tr>
          </thead>
          <tbody>
            {segments.map((r) => {
              const s = r.segment;
              const corner = s.kind !== "straight";
              return (
                <tr
                  key={s.id}
                  className={`${corner ? "corner-row" : "straight-row"}${selected === s.id ? " selected" : ""}`}
                  onClick={() => onSelect(r)}
                  tabIndex={0}
                  onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && onSelect(r)}
                  aria-label={`${s.name}: ${fmtDelta(r.loss_s)} seconds measured, ${fmtDelta(r.normalised_s)} normalised`}
                >
                  <td>
                    <strong className={corner ? undefined : "muted"}>{s.name}</strong>
                    {s.kind !== "corner" && s.kind !== "straight" && <span className="muted"> · {s.kind}</span>}
                  </td>
                  <td className={`num ${deltaClass(r.loss_s)}`}>
                    {fmtDelta(r.loss_s)}
                    <span className="muted"> ±{fmtNum(r.loss_sigma_s, 3)}</span>
                  </td>
                  <td className="num muted">{fmtDelta(r.expected_s)}</td>
                  <td className={`num ${r.confidence === "high" || r.confidence === "medium" ? deltaClass(r.normalised_s) : ""}`}>
                    <strong>{fmtDelta(r.normalised_s)}</strong>
                    <span className="muted"> ±{fmtNum(r.normalised_sigma_s, 3)}</span> <Badge c={r.confidence} />
                  </td>
                  {corner ? (
                    <>
                      <Metric m={r.metrics.brake_onset_m} title="Brake point" />
                      <Metric m={r.metrics.min_speed_kmh} title="Minimum speed" />
                      <Metric m={r.metrics.full_throttle_m} title="Full throttle" />
                      <Metric m={r.metrics.exit_speed_kmh} title="Exit speed" />
                    </>
                  ) : (
                    <td colSpan={4} />
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="note">
        Each value: measured → <strong>normalised</strong> ± 1 standard deviation. <em>high</em> / <em>medium</em>: the
        difference is more than 3 / 2 standard deviations of its uncertainty, so it is unlikely to come from the
        reference's sampling or from placing it on your track. With about thirty values per lap, an occasional
        <em> medium</em> can still be chance; findings also need the corner to lose time. Hover a value for details;
        click a row to zoom the charts.
      </p>
    </>
  );
}
