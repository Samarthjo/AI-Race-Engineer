import { useEffect, useState } from "react";
import { api, type LiveStatus } from "../api";
import { fmtLap } from "../format";

/** Live status in the top bar (polled every 2 s); the full view is the Live Telemetry page. */
export function LivePill() {
  const [status, setStatus] = useState<LiveStatus | null>(null);

  useEffect(() => {
    let alive = true;
    const poll = async () => {
      try {
        const s = await api.live();
        if (alive) setStatus(s);
      } catch {
        if (alive) setStatus(null);
      }
    };
    poll();
    const id = window.setInterval(poll, 2000);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
  }, []);

  if (!status || !status.receiving) {
    const title = status?.error ?? "No telemetry arriving. Start `pitwall capture run` and drive.";
    return (
      <span className="pill" title={title}>
        <span className="dot" aria-hidden /> No live data
      </span>
    );
  }
  const parts = [
    status.track_name,
    status.lap_number !== undefined ? `Lap ${status.lap_number}` : undefined,
    status.current_lap_time_ms !== undefined ? fmtLap(status.current_lap_time_ms) : undefined,
    status.speed_kmh !== undefined ? `${status.speed_kmh} km/h` : undefined,
  ].filter(Boolean);
  return (
    <span className="pill on" title={`${status.packets_per_s ?? 0} packets/s`}>
      <span className="dot" aria-hidden /> LIVE · {parts.join(" · ")}
    </span>
  );
}
