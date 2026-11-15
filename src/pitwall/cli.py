"""Command-line interface: ``pitwall ...``."""

from __future__ import annotations

import json
import platform
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from pitwall import __version__
from pitwall.capture.health import IntervalStats
from pitwall.capture.rawlog import RAW_SUFFIX, compress_segment, find_segments, iter_capture
from pitwall.capture.receiver import CaptureCounters, parse_endpoint
from pitwall.capture.replay import replay as replay_records
from pitwall.capture.session import CaptureRun, analyse_capture, read_manifest
from pitwall.config import Config, load_config
from pitwall.netinfo import local_ipv4s, udp_port_available

app = typer.Typer(no_args_is_help=True, help="Pitwall AI: personal F1 race engineer.")
capture_app = typer.Typer(no_args_is_help=True, help="Record and inspect raw F1 25 UDP telemetry.")
app.add_typer(capture_app, name="capture")
track_app = typer.Typer(
    no_args_is_help=True, help="Track models: corners and straights of a circuit."
)
app.add_typer(track_app, name="track")
ref_app = typer.Typer(
    no_args_is_help=True, help="Real Formula 1 reference laps (2025 season, F1 25 base cars)."
)
app.add_typer(ref_app, name="ref")
engineer_app = typer.Typer(
    no_args_is_help=True,
    help="Race engineer: measured report, AI write-up (local model) and questions.",
)
app.add_typer(engineer_app, name="engineer")
history_app = typer.Typer(
    help="Driver history: progress per track, recurring patterns, recommendation tracking.",
)
app.add_typer(history_app, name="history")

console = Console()

ConfigOpt = Annotated[Path | None, typer.Option("--config", help="Path to config.toml")]
DataDirOpt = Annotated[Path | None, typer.Option("--data-dir", help="Override data directory")]


def _load(config: Path | None, data_dir: Path | None) -> Config:
    cfg = load_config(config)
    if data_dir is not None:
        cfg.data_dir = data_dir.expanduser()
    return cfg


def _ps5_settings(ip: str, port: int, hz: int) -> list[tuple[str, str]]:
    return [
        ("UDP Telemetry", "On"),
        ("UDP Broadcast Mode", "Off"),
        ("UDP IP Address", ip),
        ("UDP Port", str(port)),
        ("UDP Send Rate", f"{hz}Hz"),
        ("UDP Format", "F1 25 (2025), not the 2026 Season Pack format"),
    ]


@app.command()
def version() -> None:
    """Print the Pitwall version."""
    console.print(f"pitwall {__version__}")


@app.command()
def doctor(config: ConfigOpt = None, data_dir: DataDirOpt = None) -> None:
    """Check this machine is ready to capture (data dir, UDP port, network address)."""
    cfg = _load(config, data_dir)
    ok = True
    console.print(
        f"[bold]Pitwall {__version__}[/] · Python {platform.python_version()} · "
        f"{platform.system()} {platform.machine()}"
    )
    console.print(f"Config file: {cfg.source or 'none (defaults)'}")

    try:
        cfg.raw_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=cfg.raw_dir):
            pass
        console.print(f"[green]✓[/] Data directory writable: {cfg.data_dir}")
    except OSError as exc:
        ok = False
        console.print(f"[red]✗[/] Data directory not writable: {cfg.data_dir} ({exc})")

    cap = cfg.capture
    free, why = udp_port_available(cap.bind_host, cap.port)
    if free:
        console.print(f"[green]✓[/] UDP port {cap.port} available")
    else:
        ok = False
        console.print(
            f"[red]✗[/] UDP port {cap.port} not available: {why} "
            "(another capture or telemetry app running?)"
        )

    ips = local_ipv4s()
    if ips:
        console.print(f"[green]✓[/] This machine's LAN address(es): {', '.join(ips)}")
    else:
        ok = False
        console.print("[red]✗[/] No LAN IPv4 address found. Is the Mac on the network?")

    table = Table(title="Enter on the PS5: F1 25 → Settings → Telemetry Settings")
    table.add_column("Setting")
    table.add_column("Value")
    for k, v in _ps5_settings(ips[0] if ips else "<Mac IP>", cap.port, cap.expected_hz):
        table.add_row(k, v)
    console.print(table)
    if sys.platform == "darwin":
        console.print(
            "macOS: allow incoming connections for Python when the firewall asks "
            "(System Settings → Network → Firewall → Options)."
        )
    raise typer.Exit(0 if ok else 1)


# ---------------------------------------------------------------------------- capture


def _status_line(st: IntervalStats, c: CaptureCounters) -> None:
    now = time.strftime("%H:%M:%S")
    if st.packets == 0:
        console.print(f"[dim]{now}[/] waiting for packets… (stored {c.stored})")
        return
    uid = f"{st.session_uid:016x}"[-8:] if st.session_uid is not None else "-"
    loss = f"{st.loss_pct:.2f}%" if st.loss_pct is not None else "n/a"
    fmt_warn = "" if st.packet_format == 2025 else " [yellow](format≠2025)[/]"
    console.print(
        f"[dim]{now}[/] {st.packets_per_s:6.0f} pkt/s · telemetry {st.telemetry_hz:5.1f} Hz · "
        f"loss {loss} · fmt {st.packet_format}{fmt_warn} v{st.game_version} · session …{uid} · "
        f"from {','.join(st.sources) or '-'} · stored {c.stored}"
        + (f" · [red]forward errors {c.forward_errors}[/]" if c.forward_errors else "")
        + (f" · [yellow]rejected {c.rejected_source}[/]" if c.rejected_source else "")
    )


@capture_app.command("run")
def capture_run(
    config: ConfigOpt = None,
    data_dir: DataDirOpt = None,
    port: Annotated[int | None, typer.Option(help="UDP port to listen on")] = None,
    forward: Annotated[
        list[str] | None, typer.Option("--forward", help="host:port to forward packets to")
    ] = None,
    no_forward: Annotated[bool, typer.Option("--no-forward", help="Do not forward")] = False,
    allow_source: Annotated[
        list[str] | None, typer.Option("--allow-source", help="Only accept packets from this IP")
    ] = None,
    no_compress: Annotated[bool, typer.Option("--no-compress")] = False,
    expected_hz: Annotated[int | None, typer.Option(help="Configured PS5 send rate")] = None,
    label: Annotated[str | None, typer.Option(help="Short label, e.g. monza-tt-clean")] = None,
    note: Annotated[str | None, typer.Option(help="Free-text note stored with the capture")] = None,
) -> None:
    """Record UDP telemetry until Ctrl+C. Stores every packet raw; forwards a copy."""
    cfg = _load(config, data_dir)
    s = cfg.capture
    if port is not None:
        s.port = port
    if forward:
        s.forward = list(forward)
    if no_forward:
        s.forward = []
    if allow_source:
        s.allow_sources = list(allow_source)
    if no_compress:
        s.compress = False
    if expected_hz is not None:
        s.expected_hz = expected_hz
    for f in s.forward:
        parse_endpoint(f)

    run = CaptureRun(cfg.raw_dir, s, label=label, note=note, on_status=_status_line)
    try:
        run.prepare()
    except OSError as exc:
        console.print(f"[red]Cannot listen on UDP {s.bind_host}:{s.port}: {exc}[/]")
        raise typer.Exit(1) from exc

    def _stop(signum: int, _frame: Any) -> None:
        run.stop_event.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    ips = ", ".join(local_ipv4s()) or "unknown"
    console.print(f"[bold green]Capturing[/] on UDP {s.bind_host}:{s.port} · Mac IP(s): {ips}")
    console.print(f"Writing to {run.directory}")
    console.print(f"Forwarding to: {', '.join(s.forward) or 'nowhere'} · Ctrl+C to stop")
    manifest = run.run()

    console.print(
        f"\n[bold]Capture {run.capture_id} stopped.[/] "
        f"{manifest['counters']['stored']} packets stored."
    )
    _print_checks(manifest["health"]["checks"])
    if manifest["compression_errors"]:
        console.print("[yellow]Compression errors (raw files kept):[/]")
        for err in manifest["compression_errors"]:
            console.print(f"  {err}")
    console.print(f"Details: pitwall capture stats {run.capture_id}")


def _resolve_capture(path_or_id: str, cfg: Config) -> Path:
    p = Path(path_or_id).expanduser()
    if p.exists():
        return p
    candidate = cfg.raw_dir / path_or_id
    if candidate.exists():
        return candidate
    console.print(f"[red]No capture found at {p} or {candidate}[/]")
    raise typer.Exit(1)


def _print_checks(checks: list[dict[str, str]]) -> None:
    style = {"ok": "[green]✓[/]", "warn": "[yellow]![/]", "fail": "[red]✗[/]", "info": "[blue]i[/]"}
    for c in checks:
        console.print(f"  {style.get(c['status'], '?')} {c['name']}: {c['detail']}")


@capture_app.command("stats")
def capture_stats(
    capture: Annotated[str, typer.Argument(help="Capture id, directory or segment file")],
    config: ConfigOpt = None,
    data_dir: DataDirOpt = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the full report as JSON")] = False,
    expected_hz: Annotated[int, typer.Option(help="Configured PS5 send rate")] = 60,
) -> None:
    """Health report for a stored capture: rates, formats, sessions, loss, gaps."""
    cfg = _load(config, data_dir)
    path = _resolve_capture(capture, cfg)
    health, summary = analyse_capture(path, expected_hz)
    report = health.report()
    report["read"] = summary.__dict__
    if as_json:
        console.print_json(json.dumps(report))
        return

    console.print(f"[bold]{path}[/]")
    console.print(
        f"{report['packets']} packets · {report['bytes'] / 1e6:.1f} MB · "
        f"active {report['active_s']:.0f} s · {report['first_utc']} → {report['last_utc']}"
    )
    if summary.truncated_segments:
        console.print(
            f"[yellow]Truncated segments (crash/kill; data up to the cut is valid): "
            f"{summary.truncated_segments}[/]"
        )
    for err in summary.errors:
        console.print(f"[red]{err}[/]")
    _print_checks(report["checks"])

    t = Table(title="Packet types")
    for col in ("id", "name", "count", "rate Hz", "sizes (bytes: n)"):
        t.add_column(col)
    for row in report["packet_types"]:
        sizes = ", ".join(f"{k}: {v}" for k, v in row["sizes"].items())
        t.add_row(str(row["id"]), row["name"], str(row["count"]), str(row["rate_hz"]), sizes)
    console.print(t)

    t = Table(title="Sessions")
    for col in ("session", "packets", "active s", "session time", "flashbacks", "loss %"):
        t.add_column(col)
    for s in report["sessions"]:
        span = s["session_time_span_s"]
        t.add_row(
            s["session_uid"],
            str(s["packets"]),
            str(s["active_s"]),
            f"{span[0]:.1f}-{span[1]:.1f}" if span[0] is not None else "-",
            str(s["flashback_rewinds"]),
            "n/a" if s["loss_pct"] is None else f"{s['loss_pct']:.2f}",
        )
    console.print(t)
    gaps = report["gaps"]
    if gaps["count"]:
        console.print(
            f"{gaps['count']} silences > {gaps['threshold_s']:.0f} s (pauses/menus); "
            "largest: " + ", ".join(f"{g['duration_s']} s" for g in gaps["largest"][:5])
        )


@capture_app.command("list")
def capture_list(config: ConfigOpt = None, data_dir: DataDirOpt = None) -> None:
    """List recorded captures."""
    cfg = _load(config, data_dir)
    if not cfg.raw_dir.exists():
        console.print(f"No captures in {cfg.raw_dir}")
        return
    t = Table(title=str(cfg.raw_dir))
    t.add_column("capture", no_wrap=True)
    for col in ("label", "status", "packets", "active s", "loss %", "MB"):
        t.add_column(col)
    for d in sorted(p for p in cfg.raw_dir.iterdir() if p.is_dir()):
        m = read_manifest(d) or {}
        h = m.get("health", {})
        size = sum(f.stat().st_size for f in d.iterdir() if f.is_file()) / 1e6
        loss = h.get("loss", {}).get("pct")
        t.add_row(
            d.name,
            m.get("label") or "",
            m.get("status", "?"),
            str(h.get("packets", "")),
            str(h.get("active_s", "")),
            "" if loss is None else f"{loss:.2f}",
            f"{size:.1f}",
        )
    console.print(t)


@capture_app.command("replay")
def capture_replay(
    capture: Annotated[str, typer.Argument(help="Capture id, directory or segment file")],
    to: Annotated[str, typer.Option("--to", help="host:port to send to")] = "127.0.0.1:20778",
    speed: Annotated[float, typer.Option(help="1 = real time, 0 = as fast as possible")] = 1.0,
    config: ConfigOpt = None,
    data_dir: DataDirOpt = None,
) -> None:
    """Re-send a stored capture as UDP packets (develop and test without the PS5)."""
    cfg = _load(config, data_dir)
    path = _resolve_capture(capture, cfg)
    target = parse_endpoint(to)
    console.print(f"Replaying {path} → {to} at {'max' if speed == 0 else f'{speed}x'} speed")
    try:
        result = replay_records(iter_capture(path), target, speed=speed)
    except KeyboardInterrupt:
        console.print("Stopped.")
        raise typer.Exit(130) from None
    console.print(f"Sent {result.sent} packets in {result.seconds:.1f} s")


@capture_app.command("compact")
def capture_compact(
    capture: Annotated[str, typer.Argument(help="Capture id or directory")],
    force: Annotated[
        bool, typer.Option(help="Compact even if the capture looks still running")
    ] = False,
    config: ConfigOpt = None,
    data_dir: DataDirOpt = None,
) -> None:
    """Compress leftover uncompressed segments (e.g. after a crash). Lossless and verified."""
    cfg = _load(config, data_dir)
    path = _resolve_capture(capture, cfg)
    manifest = read_manifest(path) if path.is_dir() else None
    if manifest and manifest.get("status") == "running" and not force:
        console.print(
            "[yellow]Manifest says this capture is still running. "
            "Stop it first, or pass --force if it crashed.[/]"
        )
        raise typer.Exit(1)
    raw = [p for p in find_segments(path) if p.name.endswith(RAW_SUFFIX)]
    if not raw:
        console.print("Nothing to compact.")
        return
    for seg in raw:
        out = compress_segment(seg)
        console.print(f"  {seg.name} → {out.name}")


# --------------------------------------------------------------- sessions and laps


def _fmt_ms(ms: int | None) -> str:
    if ms is None:
        return "-"
    minutes, rest = divmod(int(ms), 60_000)
    secs = rest / 1000
    return f"{minutes}:{secs:06.3f}" if minutes else f"{secs:.3f}"


def _db(cfg: Config):
    from pitwall.ingest.pipeline import DB_NAME
    from pitwall.storage import db

    return db.connect(cfg.data_dir / DB_NAME)


def _pick_session(conn: Any, query: str):
    from pitwall.storage import db

    matches = db.find_sessions(conn, query)
    if not matches:
        console.print(f"[red]No session matches {query!r}. See: pitwall sessions[/]")
        raise typer.Exit(1)
    if len(matches) > 1:
        console.print(f"[yellow]{len(matches)} sessions match {query!r}; be more specific:[/]")
        for m in matches[:10]:
            console.print(f"  {m['session_key']}  {m['track_name']}  {m['session_type_name']}")
        raise typer.Exit(1)
    return matches[0]


@app.command()
def ingest(
    captures: Annotated[
        list[str] | None, typer.Argument(help="Capture ids or directories (default: all new)")
    ] = None,
    force: Annotated[bool, typer.Option(help="Re-ingest captures already ingested")] = False,
    config: ConfigOpt = None,
    data_dir: DataDirOpt = None,
) -> None:
    """Decode captures into sessions and laps (safe to re-run; results are replaced)."""
    from pitwall.ingest.pipeline import PIPELINE_VERSION, ingest_capture
    from pitwall.storage import db

    cfg = _load(config, data_dir)
    conn = _db(cfg)
    if captures:
        targets = [_resolve_capture(c, cfg) for c in captures]
    else:
        done = db.ingested(conn)
        targets = []
        for d in (
            sorted(p for p in cfg.raw_dir.glob("*") if p.is_dir()) if cfg.raw_dir.exists() else []
        ):
            m = read_manifest(d) or {}
            if m.get("status") == "running":
                console.print(f"[dim]skip {d.name}: capture still running[/]")
                continue
            if not force and done.get(d.name) == PIPELINE_VERSION:
                continue
            targets.append(d)
    if not targets:
        console.print("Nothing new to ingest. (Use --force to re-ingest everything.)")
        return
    for path in targets:
        if not path.is_dir():
            console.print(f"[red]{path} is not a capture directory[/]")
            raise typer.Exit(1)
        res = ingest_capture(path, cfg.data_dir, conn)
        err = sum(res.errors.values())
        console.print(
            f"[bold]{res.capture_id}[/]: {res.decoded}/{res.datagrams} packets decoded"
            + (f", [yellow]{err} not decoded {res.errors}[/]" if err else "")
        )
        for ex in res.error_examples[:3]:
            console.print(f"    [dim]{ex}[/]")
        if res.read.truncated_segments:
            console.print(f"  [yellow]truncated segments: {res.read.truncated_segments}[/]")
        for so in res.sessions:
            s = so.summary
            console.print(
                f"  {s['track_name']} · {s['session_type_name']} · {s['lap_count']} laps "
                f"({s['usable_laps']} usable) · best usable {_fmt_ms(s['best_usable_lap_ms'])} "
                f"· session …{s['session_uid'][-8:]}"
            )
        if res.skipped_sessions:
            console.print(
                f"  [dim]{len(res.skipped_sessions)} session(s) without lap data skipped[/]"
            )
    _update_history(cfg, conn)


def _update_history(cfg: Config, conn: Any) -> None:
    """Bring the history cache up to date (only sessions that are new or out of date)."""
    from pitwall.history.service import ensure

    built = ensure(conn, cfg.data_dir)
    if built["sessions"]:
        console.print(
            f"History: {built['sessions']} session(s) analysed ({built['laps']} laps) in "
            f"{built['seconds']} s."
        )


@app.command()
def sessions(
    limit: Annotated[int, typer.Option(help="How many to show")] = 20,
    config: ConfigOpt = None,
    data_dir: DataDirOpt = None,
) -> None:
    """List ingested sessions, newest first."""
    from pitwall.storage import db

    cfg = _load(config, data_dir)
    rows = db.list_sessions(_db(cfg), limit=limit)
    if not rows:
        console.print("No sessions yet. Record with `pitwall capture run`, then `pitwall ingest`.")
        return
    t = Table(title="Sessions (use the session id with `pitwall laps`)")
    t.add_column("session id", no_wrap=True)
    for col in ("started (UTC)", "track", "type", "laps", "usable", "best usable", "flashbacks"):
        t.add_column(col)
    for r in rows:
        t.add_row(
            r["session_uid"][-8:],
            (r["first_utc"] or "")[:16].replace("T", " "),
            r["track_name"] or "-",
            r["session_type_name"] or "-",
            str(r["lap_count"]),
            str(r["usable_laps"]),
            _fmt_ms(r["best_usable_lap_ms"]),
            str(r["flashbacks"]),
        )
    console.print(t)


@app.command()
def laps(
    session: Annotated[str, typer.Argument(help="Session id (or any unique part), or 'latest'")],
    config: ConfigOpt = None,
    data_dir: DataDirOpt = None,
) -> None:
    """Lap table for one session: times, sectors, validity, data quality."""
    from pitwall.f1udp import enums
    from pitwall.storage import db

    cfg = _load(config, data_dir)
    conn = _db(cfg)
    s = _pick_session(conn, session)
    console.print(
        f"[bold]{s['track_name']}[/] · {s['session_type_name']} · {s['game_mode_name']} · "
        f"game v{s['game_version']} · {s['first_utc']} · session …{s['session_uid'][-8:]}"
    )
    assists = json.loads(s["assists_json"] or "{}")
    if assists:
        console.print(
            "Assists (session): braking "
            + enums.name(enums.BRAKING_ASSIST, assists.get("braking_assist"))
            + " · gearbox "
            + enums.name(enums.GEARBOX_ASSIST, assists.get("gearbox_assist"))
            + " · steering "
            + enums.name(enums.ON_OFF, assists.get("steering_assist"))
            + " · racing line "
            + enums.name(enums.RACING_LINE, assists.get("dynamic_racing_line"))
        )
    t = Table()
    for col in ("#", "lap", "kind", "time", "S1", "S2", "S3", "valid", "usable", "tyre", "loss %"):
        t.add_column(col, no_wrap=col in ("time", "S1", "S2", "S3"))
    t.add_column("issues / notes")
    for r in db.list_laps(conn, s["session_key"]):
        issues = json.loads(r["issues_json"])
        notes = json.loads(r["notes_json"])
        valid = {None: "-", 1: "[green]yes[/]", 0: "[red]no[/]"}[r["valid"]]
        tyre = (
            f"{enums.name(enums.VISUAL_COMPOUNDS, r['tyre_visual'], 'tyre')} ({r['tyre_age_laps']})"
            if r["tyre_visual"] is not None
            else "-"
        )
        text = "; ".join([f"[red]{i}[/]" for i in issues] + [f"[dim]{n}[/]" for n in notes])
        t.add_row(
            str(r["lap_index"]),
            str(r["lap_number"]),
            r["kind"],
            _fmt_ms(r["lap_time_ms"]),
            _fmt_ms(r["s1_ms"]),
            _fmt_ms(r["s2_ms"]),
            _fmt_ms(r["s3_ms"]),
            valid,
            "[green]yes[/]" if r["usable"] else "no",
            tyre,
            f"{r['loss_pct']:.1f}",
            text,
        )
    console.print(t)
    console.print("[dim]# = lap index (use with `pitwall export`); lap = game lap number[/]")


@app.command()
def export(
    session: Annotated[str, typer.Argument(help="Session id (or unique part), or 'latest'")],
    lap: Annotated[
        int | None, typer.Option(help="Lap index from `pitwall laps` (default: all)")
    ] = None,
    out: Annotated[Path | None, typer.Option(help="Output file (.csv or .parquet)")] = None,
    config: ConfigOpt = None,
    data_dir: DataDirOpt = None,
) -> None:
    """Export per-frame player telemetry of a session or one lap to CSV/Parquet."""
    import pyarrow as pa
    import pyarrow.csv as pacsv
    import pyarrow.parquet as pq

    from pitwall.ingest.pipeline import read_frames

    cfg = _load(config, data_dir)
    s = _pick_session(_db(cfg), session)
    table = read_frames(cfg.data_dir / s["frames_path"])
    if lap is not None:
        table = table.filter(pa.array(table["lap_index"].to_numpy() == lap))
        if table.num_rows == 0:
            console.print(f"[red]No frames for lap index {lap}[/]")
            raise typer.Exit(1)
    if out is None:
        suffix = f"_lap{lap}" if lap is not None else ""
        out = Path(f"{s['session_uid'][-8:]}{suffix}.csv")
    if out.suffix == ".parquet":
        pq.write_table(table, out, compression="zstd")
    else:
        pacsv.write_csv(table, out)
    console.print(f"Wrote {table.num_rows} frames x {table.num_columns} columns to {out}")


@app.command()
def decode(
    capture: Annotated[str, typer.Argument(help="Capture id, directory or segment file")],
    packet: Annotated[
        str, typer.Option(help="Packet type name or id, e.g. CAR_TELEMETRY")
    ] = "CAR_TELEMETRY",
    limit: Annotated[int, typer.Option(help="How many packets to print")] = 1,
    skip: Annotated[int, typer.Option(help="Skip this many matching packets first")] = 0,
    all_cars: Annotated[bool, typer.Option("--all-cars", help="Include all 22 cars")] = False,
    config: ConfigOpt = None,
    data_dir: DataDirOpt = None,
) -> None:
    """Print decoded packets as JSON (to compare values with what the game shows)."""
    from pitwall.f1udp.decoder import DecodeError, decode_packet, to_python
    from pitwall.f1udp.header import PacketHeader, PacketId, PacketTooShortError

    cfg = _load(config, data_dir)
    path = _resolve_capture(capture, cfg)
    try:
        pid = int(packet) if packet.isdigit() else int(PacketId[packet.upper()])
    except KeyError:
        console.print(
            f"[red]Unknown packet type {packet!r}. Known: {[p.name for p in PacketId]}[/]"
        )
        raise typer.Exit(1) from None
    shown = seen = 0
    for rec in iter_capture(path):
        try:
            hdr = PacketHeader.unpack(rec.payload)
        except PacketTooShortError:
            continue
        if hdr.packet_id != pid:
            continue
        seen += 1
        if seen <= skip:
            continue
        try:
            pkt = decode_packet(rec.payload)
        except DecodeError as exc:
            console.print(f"[red]{exc}[/]")
            continue
        data = to_python(pkt)
        if "car" in data and isinstance(data["car"], list) and not all_cars:
            idx = hdr.player_car_index
            data["car"] = {"player_car_index": idx, "data": data["car"][idx] if idx < 22 else None}
        console.print_json(json.dumps(data))
        shown += 1
        if shown >= limit:
            break
    if shown == 0:
        console.print(f"No decodable {PacketId(pid).name} packets found.")


# ------------------------------------------------------------------ analysis and UI


@app.command("compare")
def compare_cmd(
    session: Annotated[str, typer.Argument(help="Session id (or unique part), or 'latest'")],
    lap: Annotated[int, typer.Option(help="Lap index (# in `pitwall laps`)")],
    ref: Annotated[
        str,
        typer.Option(help="session_best | personal_best | theoretical_best | lap"),
    ] = "session_best",
    ref_lap: Annotated[
        int | None, typer.Option(help="With --ref lap: the reference lap index")
    ] = None,
    ref_session: Annotated[
        str | None, typer.Option(help="With --ref lap: session of the reference lap")
    ] = None,
    config: ConfigOpt = None,
    data_dir: DataDirOpt = None,
) -> None:
    """Compare a lap with a reference: time delta, sectors, where time is lost, lap metrics."""
    from pitwall.analysis.compare import compare as run_compare
    from pitwall.analysis.compare import resolve_reference
    from pitwall.analysis.metrics import DEFINITIONS, load_lap_metrics
    from pitwall.analysis.trace import NotComparableError, load_trace
    from pitwall.storage import db

    cfg = _load(config, data_dir)
    conn = _db(cfg)
    s = dict(_pick_session(conn, session))
    lap_row = db.get_lap(conn, s["session_key"], lap)
    if lap_row is None:
        console.print(f"[red]No lap #{lap} in this session. See: pitwall laps {session}[/]")
        raise typer.Exit(1)
    ref_key = None
    if ref == "lap":
        ref_key = (
            dict(_pick_session(conn, ref_session))["session_key"]
            if ref_session
            else s["session_key"]
        )
    try:
        trace = load_trace(cfg.data_dir, s, dict(lap_row))
        reference = resolve_reference(
            conn, cfg.data_dir, s, ref, ref_session_key=ref_key, ref_lap_index=ref_lap
        )
        c = run_compare(trace, reference)
    except NotComparableError as exc:
        console.print(f"[red]Cannot compare: {exc}[/]")
        raise typer.Exit(1) from None

    ref_ms = reference.official_time_ms
    lap_ms = lap_row["lap_time_ms"]
    delta = (lap_ms - ref_ms) / 1000 if ref_ms else float(c.delta[-1])
    sign = "[red]+" if delta > 0 else "[green]"
    console.print(
        f"[bold]{s['track_name']}[/] · lap {lap_row['lap_number']} {_fmt_ms(lap_ms)} vs "
        f"{reference.label} {_fmt_ms(ref_ms)}  →  {sign}{delta:.3f} s[/]"
    )
    t = Table(title="Sectors (game's official times)")
    for col in ("sector", "this lap", "reference", "delta"):
        t.add_column(col)
    for sec in c.sectors:
        d = sec["delta_s"]
        t.add_row(
            f"S{sec['sector']}",
            _fmt_ms(sec["lap_ms"]),
            _fmt_ms(sec["ref_ms"]),
            ("[red]+" if d > 0 else "[green]") + f"{d:.3f}[/]",
        )
    console.print(t)

    def windows(title: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            console.print(f"{title}: none above 0.010 s")
            return
        console.print(title + ":")
        for w in rows:
            console.print(f"  {w['start_m']:.0f}-{w['end_m']:.0f} m: {w['seconds']:+.3f} s")

    windows("Largest losses over 200 m", c.largest_losses)
    windows("Largest gains over 200 m", c.largest_gains)

    lap_m = load_lap_metrics(cfg.data_dir, s, dict(lap_row))
    ref_m: dict[str, Any] | None = None
    if len(reference.sources) == 1:
        src = reference.sources[0]
        src_s = db.get_session(conn, src["session_key"])
        src_l = db.get_lap(conn, src["session_key"], src["lap_index"])
        if src_s is not None and src_l is not None:
            ref_m = load_lap_metrics(cfg.data_dir, dict(src_s), dict(src_l))
    mt = Table(title="Lap metrics")
    for col in ("metric", "this lap", "reference"):
        mt.add_column(col)
    for key in DEFINITIONS:
        mt.add_row(
            DEFINITIONS[key],
            str(lap_m.get(key)),
            "-" if ref_m is None else str(ref_m.get(key)),
        )
    console.print(mt)
    console.print(
        "[dim]Delta = this lap minus reference; positive means slower. "
        "Measured from your own laps (60 Hz).[/]"
    )


@app.command()
def demo(
    config: ConfigOpt = None,
    data_dir: DataDirOpt = None,
) -> None:
    """Create a clearly labelled synthetic demo session, to explore the app without the PS5."""
    from pitwall.ingest.pipeline import ingest_capture
    from pitwall.synth.demo import write_demo_capture, write_demo_references

    cfg = _load(config, data_dir)
    path = write_demo_capture(cfg.raw_dir)
    conn = _db(cfg)
    res = ingest_capture(path, cfg.data_dir, conn)
    s = res.sessions[0].summary
    console.print(
        f"Created [bold]{res.capture_id}[/]: {s['lap_count']} synthetic laps "
        f"(session …{s['session_uid'][-8:]}). It is marked as synthetic everywhere and never "
        "counts as your own lap."
    )
    keys = write_demo_references(cfg.data_dir, conn)
    console.print(
        f"Stored {len(keys)} synthetic reference laps (drivers PRO and FST) for the Driver "
        "Benchmark; they stand in for real F1 data and are only compared with synthetic laps."
    )
    _update_history(cfg, conn)
    console.print(
        "Try: [bold]uv run pitwall laps latest[/], [bold]uv run pitwall benchmark latest "
        "--lap 1 --driver PRO[/]  or  [bold]uv run pitwall serve[/]"
    )


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Address to serve on")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Web UI / API port")] = 8000,
    live_port: Annotated[
        int, typer.Option(help="UDP port with the capture's forwarded copy")
    ] = 20778,
    no_live: Annotated[bool, typer.Option("--no-live", help="Disable the live status")] = False,
    open_browser: Annotated[bool, typer.Option("--open", help="Open the browser")] = False,
    config: ConfigOpt = None,
    data_dir: DataDirOpt = None,
) -> None:
    """Start the local web app (Lap Analysis) and API."""
    import threading
    import webbrowser

    import uvicorn

    from pitwall.api.app import STATIC_DIR, create_app
    from pitwall.live.engine import LiveEngine

    cfg = _load(config, data_dir)
    live = None if no_live else LiveEngine("127.0.0.1", live_port, data_dir=cfg.data_dir)
    application = create_app(cfg.data_dir, live, cfg.reference, cfg.engineer)
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}"
    console.print(f"[bold green]Pitwall[/] running at {url}  (Ctrl+C to stop)")
    if not (STATIC_DIR / "index.html").exists():
        console.print("[yellow]Web UI not built; only the API is available (see web/README.md).[/]")
    if live is not None:
        console.print(
            f"Live telemetry listens on UDP {live_port} (the copy `pitwall capture run` "
            "forwards); open Live Telemetry in the app."
        )
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    uvicorn.run(application, host=host, port=port, log_level="warning")


# ---------------------------------------------------------------- corners and tracks


def _m(v: float | None, digits: int = 0) -> str:
    return "-" if v is None else f"{v:.{digits}f}"


def _signed(v: float | None, digits: int = 3) -> str:
    if v is None:
        return "-"
    color = "red" if v > 0.0005 else ("green" if v < -0.0005 else "white")
    return f"[{color}]{v:+.{digits}f}[/]"


@app.command("corners")
def corners_cmd(
    session: Annotated[str, typer.Argument(help="Session id (or unique part), or 'latest'")],
    lap: Annotated[int, typer.Option(help="Lap index (# in `pitwall laps`)")],
    ref: Annotated[
        str, typer.Option(help="session_best | personal_best | theoretical_best | lap")
    ] = "session_best",
    ref_lap: Annotated[int | None, typer.Option(help="With --ref lap: reference lap index")] = None,
    config: ConfigOpt = None,
    data_dir: DataDirOpt = None,
) -> None:
    """Corner-by-corner analysis of a lap: where time is lost and why (measured findings)."""
    from pitwall.analysis.compare import compare as run_compare
    from pitwall.analysis.compare import resolve_reference
    from pitwall.analysis.corners import analyse_corners
    from pitwall.analysis.findings import lap_findings
    from pitwall.analysis.trace import NotComparableError, load_trace
    from pitwall.storage import db
    from pitwall.track.service import model_for_session

    cfg = _load(config, data_dir)
    conn = _db(cfg)
    s = dict(_pick_session(conn, session))
    lap_row = db.get_lap(conn, s["session_key"], lap)
    if lap_row is None:
        console.print(f"[red]No lap #{lap} in this session.[/]")
        raise typer.Exit(1)
    try:
        trace = load_trace(cfg.data_dir, s, dict(lap_row))
        reference = resolve_reference(
            conn, cfg.data_dir, s, ref, ref_session_key=s["session_key"], ref_lap_index=ref_lap
        )
        comparison = run_compare(trace, reference)
        model = model_for_session(conn, cfg.data_dir, s)
    except NotComparableError as exc:
        console.print(f"[red]Cannot analyse: {exc}[/]")
        raise typer.Exit(1) from None
    analysis = analyse_corners(comparison, model)
    console.print(
        f"[bold]{s['track_name']}[/] · lap {lap_row['lap_number']} vs {reference.label} · "
        f"delta {_signed(analysis.lap_delta_s)} s"
    )
    status = "[yellow]draft[/]" if model.status == "draft" else "[green]confirmed[/]"
    console.print(
        f"Track model [bold]{model.key}[/] v{model.version} ({status}): "
        f"{len(model.corners)} corners"
        + (
            "  [dim]check with `pitwall track show`, then `pitwall track confirm`[/]"
            if model.status == "draft"
            else ""
        )
    )
    t = Table(title="Corners (lap / reference)")
    for col in (
        "segment",
        "time lost",
        "of which exit carry",
        "brake point",
        "entry km/h",
        "min km/h",
        "full throttle",
        "exit km/h",
    ):
        t.add_column(col)
    for r in analysis.segments:
        seg = r.segment
        if not seg.is_corner:
            t.add_row(f"[dim]{seg.name}[/]", _signed(r.total_s), "", "", "", "", "", "")
            continue
        a, b = r.lap, r.ref
        assert a is not None and b is not None
        bp = (
            "-"
            if a.brake_onset_m is None or b.brake_onset_m is None
            else (
                f"{b.brake_onset_m - a.brake_onset_m:.0f} m earlier"
                if a.brake_onset_m < b.brake_onset_m - 2.0
                else (
                    f"{a.brake_onset_m - b.brake_onset_m:.0f} m later"
                    if a.brake_onset_m > b.brake_onset_m + 2.0
                    else "same"
                )
            )
        )
        ft = (
            "-"
            if a.full_throttle_m is None or b.full_throttle_m is None
            else f"{a.full_throttle_m - b.full_throttle_m:+.0f} m"
        )
        t.add_row(
            f"[bold]{seg.name}[/] [dim]{seg.kind}[/]",
            _signed(r.total_s),
            _signed(r.carried_s) if r.carried_s else "",
            bp,
            f"{_m(a.entry_speed_kmh)} / {_m(b.entry_speed_kmh)}",
            f"{_m(a.min_speed_kmh)} / {_m(b.min_speed_kmh)}",
            ft,
            f"{_m(a.exit_speed_kmh)} / {_m(b.exit_speed_kmh)}",
        )
    console.print(t)
    console.print(
        f"[dim]Segments add up to {analysis.accounted_s():+.3f} s "
        f"(lap delta {analysis.lap_delta_s:+.3f} s).[/]"
    )
    findings = lap_findings(analysis)
    if not findings:
        console.print("No findings: no measured difference above the thresholds.")
        return
    console.print("[bold]Findings[/] (biggest first; impacts within one corner can overlap):")
    for i, f in enumerate(findings, 1):
        console.print(
            f" {i}. [bold]{f.segment_name}[/] · {f.title} · {_signed(f.time_impact_s)} s · "
            f"{f.confidence} confidence"
        )
        console.print(f"    {f.detail}")
        if f.suggestion:
            console.print(f"    [cyan]Try:[/] {f.suggestion}")


def _resolve_track(conn: Any, cfg: Config, key_or_session: str) -> str:
    """A track model key as given, or the key of the track a session was driven on."""
    from pitwall.track.model import load_model
    from pitwall.track.service import session_track_key

    try:
        if load_model(cfg.data_dir, key_or_session) is not None:
            return key_or_session
    except ValueError:
        pass  # not a valid key: treat it as a session id
    return session_track_key(dict(_pick_session(conn, key_or_session)))


@track_app.command("list")
def track_list(config: ConfigOpt = None, data_dir: DataDirOpt = None) -> None:
    """List saved track models."""
    from pitwall.track.model import list_models

    cfg = _load(config, data_dir)
    models = list_models(cfg.data_dir)
    if not models:
        console.print(
            "No track models yet; one is drafted automatically the first time you "
            "analyse corners (or run `pitwall track draft <session>`)."
        )
        return
    t = Table()
    for col in ("key", "track", "status", "version", "corners", "length m", "drafted from"):
        t.add_column(col)
    for m in models:
        src = m.source or {}
        t.add_row(
            m.key,
            m.track_name,
            m.status,
            str(m.version),
            str(len(m.corners)),
            f"{m.length_m:.0f}",
            f"lap {src.get('lap_number', '?')} · {src.get('session_key', '')}",
        )
    console.print(t)


@track_app.command("show")
def track_show(
    track: Annotated[str, typer.Argument(help="Track model key, or a session id")],
    config: ConfigOpt = None,
    data_dir: DataDirOpt = None,
) -> None:
    """Show the segments of a track model."""
    from pitwall.track.model import load_model, model_path

    cfg = _load(config, data_dir)
    key = _resolve_track(_db(cfg), cfg, track)
    m = load_model(cfg.data_dir, key)
    if m is None:
        console.print(f"[red]No track model {key}. Draft one: pitwall track draft <session>[/]")
        raise typer.Exit(1)
    console.print(
        f"[bold]{m.key}[/] · {m.track_name} · {m.status} v{m.version} · "
        f"{m.length_m:.0f} m · file {model_path(cfg.data_dir, key)}"
    )
    errors = m.validate()
    for e in errors:
        console.print(f"[red]{e}[/]")
    t = Table()
    for col in ("id", "name", "kind", "from m", "to m", "apex m"):
        t.add_column(col)
    for seg in m.segments:
        t.add_row(
            seg.id,
            seg.name,
            seg.kind,
            f"{seg.start_m:.0f}",
            f"{seg.end_m:.0f}",
            ", ".join(f"{a:.0f}" for a in seg.apexes_m),
        )
    console.print(t)


@track_app.command("draft")
def track_draft(
    session: Annotated[str, typer.Argument(help="Session id (or unique part), or 'latest'")],
    lap: Annotated[int | None, typer.Option(help="Lap index to detect corners from")] = None,
    force: Annotated[bool, typer.Option(help="Replace an existing (even confirmed) model")] = False,
    config: ConfigOpt = None,
    data_dir: DataDirOpt = None,
) -> None:
    """Detect corners automatically from a fast lap and save them as a draft model."""
    from pitwall.analysis.trace import NotComparableError
    from pitwall.track.model import load_model, save_model
    from pitwall.track.service import draft_for_session, session_track_key

    cfg = _load(config, data_dir)
    conn = _db(cfg)
    s = dict(_pick_session(conn, session))
    key = session_track_key(s)
    existing = load_model(cfg.data_dir, key)
    if existing is not None and not force:
        console.print(
            f"[yellow]{key} already exists ({existing.status} v{existing.version}). "
            "Use --force to replace it.[/]"
        )
        raise typer.Exit(1)
    try:
        m = draft_for_session(conn, cfg.data_dir, s, lap)
    except NotComparableError as exc:
        console.print(f"[red]Cannot draft: {exc}[/]")
        raise typer.Exit(1) from None
    if existing is not None:
        m.version = existing.version + 1
    path = save_model(cfg.data_dir, m)
    console.print(f"Drafted [bold]{m.key}[/]: {len(m.corners)} corners → {path}")
    console.print(
        "Check it with `pitwall track show` (or on the map in `pitwall serve`), "
        "then `pitwall track confirm`."
    )


@track_app.command("confirm")
def track_confirm(
    track: Annotated[str, typer.Argument(help="Track model key, or a session id")],
    config: ConfigOpt = None,
    data_dir: DataDirOpt = None,
) -> None:
    """Mark a track model as checked; it is then used unchanged for every lap."""
    from pitwall.track.model import confirm_model

    cfg = _load(config, data_dir)
    key = _resolve_track(_db(cfg), cfg, track)
    try:
        m = confirm_model(cfg.data_dir, key)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from None
    console.print(f"[green]{m.key} confirmed[/] (v{m.version}, {len(m.corners)} corners).")


@track_app.command("rename")
def track_rename(
    track: Annotated[str, typer.Argument(help="Track model key, or a session id")],
    segment: Annotated[str, typer.Argument(help="Segment id, e.g. c3")],
    name: Annotated[str, typer.Argument(help="New name")],
    config: ConfigOpt = None,
    data_dir: DataDirOpt = None,
) -> None:
    """Rename a corner or straight."""
    from pitwall.track.model import rename_segment

    cfg = _load(config, data_dir)
    key = _resolve_track(_db(cfg), cfg, track)
    try:
        m = rename_segment(cfg.data_dir, key, segment, name)
    except (FileNotFoundError, KeyError) as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from None
    console.print(f"Renamed {segment} in {m.key} (v{m.version}).")


# ------------------------------------------------------------- real-F1 references


SeasonOpt = Annotated[int, typer.Option(help="Season; F1 25 base cars are 2025 cars")]
SyntheticOpt = Annotated[
    bool, typer.Option("--synthetic", help="Use the demo's synthetic archive (not real data)")
]


def _archive(cfg: Config, season: int, synthetic: bool):
    from pitwall.reference.service import make_archive

    if synthetic:
        return make_archive(cfg.data_dir, season, synthetic=True)
    r = cfg.reference
    return make_archive(
        cfg.data_dir,
        season,
        base_url=r.base_url,
        schedule_url=r.schedule_url,
        timeout_s=r.timeout_s,
    )


def _ref_fail(exc: Exception) -> typer.Exit:
    console.print(f"[red]{exc}[/]")
    return typer.Exit(1)


@ref_app.command("events")
def ref_events(
    season: SeasonOpt = 2025,
    synthetic: SyntheticOpt = False,
    config: ConfigOpt = None,
    data_dir: DataDirOpt = None,
) -> None:
    """List the season's events and sessions (FastF1's calendar)."""
    from pitwall.reference.archive import FetchError

    cfg = _load(config, data_dir)
    try:
        events = _archive(cfg, season, synthetic).events()
    except FetchError as exc:
        raise _ref_fail(exc) from None
    t = Table(title=f"{season} events")
    for col in ("round", "event", "location", "date", "sessions"):
        t.add_column(col)
    for e in events:
        t.add_row(str(e.round), e.name, e.location, e.date or "", ", ".join(e.sessions))
    console.print(t)


@ref_app.command("laps")
def ref_laps(
    event: Annotated[str, typer.Argument(help="Event, e.g. 'Italian Grand Prix' or 'monza'")],
    session: Annotated[str, typer.Option(help="Qualifying, Race, Sprint, ...")] = "Qualifying",
    driver: Annotated[str | None, typer.Option(help="All laps of one driver (e.g. VER)")] = None,
    part: Annotated[str | None, typer.Option(help="Only Q1, Q2 or Q3")] = None,
    season: SeasonOpt = 2025,
    synthetic: SyntheticOpt = False,
    config: ConfigOpt = None,
    data_dir: DataDirOpt = None,
) -> None:
    """Each driver's fastest valid lap of a session, or all laps of one driver."""
    from pitwall.reference.archive import FetchError
    from pitwall.reference.laps import ReferenceDataError, classification, parse_lap_rows

    cfg = _load(config, data_dir)
    ar = _archive(cfg, season, synthetic)
    try:
        ev = ar.find_event(event)
        if driver:
            code = driver.upper()
            rows = parse_lap_rows(ar.driver_laptimes(ev.name, session, code), code)
            t = Table(title=f"{code} · {season} {ev.name} {session}")
            for col in ("lap", "time", "S1", "S2", "S3", "part", "tyre", "can be a reference"):
                t.add_column(col)
            for r in rows:
                t.add_row(
                    str(r.lap_number),
                    _fmt_ms(r.lap_time_ms),
                    *(_fmt_ms(x) for x in r.sectors_ms),
                    r.session_part or "",
                    f"{r.compound or ''} {r.tyre_life or ''}".strip(),
                    "[green]yes[/]" if r.usable else f"[dim]no: {', '.join(r.issues)}[/]",
                )
            console.print(t)
            return
        rows = classification(parse_lap_rows(ar.session_laptimes(ev.name, session)), part)
    except (FetchError, ReferenceDataError, LookupError) as exc:
        raise _ref_fail(exc) from None
    t = Table(title=f"{season} {ev.name} {session}: fastest valid lap per driver")
    for col in ("#", "driver", "team", "lap", "time", "gap", "part"):
        t.add_column(col)
    best = rows[0].lap_time_ms if rows else None
    for i, r in enumerate(rows, 1):
        gap = (
            "" if best is None or r.lap_time_ms is None else f"+{(r.lap_time_ms - best) / 1000:.3f}"
        )
        t.add_row(
            str(i),
            r.driver,
            r.team or "",
            str(r.lap_number),
            _fmt_ms(r.lap_time_ms),
            gap if i > 1 else "",
            r.session_part or "",
        )
    console.print(t)
    console.print(
        "[dim]Valid = not deleted, timing accurate, timed (not filled in) and not an out or in "
        "lap. Store one with `pitwall ref fetch`.[/]"
    )


def _print_ref(ref: Any) -> None:
    caps = ref.capabilities
    console.print(
        f"[bold]{ref.key}[/]\n  {ref.label} · {_fmt_ms(ref.lap_time_ms)} "
        f"(S1 {_fmt_ms(ref.sectors_ms[0])}, S2 {_fmt_ms(ref.sectors_ms[1])}, "
        f"S3 {_fmt_ms(ref.sectors_ms[2])}) · {ref.team or ''} · {ref.compound or ''}"
    )
    console.print(
        f"  Data: {caps.car_samples} car samples (median {caps.car_interval_s:.2f} s apart, "
        f"longest gap {caps.car_max_gap_s:.2f} s), {caps.pos_samples} positions; brake on/off; "
        "no steering or wheel slip."
    )
    for note in ref.checks.get("notes", []):
        console.print(f"  [yellow]{note}[/]")
    for src in ref.sources:
        console.print(f"  [dim]{src.get('url')}  sha256 {str(src.get('sha256', ''))[:12]}…[/]")


@ref_app.command("fetch")
def ref_fetch(
    event: Annotated[str, typer.Argument(help="Event, e.g. 'Italian Grand Prix' or 'monza'")],
    driver: Annotated[str, typer.Argument(help="Driver code, e.g. VER")],
    session: Annotated[str, typer.Option(help="Qualifying, Race, Sprint, ...")] = "Qualifying",
    lap: Annotated[int | None, typer.Option(help="A specific lap (default: fastest valid)")] = None,
    part: Annotated[str | None, typer.Option(help="Fastest lap within Q1, Q2 or Q3")] = None,
    season: SeasonOpt = 2025,
    synthetic: SyntheticOpt = False,
    config: ConfigOpt = None,
    data_dir: DataDirOpt = None,
) -> None:
    """Download, check and store a reference lap (kept offline afterwards)."""
    from pitwall.reference.archive import FetchError
    from pitwall.reference.laps import ReferenceDataError
    from pitwall.reference.service import fetch_reference

    cfg = _load(config, data_dir)
    ar = _archive(cfg, season, synthetic)
    try:
        ev = ar.find_event(event)
        ref = fetch_reference(
            _db(cfg), cfg.data_dir, ar, ev.name, session, driver, lap=lap, part=part
        )
    except (FetchError, ReferenceDataError, LookupError) as exc:
        raise _ref_fail(exc) from None
    console.print("[green]Stored[/] (lap time checked against the official timing):")
    _print_ref(ref)


@ref_app.command("list")
def ref_list(config: ConfigOpt = None, data_dir: DataDirOpt = None) -> None:
    """Reference laps stored on this machine."""
    from pitwall.storage import db

    cfg = _load(config, data_dir)
    rows = db.list_ref_laps(_db(cfg))
    if not rows:
        console.print("No reference laps yet: `pitwall ref fetch monza VER` stores one.")
        return
    t = Table()
    for col in ("key", "driver", "event", "session", "lap", "time", "data"):
        t.add_column(col)
    for r in rows:
        t.add_row(
            r["ref_key"],
            r["driver"],
            f"{r['season']} {r['event']}",
            f"{r['session']} {r['session_part'] or ''}".strip(),
            str(r["lap_number"]),
            _fmt_ms(r["lap_time_ms"]),
            "synthetic" if r["synthetic"] else r["provider"],
        )
    console.print(t)


def _pick_reference(
    conn: Any, cfg: Config, s: dict[str, Any], driver: str, session: str, part: str | None
) -> str:
    """A stored reference of ``driver`` for the session's event, fetched when missing."""
    from pitwall.reference.archive import FetchError
    from pitwall.reference.laps import ReferenceDataError
    from pitwall.reference.service import event_for, fetch_reference, season_for
    from pitwall.storage import db

    season, event = season_for(s), event_for(s)
    synthetic = bool(s.get("synthetic"))
    code = driver.upper()
    stored = [
        r
        for r in db.list_ref_laps(conn, synthetic=synthetic)
        if r["season"] == season
        and r["event"] == event
        and r["session"] == session
        and r["driver"] == code
        and (part is None or r["session_part"] == part)
    ]
    if stored:
        return min(stored, key=lambda r: r["lap_time_ms"])["ref_key"]
    try:
        ref = fetch_reference(
            conn, cfg.data_dir, _archive(cfg, season, synthetic), event, session, code, part=part
        )
    except (FetchError, ReferenceDataError, LookupError) as exc:
        raise _ref_fail(exc) from None
    console.print(f"Fetched and stored {ref.label}.")
    return ref.key


@app.command("benchmark")
def benchmark_cmd(
    session: Annotated[str, typer.Argument(help="Session id (or unique part), or 'latest'")],
    lap: Annotated[int, typer.Option(help="Lap index (# in `pitwall laps`)")],
    ref: Annotated[
        str | None, typer.Option(help="Stored reference key (`pitwall ref list`)")
    ] = None,
    driver: Annotated[
        str | None,
        typer.Option(help="Or a driver code: their fastest valid lap (fetched if needed)"),
    ] = None,
    ref_session: Annotated[str, typer.Option(help="Reference session")] = "Qualifying",
    part: Annotated[str | None, typer.Option(help="Q1, Q2 or Q3")] = None,
    config: ConfigOpt = None,
    data_dir: DataDirOpt = None,
) -> None:
    """Benchmark a lap against a real F1 lap: measured, then physics-normalised, per corner."""
    from pitwall.analysis.benchmark import benchmark, place_reference
    from pitwall.analysis.trace import NotComparableError, load_trace
    from pitwall.reference.service import check_pairing, load_reference
    from pitwall.storage import db
    from pitwall.track.service import model_for_session

    cfg = _load(config, data_dir)
    conn = _db(cfg)
    s = dict(_pick_session(conn, session))
    lap_row = db.get_lap(conn, s["session_key"], lap)
    if lap_row is None:
        console.print(f"[red]No lap #{lap} in this session.[/]")
        raise typer.Exit(1)
    if ref is None and driver is None:
        console.print(
            "[red]Choose a reference: --driver VER (or --ref KEY, see `pitwall ref list`).[/]"
        )
        raise typer.Exit(1)
    try:
        key = ref or _pick_reference(conn, cfg, s, driver or "", ref_session, part)
        reference = load_reference(conn, cfg.data_dir, key)
        check_pairing(s, reference)
        trace = load_trace(cfg.data_dir, s, dict(lap_row))
        model = model_for_session(conn, cfg.data_dir, s)
        b = benchmark(trace, place_reference(reference, trace), model)
    except (NotComparableError, LookupError) as exc:
        console.print(f"[red]Cannot benchmark: {exc}[/]")
        raise typer.Exit(1) from None

    reg = b.placed.placement.registration
    delta = trace.lap_time_s - reference.lap_time_s
    mine = f"lap {lap_row['lap_number']} {_fmt_ms(lap_row['lap_time_ms'])}"
    console.print(
        f"[bold]{s['track_name']}[/] · {mine} vs {reference.label} "
        f"{_fmt_ms(reference.lap_time_ms)}  →  {_signed(delta)} s"
    )
    quality = {"good": "[green]good[/]", "check": "[yellow]check[/]"}.get(reg.quality, reg.quality)
    console.print(
        f"Placed on your track: {quality} (positions {reg.median_m:.1f} m from your line, "
        f"scale {reg.scale:.3f}, start line {reg.start_offset_m:+.1f} m)"
    )
    for note in reg.notes:
        console.print(f"  [yellow]{note}[/]")
    caps = reference.capabilities
    console.print(
        f"[dim]Reference data: about every {caps.car_interval_s:.2f} s (gaps up to "
        f"{caps.car_max_gap_s:.2f} s), brake on/off, no steering or wheel slip.[/]"
    )
    tm = b.systematic["time"]
    mm = b.systematic["metrics"]
    parts = []
    for group, label in (("braking_corners", "braking corners"), ("straights", "straights")):
        if tm[group].get("pace_pct") is not None:
            parts.append(f"{label} {tm[group]['pace_pct']:+.2f} %")
    if mm["brake_onset_m"]["offset"] is not None:
        off = mm["brake_onset_m"]["offset"]
        parts.append(f"brake points {abs(off):.0f} m {'earlier' if off < 0 else 'later'}")
    if mm["min_speed_kmh"]["offset"] is not None:
        parts.append(f"minimum speeds {mm['min_speed_kmh']['offset']:+.1f} %")
    console.print("Systematic (game vs real car, median over the lap): " + ", ".join(parts))

    t = Table(title="Corners: measured, then normalised (± 1 sd; confidence of the difference)")
    for col in (
        "segment",
        "time Δ",
        "systematic",
        "normalised",
        "brake point",
        "min speed",
        "throttle",
        "exit",
    ):
        t.add_column(col)

    def metric(r: Any, key: str, unit: str) -> str:
        m = r.metrics.get(key)
        if m is None or m.delta is None:
            return "-"
        norm = "" if m.normalised is None else f" → {m.normalised:+.0f}"
        badge = "" if m.confidence in ("n/a", "within uncertainty") else f" [bold]{m.confidence}[/]"
        return f"{m.delta:+.0f}{norm} ±{m.sigma:.0f} {unit}{badge}"

    for r in b.segments:
        seg = r.segment
        norm = (
            "-"
            if r.normalised_s is None
            else f"{_signed(r.normalised_s)} ±{r.normalised_sigma_s:.3f}"
        )
        if not seg.is_corner:
            t.add_row(
                f"[dim]{seg.name}[/]",
                _signed(r.loss_s),
                _signed(r.expected_s),
                norm,
                "",
                "",
                "",
                "",
            )
            continue
        throttle = r.metrics.get("full_throttle_m")
        t.add_row(
            f"[bold]{seg.name}[/]",
            _signed(r.loss_s),
            _signed(r.expected_s),
            norm + ("" if r.confidence in ("n/a", "within uncertainty") else f" {r.confidence}"),
            metric(r, "brake_onset_m", "m"),
            metric(r, "min_speed_kmh", "km/h"),
            metric(
                r,
                "full_throttle_m"
                if throttle and throttle.delta is not None
                else "throttle_pickup_m",
                "m",
            ),
            metric(r, "exit_speed_kmh", "km/h"),
        )
    console.print(t)
    console.print(
        "[dim]Brake point and throttle: + = later than the reference; speeds: + = faster. "
        "Normalised = after the systematic difference. 'high' / 'medium' = the difference is "
        "more than 3 / 2 standard deviations of its uncertainty.[/]"
    )
    if b.findings:
        console.print(
            "[bold]Findings vs the reference[/] (normalised; impacts in one corner can overlap):"
        )
        for i, f in enumerate(b.findings, 1):
            console.print(
                f" {i}. [bold]{f.segment_name}[/] · {f.title} · {_signed(f.time_impact_s)} s · "
                f"{f.confidence} confidence"
            )
            console.print(f"    {f.detail}")
            if f.suggestion:
                console.print(f"    [cyan]Try:[/] {f.suggestion}")
    else:
        console.print(
            "No findings: no corner differs from your systematic offset by more than its "
            "threshold and uncertainty."
        )
    console.print("[dim]Not assessed against real F1 data: " + "; ".join(b.not_assessed) + ".[/]")


# ----------------------------------------------------------------- race engineer

RefOpt = Annotated[str, typer.Option(help="session_best | personal_best | theoretical_best | lap")]
RefLapOpt = Annotated[int | None, typer.Option(help="With --ref lap: the reference lap index")]
ProOpt = Annotated[
    str | None, typer.Option("--driver", help="Also compare with a real F1 driver, e.g. VER")
]


def _provider(cfg: Config):
    from pitwall.engineer.llm import make_provider

    e = cfg.engineer
    return make_provider(
        e.provider,
        model=e.model,
        base_url=e.base_url,
        timeout_s=e.timeout_s,
        temperature=e.temperature,
        num_ctx=e.num_ctx,
        think=e.think,
    )


def _lap_context(
    cfg: Config,
    conn: Any,
    session: str,
    lap: int,
    ref: str,
    ref_lap: int | None,
    driver: str | None,
):
    from pitwall.analysis.trace import NotComparableError
    from pitwall.engineer.service import build_context

    s = dict(_pick_session(conn, session))
    pro = _pick_reference(conn, cfg, s, driver, "Qualifying", None) if driver else None
    try:
        return build_context(
            conn, cfg.data_dir, s["session_key"], lap, ref_kind=ref, ref_lap=ref_lap, pro_key=pro
        )
    except (LookupError, NotComparableError) as exc:
        console.print(f"[red]Cannot build the report: {exc}[/]")
        raise typer.Exit(1) from None


@engineer_app.command("status")
def engineer_status(config: ConfigOpt = None, data_dir: DataDirOpt = None) -> None:
    """Check the local model: is Ollama running, is the model installed?"""
    cfg = _load(config, data_dir)
    st = _provider(cfg).status()
    console.print(f"Provider: {st['provider']} at {st['base_url']} · model [bold]{st['model']}[/]")
    if not st["reachable"]:
        console.print(f"[red]✗[/] {st['error']}")
        console.print(
            "  Install Ollama on the Mac (ollama.com), start it, then: "
            f"[bold]ollama pull {st['model']}[/]"
        )
        raise typer.Exit(1)
    console.print(f"[green]✓[/] Ollama {st['version']} is running")
    installed = ", ".join(st["installed"]) or "none"
    if st["model_installed"]:
        console.print(f"[green]✓[/] {st['model']} is installed (all models: {installed})")
    else:
        console.print(f"[red]✗[/] {st['error']} (installed: {installed})")
        raise typer.Exit(1)


@engineer_app.command("report")
def engineer_report(
    session: Annotated[str, typer.Argument(help="Session id (or unique part), or 'latest'")],
    lap: Annotated[int, typer.Option(help="Lap index (# in `pitwall laps`)")],
    ref: RefOpt = "session_best",
    ref_lap: RefLapOpt = None,
    driver: ProOpt = None,
    as_json: Annotated[bool, typer.Option("--json", help="Facts and report as JSON")] = False,
    config: ConfigOpt = None,
    data_dir: DataDirOpt = None,
) -> None:
    """The measured report (no AI): focus for the next lap, where the time goes, notes."""
    from pitwall.engineer.report import render_text

    cfg = _load(config, data_dir)
    ctx = _lap_context(cfg, _db(cfg), session, lap, ref, ref_lap, driver)
    if as_json:
        console.print_json(json.dumps(ctx.report.to_dict()))
        return
    console.print(render_text(ctx.report), markup=False, highlight=False)


@engineer_app.command("narrate")
def engineer_narrate(
    session: Annotated[str, typer.Argument(help="Session id (or unique part), or 'latest'")],
    lap: Annotated[int, typer.Option(help="Lap index (# in `pitwall laps`)")],
    ref: RefOpt = "session_best",
    ref_lap: RefLapOpt = None,
    driver: ProOpt = None,
    config: ConfigOpt = None,
    data_dir: DataDirOpt = None,
) -> None:
    """The report written up by the local AI, checked against the facts."""
    from pitwall.engineer.narrator import render_narrative
    from pitwall.engineer.service import narrate_lap

    cfg = _load(config, data_dir)
    conn = _db(cfg)
    ctx = _lap_context(cfg, conn, session, lap, ref, ref_lap, driver)
    with console.status(f"Asking {cfg.engineer.model}…"):
        result, nid = narrate_lap(conn, ctx, _provider(cfg))
    status = {
        "grounded": "[green]checked: every number and corner traced to the measured facts[/]",
        "fallback": "[yellow]the AI text failed the fact check twice: measured report below[/]",
        "unavailable": f"[yellow]no AI available ({result.error}): measured report below[/]",
    }[result.status]
    console.print(status)
    console.print(render_narrative(result.output), markup=False, highlight=False)
    console.print(
        f"[dim]{result.provider} · {result.model} · prompt {result.prompt_version} · "
        f"{result.attempts} attempt(s) · {result.duration_s:.1f} s · stored as {nid[:8]}[/]"
    )


@engineer_app.command("ask")
def engineer_ask(
    session: Annotated[str, typer.Argument(help="Session id (or unique part), or 'latest'")],
    question: Annotated[str, typer.Argument(help="Your question, in quotes")],
    lap: Annotated[int, typer.Option(help="Lap index (# in `pitwall laps`)")],
    ref: RefOpt = "session_best",
    ref_lap: RefLapOpt = None,
    driver: ProOpt = None,
    config: ConfigOpt = None,
    data_dir: DataDirOpt = None,
) -> None:
    """Ask the engineer about a lap; answers only from measured data, or refuses."""
    from pitwall.engineer.service import ask_lap

    cfg = _load(config, data_dir)
    conn = _db(cfg)
    ctx = _lap_context(cfg, conn, session, lap, ref, ref_lap, driver)
    with console.status(f"Asking {cfg.engineer.model}…"):
        result, _ = ask_lap(conn, cfg.data_dir, ctx, _provider(cfg), question)
    out = result.output
    console.print(out["answer"], markup=False, highlight=False)
    if out.get("cites"):
        console.print(f"[dim]Sources: {', '.join(out['cites'])}[/]")
    note = {
        "grounded": "checked against the measured facts",
        "refused": "the data cannot answer this",
        "fallback": "the AI answer failed the fact check twice",
        "unavailable": f"no AI available: {result.error}",
    }[result.status]
    tools = ", ".join(c["name"] for c in result.tool_calls) or "none"
    console.print(f"[dim]{note} · tools used: {tools} · {result.duration_s:.1f} s[/]")


@engineer_app.command("eval")
def engineer_eval(
    limit: Annotated[int, typer.Option(help="Number of laps")] = 20,
    session: Annotated[str | None, typer.Option(help="Only this session")] = None,
    model: Annotated[str | None, typer.Option(help="Model to evaluate (default: config)")] = None,
    synthetic: Annotated[bool, typer.Option("--synthetic", help="Use demo sessions too")] = False,
    config: ConfigOpt = None,
    data_dir: DataDirOpt = None,
) -> None:
    """Write up many laps with a model and produce a sheet to rate them (the Stage 5 eval)."""
    from dataclasses import replace

    from pitwall.analysis.trace import NotComparableError
    from pitwall.engineer.eval import run_eval

    cfg = _load(config, data_dir)
    if model:
        cfg.engineer = replace(cfg.engineer, model=model)
    conn = _db(cfg)
    sessions = [dict(_pick_session(conn, session))] if session else None
    provider = _provider(cfg)
    st = provider.status()
    if not st["reachable"] or not st["model_installed"]:
        console.print(f"[red]{st['error']}[/]")
        raise typer.Exit(1)
    try:
        summary, path = run_eval(
            conn, cfg.data_dir, provider, limit=limit, sessions=sessions, synthetic=synthetic,
            progress=lambda i, n, label: console.print(f"[dim]{i}/{n} {label}[/]"),
        )  # fmt: skip
    except NotComparableError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from None
    if not summary["laps"]:
        console.print("No lap to evaluate: record and ingest some laps first.")
        raise typer.Exit(1)
    console.print(
        f"{summary['laps']} laps · grounded first time {summary['grounded_first']} · after the "
        f"retry {summary['grounded']} · fell back to the measured report {summary['fallback']} "
        f"· median {summary['median_s']:.1f} s per lap"
    )
    console.print(f"Rate the write-ups in [bold]{path}[/] (accuracy and usefulness, 1-5).")


# ------------------------------------------------------------------ history (Stage 7)

_OUTCOME_STYLE = {
    "improved": "green",
    "worse": "red",
    "unchanged": "yellow",
    "inconclusive": "dim",
    "open": "cyan",
    "not enough data": "dim",
}


def _date(utc: str | None) -> str:
    return (utc or "")[:16].replace("T", " ")


def _outcome(status: str) -> str:
    return f"[{_OUTCOME_STYLE.get(status, 'white')}]{status}[/]"


def _trend_text(t: dict[str, Any]) -> str:
    if t["status"] == "not enough data":
        return f"not enough data ({t.get('reason', '')})"
    after, before = _fmt_ms(round(t["after"] * 1000)), _fmt_ms(round(t["before"] * 1000))
    return (
        f"{_outcome(t['status'])} {_signed(t['delta'])} s (median {after} vs {before}; "
        f"counts from {t['threshold']:.3f} s)"
    )


def _resolve_group(conn: Any, cfg: Config, arg: str) -> str:
    """A history group id (track key and car class) from a group id, a track key or name,
    or a session id (its track and car class)."""
    from pitwall.history import service as hs
    from pitwall.history import trends
    from pitwall.track.service import session_track_key

    if ":" in arg:
        return arg
    groups = trends.groups(hs.load_rows(conn, any_formula=True))
    hits = [
        g
        for g in groups
        if arg.lower() in (g["track_key"].lower(), (g["track_name"] or "").lower())
        or arg.lower() in (g["track_name"] or "").lower()
    ]
    if len(hits) == 1:
        return hits[0]["id"]
    if len(hits) > 1:
        console.print(f"[yellow]{len(hits)} histories match {arg!r}; use one of these ids:[/]")
        for g in hits:
            kind = "synthetic" if g["synthetic"] else "real"
            console.print(f"  {g['id']}  ({g['track_name']}, {g['formula_name']}, {kind})")
        raise typer.Exit(1)
    s = dict(_pick_session(conn, arg))
    return trends.group_id(session_track_key(s), s.get("formula"))


@history_app.callback(invoke_without_command=True)
def history_overview(
    ctx: typer.Context, config: ConfigOpt = None, data_dir: DataDirOpt = None
) -> None:
    """Every track you have driven: sessions, laps, best lap and the latest trend."""
    if ctx.invoked_subcommand is not None:
        return
    from pitwall.history import service as hs

    cfg = _load(config, data_dir)
    conn = _db(cfg)
    ov = hs.overview(conn, cfg.data_dir)
    if ov["built"]["sessions"]:
        console.print(
            f"[dim]History updated: {ov['built']['sessions']} session(s), {ov['built']['laps']} "
            f"laps in {ov['built']['seconds']} s.[/]"
        )
    if not ov["groups"]:
        console.print("No history yet: record and ingest some laps first (or `pitwall demo`).")
        return
    t = Table(title="Driver history")
    for col in ("id", "track", "car", "data", "sessions", "laps", "best", "latest best", "trend"):
        t.add_column(col)
    for g in ov["groups"]:
        tr = g["trend"]
        trend = (
            "not enough data"
            if tr["status"] == "not enough data"
            else f"{_outcome(tr['status'])} {_signed(tr['delta'])} s"
        )
        t.add_row(
            g["id"],
            g["track_name"],
            g["formula_name"],
            "[magenta]synthetic[/]" if g["synthetic"] else "real",
            str(g["sessions"]),
            str(g["laps"]),
            _fmt_ms(g["best_lap_ms"]),
            _fmt_ms(g["latest_best_lap_ms"])
            + (" [green]new best[/]" if g["new_best_in_latest"] else ""),
            trend,
        )
    console.print(t)
    console.print("[dim]Trend: latest session against the earlier ones (medians, noise-aware).[/]")
    if ov["open_recommendations"]:
        console.print("\n[bold]Work on next time[/] (from your latest session on each track)")
        for r in ov["open_recommendations"]:
            tag = " [magenta](synthetic)[/]" if r["synthetic"] else ""
            console.print(
                f" - {r['track_name']}{tag} · {r['segment_name']} · {r['title']} "
                f"({r['laps']} of {r['of']} laps): {r['suggestion'] or ''}"
            )
            console.print(f"   [dim]Target: {r['metric_words']} ({r['better']} is better)[/]")
    for prob in ov["cache"]["problems"]:
        console.print(f"[yellow]Not analysed:[/] {prob['session_key']}: {prob['reason']}")
    console.print("Details: [bold]uv run pitwall history show <track>[/]")


@history_app.command("show")
def history_show(
    track: Annotated[
        str, typer.Argument(help="History id, track name or key, or a session id (its track)")
    ],
    pro: Annotated[
        str | None, typer.Option(help="Stored real-F1 reference key (see `pitwall ref list`)")
    ] = None,
    config: ConfigOpt = None,
    data_dir: DataDirOpt = None,
) -> None:
    """One track's history: sessions, trend, corners, recurring patterns, recommendations."""
    from pitwall.history import service as hs

    cfg = _load(config, data_dir)
    conn = _db(cfg)
    gid = _resolve_group(conn, cfg, track)
    try:
        v = hs.track_history(conn, cfg.data_dir, gid, pro_key=pro)
    except (LookupError, ValueError) as exc:
        console.print(f"[red]{str(exc).strip(chr(39))}[/]")
        raise typer.Exit(1) from None
    g = v["group"]
    kind = "[magenta]synthetic data[/]" if g["synthetic"] else "real laps"
    console.print(
        f"[bold]{g['track_name']}[/] · {g['formula_name']} · {kind} · {g['sessions']} sessions · "
        f"{g['laps']} usable laps · best {_fmt_ms(g['best_lap_ms'])} ({_date(g['best']['date'])}, "
        f"lap {g['best']['lap_number']})"
    )
    if v["excluded_laps"]:
        console.print(
            f"[yellow]{v['excluded_laps']} laps were analysed with an older version of the "
            "corners and are left out.[/]"
        )

    t = Table(title="Sessions")
    for col in ("date", "label", "laps", "best", "median", "spread", "ideal"):
        t.add_column(col)
    for s in v["sessions"]:
        t.add_row(
            _date(s["date"]),
            s["label"] or "",
            str(s["laps"]),
            _fmt_ms(s["best_lap_ms"]),
            _fmt_ms(round(s["median_s"] * 1000)),
            "-" if s["spread_s"] is None else f"±{s['spread_s']:.3f}",
            "-" if s["ideal_s"] is None else _fmt_ms(round(s["ideal_s"] * 1000)),
        )
    console.print(t)
    tr = v["trend"]
    console.print(f"[bold]Trend[/] (latest session vs earlier ones): {_trend_text(tr)}")
    for c in tr.get("caveats", []):
        console.print(f"  [yellow]{c}[/]")

    seg = [x for x in v["segment_trends"] if x["status"] in ("improved", "worse")]
    if v["segment_trends"]:
        quiet = len(v["segment_trends"]) - len(seg)
        console.print(
            f"[bold]Corners and straights[/] (latest vs earlier): {len(seg)} changed, "
            f"{quiet} unchanged or not enough laps"
        )
        for x in seg:
            console.print(
                f"  {x['segment_name']}: {_outcome(x['status'])} {_signed(x['delta'], 3)} s"
            )

    corners = v["corners"]
    if corners["status"] == "ok":
        t = Table(title=f"Time left against your best (last {corners['recent_laps']} laps)")
        for col in ("corner (with exit straight)", "mean gap", "median gap", "spread", ""):
            t.add_column(col)
        weak = {c["segment_id"] for c in corners["weakest"]}
        strong = {c["segment_id"] for c in corners["strongest"]}
        for c in corners["corners"]:
            tag = (
                "[red]weakest[/]"
                if c["segment_id"] in weak
                else "[green]strongest[/]"
                if c["segment_id"] in strong
                else ""
            )
            t.add_row(
                c["zone_name"],
                _signed(c["mean_gap_s"]),
                _signed(c["median_gap_s"]),
                "-" if c["spread_s"] is None else f"±{c['spread_s']:.3f}",
                tag,
            )
        console.print(t)
        if corners["ideal_lap_s"]:
            ideal = _fmt_ms(round(corners["ideal_lap_s"] * 1000))
            typical = _fmt_ms(round(corners["typical_lap_s"] * 1000))
            console.print(f"Ideal lap from your best segments: {ideal} · typical lap: {typical}")
    else:
        console.print(
            f"[dim]Corners: not enough laps yet (needs {corners['min_laps']} recent laps).[/]"
        )

    pat = v["patterns"]
    console.print(f"\n[bold]Recurring patterns[/] (latest {pat['compared_laps']} compared laps)")
    if pat["status"] != "ok":
        console.print(
            f"  [dim]not enough data: needs {pat['min_laps']} laps compared with their "
            "session's best[/]"
        )
    elif not pat["patterns"]:
        console.print("  none above the thresholds")
    for x in pat["patterns"]:
        console.print(
            f"  {x['segment_name']} · {x['title']}: {x['laps']} of {x['of']} laps, in "
            f"{x['sessions']} of {x['of_sessions']} sessions, {_signed(x['mean_impact_s'])} s "
            "each time"
        )

    console.print("\n[bold]Recommendations and outcomes[/]")
    if not v["recommendations"]:
        console.print("  none yet (made after a session with recurring findings)")
    for r in v["recommendations"]:
        line = (
            f"  {_date(r['session_date'])} · {r['segment_name']} · {r['title']} "
            f"({r['laps']} of {r['of']} laps) → {_outcome(r['outcome'])}"
        )
        if r["outcome"] not in ("open", "inconclusive"):
            m = r["metric_change"]
            unit = f" {r['unit']}" if r["unit"] else ""
            before, after = _m(m["before"], 1), _m(m["after"], 1)
            line += f" · {r['metric_words']}: {before}{unit} → {after}{unit}"
            st = r["segment_time"]
            if st["delta"] is not None:
                line += f" · time there {_signed(st['delta'])} s"
        console.print(line)
        if r["outcome"] == "open":
            console.print(f"    [cyan]Try:[/] {r['suggestion'] or r['title']}")

    if pro:
        p = v.get("pro") or {}
        if p.get("error"):
            console.print(f"[red]Real-F1 comparison: {p['error']}[/]")
        else:
            console.print(
                f"\n[bold]Against {p['driver']} ({p['season']} {p['event']} {p['session']})[/]"
                f", your best lap here, normalised (estimates; {p['rule']}):"
            )
            for label, items in (("weaker", p["weakest"]), ("stronger", p["strongest"])):
                for c in items:
                    console.print(
                        f"  {label}: {c['segment_name']} {_signed(c['normalised_s'])} s "
                        f"(± {c['sigma_s']:.3f})"
                    )
            if not p["weakest"] and not p["strongest"]:
                console.print("  every corner within the uncertainty")


@history_app.command("build")
def history_build_cmd(
    force: Annotated[bool, typer.Option(help="Analyse every session again")] = False,
    config: ConfigOpt = None,
    data_dir: DataDirOpt = None,
) -> None:
    """Analyse sessions whose history is missing or out of date (normally automatic)."""
    from pitwall.history import service as hs

    cfg = _load(config, data_dir)
    conn = _db(cfg)
    res = hs.rebuild(conn, cfg.data_dir, force=force)
    console.print(
        f"History: {res['sessions']} session(s) analysed ({res['laps']} laps) in "
        f"{res['seconds']} s."
    )
    for prob in hs.cache_state(conn)["problems"]:
        console.print(f"[yellow]Not analysed:[/] {prob['session_key']}: {prob['reason']}")


if __name__ == "__main__":
    app()
