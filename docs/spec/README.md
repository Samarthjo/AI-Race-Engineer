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

What Stage 0 already assumes from the spec (to re-verify in Stage 1):
- 29-byte little-endian packet header (`src/pitwall/f1udp/header.py`)
- packet ids 0–15 (`PacketId`)
