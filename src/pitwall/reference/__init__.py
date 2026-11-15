"""Real Formula 1 reference laps (Stage 4): where they come from, how they are stored.

* :mod:`archive`: downloads from the TracingInsights archive, through a raw cache that keeps
  every downloaded file byte-for-byte (never modified; later reads work offline).
* :mod:`laps`: lap tables and telemetry parsed into Pitwall's common schema, with
  capability flags (binary brake, no steering, about 4 Hz samples).
* :mod:`store`: normalised reference laps (Parquet) and their catalogue (SQLite).
* :mod:`service`: choosing a reference (season, event, session, driver, fastest valid lap),
  the era guard (F1 25 base cars = 2025 season) and the game track to event mapping.
"""
