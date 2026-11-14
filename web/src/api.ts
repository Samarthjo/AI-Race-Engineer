// Types and fetch helpers for the local Pitwall API (`pitwall serve`).

export type Session = {
  session_key: string;
  capture_id: string;
  session_uid: string;
  label: string | null;
  synthetic: boolean;
  track_id: number | null;
  track_name: string | null;
  track_length_m: number | null;
  session_type_name: string | null;
  game_mode_name: string | null;
  game_version: string | null;
  first_utc: string | null;
  lap_count: number;
  usable_laps: number;
  best_usable_lap_ms: number | null;
  sector_starts_m: [number, number] | null;
};

export type Lap = {
  lap_index: number;
  lap_number: number;
  kind: string;
  complete: boolean;
  valid: boolean | null;
  usable: boolean;
  lap_time_ms: number | null;
  sectors_ms: (number | null)[];
  issues: string[];
  notes: string[];
  comparable: boolean;
  not_comparable_reason: string | null;
};

export type Metrics = Record<string, number | number[] | null>;

export type Sector = {
  sector: number;
  start_m: number;
  end_m: number;
  lap_ms: number | null;
  ref_ms: number | null;
  delta_s: number;
  delta_source: "official" | "trace";
};

export type Window = { start_m: number; end_m: number; seconds: number };

export type Series = (number | null)[];

export type Comparison = {
  session: Session;
  lap: Lap & { metrics: Metrics; max_gap_m: number };
  reference: {
    kind: string;
    label: string;
    lap_time_ms: number | null;
    trace_time_s: number;
    sectors_ms: (number | null)[];
    sources: { session_key: string; lap_index: number; lap_number: number; sectors: number[] }[];
    metrics: Metrics | null;
  };
  delta_s: number;
  sectors: Sector[];
  largest_losses: Window[];
  largest_gains: Window[];
  metric_definitions: Record<string, string>;
  trace: {
    step_m: number;
    distance: number[];
    delta: Series;
    loss_rate: Series;
    x: Series;
    z: Series;
    lap: Record<string, Series>;
    ref: Record<string, Series>;
  };
};

export type LiveStatus = {
  listening: boolean;
  receiving: boolean;
  error?: string | null;
  packets_per_s?: number;
  track_name?: string;
  session_type_name?: string;
  lap_number?: number;
  lap_distance_m?: number;
  current_lap_time_ms?: number;
  speed_kmh?: number;
  gear?: number;
};

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function get<T>(path: string, params: Record<string, string | number | undefined> = {}) {
  const query = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined) query.set(k, String(v));
  const qs = query.toString();
  const res = await fetch(`api/${path}${qs ? `?${qs}` : ""}`);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

export const api = {
  sessions: () => get<Session[]>("sessions"),
  laps: (session: string) => get<{ session: Session; laps: Lap[] }>("laps", { session }),
  compare: (p: { session: string; lap: number; ref: string; ref_session?: string; ref_lap?: number }) =>
    get<Comparison>("compare", p),
  live: () => get<LiveStatus>("live"),
};
