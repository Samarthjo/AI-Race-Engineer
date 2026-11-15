# Unfinished items

Things deliberately postponed or still waiting for something, so they are not forgotten. Each
row says why it is open and what closes it. Review this list at the end of every stage and
before calling the project done.

Last reviewed: 2026-11-15 (Stage 5).

## Waiting for real recordings (needs your Mac and PS5)

| # | Item | Why it is open | What closes it | Since |
|---|---|---|---|---|
| U1 | Validate Stages 1-3 on golden sessions | Everything so far is verified on synthetic data | Record the golden sessions (docs/golden-sessions.md); check lap times against the game, and review corners and findings | Stage 1 |
| U2 | Tune lock-up / wheelspin thresholds | Slip-ratio thresholds (-0.20 / +0.20) are initial values | Real no-assist laps with known lock-ups and wheelspin | Stage 3 |
| U3 | Stage 4 gate: a real Monza lap against Verstappen | No real F1 25 lap available in development | Record Monza Time Trial laps, `pitwall ref fetch monza VER`, review the Driver Benchmark page | Stage 4 |
| U4 | Calibrate registration limits and pro thresholds | Placement limits (scale, residuals, start offset), the along-track uncertainty (median sideways residual) and pro thresholds (10 m, 4 km/h, 12 m, 0.05 s) come from synthetic data and real-vs-real checks | Several real Monza laps against several drivers | Stage 4 |
| U5 | Check the game's start line and sector lines against the real ones at Monza | The offset is measured per lap but never yet seen on real game data | First real Monza benchmark (the "Placed on your track" tile) | Stage 4 |
| U6 | Official EA UDP spec check | The F1 25 layouts were built from the spec appendix without the official file in the repo (forums.ea.com unreachable from the cloud session) | Add EA's spec to docs/spec and compare the size table | Stage 0 |
| U7 | Stage 5 gate: choose the local model | No model can run in the development environment; the narrator was tested against a stand-in for Ollama | `pitwall engineer eval` with one or two models on about 20 of your laps, rate them; zero unsupported claims; set `[engineer] model` | Stage 5 |

## Data sources

| # | Item | Why it is open | What closes it | Since |
|---|---|---|---|---|
| D1 | FastF1 / F1 timing archive with an F1 TV login | Since about 15 Oct 2026 livetiming.formula1.com answers 401 without F1 TV authentication (FastF1 issue #978); FastF1's own mirror has no 2025 data | Optional provider using FastF1 with your F1 TV login, writing the same reference format; only if the TracingInsights archive stops being enough | Stage 4 |
| D2 | OpenF1 provider | Free historical data (car data about 3.7 Hz, positions, laps), but its API was blocked in the cloud development environment so it could not be tested | Implement and test from the Mac as a second source and a cross-check | Stage 4 |
| D3 | Reference archive dependency | The default source is a third-party GitHub repository (TracingInsights, Apache-2.0); it could move or change format | Downloaded files are kept unchanged, so stored references keep working; a mirror URL can be set in the config. A format change is refused with a clear error | Stage 4 |
| D4 | Re-download / refresh of cached reference files | Cached files are never replaced (by design); fixing a bad upstream file needs deleting it by hand | Add `pitwall ref fetch --refresh` that stores a new version next to the old one | Stage 4 |

## Product and analysis

| # | Item | Why it is open | What closes it | Since |
|---|---|---|---|---|
| P1 | 2026 Season Pack UDP format | Agreed to start with the F1 25 format and 2025 cars | A versioned decoder for the 2026 format and a 2026 reference season (era guard) | Plan |
| P2 | Second track | To be chosen after the Monza work | Your choice; corner names and a check of the track model | Stage 3 |
| P3 | Personal best ignores game version | A game patch can change physics; personal bests mix versions | Separate personal bests (and track models) by game version | Stage 2 |
| P4 | Map orientation and real corner numbers | The map uses the game's coordinates; corners get "C1..." names unless the count matches Monza's list | Use the real corner markers (archive corners.json, placed with the registration) to name corners T1-T11 | Stage 3 |
| P5 | Findings and benchmarks are not stored | Computed on demand | Store findings with versions in Stage 7 (history) | Stage 3 |
| P6 | Corner-to-corner variation of game vs real physics | The normalisation removes one offset per value; what varies by corner stays in the normalised values | A per-corner physics model, or offsets learned from many of your laps | Stage 4 |
| P7 | Benchmark against several drivers at once / an "ideal real lap" | One reference lap at a time | Later, if useful | Stage 4 |
| E1 | Claude API adapter for the engineer | Local model first (agreed); the `LLMProvider` interface is ready | Add only if the local model's eval falls short | Stage 5 |
| E2 | Driver profile and history in the engineer | `get_history` answers "not available" | Stage 7: history, recurring patterns, recommendation tracking feed the engineer | Stage 5 |
| E3 | Fact check limits | It checks numbers, corners, laps, time direction, estimates and topics, not every relation in a sentence (e.g. which of two cited corners a number belongs to) | Extend the checks if the eval finds such errors | Stage 5 |
| E4 | Streaming and cancelling AI requests | The page waits for the whole answer | Stream tokens to the page if waits are long on the Mac | Stage 5 |
| E5 | GitHub Actions on Node 20 | CI warns that `actions/checkout@v4`, `setup-node@v4` and `setup-uv@v6` run on deprecated Node 20 | Move to their Node 24 releases | Stage 4 |

Closed items move to the bottom with the stage that closed them.

## Closed

| # | Item | Closed in |
|---|---|---|
| - | Real F1 reference data reachable without F1 TV (TracingInsights archive) | Stage 4 |
