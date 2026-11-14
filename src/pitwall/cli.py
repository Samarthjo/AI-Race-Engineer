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


if __name__ == "__main__":
    app()
