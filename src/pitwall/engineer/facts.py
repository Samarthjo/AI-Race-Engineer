"""Facts: the measured statements the engineer, and the AI, may use.

A fact has a stable id (``lap``, ``sector:2``, ``seg:c3``, ``find:c1:EARLY_BRAKE``,
``pro:c2:LOW_MIN_SPEED``...), a deterministic sentence, and the numbers that may be quoted
from it: exactly the numbers written in that sentence. Labels are not numbers: corner and
sector names (``T1-T2``, ``C3``, ``S2``, ``Lesmo 1``), ``lap 7`` and years are recognised as
labels before numbers are read.

The same reader is used on the AI's text by the grounding check, so "what may be quoted"
and "what was quoted" are measured the same way.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

UNITS: dict[str, str] = {
    "km/h": "km/h",
    "kph": "km/h",
    "kmh": "km/h",
    "ms": "ms",
    "millisecond": "ms",
    "milliseconds": "ms",
    "s": "s",
    "sec": "s",
    "secs": "s",
    "second": "s",
    "seconds": "s",
    "m": "m",
    "metre": "m",
    "metres": "m",
    "meter": "m",
    "meters": "m",
    "%": "%",
    "percent": "%",
    "rpm": "rpm",
}
_UNIT_RE = (
    r"(?P<unit>km\s*/\s*h|kph|kmh|milliseconds?|ms|seconds?|secs?|s|metres?|meters?|m|%|"
    r"percent|rpm)(?![A-Za-z0-9])"
)
_NUMBER = re.compile(
    r"(?P<laptime>(?<![\d:.])(?P<min>\d{1,2}):(?P<sec>[0-5]\d)\.(?P<frac>\d{1,3})(?![\d:]))"
    r"|(?P<num>(?<![\w.,])(?P<sign>[+\-\u2212]?)(?P<int>\d{1,3}(?:,\d{3})+|\d+)"
    r"(?:\.(?P<dec>\d+))?(?![\d]))"
    r"(?:\s*" + _UNIT_RE + r")?",
    re.IGNORECASE,
)
_LABELS = [
    re.compile(r"\b[TCSQ]\d+(?:\s*[-\u2013]\s*[TCSQ]?\d+)*\b"),  # T1-T2, T8-T10, C3, S2, Q3
    re.compile(r"\b(?:sector|turn|corner|round|stage|gear)\s+\d+\b", re.IGNORECASE),
    re.compile(r"\b20[1-3]\d\b(?!\s*(?:km/h|kph|ms|m|s|%)(?![A-Za-z]))"),  # years
]
LAP_LABEL = re.compile(r"\blaps?\s+(?:#\s*)?(\d+)\b", re.IGNORECASE)


@dataclass(frozen=True)
class Num:
    value: float  # in the base unit: s for time, m, km/h, %, rpm
    unit: str  # "" when none was written
    sign: int  # +1 / -1 when written with a sign, 0 when not
    decimals: int  # as written (rounding tolerance)
    start: int
    end: int

    def matches(self, other: Num) -> bool:
        """Could ``self`` (as written) be ``other`` rounded to ``self``'s decimals?"""
        if self.unit and other.unit and _family(self.unit) != _family(other.unit):
            return False
        tol = 0.5 * 10 ** (-self.decimals) + 1e-9
        return abs(abs(self.value) - abs(other.value)) <= tol


def _family(unit: str) -> str:
    return "s" if unit in ("s", "ms") else unit


def strip_labels(text: str, names: list[str] | tuple[str, ...] = ()) -> str:
    """The text with corner/sector names, lap labels and years blanked out (same length)."""

    def blank(m: re.Match[str]) -> str:
        return " " * (m.end() - m.start())

    out = text
    for name in sorted({n for n in names if n}, key=len, reverse=True):
        out = re.sub(re.escape(name), blank, out, flags=re.IGNORECASE)
    out = LAP_LABEL.sub(blank, out)
    for pattern in _LABELS:
        out = pattern.sub(blank, out)
    return out


def parse_numbers(text: str, names: list[str] | tuple[str, ...] = ()) -> list[Num]:
    """Every number written in ``text`` (labels excluded), with its unit and rounding."""
    clean = strip_labels(text, names)
    out: list[Num] = []
    for m in _NUMBER.finditer(clean):
        unit_raw = (m.group("unit") or "").lower().replace(" ", "")
        unit = UNITS.get(unit_raw, unit_raw)
        if m.group("laptime"):
            frac = m.group("frac")
            value = int(m.group("min")) * 60 + int(m.group("sec")) + int(frac) / 10 ** len(frac)
            out.append(Num(value, "s", 0, len(frac), m.start(), m.end()))
            continue
        digits = m.group("int").replace(",", "")
        dec = m.group("dec") or ""
        value = float(f"{digits}.{dec}" if dec else digits)
        decimals = len(dec)
        if unit == "ms":
            value, decimals, unit = value / 1000, decimals + 3, "s"
        sign_txt = m.group("sign")
        sign = -1 if sign_txt in ("-", "\u2212") else (1 if sign_txt == "+" else 0)
        out.append(Num(sign * value if sign else value, unit, sign, decimals, m.start(), m.end()))
    return out


def lap_labels(text: str) -> list[int]:
    return [int(m.group(1)) for m in LAP_LABEL.finditer(text)]


@dataclass
class Fact:
    id: str
    kind: str  # lap | sector | segment | finding | strength | consistency | context | note
    text: str  # the deterministic statement, with every quotable number
    segment_id: str | None = None
    segment_name: str | None = None
    basis: str = "self"  # self | pro_normalised | pro_measured
    estimate: bool = False  # real-F1 values are estimates: the AI must say "about"
    code: str | None = None  # finding code
    data: dict[str, Any] = field(default_factory=dict)
    numbers: list[Num] = field(default_factory=list)

    def bind(self, names: list[str]) -> Fact:
        self.numbers = parse_numbers(self.text, names)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "text": self.text,
            "segment_id": self.segment_id,
            "segment_name": self.segment_name,
            "basis": self.basis,
            "estimate": self.estimate,
            "code": self.code,
            "data": self.data,
        }


def fmt_s(v: float, digits: int = 3) -> str:
    """Signed seconds as written in facts: ``+0.384 s`` / ``-0.062 s``."""
    if not math.isfinite(v):
        return "n/a"
    r = round(v, digits)
    if r == 0:
        return f"{0:.{digits}f} s"
    return f"{r:+.{digits}f} s"


def fmt_lap(ms: int | None) -> str:
    if ms is None:
        return "no time"
    minutes, rest = divmod(int(ms), 60_000)
    return f"{minutes}:{rest / 1000:06.3f}" if minutes else f"{rest / 1000:.3f} s"
