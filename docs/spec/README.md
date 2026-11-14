# Official F1 25 UDP specification

The packet decoder (Stage 1) must be written and checked against EA's official specification,
not against memory or third-party parsers.

**Needed here:** the official "F1® 25 UDP Specification" published by EA/Codemasters on the EA
Forums (Game Info Hub). The current post (spec version 9.0 at the time of planning) contains both:

- the **F1 25** UDP format (packetFormat 2025): the one we use, since you drive the base 2025 cars
- the **2026 Season Pack** UDP format: postponed

Please download/copy it on the Mac and commit it into this folder, e.g.:

```
docs/spec/F1_25_UDP_Specification_v9.0.pdf   (or .txt / .md with the full struct definitions)
```

The cloud development environment cannot reach forums.ea.com, so this is a manual step.

Status of the layouts in `src/pitwall/f1udp/f125.py`:
- All 16 packet layouts (and the 29-byte header) were cross-checked against two independent
  F1 25 implementations: Rust crate `f1-game-library-models-25` v0.5.0 and npm
  `@deltazeroproduction/f1-udp-parser` v1.2.2. They agree on every packet size and field order.
- `pitwall capture stats` compares the size of every received packet with the spec, so the
  first real recording validates the layouts on actual game data.
- Still to do: line-by-line check against the official document once it is in this folder.
