# Pitwall AI: notes for contributors and AI sessions

Personal F1 race engineer for F1 25 (PS5) telemetry. Runs locally on a Mac Mini M4.

- **Plan and architecture:** `docs/plan.md` (approved). Work proceeds stage by stage.
- **User guide:** `docs/user-guide.md`. It is a living document.

## Agreed decisions and hardware

- Cars: base **2025** cars, so the **F1 25 UDP format** (packetFormat 2025) and the **2025 FastF1
  season** as reference. The 2026 Season Pack format is postponed.
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
   `uv run pyright`, `uv run pytest`.

## Principles (from the plan)

- Accuracy over presentation. Every finding must come from measured data; when data is
  insufficient, say so instead of guessing.
- Raw captures (`raw/`, L0) are never modified. Every derived layer must be rebuildable from
  them. Bump `PIPELINE_VERSION` (`src/pitwall/ingest/pipeline.py`) whenever derived outputs
  change, so `pitwall ingest` re-processes old recordings.
- Keep raw data, calculated metrics and AI-generated text strictly separate.
- Never assume F1 25 and real F1 physics are identical.
- F1 25 UDP layouts live in `src/pitwall/f1udp/f125.py` and are size-checked at import. Field
  names follow EA's spec in snake_case. Wheel order is RL, RR, FL, FR.

## Layout

```
src/pitwall/capture/   UDP capture, raw log (L0), health checks, replay
src/pitwall/f1udp/     packet layouts (f125.py), decoder, enum names
src/pitwall/ingest/    sessions, flashback/pause handling, laps, frame tables (L1/L2)
src/pitwall/storage/   SQLite catalogue
src/pitwall/cli.py     `pitwall` command
tests/                 synthetic sessions with known answers (tests/synth_session.py)
```
