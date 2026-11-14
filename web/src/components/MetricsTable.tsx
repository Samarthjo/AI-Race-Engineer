import type { Metrics } from "../api";
import { fmtNum } from "../format";

const ROWS: { key: string; label: string; digits: number; unit: string }[] = [
  { key: "top_speed_kmh", label: "Top speed", digits: 0, unit: "km/h" },
  { key: "avg_speed_kmh", label: "Average speed", digits: 1, unit: "km/h" },
  { key: "full_throttle_pct", label: "Full throttle", digits: 1, unit: "%" },
  { key: "braking_pct", label: "Braking", digits: 1, unit: "%" },
  { key: "coasting_pct", label: "Coasting", digits: 1, unit: "%" },
  { key: "gear_changes", label: "Gear changes", digits: 0, unit: "" },
  { key: "max_rpm", label: "Max RPM", digits: 0, unit: "" },
  { key: "drs_open_pct", label: "DRS open", digits: 1, unit: "%" },
  { key: "fuel_used_kg", label: "Fuel used", digits: 3, unit: "kg" },
  { key: "ers_deployed_mj", label: "ERS deployed", digits: 2, unit: "MJ" },
  { key: "ers_harvested_mj", label: "ERS harvested", digits: 2, unit: "MJ" },
  { key: "brake_temp_max_c", label: "Max brake temp", digits: 0, unit: "°C" },
];

function num(v: Metrics[string] | undefined): number | null {
  return typeof v === "number" ? v : null;
}

export function MetricsTable(props: {
  lap: Metrics;
  reference: Metrics | null;
  definitions: Record<string, string>;
}) {
  const { lap, reference: ref, definitions } = props;
  const tyres = (m: Metrics | null) =>
    Array.isArray(m?.tyre_surface_temp_c) ? (m!.tyre_surface_temp_c as number[]).map((t) => fmtNum(t, 0)).join(" / ") : "–";
  return (
    <>
      <table className="data">
        <thead>
          <tr>
            <th>Metric</th>
            <th className="num">This lap</th>
            <th className="num">Reference</th>
            <th className="num">Difference</th>
          </tr>
        </thead>
        <tbody>
          {ROWS.map((r) => {
            const a = num(lap[r.key]);
            const b = ref ? num(ref[r.key]) : null;
            const diff = a !== null && b !== null ? a - b : null;
            return (
              <tr key={r.key} title={definitions[r.key]}>
                <td>
                  {r.label} {r.unit && <span className="muted">({r.unit})</span>}
                </td>
                <td className="num">{fmtNum(a, r.digits)}</td>
                <td className="num">{ref ? fmtNum(b, r.digits) : "–"}</td>
                <td className="num">
                  {diff === null ? "–" : `${diff > 0 ? "+" : diff < 0 ? "−" : ""}${fmtNum(Math.abs(diff), r.digits)}`}
                </td>
              </tr>
            );
          })}
          <tr title={definitions.tyre_surface_temp_c}>
            <td>
              Tyre surface temp <span className="muted">(RL/RR/FL/FR, °C)</span>
            </td>
            <td className="num">{tyres(lap)}</td>
            <td className="num">{ref ? tyres(ref) : "–"}</td>
            <td className="num">–</td>
          </tr>
        </tbody>
      </table>
      <p className="note">
        Measured from the frames of each lap; hover a row for its exact definition.
        {!ref && " The theoretical best is stitched from several laps, so it has no lap metrics."}
      </p>
    </>
  );
}
