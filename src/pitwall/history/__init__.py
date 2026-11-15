"""Driver development and history (Stage 7).

* :mod:`build`: the history cache: for every usable lap, segment times, corner metrics and
  findings against its session's best lap, stored with the track model version and the
  analysis version, rebuilt from the recordings when either changes.
* :mod:`trends`: per track, sessions over time, lap-time and corner trends, strongest and
  weakest corners; noise-aware, and never a conclusion from too few laps.
* :mod:`patterns`: recurring findings (how often, in how many sessions).
* :mod:`recommendations`: what to work on after each session, with a target metric, judged
  in the next session as improved, unchanged, worse or inconclusive.
* :mod:`profile`: the driver profile, as facts the race engineer may quote.

Real and synthetic laps are never mixed, and only laps of the same track (and track model)
and the same car class are compared.
"""
