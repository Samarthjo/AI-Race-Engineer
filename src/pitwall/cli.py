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


if __name__ == "__main__":
    app()
