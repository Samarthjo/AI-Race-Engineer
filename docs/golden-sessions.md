# Golden sessions: recording checklist (Stage 0 gate)

These recordings become the reference test data for everything that follows: the decoder, the lap
detection, the corner analysis. Every scenario below exercises a case the software must handle
correctly, so please record each one as its **own capture** with the given label.

Setup for all of them:
- Base 2025 car, **no assists**, controller.
- UDP settings from [setup-ps5-mac.md](setup-ps5-mac.md) (60 Hz, F1 25 format).
- Start the capture **before** leaving the garage or menu, and stop it **after** returning to the menu.

```bash
caffeinate -i uv run pitwall capture run --label <label> --note "<anything worth knowing>"
```

## Scenarios

| # | Label | What to do | Why |
|---|---|---|---|
| 1 | `monza-tt-clean` | Time Trial, Monza: out-lap, then **at least 5 clean flying laps**, then back to the menu. | Core analysis data; consistency between laps |
| 2 | `monza-invalid` | Time Trial, Monza: one lap where you clearly cut a chicane (lap gets invalidated), then one clean lap. | Lap validity flags |
| 3 | `monza-flashback` | A mode with flashbacks enabled (e.g. a Grand Prix practice session): drive, make a mistake mid-lap, **use a flashback**, finish the lap. | Rewind handling: the lap must be rebuilt without duplicated data |
| 4 | `monza-pause` | Mid-lap, **pause for ~30 seconds**, resume, finish the lap. | Pauses must not count as lost packets or lap time |
| 5 | `monza-restart` | Start a lap, then **restart the session / lap** from the pause menu; drive one more lap. | New session detection |
| 6 | `monza-pit` | Practice session: drive a lap, **enter the pits**, wait, exit, drive another lap. | Pit lane / in-lap / out-lap tagging |
| 7 | `monza-quit` | Start a lap and **quit to the main menu mid-lap**. | Incomplete laps and interrupted sessions |
| 8 | `soak-60min` | Any mode, about **60 minutes** of continuous driving (can be a long practice). | Stability, file rotation, packet loss over time |

## Write down while driving

For each capture, fill in a row (copy the table into a note or edit this file):

| Capture id | Label | Lap times shown in game (and sector times if visible) | Notes (mistakes, flashback lap, weather, setup) |
|---|---|---|---|
| | | | |

The in-game lap times let us check that the decoder computes **exactly** the same times
(to the millisecond).

Also note once: your **controller settings** (steering deadzone, linearity, saturation, trigger
settings, vibration). They aren't sent over UDP, but they affect how your inputs look.

## After recording

Check each capture:

```bash
uv run pitwall capture stats <capture-id>
```

All checks should be ✓ (except `packet_loss` may show `info` on very short captures).

Then turn them into laps and compare with your notes:

```bash
uv run pitwall ingest
```

```bash
uv run pitwall laps <session id>
```

Lap and sector times must match the game exactly. The out-lap, invalid lap, flashback lap, paused
lap and the quit lap must be labelled as such. If anything differs, note the capture id and the
lap: that's exactly what this checklist is for.

Sharing captures for development: raw captures are tens of MB each, so plain git is not suitable.
When needed we'll use Git LFS in the private repo, or another transfer.
