# Pitwall AI

Personal Formula 1 race engineer for **F1 25 on PS5**. Records your telemetry over UDP, analyses
your laps corner by corner, benchmarks them against real F1 drivers (FastF1, 2025 season) and
writes evidence-backed coaching reports. Runs locally on a Mac.

- **How to use it:** [docs/user-guide.md](docs/user-guide.md)
- **Development plan:** [docs/plan.md](docs/plan.md)

## Status

| Stage | Scope | State |
|---|---|---|
| 0 | Capture kit: raw UDP recording, forwarding, health checks, replay | done |
| 1 | Packet decoder, sessions and laps | done (awaiting validation on real recordings) |
| 2 | Lap processing, self-comparison, first UI | planned |
| 3 | Track model, corner analysis engine | planned |
| 4 | FastF1 reference, pro benchmark | planned |
| 5 | Race engineer reports (deterministic, then local LLM) | planned |
| 6 | Live telemetry | planned |
| 7 | Driver development and history | planned |

## Quick start (Mac)

1. Install [uv](https://docs.astral.sh/uv/):

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. Get the code and install it:

   ```bash
   git clone https://github.com/Samarthjo/AI-Race-Engineer.git && cd AI-Race-Engineer && uv sync
   ```

3. Check the Mac is ready and see which IP to enter on the PS5:

   ```bash
   uv run pitwall doctor
   ```

4. Set up the PS5 and network as described in [docs/setup-ps5-mac.md](docs/setup-ps5-mac.md).

5. Record (keeps the Mac awake while capturing; Ctrl+C to stop):

   ```bash
   caffeinate -i uv run pitwall capture run --label monza-tt-clean
   ```

6. Inspect what was recorded:

   ```bash
   uv run pitwall capture list
   ```

   ```bash
   uv run pitwall capture stats <capture-id>
   ```

7. Turn recordings into laps and look at them:

   ```bash
   uv run pitwall ingest
   ```

   ```bash
   uv run pitwall laps latest
   ```

Then follow the recording checklist in [docs/golden-sessions.md](docs/golden-sessions.md).

## Commands

| Command | Purpose |
|---|---|
| `pitwall doctor` | Data dir, UDP port, LAN IP, PS5 settings to enter |
| `pitwall capture run` | Record every UDP packet raw, forward a copy to `127.0.0.1:20778`, live health line |
| `pitwall capture stats <id>` | Packet rates, UDP format, sessions, estimated packet loss, flashbacks, pauses |
| `pitwall capture list` | All captures with label, status and loss |
| `pitwall capture replay <id>` | Re-send a capture over UDP (real time or `--speed 0`) |
| `pitwall capture compact <id>` | Compress leftover raw segments after a crash (lossless, verified) |
| `pitwall ingest` | Decode new captures into sessions and laps |
| `pitwall sessions` | List sessions |
| `pitwall laps <session\|latest>` | Lap times, sectors, validity, flashbacks, data quality |
| `pitwall export <session> --lap N` | Per-frame telemetry of a lap to CSV/Parquet |
| `pitwall decode <id>` | Print decoded packets as JSON |

## Data

Default data directory: `~/PitwallData` (override with `--data-dir`, `PITWALL_DATA_DIR`, or
`~/.config/pitwall/config.toml`, see [docs/config.example.toml](docs/config.example.toml)).

```
~/PitwallData/raw/<capture_id>/
    capture.json          manifest: settings, label, note, final health report
    seg_00000.pwraw.zst   raw packets (never modified; compressed losslessly after rotation)
```

```
~/PitwallData/pitwall.db                                 sessions + laps catalogue
~/PitwallData/sessions/<capture_id>/<session>/frames.parquet  decoded player telemetry per frame
```

Raw captures are the foundation: every later layer (laps, metrics, findings, reports) can be
rebuilt from them, so improving the analysis never requires re-driving a lap.

## Development

```bash
uv run pytest
```

```bash
uv run ruff check . && uv run ruff format --check .
```

```bash
uv run pyright
```
