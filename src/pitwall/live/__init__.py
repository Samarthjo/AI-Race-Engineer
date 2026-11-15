"""Live telemetry (Stage 6).

* :mod:`engine`: the packets that ``pitwall capture run`` forwards (UDP 20778), decoded as
  they arrive: what the Live page shows, laps of the live session, flashbacks, packet health.
* :mod:`reference`: the lap the live delta is measured against (personal best, best lap of
  this session, or a real F1 lap placed on your track).
* :mod:`analysis`: the provisional corner report built seconds after each completed lap.
* :mod:`demo`: a synthetic session replayed into the engine, to try the Live page without
  the PS5 (always marked synthetic).

Recording stays the capture's job: the live engine never writes raw data. Everything it
shows is provisional; ``pitwall ingest`` rebuilds the laps from the raw recording.
"""
