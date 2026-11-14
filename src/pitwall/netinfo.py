"""Local network helpers: which IP the PS5 should send to, and whether a port is free."""

from __future__ import annotations

import socket


def primary_ipv4() -> str | None:
    """IPv4 address of the interface used for outbound traffic (no packets are sent)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))  # TEST-NET-1; UDP connect only selects a route
        addr = sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()
    return None if addr.startswith("0.") else addr


def local_ipv4s() -> list[str]:
    found: set[str] = set()
    primary = primary_ipv4()
    if primary:
        found.add(primary)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(str(info[4][0]))
    except OSError:
        pass
    return sorted(ip for ip in found if not ip.startswith("127."))


def udp_port_available(host: str, port: int) -> tuple[bool, str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((host, port))
    except OSError as exc:
        return False, str(exc)
    finally:
        sock.close()
    return True, "available"
