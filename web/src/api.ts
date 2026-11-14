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
  track_model: TrackModel | null;
  track_model_error: string | null;
  corners: SegmentResult[];
  findings: Finding[];
  corner_definitions: Record<string, string>;
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

export type SegmentDef = {
  id: string;
  name: string;
  kind: "corner" | "complex" | "flat" | "straight";
  start_m: number;
  end_m: number;
  apexes_m: number[];
};

export type TrackModel = {
  key: string;
  track_name: string;
  status: "draft" | "confirmed";
  version: number;
  synthetic: boolean;
  length_m: number;
  source: { session_key?: string; lap_index?: number; lap_number?: number; lap_time_ms?: number };
  updated_utc: string;
  segments: SegmentDef[];
};

export type CornerMetrics = {
  brake_onset_m: number | null;
  brake_release_m: number | null;
  braking_distance_m: number | null;
  entry_speed_kmh: number | null;
  peak_brake: number | null;
  time_to_peak_s: number | null;
  release_rate: number | null;
  trail_brake_m: number;
  lift_m: number | null;
  min_speed_kmh: number;
  min_speed_m: number;
  gear_at_apex: number | null;
  throttle_pickup_m: number | null;
  full_throttle_m: number | null;
  hesitation: number;
  coasting_m: number;
  exit_speed_kmh: number;
  lockup_m: number | null;
  wheelspin_m: number | null;
};

export type SegmentResult = {
  segment: SegmentDef;
  lap_time_s: number;
  ref_time_s: number;
  loss_s: number;
  carried_s: number;
  carried_to: string | null;
  residual_s: number | null;
  total_s: number;
  phases: Record<string, number>;
  phase_bounds_m: number[];
  lap: CornerMetrics | null;
  ref: CornerMetrics | null;
};

export type Finding = {
  code: string;
  segment_id: string;
  segment_name: string;
  title: string;
  detail: string;
  phase: string;
  phases: string[];
  time_impact_s: number;
  confidence: "high" | "medium";
  measured: Record<string, number | string | null>;
  evidence: { start_m: number; end_m: number };
  suggestion: string | null;
};

export type Consistency = {
  track_model: TrackModel;
  laps: number;
  lap_numbers: number[];
  ideal_lap_s: number | null;
  note: string | null;
  segments: {
    id: string;
    name: string;
    kind: string;
    best_s: number;
    best_lap_number: number;
    mean_s: number;
    worst_s: number;
    std_s: number;
    inconsistent: boolean;
    times_s: number[];
  }[];
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

async function get<T>(
  path: string,
  params: Record<string, string | number | undefined> = {},
  init?: RequestInit,
) {
  const query = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined) query.set(k, String(v));
  const qs = query.toString();
  const res = await fetch(`api/${path}${qs ? `?${qs}` : ""}`, init);
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
  consistency: (session: string) => get<Consistency>("consistency", { session }),
  confirmTrack: (key: string) =>
    get<TrackModel>(`tracks/${encodeURIComponent(key)}/confirm`, {}, { method: "POST" }),
};
