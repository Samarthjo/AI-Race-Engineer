# Pitwall AI: notes for contributors and AI sessions

Personal F1 race engineer for F1 25 (PS5) telemetry. Runs locally on a Mac Mini M4.

- **Plan and architecture:** `docs/plan.md` (approved). Work proceeds stage by stage.
- **User guide:** `docs/user-guide.md`. It is a living document.

## Agreed decisions and hardware

- Cars: base **2025** cars, so the **F1 25 UDP format** (packetFormat 2025) and the **2025 FastF1
  season** as reference. The 2026 Season Pack format is postponed.
- Reference data: FastF1's 2025 data read from the public TracingInsights archive
  (`pitwall.reference`), because F1's own archive needs an F1 TV login since Oct 2026.
  Downloaded files go to the raw cache unchanged; real F1 data is never committed to the
  repository (tests use `pitwall.synth.pro`). Open items live in `docs/unfinished.md`.
- Driver input: **DualSense controller**, **no assists**, Time Trial first, **Monza** first.
- UI: React + TypeScript (Stage 2+). Backend: Python, FastAPI.
- AI engineer: **local model first** (Ollama), behind an `LLMProvider` interface; Claude API
  adapter later if needed.
- Hardware: Mac mini **M4 Pro, 12-core CPU, 16-core GPU, 16-core Neural Engine, 24 GB**
  unified memory. Budget roughly 14–16 GB for a local model plus context (to be measured).
- Repository: https://github.com/Samarthjo/AI-Race-Engineer

## Rule: after every development stage (or user-visible change)

1. Update the affected sections of `docs/user-guide.md` (commands, outputs, troubleshooting).
2. **Append** an entry to its "Release notes" section: what's new, how to use it, limitations.
   Never rewrite earlier entries.
3. Update the status table in `README.md` and in the user guide.
4. All checks must pass: `uv run ruff check .`, `uv run ruff format --check .`,
   `uv run pyright`, `uv run pytest`, and for UI changes `cd web && npm run build`
   (the build output in `src/pitwall/api/static/` is committed; CI checks it is current).

## Principles (from the plan)

- Accuracy over presentation. Every finding must come from measured data; when data is
  insufficient, say so instead of guessing.
- Raw captures (`raw/`, L0) are never modified. Every derived layer must be rebuildable from
  them. Bump `PIPELINE_VERSION` (`src/pitwall/ingest/pipeline.py`) whenever derived outputs
  change, so `pitwall ingest` re-processes old recordings.
- Keep raw data, calculated metrics and AI-generated text strictly separate.
- Never assume F1 25 and real F1 physics are identical.
- Synthetic data (`pitwall.synth`, `pitwall demo`) is always marked `synthetic` and must never
  mix with real laps (personal bests, history, references).
- Comparisons with real F1 data check capability flags first (binary brake, no steering, no
  wheel slip) and carry measured uncertainty; pro findings use the physics-normalised values
  (`analysis/benchmark.py`). Bump `REFERENCE_PIPELINE_VERSION` (`reference/store.py`) when
  the normalised reference format changes.
- The AI never sees telemetry, only facts (`engineer/facts.py`, built by `engineer/report.py`).
  Every AI text passes `engineer/validate.py` (numbers, corners, laps, direction, estimates,
  topics) or is replaced by the deterministic report; AI texts are stored separately
  (`narratives` table) with model and prompt version. Bump `PROMPT_VERSION` /
  `QA_PROMPT_VERSION` when prompts change and `REPORT_VERSION` when the facts change; keep the
  golden report (`tests/golden/`) reviewed.
- Findings are deterministic rules with documented thresholds (`analysis/findings.py`); each
  keeps measurement and suggestion separate, and impacts within one corner may overlap.
  Never emit a finding below its threshold or without the channels it needs.
- UI colours follow the validated palette in `web/src/styles.css` (blue = this lap, orange =
  reference, blue/red diverging for gain/loss); never use colour as the only cue.
- F1 25 UDP layouts live in `src/pitwall/f1udp/f125.py` and are size-checked at import. Field
  names follow EA's spec in snake_case. Wheel order is RL, RR, FL, FR.

## Layout

```
src/pitwall/capture/   UDP capture, raw log (L0), health checks, replay
src/pitwall/f1udp/     packet layouts (f125.py), decoder, enum names
src/pitwall/ingest/    sessions, flashback/pause handling, laps, frame tables (L1/L2)
src/pitwall/storage/   SQLite catalogue
src/pitwall/analysis/  traces, delta, references, lap metrics (Stage 2); corners, findings,
                       consistency (Stage 3); sampled metrics + error bars, benchmark (Stage 4)
src/pitwall/track/     track models: draft from a lap, JSON storage, confirm/rename; registration
                       (placing a real F1 lap on your track)
src/pitwall/reference/ real-F1 reference laps: archive + raw cache, lap tables, telemetry checks,
                       normalised store, era/track guards (Stage 4)
src/pitwall/engineer/  race engineer (Stage 5): facts, deterministic report, Ollama provider,
                       grounding check, narrator, Q&A tools, stored AI texts, model eval
src/pitwall/api/       FastAPI app, live status, built UI in static/
src/pitwall/synth/     synthetic track, driver model, sessions, demo, synthetic "real F1"
                       archive (never real data)
src/pitwall/cli.py     `pitwall` command
web/                   React + TypeScript UI (built into src/pitwall/api/static/)
tests/                 tests against synthetic sessions with known answers
```
