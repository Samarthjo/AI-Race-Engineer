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

/** What the charts and the track map need: one lap and a reference on one distance axis. */
export type TraceView = { session: Session; trace: Comparison["trace"] };

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
  basis?: "self" | "pro_normalised";
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

// ---------------------------------------------------------------- real-F1 references

export type Capabilities = {
  brake: "binary" | "pressure";
  steering: boolean;
  wheel_slip: boolean;
  car_interval_s: number;
  car_max_gap_s: number;
  pos_interval_s: number;
  pos_max_gap_s: number;
  car_samples: number;
  pos_samples: number;
};

export type RefSummary = {
  key: string;
  provider: string;
  synthetic: boolean;
  season: number;
  event: string;
  session: string;
  session_part: string | null;
  driver: string;
  driver_number: string | null;
  driver_name: string | null;
  team: string | null;
  lap_number: number;
  lap_time_ms: number;
  sectors_ms: (number | null)[];
  compound: string | null;
  tyre_life: number | null;
  track_status: string | null;
  capabilities: Capabilities;
  checks: { notes?: string[] } & Record<string, unknown>;
  fetched_utc: string;
  current: boolean;
};

export type RefOptions = {
  stored: RefSummary[];
  season: number | null;
  event: string | null;
  synthetic?: boolean;
  sessions?: string[];
  error: string | null;
};

export type RefDriver = {
  driver: string;
  team: string | null;
  lap_number: number;
  lap_time_ms: number | null;
  session_part: string | null;
};

export type Confidence = "high" | "medium" | "within uncertainty" | "n/a";

export type MetricCompare = {
  key: string;
  label: string;
  unit: "m" | "km/h";
  lap: number | null;
  ref: number | null;
  ref_raw: number | null;
  ref_sigma: number | null;
  delta: number | null;
  offset: number | null;
  normalised: number | null;
  sigma: number | null;
  z: number | null;
  confidence: Confidence;
  note: string | null;
};

export type SegmentBench = {
  segment: SegmentDef;
  lap_time_s: number;
  ref_time_s: number;
  loss_s: number;
  loss_sigma_s: number;
  expected_s: number | null;
  normalised_s: number | null;
  normalised_sigma_s: number | null;
  z: number | null;
  confidence: Confidence;
  metrics: Record<string, MetricCompare>;
  ref_notes: string[];
};

export type Registration = {
  reflected: boolean;
  rotation_deg: number;
  scale: number;
  rms_m: number;
  median_m: number;
  p95_m: number;
  max_m: number;
  start_offset_m: number;
  length_ratio: number;
  points: number;
  quality: "good" | "check" | "poor";
  notes: string[];
  along_sigma_m: number;
};

export type BenchmarkData = {
  session: Session;
  lap: Lap & { metrics: Metrics; max_gap_m: number };
  reference: RefSummary & {
    label: string;
    trace_time_s: number;
    official_sectors_ms: (number | null)[];
    sectors_on_your_lines_s: number[] | null;
    sources: { url?: string; sha256?: string; fetched_utc?: string }[];
  };
  delta_s: number;
  sectors: Sector[];
  largest_losses: Window[];
  largest_gains: Window[];
  registration: Registration;
  systematic: {
    time: Record<string, { segments: number; pace_ratio: number | null; pace_pct?: number }>;
    metrics: Record<string, { offset: number | null; unit: "m" | "%"; corners: number }>;
  };
  segments: SegmentBench[];
  findings: Finding[];
  strengths: { segment_id: string; segment_name: string; normalised_s: number; sigma_s: number }[];
  not_assessed: string[];
  metric_labels: Record<string, { label: string; unit: string }>;
  track_model: TrackModel;
  trace: Comparison["trace"] & { ref_x: Series; ref_z: Series; ref_sample_m: number[] };
};

// ------------------------------------------------------------------- race engineer

export type Fact = {
  id: string;
  kind: string;
  text: string;
  segment_id: string | null;
  segment_name: string | null;
  basis: string;
  estimate: boolean;
  code: string | null;
  data: Record<string, unknown>;
};

export type FocusItem = {
  rank: number;
  fact_id: string;
  segment_id: string;
  segment_name: string;
  title: string;
  instruction: string;
  measured: string;
  impact_s: number;
  confidence: string;
  basis: string;
  recurring?: string | null; // history fact id when this is a recurring pattern
};

export type NarrativePart = { title?: string; text: string; cites: string[] };
export type NarrativeOutput = { summary: NarrativePart; sections: NarrativePart[]; focus: NarrativePart[] };
export type AnswerOutput = { answer: string; cites: string[]; refused: boolean };
export type CheckLog = {
  attempt: number;
  violations: { kind: string; where: string; detail: string }[];
  repaired_citations?: boolean;
  error?: string;
};

export type Narrative = {
  narrative_id: string;
  status: "grounded" | "fallback" | "unavailable";
  output: NarrativeOutput;
  attempts: number;
  log: CheckLog[];
  provider: string;
  model: string;
  prompt_version: string;
  duration_s: number;
  error: string | null;
};

export type Answer = {
  narrative_id: string;
  status: "grounded" | "refused" | "fallback" | "unavailable";
  question: string;
  output: AnswerOutput;
  attempts: number;
  tool_calls: { name: string; arguments: Record<string, unknown> }[];
  log: CheckLog[];
  model: string;
  duration_s: number;
  error: string | null;
};

export type StoredText = {
  narrative_id: string;
  kind: "report" | "answer";
  question: string | null;
  status: string;
  model: string;
  prompt_version: string;
  output: NarrativeOutput | AnswerOutput;
  created_utc: string;
};

export type EngineerReport = {
  version: string;
  headline: string;
  lap: { lap_index: number; lap_number: number; lap_time_ms: number | null; usable: boolean };
  reference: { kind: string; label: string; lap_time_ms: number | null };
  delta_s: number;
  facts: Fact[];
  focus: FocusItem[];
  losses: { fact_id: string; segment_id: string; segment_name: string; loss_s: number; causes: string[]; cause_titles: string[] }[];
  strengths: string[];
  consistency: string[];
  notes: string[];
  history_facts: string[];
  pro: { key: string; driver: string; label: string; lap_time_ms: number; findings: string[] } | null;
  text: string;
  history: StoredText[];
};

export type EngineerStatus = {
  provider: string;
  model: string;
  base_url: string;
  reachable: boolean;
  version: string | null;
  installed: string[];
  model_installed: boolean;
  error: string | null;
};

export type Wheels = { rl: number; rr: number; fl: number; fr: number };

export type LiveLapSummary = {
  number: number;
  lap_time_ms: number | null;
  sectors_ms: (number | null)[];
  valid: boolean;
  kind: string;
  usable: boolean;
  issues: string[];
  flashbacks: number;
  loss_pct: number;
  delta_s: number | null;
  sector_deltas: (number | null)[];
  reference: string | null;
  report: {
    status: "ready" | "skipped";
    reason?: string;
    provisional?: boolean;
    reference?: string;
    headline?: string;
    delta_s?: number;
    focus?: FocusItem[];
    losses?: { segment_name: string; loss_s: number; causes: string[] }[];
    notes?: string[];
  } | null;
};

export type LiveRef = { key: string; kind: string; label: string; lap_time_ms: number; estimate: boolean };

export type LiveStatus = {
  seq?: number;
  listening: boolean;
  receiving: boolean;
  error?: string | null;
  port?: number;
  last_packet_age_s?: number | null;
  packets_per_s?: number;
  loss_pct?: number;
  decode_errors?: number;
  session?: {
    session_uid: string;
    track_id: number;
    track_name: string;
    track_length_m: number;
    session_type_name: string;
    sector2_start_m: number;
    sector3_start_m: number;
    weather: string;
    track_temp_c: number;
    air_temp_c: number;
    synthetic: boolean;
  } | null;
  track_name?: string;
  session_type_name?: string;
  reference?: LiveRef | null;
  reference_choice?: string;
  reference_note?: string | null;
  current_lap?: { number: number; flashbacks: number; started_at_line: boolean } | null;
  laps?: LiveLapSummary[];
  lap_number?: number;
  lap_distance_m?: number;
  current_lap_time_ms?: number;
  last_lap_time_ms?: number | null;
  sector?: number;
  lap_invalid?: boolean;
  delta_s?: number | null;
  predicted_lap_ms?: number | null;
  sector_deltas?: (number | null)[];
  speed_kmh?: number;
  gear?: number;
  rpm?: number;
  max_rpm?: number;
  rev_lights_pct?: number;
  throttle?: number;
  brake?: number;
  steer?: number;
  drs?: boolean;
  drs_allowed?: boolean;
  x?: number;
  z?: number;
  tyre_surface_c?: Wheels;
  tyre_inner_c?: Wheels;
  tyre_pressure_psi?: Wheels;
  tyre_wear_pct?: Wheels;
  brake_temp_c?: Wheels;
  tyre_compound?: string;
  tyre_age_laps?: number;
  fuel_kg?: number;
  fuel_laps?: number;
  ers_store_mj?: number;
  ers_mode?: number;
};

export type LiveTrack = {
  available: boolean;
  track_length_m?: number;
  distance?: Series;
  x?: Series;
  z?: Series;
  segments?: SegmentDef[];
};

// ---------------------------------------------------------------- history (Stage 7)

export type ChangeStatus = "improved" | "worse" | "unchanged" | "not enough data";

export type Change = {
  status: ChangeStatus;
  before: number | null;
  after: number | null;
  delta: number | null;
  sigma: number | null;
  threshold: number | null;
  n_before: number;
  n_after: number;
};

export type HistoryGroup = {
  id: string;
  track_key: string;
  track_name: string;
  formula: number | null;
  formula_name: string;
  synthetic: boolean;
  sessions: number;
  laps: number;
  first_date: string | null;
  last_date: string | null;
  best_lap_ms: number;
  best: { session_key: string; lap_index: number; lap_number: number; date: string | null };
  latest_best_lap_ms: number;
};

export type Pattern = {
  code: string;
  segment_id: string;
  segment_name: string;
  title: string;
  laps: number;
  of: number;
  share: number;
  sessions: number;
  of_sessions: number;
  mean_impact_s: number;
};

export type Recommendation = {
  code: string;
  segment_id: string;
  segment_name: string;
  title: string;
  suggestion: string | null;
  laps: number;
  of: number;
  mean_impact_s: number;
  metric: string;
  metric_words: string;
  unit: string;
  better: "higher" | "lower";
  min_change: number;
  session_key: string;
  session_date: string | null;
  outcome: "improved" | "worse" | "unchanged" | "inconclusive" | "open";
  metric_change?: Change;
  segment_time?: Change;
  next_session_key: string | null;
  next_session_date?: string | null;
};

export type OpenRecommendation = Omit<Recommendation, "outcome" | "next_session_key"> & {
  group: string;
  track_name: string;
  synthetic: boolean;
};

export type DashboardGroup = HistoryGroup & {
  new_best_in_latest: boolean;
  trend: { status: ChangeStatus; delta: number | null; before: number | null; after: number | null };
  top_pattern: Pattern | null;
  series: { date: string | null; best_lap_ms: number; median_s: number }[];
};

export type CacheState = {
  analysis_version: string;
  sessions: number;
  by_status: Record<string, number>;
  problems: { session_key: string; track_key: string; status: string; reason: string | null }[];
};

export type Dashboard = {
  totals: {
    sessions: number;
    sessions_with_laps: number;
    real_sessions: number;
    synthetic_sessions: number;
    usable_laps: number;
    synthetic_usable_laps: number;
    tracks: number;
    synthetic_tracks: number;
  };
  built: { sessions: number; laps: number; seconds: number };
  latest_session: {
    session_key: string;
    date: string | null;
    track_name: string | null;
    label: string | null;
    synthetic: boolean;
    usable_laps: number | null;
    best_usable_lap_ms: number | null;
  } | null;
  groups: DashboardGroup[];
  open_recommendations: OpenRecommendation[];
  cache: CacheState;
  thresholds: Record<string, number | string>;
};

export type HistorySession = {
  session_key: string;
  date: string | null;
  label: string | null;
  laps: number;
  best_lap_ms: number;
  best_lap_number: number;
  median_s: number;
  spread_s: number | null;
  ideal_s: number | null;
  lap_times_s: number[];
  team_id: number | null;
  weather: string;
  track_temp_c: number | null;
};

export type CornerGap = {
  segment_id: string;
  segment_name: string;
  kind: string;
  zone?: string[];
  zone_name?: string;
  best_s: number;
  typical_s: number;
  mean_gap_s: number;
  median_gap_s: number;
  spread_s: number | null;
  laps: number;
};

export type ProCorner = {
  segment_id: string;
  segment_name: string;
  normalised_s: number;
  sigma_s: number;
  verdict: "weaker" | "stronger" | "within uncertainty";
};

export type ProHistory =
  | { key: string; error: string }
  | {
      key: string;
      error?: undefined;
      driver: string;
      season: number;
      event: string;
      session: string;
      synthetic: boolean;
      lap: { session_key: string; lap_index: number; lap_number: number; lap_time_ms: number };
      ref_lap_time_ms: number;
      corners: ProCorner[];
      weakest: ProCorner[];
      strongest: ProCorner[];
      rule: string;
    };

export type TrackHistory = {
  group: HistoryGroup;
  model: { key: string; version: number; status: string; segments: { id: string; name: string; kind: string }[] } | null;
  excluded_laps: number;
  sessions: HistorySession[];
  trend: Change & {
    what: string;
    min_laps: number;
    min_change_s: number;
    sessions_before?: number;
    reason?: string;
    caveats?: string[];
  };
  segment_trends: (Change & { segment_id: string; segment_name: string; kind: string })[];
  corners: {
    status: "ok" | "not enough data";
    recent_laps: number;
    min_laps: number;
    segments: CornerGap[];
    corners: CornerGap[];
    weakest: CornerGap[];
    strongest: CornerGap[];
    ideal_lap_s: number | null;
    typical_lap_s: number | null;
  };
  patterns: { status: string; compared_laps: number; window: number; min_laps: number; patterns: Pattern[] };
  recommendations: Recommendation[];
  thresholds: Record<string, number | string>;
  pro?: ProHistory;
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
  references: (session: string) => get<RefOptions>("references", { session }),
  referenceDrivers: (session: string, ref_session: string) =>
    get<{ season: number; event: string; session: string; drivers: RefDriver[] }>("references/drivers", {
      session,
      ref_session,
    }),
  fetchReference: (p: { session: string; driver: string; ref_session: string; part?: string }) =>
    get<RefSummary>("references/fetch", p, { method: "POST" }),
  benchmark: (p: { session: string; lap: number; ref: string }) => get<BenchmarkData>("benchmark", p),
  liveReferences: () =>
    get<{ options: { key: string; label: string }[]; choice: string; reference: LiveRef | null; note: string | null }>(
      "live/references",
    ),
  chooseLiveReference: (key: string) => get<{ choice: string }>("live/reference", { key }, { method: "POST" }),
  liveTrack: () => get<LiveTrack>("live/track"),
  liveDemo: (action: "start" | "stop", speed = 1) =>
    get<{ running: boolean }>("live/demo", { action, speed }, { method: "POST" }),
  engineerStatus: () => get<EngineerStatus>("engineer/status"),
  engineerReport: (p: EngineerParams) => get<EngineerReport>("engineer/report", p),
  narrate: (p: EngineerParams) => get<Narrative>("engineer/narrate", p, { method: "POST" }),
  dashboard: () => get<Dashboard>("dashboard"),
  history: (group: string, pro?: string) => get<TrackHistory>("history", { group, pro: pro || undefined }),
  buildHistory: (force = false) =>
    get<{ sessions: number; laps: number; seconds: number; cache: CacheState }>(
      "history/build",
      { force: force ? "true" : undefined },
      { method: "POST" },
    ),
  ask: (p: EngineerParams & { question: string }) =>
    get<Answer>("engineer/ask", {}, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(p),
    }),
};

export type EngineerParams = { session: string; lap: number; ref: string; ref_lap?: number; pro?: string };
