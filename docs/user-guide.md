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
| Corner-by-corner analysis: corners detected from your laps, time lost per corner and phase | ✅ Stage 3 |
| Findings: measured driving differences per corner, with suggestions | ✅ Stage 3 |
| Consistency per corner and your ideal lap (best segments) | ✅ Stage 3 |
| Real F1 reference laps (2025 season): download once, checked, kept offline | ✅ Stage 4 |
| Driver Benchmark: your lap against a real F1 lap, measured and physics-normalised, with uncertainty | ✅ Stage 4 |
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

### Step 8: benchmark against a real F1 driver

Store a real reference lap once (it needs the internet only this one time):

```bash
uv run pitwall ref laps monza
```

```bash
uv run pitwall ref fetch monza VER
```

The first lists every driver's fastest valid lap of the 2025 Italian Grand Prix qualifying;
the second downloads Max Verstappen's (his pole lap, 1:18.792), checks it and keeps it on the
Mac. Then open **Driver Benchmark** in the web app, or in the terminal:

```bash
uv run pitwall benchmark latest --lap 4 --driver VER
```

The page and the results are explained in [3.6](#36-the-driver-benchmark-page). In the app,
**Add a reference…** does the same as `pitwall ref fetch`, for the track of the session you
picked.

### No PS5 nearby? Try the demo

```bash
uv run pitwall demo
```

This creates a **synthetic** session on a made-up test track. Each lap makes deliberate
mistakes in known places: braking 15 m early into T1, slower through T2 or T6, late to full
throttle out of T4, softer braking, a front lock-up, rear wheelspin, an invalid lap and a
flashback lap. It is labelled "synthetic" everywhere: in `pitwall sessions`, in the app
(yellow banner), and it never counts towards your personal bests. It is useful to learn the
screens, and to see every type of finding, before you have real laps.

The demo also stores two **synthetic reference laps** for the Driver Benchmark, standing in
for real F1 data (recorded the way real F1 data is: about 4 samples a second, on/off brake,
a different coordinate frame): **PRO** drives the same car as the demo laps, **FST** a faster
car. They are only ever compared with synthetic laps. Try
`uv run pitwall benchmark latest --lap 1 --driver PRO`.

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

**Findings** (top of the right column): what the lap does differently from the reference,
corner by corner, biggest time cost first. Each finding shows:
- the corner and what was measured (e.g. "Full throttle comes 30 m later (3120 m vs 3090 m
  into the lap)");
- the time it costs, and in which phases (approach, braking, mid-corner, exit, or "carried"
  down the next straight). "Event" means it was measured but cost no measurable time
  (lock-ups, wheelspin);
- a confidence badge: **high** when the difference is well above the measurement
  resolution and costs at least 0.05 s, otherwise **medium**;
- a **Try** suggestion: an incremental target (half the gap first), never a jump straight to
  the reference.

Click "show on charts" to zoom in. Findings of the same corner can overlap (e.g. a lower
minimum speed and a later throttle), so don't add their times up; the corner table shows each
corner's total.

The findings that exist so far:

| Finding | Measured as |
|---|---|
| Brakes earlier | Brake point (brake ≥ 10 % for 3 m) at least 6 m earlier, with similar pressure |
| Softer initial braking | Peak brake at least 10 percentage points lower; replaces "brakes earlier" when that is the cause |
| Brakes later but loses the apex | Brake point at least 6 m later, but minimum speed at least 3 km/h lower or the apex 10 m later |
| Lower minimum speed | Minimum speed at least 3 km/h lower |
| Later on the throttle | Throttle ≥ 20 %, or full throttle, at least 8 m later |
| Throttle hesitation | Throttle drops by 30 % or more after it was picked up |
| Coasting | At least 10 m more distance with neither pedal, 15 m or more in total |
| Slower exit | Exit speed at least 4 km/h lower, costing time on the next straight (only when no throttle or apex finding explains it) |
| Front lock-up / rear wheelspin | Wheel slip ratio beyond ±0.20 (from the game's wheel data) for 3 m or more |

A finding needs at least 0.02 s of time cost in its phase (except lock-ups and wheelspin), so
tiny differences never produce advice.

**Corner by corner** (below the charts): one row per corner and straight, in lap order:
- **Time Δ**: time lost (+) or gained (−) there. A corner includes its **exit carry**, the
  time lost on the following straight because the exit was slower.
- **Brake point**: earlier or later than the reference, in metres.
- **Entry, Min and Exit km/h**: this lap / reference.
- **Full throttle**: earlier or later than the reference, in metres.
- **Spread**: how much this segment's time varies over the session's usable laps (standard
  deviation). Red = more than ±0.1 s, i.e. inconsistent.

The rows add up exactly to the lap delta. Below the table: your **ideal lap**, the sum of
your best time in every segment this session. Click a row to zoom the charts and highlight
the corner on the map.

**Corners and the track model:** the first time you analyse a track, Pitwall detects its
corners automatically from your fastest usable lap there:
- braking or lifting zones become corners (chicanes are merged into one "complex");
- corners you take flat out are found from the shape of the line;
- everything in between becomes straights.

This is a **draft**: a banner asks you to check the labels on the map (C1, C2, ...) and the
corner table. When they look right, click **Confirm corners**, or run
`uv run pitwall track confirm latest`. From then on, every lap on that track is cut into
exactly the same pieces. At Monza, the corners get their real names (e.g. "Variante del
Rettifilo (T1-T2)") if the detected braking corners match Monza's six. See
[3.5](#35-track-models) to rename corners or fix their boundaries.

### 3.3 Comparing in the terminal

```bash
uv run pitwall compare latest --lap 4 --ref theoretical_best
```

This prints the same comparison as text: lap vs reference, sector deltas, the biggest
200 m losses and gains, and the lap metrics side by side. `--ref` takes `session_best`
(default), `personal_best`, `theoretical_best` or `lap` (with `--ref-lap <#>`, and
optionally `--ref-session <session>`).

### 3.4 Corners in the terminal

```bash
uv run pitwall corners latest --lap 4
```

This prints the corner table (time lost per corner including exit carry, brake point, entry,
minimum and exit speed, full-throttle point) and the findings with their suggestions. It
takes the same `--ref` options as `compare`.

### 3.5 Track models

Track models are saved in `~/PitwallData/tracks/<key>.json`, for example
`track11-monza.json`.

| Command | What it does |
|---|---|
| `uv run pitwall track list` | All saved models: status (draft / confirmed), version, number of corners |
| `uv run pitwall track show latest` | The segments of the model used by the latest session (or give a key) |
| `uv run pitwall track rename latest c3 "Lesmo 1"` | Rename a corner (or straight, e.g. `s2`) |
| `uv run pitwall track draft latest --lap 7 --force` | Re-detect the corners from a specific lap (e.g. your cleanest one) |
| `uv run pitwall track confirm latest` | Mark the model as checked |

You can also edit the JSON file directly, e.g. to move a boundary (`start_m` / `end_m`, in
metres from the line). Segments must cover the whole lap without gaps, and `pitwall track
show` reports any problem. Each change increases the model's version.

### 3.6 The Driver Benchmark page

The **Driver Benchmark** page puts a real Formula 1 lap on your track and compares it with
your lap, corner by corner. Read it as *directional*: the comparison with your own laps
(Lap Analysis) remains the more precise one.

**Where the real data comes from.** Formula 1's own timing archive (which FastF1 reads) has
required an F1 TV login since October 2026. Pitwall reads the same data, as processed by
FastF1, from the public **TracingInsights archive** on GitHub (one repository per season).
Each file is downloaded once, stored unchanged with its address and a SHA-256 checksum, and
read from the Mac afterwards, offline. Only the 2025 season is allowed: F1 25 models the 2025
cars (the "era guard"). The track must match: your Monza laps are compared with the Italian
Grand Prix.

**Which lap.** By default each driver's fastest *valid* lap: not deleted by the stewards,
timing marked accurate, really timed (not filled in) and not an out or in lap. The lap time
from the telemetry must equal the official one, or the lap is refused.

**What real F1 data can and cannot show.** About four samples a second (a median 0.24 s
apart, about 20 m at 300 km/h, with occasional gaps up to about 1 s), whole km/h, throttle in
whole percent, and the brake only as on/off. There is no steering and no wheel slip. So brake
pressure, release and trail braking, steering, lock-ups, wheelspin, coasting and throttle
hesitation are **not compared**; the page lists them under *About the reference data*.

**How the lap is placed on your track.** The real positions are in the circuit's own
coordinates; your lap gives the game's. Pitwall finds the rotation, scale and shift that fit
one onto the other, then gives every real sample a distance on your lap. The *Placed on your
track* tile shows how well: the median distance of the real positions from your line, and
the real start line's distance from yours. A dashed orange line on the map shows the real
lap on your track. A poor fit is refused; a doubtful one shows a yellow banner.

**Summary tiles.** Your lap, the reference (driver, team, event, session and Q1/Q2/Q3), the
delta at the line, the systematic pace difference (see below) and the placement quality.

**Charts and map.** As on Lap Analysis: blue is your lap, orange the reference. The
reference's brake trace is on/off and it has no steering line. Its lines join samples about
20 m apart, so its exact shape between two samples is unknown.

**Measured, systematic, normalised.** The game car is not the real car, and the game is not
real life. So for every value Pitwall shows:

- **measured**: your value minus the reference's (time, brake point, speeds, throttle);
- **systematic**: the median of that difference over all corners (for times: the median time
  ratio, separately for braking corners and for straights), i.e. what is the same everywhere
  and so most likely comes from the car and the physics;
- **normalised**: measured minus systematic, the part that is specific to this corner, and
  most likely to your driving.

Example: if you brake 12 m before Verstappen everywhere and 30 m before him into the
Rettifilo, the Rettifilo is 18 m earlier than your norm.

**± and confidence.** Every reference value is estimated between its samples, so it has an
uncertainty, shown as ± one standard deviation. It is **measured, not assumed**: Pitwall
records *your own* lap the way the real F1 feed would (same sample spacing, whole km/h, on/off
brake), many times at random sampling phases, and sees how far the estimates land from your
exact 60 Hz values at that corner. To that it adds the spacing of the reference's own samples
there and the uncertainty of placing it on your track. *high* / *medium* mean the difference
is more than 3 / 2 standard deviations, so it is unlikely to come from the data's limits.
With about thirty values per lap, an occasional *medium* can still be chance.

**Findings vs a real driver.** Only when a corner loses time beyond the systematic pace
difference (at least 0.05 s and two standard deviations) *and* a normalised value explains
it: brakes earlier (at least 10 m), lower minimum speed (4 km/h), later on the throttle
(12 m), or a slower exit when the data cannot tell which of those it is. Each is labelled
*vs real F1 · normalised* and keeps the measurement separate from the suggestion. Because the
real brake is on/off, a softer first brake application looks like an earlier brake point;
the finding says so. Corners where you are better than your norm are listed under the
findings.

**Sectors.** Measured on your game's sector lines (the reference's times there come from
its trace); the reference's official sector times, on the real lines, are shown below.

### 3.7 Reference laps in the terminal

| Command | What it does |
|---|---|
| `uv run pitwall ref events` | The 2025 events and their sessions |
| `uv run pitwall ref laps monza` | Every driver's fastest valid lap of the qualifying (`--session Race`, `--part Q3`) |
| `uv run pitwall ref laps monza --driver VER` | All of one driver's laps, and why a lap cannot be a reference |
| `uv run pitwall ref fetch monza VER` | Downloads, checks and stores the fastest valid lap (`--lap 17` for a specific one) |
| `uv run pitwall ref list` | Reference laps stored on this Mac |
| `uv run pitwall benchmark latest --lap 4 --driver VER` | The benchmark in the terminal (`--ref <key>` for a stored lap) |

Events can be named in full ("Italian Grand Prix") or by place ("monza"). The download
addresses can be changed in the config file (`[reference]`, see
[config.example.toml](config.example.toml)), for example to a mirror.

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
| `pitwall corners <session\|latest> --lap # [--ref …]` | Corner table and findings in the terminal |
| `pitwall track list \| show \| draft \| confirm \| rename` | Manage track models (corners); see [3.5](#35-track-models) |
| `pitwall ref events \| laps \| fetch \| list` | Real F1 reference laps; see [3.7](#37-reference-laps-in-the-terminal) |
| `pitwall benchmark <session\|latest> --lap # --driver VER` | Your lap against a real F1 lap, in the terminal (`--ref <key>`, `--ref-session`, `--part`) |
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
  tracks/<key>.json           track models (corners)
  references/
      raw/                    downloaded real-F1 files, unchanged, each with a .source.json
                              (address, SHA-256, download time)
      laps/<key>.parquet      reference laps: their measured samples
```

- `references/raw/` keeps the reference data working offline. Everything in
  `references/laps/` and in the catalogue is rebuilt from it when Pitwall's processing
  changes. Real F1 data stays on your Mac; it is never added to the code repository.

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
| Corners look wrong (merged, missing or split) | Re-draft from a cleaner lap: `uv run pitwall track draft latest --lap <#> --force`, or edit the JSON file (see 3.5) |
| `Corner analysis unavailable: track model … is N m but this session's track is M m` | The game's track length changed (e.g. after a game update). Re-draft with the command shown in the message |
| "No findings" | The lap matches the reference corner by corner within the thresholds, or the differences are too small to be measured reliably |
| `cannot reach raw.githubusercontent.com` (or a timeout) while fetching a reference | The Mac needs the internet once per reference lap. Check the connection; a VPN, firewall or proxy may block GitHub's file server. A mirror can be set in the config file (`[reference] base_url`) |
| `refused the request (HTTP 403)` | A proxy or network policy blocks the archive; try another network or a mirror |
| `not in the archive (HTTP 404)` | That event, session, driver or lap does not exist in the archive (check the names with `pitwall ref events` and `pitwall ref laps`) |
| `lap N cannot be a reference: deleted / out lap / …` | Pick another lap, or leave out `--lap` to get the fastest valid one |
| `telemetry ends at … but the official lap time is …` | The archive's telemetry does not match the official timing for that lap; Pitwall refuses it rather than compare a wrong lap |
| `Cannot benchmark: no real-F1 season is defined for UDP format …` | Set UDP Format to F1 25 (2025) in the game; the 2026 Season Pack format is not supported yet |
| `… has no real-F1 reference (reverse or unknown layout)` | Reverse layouts don't exist in real F1 |
| `the reference is from the …, but this lap is at …` | Choose a reference from the event of your track |
| `synthetic laps are only compared with synthetic references` | Demo laps use the demo's synthetic references (PRO, FST); real laps use real references |
| `cannot place the reference on your track: …` | The real lap does not fit your lap's path (wrong track or layout, or a lap with an off). Try another of your laps |
| Yellow banner "check it on the map" on Driver Benchmark | The fit is usable but not perfect (e.g. the start lines are far apart); compare the dashed orange line with your path on the map |
| Why not FastF1 directly? | Since October 2026 Formula 1's timing archive answers "401 Unauthorized" without an F1 TV login, for FastF1 too. Using it with your login is a possible later addition (see docs/unfinished.md) |

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

### Stage 3: corner-by-corner analysis and findings (2026-11-14)

**New**
- Track models: corners are detected automatically from your fastest usable lap on a track
  (braking/lifting zones, chicanes merged, flat-out corners from the line's curvature,
  straights in between), saved as a draft you confirm in the app or with
  `pitwall track confirm`. `pitwall track list | show | draft | confirm | rename`.
- Per-corner metrics, each with a written definition: brake point, braking distance, entry
  speed, peak brake and how quickly it is reached and released, trail braking, minimum speed
  and where it is, gear at the apex, throttle pickup, full-throttle point, throttle
  hesitation, coasting, exit speed, lock-up and wheelspin distance.
- Time accounting: the lap delta is split exactly over corners and straights; each corner
  into approach, braking, mid-corner and exit; and the time a slower exit costs on the next
  straight is credited to that corner ("exit carry").
- Findings with root causes: e.g. softer braking (instead of "brakes earlier") or "slower
  exit" only when nothing else explains it. Each has measured values, time impact,
  confidence and an incremental suggestion.
- Consistency: spread of every corner over the session's usable laps, and your ideal lap from
  your best segments.
- App: Findings card, Corner by corner table (click to zoom and highlight), corner labels on
  the map, and the draft banner with **Confirm corners**. Terminal: `pitwall corners`.
- The demo session now contains braking early, slower corners, late throttle, softer
  braking, a lock-up and wheelspin, so every finding type can be explored.

**Checked against synthetic laps with known mistakes**
- Segments add up exactly to the lap delta; phases add up exactly to each corner's loss.
- Brake points are within 3 m of the true value; minimum speeds within 1.5 km/h.
- Braking 15 m early gives exactly one "brakes earlier" of about 15 m at that corner.
- Late throttle, a slower corner, softer braking, a lock-up and wheelspin each give exactly
  the expected finding, at the right corner.
- Identical and near-identical laps give no findings at all.

**Limitations**
- Verified on synthetic laps only. Thresholds (especially wheel slip for lock-ups and
  wheelspin) will be tuned on your real recordings. The Stage 3 gate is your review of the
  findings on your golden laps.
- Corner detection depends on the lap it is drafted from: draft from a clean, fast lap.
  Official F1 corner numbers arrive with the FastF1 track alignment in Stage 4; until then,
  only Monza gets suggested names.
- Corner analysis is computed when you open a lap; it is not stored yet (history arrives in
  Stage 7).
- Against the theoretical best (stitched from several laps), corner metrics near a sector
  line mix two laps; prefer the session best for corner findings.

### Stage 4: real F1 reference laps and the Driver Benchmark (2026-11-15)

**New**
- Real Formula 1 reference laps from the 2025 season. Formula 1's own archive now needs an
  F1 TV login, so Pitwall reads FastF1's processed data from the public TracingInsights
  archive on GitHub. Every file is downloaded once, kept unchanged with its address and a
  SHA-256 checksum, and used offline afterwards.
- Choosing a lap: season, event, session (Q1/Q2/Q3), driver, and by default the fastest
  *valid* lap (not deleted, timing accurate, timed, not an out or in lap). The telemetry must
  end at the official lap time, or the lap is refused.
- Common data format with capability flags: only measured samples are kept (the archive's
  interpolated rows are dropped), the brake is on/off, and there is no steering or wheel
  slip; comparisons that need those are never made.
- Placing a real lap on your track: rotation, scale and shift found automatically (mirror
  images too), with the start-line offset and the fit quality; a poor fit is refused.
- Measured error bars: your own lap is recorded the way the real F1 feed would record it, many
  times, to see how far each estimate can be off at each corner.
- Physics-normalised view: the systematic difference between the game car and the real car
  (median over the lap) is separated from what is specific to each corner.
- Findings vs a real driver (brakes earlier, lower minimum speed, later on the throttle,
  slower exit), only beyond two standard deviations of their uncertainty and when the corner
  loses time beyond the systematic pace difference.
- App: the **Driver Benchmark** page (summary, charts, map with the real lap's path, corner
  table with measured, systematic and normalised values and confidence labels, findings,
  sectors, notes on the data) and **Add a reference…**. Terminal: `pitwall ref events | laps
  | fetch | list` and `pitwall benchmark`.
- The demo also stores two synthetic reference laps (PRO and FST) so the page can be explored
  without real data.
- Config: a `[reference]` table to change the download addresses.

**Checked**
- On real data (in development): Max Verstappen's 2025 Monza pole lap (1:18.792, Q3) is found
  as the fastest valid lap, its sectors add up to the lap time, its telemetry ends exactly at
  it, and the qualifying order of the archive matches the real result (VER, NOR, PIA, LEC,
  HAM). Placing one real Monza lap on another, moved by a known rotation, scale and mirror
  image, recovers them within 0.01° and 0.1 %, with the corners within 1 m.
- On synthetic laps with known answers: the reference keeps its exact lap time on your
  timeline; the start line, the transform and each sample's distance are recovered within
  1.5 m; your lap against the same driving at real-F1 data quality gives **no** findings;
  against a faster car driven the same way, also none; braking 30 m early, a slower corner and
  late throttle are each found at the right corner, after normalisation; lock-ups, wheelspin
  and soft braking are never claimed against real data.

**Limitations**
- Not yet checked with a real F1 25 lap: the placement quality limits, the thresholds and the
  normalisation are set from synthetic data and real reference data, and will be tuned on
  your Monza laps. The Stage 4 gate is your review of a real lap against Verstappen.
- Directional: with about 4 samples a second, brake and throttle points of the real lap are
  known to roughly ±5-10 m, and a single corner's difference of under about 10 m cannot be
  separated from the data's limits.
- The normalisation removes one systematic offset per value (and one pace ratio for corners
  and one for straights); differences between the game car and the real car that vary from
  corner to corner remain in the normalised values.
- Downloading needs the internet once per lap; afterwards everything works offline.
  Practice sessions and races can be used, but qualifying laps are the cleanest references.
- Results are computed when you open a lap; they are not stored yet (Stage 7).
- Real F1 corner numbers (T1-T11) are not yet applied to your track model: the archive's
  corner markers can now be placed on your track, but naming from them is still to do (see
  docs/unfinished.md, P4). Monza's braking corners keep their suggested names.
