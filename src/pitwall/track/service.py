"""Find (or draft) the track model to use for a session."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from pitwall.analysis.trace import NotComparableError, load_trace
from pitwall.storage import db
from pitwall.track.draft import draft_model
from pitwall.track.model import TrackModel, load_model, save_model, track_key


def session_track_key(session: dict[str, Any]) -> str:
    return track_key(session["track_id"], session["track_name"], bool(session.get("synthetic")))


def draft_for_session(
    conn: sqlite3.Connection,
    data_dir: Path,
    session: dict[str, Any],
    lap_index: int | None = None,
) -> TrackModel:
    """Draft a model from a chosen lap, or the fastest usable lap on this track."""
    if lap_index is not None:
        lap_row = db.get_lap(conn, session["session_key"], lap_index)
        src_session = session
    else:
        found = db.personal_best_lap(
            conn,
            track_id=session["track_id"],
            formula=session["formula"],
            synthetic=session.get("synthetic", 0),
        )
        if found is not None:
            src_session, lap_row = dict(found[0]), found[1]
        else:
            lap_row = db.best_usable_lap(conn, session["session_key"])
            src_session = session
    if lap_row is None:
        raise NotComparableError("no usable lap on this track to detect the corners from")
    lap = dict(lap_row)
    trace = load_trace(data_dir, src_session, lap)
    return draft_model(
        trace,
        track_id=session["track_id"],
        track_name=session["track_name"] or "track",
        synthetic=bool(session.get("synthetic")),
        source={
            "session_key": src_session["session_key"],
            "lap_index": lap["lap_index"],
            "lap_number": lap["lap_number"],
            "lap_time_ms": lap["lap_time_ms"],
        },
    )


def model_for_session(
    conn: sqlite3.Connection, data_dir: Path, session: dict[str, Any]
) -> TrackModel:
    """The saved model for this track, or a new draft (saved) when there is none.

    A saved model whose length does not match the session is never overwritten
    automatically (the game's track may have changed); the user re-drafts it explicitly.
    """
    key = session_track_key(session)
    model = load_model(data_dir, key)
    if model is not None:
        if not model.fits(session["track_length_m"]):
            raise NotComparableError(
                f"track model {key} is {model.length_m:.0f} m but this session's track is "
                f"{session['track_length_m']} m; re-draft it with "
                f"`pitwall track draft {session['session_uid'][-8:]} --force`"
            )
        return model
    model = draft_for_session(conn, data_dir, session)
    save_model(data_dir, model)
    return model
