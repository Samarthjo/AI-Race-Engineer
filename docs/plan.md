# Pitwall AI — Development Plan (approved 2026-11-14)

## Context

Greenfield project (working dir empty). Goal: a local Mac Mini M4 app that records F1 25 (PS5) UDP telemetry, analyses
your laps corner-by-corner, benchmarks them against real F1 drivers via FastF1, and produces evidence-backed race-engineer
reports, with session history to track how you improve. This document covers feasibility, data limits, architecture,
stages, validation, MVP and what to postpone.

---

## 1. Feasibility verdict

| Capability | Feasible? | Confidence | Main limiter |
|---|---|---|---|
| Receive/decode/store PS5 UDP telemetry | Yes | High | LAN reliability, spec versions |
| Lap, sector, corner analysis of your own laps | Yes | High | Threshold tuning |
| You vs your own best / ideal lap | Yes | High | Needs several clean laps |
| You vs real F1 driver (FastF1) | Yes, **directional** | Medium | ~4 Hz data, binary brake, game ≠ real physics |
| Evidence-only AI engineer reports + Q&A | Yes | High | Needs strict grounding |
| Long-term driver development | Yes | High | Needs volume of sessions |
| ML driver modelling | Later | — | Needs months of data |

**Bottom line:** buildable. Most reliable coaching comes from comparing you with **your own** laps, because both sides have
full 60 Hz data and identical physics. Pro comparison stays valuable but must carry uncertainty labels and be
physics-normalised. The app should be designed around **two comparison modes**:

- **Self-benchmark**: full fidelity, drives most recommendations.
- **Pro-benchmark**: reduced fidelity, directional, labelled with confidence.

---

## 2. Data sources: what we actually get

### 2.1 F1 25 UDP (PS5)
- Packets (little-endian, packed structs, header carries `packetFormat`, `sessionUID`, `frameIdentifier`,
  `overallFrameIdentifier`, `playerCarIndex`): Motion, **Motion Ex** (player-only extended physics: wheel slip ratio/angle,
  suspension, wheel speeds, local velocity), Session, Lap Data, Event (incl. `FLBK` flashback, `SSTA`/`SEND`, speed trap),
  Participants, Car Setups, Car Telemetry, Car Status, Final Classification, Lobby Info, Car Damage, **Session History**
  (authoritative lap/sector times + validity bit flags), Tyre Sets, Time Trial, Lap Positions.
- Up to 60 Hz for high-rate packets; others at 1–20 Hz or event-driven.
- **Two UDP modes exist in F1 25:** "F1 25" and "2026 Season Pack" (since June 2026). New installs default to the 2026
  Season Pack format, which adds e.g. a *Car Telemetry 2* packet (overtake mode). `gameYear` reportedly stays `25` in both,
  so the decoder must not rely on it alone. **The decoder must be versioned per format.**
- Throttle/brake/steer are input values (0–1, −1..1), not hydraulic pressure. Assists (ABS, TC, braking assist,
  auto gearbox, racing line) change what these mean, so they get logged with every lap.
- Raw volume estimate: ~1–1.5 GB/hour uncompressed at 60 Hz with all packets (verify in Stage 0), much smaller compressed.
  Fine on the Mac.

### 2.2 FastF1 (real F1)
- Lap/sector times (ms), speed traps, compound, tyre life, track status, `Deleted` (track limits), `IsAccurate`,
  weather, `get_circuit_info()` (corner numbers + positions + angles), and car telemetry: Speed, RPM, nGear,
  Throttle (0–100), **Brake (boolean)**, DRS, plus X/Y/Z position.
- Sample rate: car data ~240 ms, position ~220 ms (≈4 Hz). At 320 km/h that's **~20 m between samples**.
- Post-session historical data currently needs **no login**. F1TV authentication is only needed for FastF1's *live* timing
  client (added after F1 locked down its live endpoints). Risk: access rules can change, so we keep our own durable copy of
  every reference lap we fetch.
- Distance from `add_distance()` comes from integrating speed and drifts. Position-based alignment is better.

### 2.3 Capability matrix: drives every design decision

| Channel | F1 25 | FastF1 | Consequence |
|---|---|---|---|
| Speed | 60 Hz | ~4 Hz | Min-speed vs pro ±(few km/h) at fast corners |
| Throttle | 0–1, 60 Hz | 0–100, ~4 Hz | Pro throttle pickup point ±1 sample (5–20 m) |
| Brake | 0–1 pedal | **on/off** | Vs pro: brake *onset & duration* only. Pressure, release, trail-braking: **self only** |
| Steering | yes | none | Self only |
| Gear / RPM / DRS | yes | yes (~4 Hz) | OK, shift points ±1 sample |
| Position XY | 60 Hz world coords | ~4 Hz own frame | Needs coordinate registration |
| G-force, slip, suspension | Motion/MotionEx | none | Self only (lock-up, wheelspin detection) |
| Tyre temp/wear/pressure | yes | compound + age | Context, self only |
| Fuel / ERS / setup | yes | none | Context only |
| Lap & sector times | exact | exact | Direct comparison OK |
| Corner definitions | none | circuit_info | Seeds our track model |

---

## 3. Key technical challenges and mitigations

1. **Common distance axis (game vs real).** Game `lapDistance` and FastF1's distance differ (track length modelling,
   integration drift, timing-line offset). *Mitigation:* per-track **track model**. Register the game XY path to the FastF1
   XY path (similarity transform via Procrustes + ICP), project both onto a canonical centreline, and build a monotonic
   distance map. Then a person checks it once per track.
2. **Coarse pro data.** *Mitigation:* every pro metric carries a resolution (≈ speed × sample interval). Findings are only
   emitted when the difference exceeds k× the combined uncertainty. Error bars are *measured empirically* by degrading your
   own 60 Hz laps to FastF1 fidelity and comparing the metrics.
3. **Game ≠ real physics; car era.** *Mitigation:*
   - Physics-normalised view: take the median offset across all corners (e.g. "you brake 12 m earlier everywhere, min speed
     −3% everywhere") as systematic. Report **deviations from that baseline** as driver-specific weaknesses.
   - Era guard: 2025 cars → 2025 season reference; 2026 Season Pack cars → 2026 season reference. Warn on mismatch.
4. **Lap integrity.** Flashbacks (`FLBK` event + `frameIdentifier` rewind), pauses, restarts (new `sessionUID`), out/in
   laps, pit, invalid laps, quitting mid-lap, UDP packet loss. *Mitigation:* explicit lap state machine plus quality flags.
   Benchmarks only use clean laps by default.
5. **Corner segmentation and thresholds.** Chicanes and complexes (Monza T1–T2) should be one segment, and flat corners are
   not braking zones. *Mitigation:* segments cover 100% of the lap (corners, complexes, straights), each split into phases
   (braking → rotation → traction). Thresholds live in versioned config and are calibrated against hand-labelled laps.
6. **Attributing time loss.** Loss on a straight is often caused by the previous corner's exit. *Mitigation:* exit-carry
   attribution, where straight loss explained by the exit-speed deficit gets assigned back to that corner.
7. **AI hallucination.** *Mitigation:* the LLM never sees raw telemetry. It only sees the findings JSON, and a validator
   rejects any number or claim that isn't traceable to a finding (§6).
8. **UDP format churn.** Patches can bump `packetVersion`, and there are two modes (F1 25 / 2026 Pack). *Mitigation:*
   versioned decoder, struct-size assertions against the spec, golden captures as regression fixtures.
9. **PS5 → Mac networking.** *Mitigation:* wired Ethernet for both if possible, DHCP reservation for the Mac, allow inbound
   UDP 20777 through the macOS firewall, and a packet-health monitor (frame gaps, rate).
10. **Dev loop without the PS5.** Development happens away from the console (e.g. this cloud container). *Mitigation:*
    ship a tiny capture tool first. You record "golden" sessions, and everything after is developed and tested with
    replay plus a synthetic packet generator.

---

## 4. Architecture

```
 PS5 (F1 25) ──UDP:20777──▶ [capture]  raw packet log (L0, append-only, rotated, zstd)
                               │  forward to localhost:20778
                               ▼
                         [api process: FastAPI]
                           ├─ live decoder ─▶ WebSocket ─▶ Live page
                           ├─ session/lap builder ─▶ L1 decoded frames (Parquet), L2 laps (SQLite + Parquet)
                           ├─ analysis engine ─▶ L3 metrics, L4 findings (SQLite)
                           ├─ reference svc (FastF1 + cache + own Parquet store)
                           ├─ track models (YAML per track, versioned)
                           ├─ engineer (deterministic report → LLM narrator → validator) ─▶ L5 narratives
                           └─ history/profile
                               ▼
                         [web UI: React]  Dashboard · Live · Lap Analysis · Benchmark · Engineer · History
```

**Capture is a separate, deliberately dumb process.** It writes raw bytes and forwards them, nothing else. API crashes or
restarts never lose packets, and `pitwall reprocess` can rebuild every derived layer from L0 whenever the analysis improves.

### Python package layout (modular, independently testable)
```
pitwall/
  capture/     UDP listener, raw log writer/reader, forwarder, health stats
  f1udp/       packet specs per format (f1_25, f1_25_2026pack), decoder, enums (tracks, compounds, assists)
  ingest/      session builder, lap state machine, flashback/pause/loss handling, validity
  reference/   FastF1 adapter, lap selection, normalisation to common schema, reference store
  track/       track model schema, calibration (auto-draft + registration), game↔ref distance mapping
  analysis/    distance-grid resampling, delta-time, segments/phases, corner metrics, findings rules, consistency
  engineer/    report assembler, recommendation templates, LLM narrator, grounding validator, Q&A tools
  history/     driver profile, trends, recurring patterns, recommendation outcome tracking
  storage/     SQLite (SQLAlchemy + Alembic), Parquet IO, raw log IO
  api/         FastAPI routes, WebSocket live feed
  cli.py       Typer: capture, replay, reprocess, ref fetch, track calibrate, analyse, serve
web/           React + TypeScript + Vite UI
tracks/        <track>.yaml track models (versioned)
tests/         unit, golden-fixture, synthetic-perturbation, replay end-to-end
```

### Data layers (Principle 3: raw vs calculated vs AI kept strictly apart)
| Layer | Content | Storage | Regenerable? |
|---|---|---|---|
| L0 Raw | Every UDP packet + receive timestamp | framed binary, zstd | **Never modified** |
| L1 Decoded | Per-packet-type tables (player car, plus all cars raw) | Parquet | from L0 |
| L2 Laps | Lap metadata (validity, times, assists, setup, tyres, fuel, conditions) + 1 m distance-grid channels | SQLite + Parquet | from L1 |
| L3 Metrics | Segment/phase/corner metrics, deltas | SQLite | from L2 + track model |
| L4 Findings | Typed findings with evidence, time impact, confidence, basis (self/pro) | SQLite | from L3 |
| L5 Narratives | Engineer text, Q&A answers, each with finding IDs, model, prompt version | SQLite | from L4 |

Every L1–L5 row carries `pipeline_version` / `analysis_version` / `track_model_version`, so we can always tell which code
produced a conclusion. Common telemetry schema with **capability flags** (e.g. `brake_is_binary`, `has_steering`), so the
analysis checks what a source can support before computing a metric (Principle 5).

### Tech stack
- Python 3.12, `uv`, ruff, pyright, pytest + hypothesis
- numpy / pandas (FastF1-native) / pyarrow / scipy
- SQLite via the standard library with numbered migrations (SQLAlchemy/Alembic only if the schema outgrows it)
- FastAPI + uvicorn + WebSockets; Typer CLI
- FastF1 (pinned version) with its cache enabled
- LLM: **Ollama (local, on the M4)** behind an `LLMProvider` interface, using JSON-schema structured output for reports
  and tool calling for Q&A. An optional Claude API adapter (`anthropic` SDK, `claude-opus-5-5`) can be added later
  without touching the engineer logic.
- Frontend: React + TypeScript + Vite, **uPlot** (fast synchronised telemetry charts at 60 Hz), SVG/Canvas track map,
  Tailwind, TanStack Query.

---

## 5. Analysis engine design (core of the product)

- **Resampling:** each lap goes onto a 1 m distance grid. `t(d)` is cumulative time, and
  `delta(d) = t_you(d) − t_ref(map(d))`. Segment loss = `delta(end) − delta(start)`, so **segment losses sum exactly to the
  lap delta** (invariant, tested).
- **Corner metrics** (game definitions; pro equivalents where the capability exists):
  - Braking: onset (brake ≥ θ for ≥ 3 frames), distance to reference apex, entry speed, peak brake, time-to-peak.
  - Release and trail: release point, release profile/slope, abrupt release under steering.
  - Speed through the corner: min speed and its location, coasting (no pedals > 0.15 s).
  - Throttle: pickup (first ≥ 0.2 after min speed), full-throttle point, hesitation (re-lift).
  - Exit speed at segment end and at +100/+200 m.
  - Gear at apex.
  - Lock-up and wheelspin from MotionEx slip ratios.
  - Time per phase.
- **Findings** are typed codes, e.g. `EARLY_BRAKE`, `OVERSLOW_ENTRY`, `LOW_MIN_SPEED`, `LATE_THROTTLE`,
  `THROTTLE_HESITATION`, `COASTING`, `ABRUPT_RELEASE`, `EXIT_DEFICIT`, `LOCKUP`, `WHEELSPIN`, `INCONSISTENT_SEGMENT`. Each one
  carries the measured values (both sides), the delta, time impact (s), confidence (high/med/low), basis (self/pro/pro-normalised),
  sample resolution and an evidence pointer (distance range). **No finding is created below the significance threshold or
  when a required capability is missing.** The system says "insufficient data" instead.
- **Recommendations** come from deterministic templates mapped from finding codes, ranked by time impact × confidence.
  Targets are incremental (a step toward the reference, not a full jump), and **measured differences are kept visually
  and semantically separate from recommendations**.
- **Consistency:** per-segment spread across clean laps, and an ideal lap (sum of best segments).

## 6. AI Race Engineer design

1. **Deterministic report first** (no LLM): ranked losses, root causes, strengths, max 3 focus items for the next lap.
   This is the ground truth, and it's useful on its own.
2. **LLM narrator:** input = findings JSON + lap context + driver profile. Output = structured sections, each citing finding
   IDs.
3. **Grounding validator:** every number, corner and claim in the output must match a cited finding (within rounding).
   On failure, regenerate once, then fall back to the deterministic report. Estimates must be worded as estimates.
4. **Q&A:** tool use over *read-only* analysis functions (`get_lap_summary`, `get_segment_findings`, `compare_laps`,
   `get_history`). Answers cite the data, and the engineer refuses when the data can't support an answer.
5. Model/prompt versions are stored with every narrative. It runs **locally via Ollama** (no cost, offline). The model is
   chosen by eval at Stage 5, and the Claude API stays available later behind the same interface if local quality falls
   short.

---

## 7. Development stages

Effort: S ≈ days, M ≈ 1–2 weeks, L ≈ 2–4 weeks of focused work. Each stage ends with a gate before the next starts.

### Stage 0 — Groundwork and capture kit [S]
- **Deliver:**
  - Repo (uv, lint, tests, CI) and config system.
  - `pitwall capture` (raw log + forwarder + live packet-health stats) and `pitwall capture stats`.
  - PS5/Mac setup guide: UDP On, Broadcast Off, IP = Mac, port 20777, 60 Hz, chosen UDP format; DHCP reservation;
    firewall; wired network.
  - Official UDP spec (both formats) obtained and committed as reference.
- **Your task:** record golden sessions from a checklist:
  - 5+ clean Monza Time Trial laps
  - a lap with a flashback
  - pause/resume
  - a session restart
  - a pit in/out
  - a track-limits invalid lap
  - quit mid-lap

  Note the in-game lap times as you go.
- **Validate:**
  - Per-packet rates match the send rate.
  - Frame gaps under 0.5% on wired.
  - 60-min soak run with no loss of file integrity.
  - Replay reproduces byte-identical streams.
- **Gate:** golden dataset exists and becomes the test fixtures.

### Stage 1 — Decoder and session/lap builder [M]
- **Deliver:**
  - Versioned decoder for all packet types, with struct-size assertions and enums.
  - Replay engine (real-time / max speed).
  - Lap state machine:
    - laps from Lap Data
    - validity from `currentLapInvalid` + Session History flags
    - flashback truncation
    - pause, out/in/pit tagging
    - incomplete-lap flag
    - packet-loss quality score
  - L1/L2 storage; per-lap assists, setup, tyres, fuel, weather.
  - CLI to list sessions and laps.
- **Validate:**
  - Decode snapshot tests; fuzzing (truncated or garbage packets never crash).
  - **Lap times == Session History ms exactly**, and match the in-game times you noted.
  - Flashback fixture is flagged and its trace is monotonic.
  - Reprocessing is deterministic (hash-stable).
- **Gate:** every golden lap classified correctly.

### Stage 2 — Lap processing, self-comparison, first UI [M]
- **Deliver:**
  - Distance-grid resampler and delta-time.
  - Lap overview metrics (sectors, top speed, full-throttle %, braking %, coasting).
  - Compare against session best, personal best and theoretical best.
  - FastAPI endpoints.
  - React shell and **Lap Analysis** page: synchronised speed/throttle/brake/gear/steer/delta charts, plus a track map
    coloured by delta with a linked cursor.
  - Basic live status (packets arriving, current lap).
- **Validate:**
  - Lap vs itself → zero delta.
  - Final delta = lap-time difference (±5 ms).
  - Synthetic time-shift tests.
  - Your visual check against the game.
- **Gate:** you can open any lap and compare it with your best.

### Stage 3 — Track model and corner analysis engine [L]
- **Deliver:**
  - Track-model schema and calibration tool. It auto-drafts segments from your laps (speed minima, XY curvature, braking
    zones) and FastF1 corner numbering, then you confirm them on a map overlay.
  - Segments/phases covering 100% of the lap.
  - All corner metrics.
  - Exit-carry attribution.
  - Findings engine.
  - Consistency stats.
  - Corner table in the UI.
  - First track: Monza.
- **Validate:**
  - Σ segment loss == lap delta.
  - Hand-labelled ~20 braking/throttle points: detector error ≤ 3 m.
  - Perturbation tests: inject the brake 15 m earlier → exactly one `EARLY_BRAKE` of ~15 m.
  - Two near-identical laps → no spurious findings.
- **Gate:** you review findings on the golden laps and agree they're right.

### Stage 4 — FastF1 reference and pro benchmark [L]
- **Deliver:**
  - Reference service: season → GP → Q/R (Q1/Q2/Q3 split) → driver → fastest valid lap (excluding deleted and inaccurate
    laps).
  - FastF1 cache plus our own Parquet reference store.
  - Common-schema normalisation with capability flags.
  - Game↔real registration and distance map per track.
  - Uncertainty model.
  - Physics-normalised view.
  - Era guard.
  - **Benchmark** page:
    - lap/sector table
    - overlays and track map
    - corner table with *measured* vs *normalised* columns and confidence badges
- **Validate:**
  - Reference lap time equals the official classification.
  - Mapped track length is consistent.
  - Corner positions overlay within ~10–15 m.
  - **Degradation test:** your 60 Hz lap degraded to 4 Hz / binary brake → metric error bars (these feed the UI
    uncertainty); self vs degraded-self produces no false findings.
- **Gate:** e.g. Monza 2025 Q, Verstappen, renders with justified confidence labels.

### Stage 5 — Race Engineer [M]
- **Deliver:**
  - **5a** deterministic report.
  - **5b** LLM narrator plus grounding validator.
  - **5c** interactive Q&A with tools.
  - **Engineer** page.
- **Validate:**
  - Golden report snapshots.
  - Adversarial tests: an injected fake number gets rejected.
  - Insufficient-data cases produce explicit refusals.
  - An eval set of ~20 laps that you rate for accuracy and usefulness.
- **Gate:** zero unsupported claims in the eval set.

### Stage 6 — Live telemetry [M]
- **Deliver:**
  - WebSocket feed (~20–30 Hz): inputs, speed, gear, RPM, tyres, fuel, ERS, sectors, packet health.
  - **Live delta** vs selected reference (yours or pro).
  - Auto-analysis on lap completion ("report ready" within seconds).
- **Validate:**
  - LAN → UI latency under 100 ms.
  - Live delta at the line == post-lap delta.
  - Replay-driven UI tests.

### Stage 7 — Driver development and history [M]
- **Deliver:**
  - **Dashboard** and **History** pages.
  - Per-track/segment trends.
  - Strongest/weakest corners (vs ideal lap and vs normalised pro).
  - Recurring-pattern detection (finding frequency with minimum sample sizes).
  - **Recommendation tracking:** each recommendation is linked to a target metric and evaluated next session as
    improved / unchanged / worse / inconclusive.
  - Driver profile fed to the engineer.
- **Validate:**
  - Synthetic histories with known trends are detected.
  - No conclusions drawn from fewer than N laps.

### Stage 8+ — Advanced (see §9)

---

## 8. Minimum viable version

**MVP = Stages 0–4 + 5a**, scoped to:

- **one track** (Monza)
- **Time Trial**
- **2025 cars, F1 25 UDP format, no assists, controller**
- **qualifying references**
- UI: Lap Analysis, Benchmark, Engineer (deterministic report), plus a basic live status

It already delivers the full primary workflow (§3 of your brief): drive → laps stored → pick lap → pick pro → corner-level
losses with evidence → ranked next-lap instructions. LLM narration (5b) follows immediately after the MVP.

## 9. Postpone until the core works

- LLM Q&A (5c)
- Full live page with live delta (6)
- History, trends and recommendation tracking (7)
- Tracks beyond 2–3 (each needs calibration)
- Race-session analysis (tyre degradation, fuel, traffic, strategy)
- Setup analysis or recommendations
- ML (style clustering, gain prediction, anomaly detection)
- Live voice "radio" engineer while driving
- Wet weather
- Multiplayer/other cars' data
- Second car era
- Desktop packaging (Tauri)
- Mobile/cloud sync
- 3D visualisation

## 10. Agreed decisions and their impact

| # | Decision | Impact on the plan |
|---|---|---|
| 1 | **Base 2025 cars** | Decoder targets the **F1 25 UDP format** first; 2026 Pack format postponed. In-game UDP Format setting must read F1 25 (verified in Stage 0 via header `packetFormat`). Reference = **2025 FastF1 season** only; era guard rejects 2026 references. |
| 2 | **Controller (DualSense)** | Triggers are analog 0–1, so brake/throttle modulation and trail-braking analysis stay valid. Steering is stick input processed by the game's controller settings (deadzone, linearity, saturation; not in UDP), so steering-based findings get lower confidence and coaching emphasises pedal/trigger technique and lines over steering finesse. Session form records controller settings manually so we know when they change. |
| 3 | **No assists** | Full brake analysis valid. ABS off → **lock-up detection** (MotionEx front slip ratio + tyre damage) becomes a priority finding. TC off → **wheelspin on exit** detection is a priority. Manual gears → **shift-point and downshift** analysis is in scope. Assist state still logged per lap; any lap with assists on gets flagged. |
| 4 | **Game mode** | Time Trial first (default kept). |
| 5 | **First tracks** | **Monza** (default kept; your brief's example). Second track chosen at the end of Stage 3. |
| 6 | **UI** | **React + TypeScript** (your call was "anything"; picked for synced 60 Hz charts and the live view). |
| 7 | **LLM: local first** | Local model on the Mac Mini M4 via **Ollama** (JSON-schema structured output + tool calling). The model is picked at Stage 5 by an eval on your laps, sized to the Mac's RAM. `LLMProvider` interface keeps a Claude API adapter as a later option, with no rewrite. Because local models are weaker, the deterministic report (5a) stays the source of truth and the grounding validator is mandatory. |
| 8 | **Private GitHub repo** | `pitwall-ai`, private, created at the start of Stage 0. Code is pushed from here and pulled/run on your Mac. |

**Resolved since approval:**
- Hardware: Mac mini with **M4 Pro (12-core CPU, 16-core GPU, 16-core Neural Engine), 24 GB
  unified memory**. For Stage 5, macOS and the app need part of that memory: plan on roughly
  14–16 GB for the local model plus its context (a planning estimate, measured in Stage 5).
  That points to mid-size instruct models (roughly 7–14B parameters, 4-bit quantised) with
  tool calling. The final pick is made by the Stage 5 eval, not by size alone.
- Code hosting: https://github.com/Samarthjo/AI-Race-Engineer
- Reference data source (Stage 4): since about 15 Oct 2026 Formula 1's timing archive needs an
  F1 TV login (for FastF1 too) and FastF1's mirror has no 2025 data. The reference service
  therefore reads FastF1's processed 2025 data from the public TracingInsights archive
  (github.com/TracingInsights/2025), behind the same raw cache and normalised store; FastF1
  with an F1 TV login and OpenF1 stay possible later sources (docs/unfinished.md).

**Still open (not blocking now):** second track (end of Stage 3).

## 11. Risks

- **Misleading pro comparisons:** uncertainty and normalisation layers, plus explicit labels.
- **Game patch changes the UDP spec:** versioned decoder plus golden fixtures.
- **F1/FastF1 access changes:** pinned version plus our own reference store.
- **Track registration fails on a circuit:** manual calibration fallback.
- **Scope creep:** stage gates.
- **Local LLM writes weak or ungrounded prose:** the deterministic report stays authoritative, the validator rejects
  anything it can't trace, and the Claude API adapter is the fallback.
- **Controller steering hides intent:** steering findings carry low confidence, and coaching focuses on triggers, braking
  points and lines.
- **Dev environment:** this cloud container can't reach forums.ea.com or docs.fastf1.dev (and likely not the F1 data
  hosts). Real FastF1 fetches and UDP capture run on your Mac, and development here uses recorded fixtures.

## 12. Verification strategy (across stages)

- **Unit tests** per module.
- **Golden-fixture tests** from your real captures.
- **Synthetic perturbation tests:** known injected errors must be found, and nothing else.
- **Invariants:**
  - Σ segment loss = lap delta
  - lap-vs-self = 0
  - lap times == Session History
- **Degradation test** for pro-comparison error bars.
- **End-to-end replay test:** golden raw log → `pitwall reprocess` → expected laps, findings and report snapshot.
- **Manual review gates** with you at Stages 0, 3, 4 and 5.

## 13. First steps after approval

1. Create the private `pitwall-ai` GitHub repo and the Stage 0 skeleton.
2. Build `pitwall capture` and write the PS5/Mac network setup guide.
3. You record the golden sessions on the Mac.
4. Start the Stage 1 decoder against those captures.
