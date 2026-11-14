# Pitwall AI: user guide

How to use Pitwall day to day. This is a living document: after every development stage, the
affected sections are updated and a new entry is added to [Release notes](#7-release-notes)
at the bottom.

## What Pitwall can do today

| Area | Status |
|---|---|
| Record F1 25 telemetry from the PS5 | ✅ Stage 0 |
| Check the recording is healthy (rate, format, packet loss, packet sizes) | ✅ Stage 0 |
| Decode every packet type; build sessions and laps | ✅ Stage 1 |
| Lap table: times, sectors, validity, out/in/flying laps, flashbacks, pauses, data quality | ✅ Stage 1 |
| Export a lap's telemetry to CSV | ✅ Stage 1 |
| Compare any lap with your session best, personal best or theoretical best | ✅ Stage 2 |
| Web app: Lap Analysis page (synchronised graphs, track map, metrics) | ✅ Stage 2 |
| Synthetic demo data, to explore the app without the PS5 | ✅ Stage 2 |
| Corner-by-corner analysis | Stage 3 |
| Comparison with real F1 drivers (FastF1) | Stage 4 |
| Race engineer reports | Stage 5 |
| Live telemetry screen | Stage 6 |
| Long-term progress tracking | Stage 7 |

---

## 1. One-time setup

### 1.1 Install on the Mac

1. Install [uv](https://docs.astral.sh/uv/), the tool that installs Python and Pitwall's
   dependencies:

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. Download Pitwall and install it:

   ```bash
   git clone https://github.com/Samarthjo/AI-Race-Engineer.git
   ```

   ```bash
   cd AI-Race-Engineer && uv sync
   ```

3. To update later, run this inside the `AI-Race-Engineer` folder:

   ```bash
   git pull && uv sync
   ```

Every command below is run from inside the `AI-Race-Engineer` folder and starts with `uv run`.

### 1.2 Check the Mac

```bash
uv run pitwall doctor
```

It checks the data folder and the UDP port, and prints this Mac's network address together with
the exact values to type into the game.

### 1.3 Game and network settings

On the PS5: **F1 25 → Settings → Telemetry Settings**

| Setting | Value |
|---|---|
| UDP Telemetry | On |
| UDP Broadcast Mode | Off |
| UDP IP Address | the Mac's address from `pitwall doctor` |
| UDP Port | 20777 |
| UDP Send Rate | 60Hz |
| UDP Format | **F1 25 (2025)**, *not* the 2026 Season Pack format |

Network tips (details in [setup-ps5-mac.md](setup-ps5-mac.md)):
- Use wired Ethernet for both the PS5 and the Mac if you can. Wi-Fi drops more packets.
- Give the Mac a fixed address (a DHCP reservation in your router), so the address you typed
  on the PS5 stays correct.
- If macOS asks whether Python may accept incoming connections, choose **Allow**.

---

## 2. Everyday workflow

The outputs shown in this guide are examples; your values will differ.

### Step 1: start recording before you drive

```bash
caffeinate -i uv run pitwall capture run --label monza-tt
```

- `caffeinate -i` stops the Mac from sleeping during the session.
- `--label` is a short name to recognise the recording later (optional). Add
  `--note "anything"` for free-text notes.
- Start it **before** leaving the menu, and keep it running for the whole session.

### Step 2: drive

Every 5 seconds a status line shows that data is arriving:

```
10:22:44    600 pkt/s · telemetry  60.0 Hz · loss 0.00% · fmt 2025 v1.12 · session …1a2b3c4d · from 192.168.1.23 · stored 21460
```

| Field | Good value |
|---|---|
| `telemetry` | about 60 Hz while driving (less in menus, 0 when paused) |
| `loss` | below 0.5 % |
| `fmt` | `2025` (otherwise fix the UDP Format setting) |
| `from` | one address: your PS5 |

### Step 3: stop recording

Back in the game menu, press **Ctrl+C**. A checklist is printed:

```
  ✓ packets_received: 152340 packets
  ✓ udp_format: packetFormat 2025 (F1 25)
  ✓ packet_ids: 13 known packet types seen
  ✓ packet_sizes: All packet sizes match the F1 25 spec
  ✓ telemetry_rate: CAR_TELEMETRY 60.0 Hz while active (expected 60 Hz)
  ✓ packet_loss: ~0.02% of high-rate packets missing (31/150210); target < 0.5%
  ✓ malformed: 0 datagrams shorter than a packet header
  ✓ sources: Single source 192.168.1.23
```

✓ means fine, ! means worth a look, ✗ means the recording has a problem
(see [Troubleshooting](#6-troubleshooting)).

### Step 4: turn the recording into laps

```bash
uv run pitwall ingest
```

This decodes every new recording into sessions and laps. It is safe to run any time, and it
only processes recordings it hasn't seen yet (or all of them with `--force`). Output:

```
20261114T183012Z: 152340/152340 packets decoded
  Monza · Time Trial · 9 laps (6 usable) · best usable 1:20.913 · session …1a2b3c4d
```

### Step 5: look at your laps

```bash
uv run pitwall sessions
```

```bash
uv run pitwall laps latest
```

`laps` accepts `latest` or a session id from `pitwall sessions` (any unique part of it).
Example:

```
Monza · Time Trial · Time Trial · game v1.12 · session …1a2b3c4d
Assists (session): braking off · gearbox manual · steering off · racing line off
 #  lap  kind    time      S1      S2      S3      valid  usable  tyre        loss %  issues / notes
 0  1    out     1:32.410  -       -       -       no     no      Medium (2)  0.0     out lap; invalid lap
 1  2    flying  1:21.402  26.811  27.604  26.987  yes    yes     Medium (2)  0.0
 2  3    flying  1:21.877  26.902  27.913  27.062  yes    no      Medium (2)  0.0     flashback used (1x)
```

### Step 6 (optional): export a lap's telemetry

```bash
uv run pitwall export latest --lap 1 --out lap1.csv
```

`--lap` takes the **#** column from `pitwall laps`. The CSV has one row per frame (60 per
second) with every channel: speed, throttle, brake, steering, gear, RPM, lap distance, position,
G-forces, tyre temperatures and pressures, fuel, ERS, wheel slip, suspension, and more. It
opens in Numbers or Excel. Use `--out lap1.parquet` for the compact Parquet format instead.

### Step 7: analyse a lap in the browser

```bash
uv run pitwall serve --open
```

This starts the Pitwall web app on this Mac and opens http://127.0.0.1:8000. It only runs
locally and reads your data folder. Stop it with Ctrl+C. The **Lap Analysis** page is
explained in [3.2](#32-the-lap-analysis-page). While `pitwall capture run` is recording, the
pill at the top right shows basic live status (track, lap, lap time, speed).

You can run `serve` and `capture run` at the same time, in two Terminal windows.

### No PS5 nearby? Try the demo

```bash
uv run pitwall demo
```

This creates a **synthetic** session on a made-up test track, where each lap makes one
deliberate mistake (late braking into T1, slow through T2 or T6, late on the throttle out of
T4, an invalid lap, a flashback lap). It is labelled "synthetic" everywhere: in `pitwall
sessions`, in the app (yellow banner), and it never counts towards your personal bests. It is
useful to learn the screens before you have real laps.

---

## 3. Understanding the results

### 3.1 The lap table

| Column | Meaning |
|---|---|
| `#` | Lap index within the session (used by `export`) |
| `lap` | Lap number as the game counts it |
| `kind` | `out` (leaving the pits), `flying` (timed lap), `in` (entering the pits), `on_track` (race laps), `garage` |
| `time`, `S1`–`S3` | Official times from the game (its session history), to the millisecond. `-` = not measured |
| `valid` | The game's own verdict (track limits / corner cutting) |
| `usable` | Clean enough to analyse; see below |
| `tyre` | Compound (laps on the set) |
| `loss %` | Telemetry packets lost on the network during this lap |
| `issues / notes` | Red: why the lap is not usable. Grey: things worth knowing that don't block analysis |

**A lap is usable** when it is complete, valid, a flying (or race) lap, has no flashback, has
at most 2 % packet loss and no data gap longer than 30 m. Only usable laps will be used as
references and for coaching.

Common issues and notes:

| Message | What it means |
|---|---|
| `out lap` / `in lap` | Not a timed flying lap |
| `invalid lap (track limits / corner cutting)` | The game invalidated the lap |
| `flashback used (Nx)` | Part of the lap was rewound; the erased part is removed from the data, but the lap is not a clean single attempt |
| `incomplete lap` | You quit, restarted or the recording stopped before the line |
| `packet loss X%` / `data gap of N m` | The network dropped data in this lap; see Troubleshooting |
| `game paused Nx` (note) | You paused; the lap is still fine |
| `assists on: …` (note) | Traction control, ABS, braking assist, steering assist or auto gearbox were on |
| `lap time sources disagree` (note) | Two of the game's own timing sources differ; please report it, it may point to a decoding issue |

### 3.2 The Lap Analysis page

At the top, choose:
- **Session**: newest first; synthetic sessions are marked.
- **Lap to analyse**: only complete flying laps can be compared. Out-laps, in-laps and
  incomplete laps don't start and end at the line. Unusable laps (invalid, flashback, packet
  loss) can be opened but are labelled.
- **Compare with**:
  - **Session best**: your fastest usable lap of this session.
  - **Personal best**: your fastest usable lap on this track (same car class), from any
    session. Real and synthetic laps are never mixed.
  - **Theoretical best**: your best sector 1, best sector 2 and best sector 3 of the
    session, stitched into one lap at the game's sector lines. It's what you'd do if you
    put your best sectors together.
  - **Another lap**: any lap of the session.

**Summary tiles:** lap time, reference time, the delta at the line, and the delta per
sector. Sector deltas use the game's official sector times. Minus (green) = faster than the
reference, plus (red) = slower.

**Charts** (all against distance from the start line, all moving together):

| Chart | How to read it |
|---|---|
| Time delta | Your time minus the reference's time *at that point of the lap*. Rising = losing time there; falling = gaining. Red shading = behind overall, blue = ahead. The value at the end is the lap-time difference. |
| Speed | Blue = this lap, orange = reference. Where the blue line is lower, you are slower at that point. |
| Throttle / Brake | Pedal application in % (the DualSense triggers). Earlier braking shows as the blue brake trace starting left of the orange one. |
| Gear | Gear selected. |
| Steering | −1 = full left, +1 = full right. With a controller, the game smooths the stick input, so read this one with care. |

- Hover any chart (or the map) to read every value at that point, in the line above the
  charts.
- Drag across a chart to zoom into a stretch; all charts follow. "Reset zoom" goes back.
- Vertical lines mark the sector 2 and sector 3 lines.

**Track map:** drawn from your lap's own positions, and coloured by where you gain (blue)
or lose (red) time against the reference, per 100 m. Grey means about equal: under 0.005 s
per 100 m. Start/finish and sector lines are marked.

**Biggest 200 m stretches:** the three stretches where you lost (or gained) the most time,
with a zoom button. They are measured, not interpreted. The corner-by-corner explanation of
*why* arrives in Stage 3.

**Lap metrics:** top and average speed, full-throttle / braking / coasting share of the lap,
gear changes, RPM, DRS, fuel, ERS, brake and tyre temperatures, for both laps. Hover a row for
its exact definition. The theoretical best has no lap metrics, because it is made of several
laps.

How accurate is it? Both laps come from your own 60 Hz telemetry and the same game physics.
Lap and sector times are the game's official values. Between frames (about 1.5 m apart at
330 km/h) values are interpolated, so a delta at a point is accurate to a few milliseconds.

### 3.3 Comparing in the terminal

```bash
uv run pitwall compare latest --lap 4 --ref theoretical_best
```

This prints the same comparison as text: lap vs reference, sector deltas, the biggest
200 m losses and gains, and the lap metrics side by side. `--ref` takes `session_best`
(default), `personal_best`, `theoretical_best` or `lap` (with `--ref-lap <#>`, and
optionally `--ref-session <session>`).

---

## 4. Command reference

| Command | What it does |
|---|---|
| `pitwall doctor` | Checks the Mac is ready; shows the PS5 settings |
| `pitwall capture run [--label L] [--note N]` | Records until Ctrl+C. Options: `--port`, `--forward host:port`, `--no-forward`, `--allow-source <PS5 IP>`, `--no-compress`, `--expected-hz` |
| `pitwall capture list` | All recordings with label, status and packet loss |
| `pitwall capture stats <id>` | Detailed health report of a recording (`--json` for everything) |
| `pitwall capture replay <id>` | Re-sends a recording over UDP (`--speed 1` real time, `--speed 0` as fast as possible; very fast replays can overflow slow receivers) |
| `pitwall capture compact <id>` | Compresses leftover raw files after a crash (lossless, verified) |
| `pitwall ingest [<id>...] [--force]` | Builds sessions and laps from recordings |
| `pitwall sessions [--limit N]` | Lists sessions, newest first |
| `pitwall laps <session\|latest>` | Lap table for one session |
| `pitwall export <session\|latest> [--lap #] [--out file.csv\|.parquet]` | Exports per-frame telemetry |
| `pitwall decode <id> [--packet CAR_TELEMETRY] [--limit 1] [--skip N] [--all-cars]` | Prints decoded packets as JSON, to compare values with what the game shows |
| `pitwall serve [--open] [--port 8000]` | Starts the web app (Lap Analysis) at http://127.0.0.1:8000; `--live-port` (default 20778) for the live status, `--no-live` to switch it off |
| `pitwall compare <session\|latest> --lap # [--ref …]` | Compares a lap with a reference in the terminal |
| `pitwall demo` | Creates a labelled synthetic demo session |
| `pitwall version` | Version |

All commands accept `--data-dir` (or the environment variable `PITWALL_DATA_DIR`) to use a
different data folder. Settings can be stored in `~/.config/pitwall/config.toml`; see
[config.example.toml](config.example.toml).

---

## 5. Where your data lives

Everything stays on the Mac, by default in `~/PitwallData`:

```
~/PitwallData/
  raw/<recording>/            original packets exactly as received (never modified)
      capture.json            label, note, settings, health report
      seg_00000.pwraw.zst     compressed raw packets
  pitwall.db                  sessions and laps catalogue
  sessions/<recording>/<session>/
      frames.parquet          decoded player-car telemetry, one row per frame
      session.json            session details: events, setup, history, decoding stats
```

- **`raw/` is the precious part.** Back it up. Everything else can be rebuilt from it with
  `uv run pitwall ingest --force`, which happens automatically when a new Pitwall version
  improves the decoding.
- Disk use: an hour of driving at 60 Hz is estimated at 1–1.5 GB of raw packets before
  compression (to be confirmed on your first recordings); closed segments are compressed
  losslessly. `uv run pitwall capture list` shows the actual size of every recording.

---

## 6. Troubleshooting

| Symptom | What to do |
|---|---|
| `waiting for packets…` forever | Check the IP and port on the PS5, that UDP Telemetry is On, that both devices are on the same (non-guest) network, and allow Python in the macOS firewall |
| `fmt` is not 2025 / `udp_format` warning | Set UDP Format to F1 25 (2025) in the game |
| `packet_sizes` ✗ | The game sent packets of a different layout than expected (new game patch, or the 2026 Season Pack format). Note the game version and report it; don't trust analysis of that recording until fixed |
| `telemetry_rate` well below 60 Hz | Set UDP Send Rate to 60Hz |
| `packet_loss` above 0.5 % or laps with `packet loss` issues | Use wired Ethernet; keep the Mac close to the router; avoid big downloads while driving |
| Laps marked `incomplete` | Keep the capture running until you are back in the menu |
| `Cannot listen on UDP …:20777` | Another telemetry app or another capture is using the port; close it |
| `ingest` says "skip … capture still running" | Stop that capture first (Ctrl+C), or name it explicitly if it crashed |
| A capture crashed (power cut, terminal closed) | Its data up to the crash is fine. Run `uv run pitwall capture compact <id> --force`, then `uv run pitwall ingest <id>` |
| `Cannot compare: out laps start or end in the pits` (or `not complete`) | Only complete flying laps start and end at the line; pick another lap |
| `Cannot compare: this session has no usable lap` | The session has no clean lap to use as session best; choose "Another lap" or "Personal best" |
| Lap Analysis shows no sessions | Run `uv run pitwall ingest` after recording (or `uv run pitwall demo` to try it) |
| `pitwall serve` says the web UI is not built | You are on a development copy; see `web/README.md` |
| Port 8000 already in use | `uv run pitwall serve --port 8010` and open that address |
| Live pill always says "No live data" | `pitwall capture run` must be running (it forwards packets to UDP 20778) and the game must be sending |

---

## 7. Release notes

Newest last. Each stage appends an entry: what's new, how to use it, known limitations.

### Stage 0: capture kit (2026-11-14)

**New:** `pitwall doctor`; `pitwall capture run | stats | list | replay | compact`.
Records every UDP packet unchanged (crash-safe, compressed, verified), forwards a copy to
`127.0.0.1:20778` for later live features, and reports recording health: rate, UDP format,
packet loss (flashbacks and pauses are not counted as loss), gaps and sources.

**Limitations:** recording only; no decoding of the data itself yet.

### Stage 1: decoder, sessions and laps (2026-11-14)

**New**
- Full decoder for all 16 F1 25 packet types (UDP format 2025), with strict checks: a packet
  is decoded only if its format and exact size match the specification.
- `pitwall ingest`, `pitwall sessions`, `pitwall laps`, `pitwall export`, `pitwall decode`.
- Laps use the game's own official times and validity, to the millisecond. Each lap records
  its kind, sectors, tyre, fuel, tyre wear, assists and car setup.
- Flashbacks: the rewound (erased) part is removed from the data, and the lap is flagged.
  Pauses are detected and do not affect lap data.
- Data quality per lap: packet loss, longest data gap, and an overall **usable** verdict with
  the reasons.
- `capture stats` now also checks that every packet size matches the F1 25 spec.

**How to check it on your own laps (the Stage 1 acceptance test)**
1. Record the [golden sessions](golden-sessions.md) and note the in-game lap times.
2. Run `uv run pitwall ingest`, then `uv run pitwall laps latest`.
3. Lap times and sectors must match the game **exactly**. The out-lap, invalid lap,
   flashback lap, paused lap and quit lap must be labelled as such.
4. `uv run pitwall capture stats <id>` must show ✓ for `packet_sizes`.

**Limitations**
- Verified so far with synthetic sessions built from the packet layouts; your real recordings
  are the final check.
- Packet layouts were cross-checked against two independent F1 25 implementations; the check
  against EA's official specification document is still pending ([spec/README.md](spec/README.md)).
- Only your own car is decoded into laps; other cars' data stays in the raw recording.
- A session split over two recordings is treated as two separate sessions.
- The 2026 Season Pack UDP format is not supported (you drive the 2025 cars).

### Stage 2: lap analysis and comparison (2026-11-14)

**New**
- `pitwall serve`: local web app with the **Lap Analysis** page. It has synchronised delta,
  speed, throttle, brake, gear and steering charts against distance, a hover readout, drag
  to zoom, a track map coloured by where time is gained and lost, the biggest 200 m
  stretches, and lap metrics side by side. Dark and light themes. A basic live status pill
  shows when the capture is running.
- References: **session best**, **personal best** (same track and car class, across
  sessions), **theoretical best** (best sectors stitched at the game's sector lines), or
  any lap.
- `pitwall compare`: the same comparison in the terminal.
- Lap metrics with written definitions: top/average speed, full throttle, braking,
  coasting, gear changes, RPM, DRS, fuel, ERS, brake and tyre temperatures.
- `pitwall demo`: a synthetic demo session to explore the app without the PS5, labelled as
  synthetic everywhere and never mixed with your real laps.

**How it works:** every lap is placed on a common distance axis, from the start line (0 m)
to the track length, every 1 m. Time comes from the game's own lap timer, so the delta at the
line equals the lap-time difference exactly.

**Checked:**
- a lap against itself gives zero delta everywhere;
- the delta at the line equals the lap-time difference;
- against synthetic laps with exactly known timing, the delta is within 5 ms at every metre;
- the biggest loss is found exactly where a mistake was injected (late throttle out of T4,
  soft braking into T1);
- the stitched theoretical best matches the sum of the best official sectors (within 3 ms).

**Limitations**
- Verified on synthetic laps; your real laps are the final check (Stage 1 acceptance test
  first).
- Comparisons are lap against lap of your own driving. The corner-by-corner analysis (braking
  points, minimum speeds, throttle pickup) is Stage 3; real F1 drivers are Stage 4.
- The map uses the game's world coordinates as they are, so it may appear rotated or mirrored
  compared with an official track map.
- Personal best requires the same game track ID and car class (formula); laps from different
  game versions are not separated yet.

