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
| Lap analysis graphs and comparison with your best lap | Stage 2 |
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

2. Download Pitwall and install it (replace the URL with your repository's):

   ```bash
   git clone https://github.com/<you>/pitwall-ai.git
   ```

   ```bash
   cd pitwall-ai && uv sync
   ```

3. To update later, run this inside the `pitwall-ai` folder:

   ```bash
   git pull && uv sync
   ```

Every command below is run from inside the `pitwall-ai` folder and starts with `uv run`.

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

---

## 3. Understanding the lap table

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
