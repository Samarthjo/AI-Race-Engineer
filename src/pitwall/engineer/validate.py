"""Grounding check for AI-written text: every number, corner and claim must trace to facts.

Checked for every part of an AI text (summary, each section, each focus item, an answer):

1. **citations**: each cited fact id exists;
2. **numbers**: every number written (with its unit) equals a number written in a cited
   fact, allowing only rounding (0.384 s may be written 0.38 s or 0.4 s, also 384 ms or a
   lap time). Calculated numbers (sums, differences, averages) are not allowed: the AI
   quotes, it does not calculate. A number found only in a fact that was not cited can be
   repaired by adding that citation; a number in no fact cannot;
3. **direction**: a time written next to "faster"/"gained" must be a gain in the cited
   facts, next to "slower"/"lost" a loss;
4. **laps**: "lap N" must be a lap of the session or of the reference;
5. **corners**: every corner or straight named must belong to a cited fact;
6. **estimates**: numbers that come only from real-F1 comparisons must be worded as
   estimates ("about", "around", "roughly", "~", "±"...);
7. **topics**: tyres, fuel, set-up, steering, lock-ups, wheelspin, coasting, gears and the
   like may only be mentioned when a cited fact mentions them;
8. **structure**: at most three focus items, each citing a finding.

Unit-less whole numbers up to 3 are allowed as counts ("two corners", "3 things").
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from pitwall.engineer.facts import Fact, Num, lap_labels, parse_numbers

MAX_FOCUS = 3
MAX_SECTIONS = 6
MAX_SUMMARY_CHARS = 700
MAX_TEXT_CHARS = 1200
COUNT_MAX = 3

HEDGE = re.compile(
    r"\b(?:about|around|roughly|approximately|approx|estimated?|nearly|almost|close to|"
    r"up to|some)\b|~|±|\+/-",
    re.IGNORECASE,
)
GAIN_WORDS = re.compile(
    r"\b(?:faster|quicker|gain(?:s|ed|ing)?|ahead|saves?|saved|saving|up on)\b", re.IGNORECASE
)
LOSS_WORDS = re.compile(
    r"\b(?:slower|los(?:e|es|t|ing)|behind|costs?|costing|deficit|down on|leak(?:s|ed|ing)?)\b",
    re.IGNORECASE,
)
TOPICS: dict[str, re.Pattern[str]] = {
    "tyres": re.compile(r"\btyres?\b|\btires?\b|\bcompound", re.IGNORECASE),
    "fuel": re.compile(r"\bfuel\b", re.IGNORECASE),
    "set-up": re.compile(
        r"\bset-?up\b|\bwings?\b|\bdownforce\b|\bdifferential\b|\bsuspension\b|\bbrake bias\b",
        re.IGNORECASE,
    ),
    "steering": re.compile(r"\bsteer", re.IGNORECASE),
    "lock-ups": re.compile(r"\block(?:s|ed|ing)?(?:-|\s)?ups?\b|\blocking\b|\blocks\b", re.I),
    "wheelspin": re.compile(r"\bwheel ?spin|\bspin(?:s|ning)?\b", re.IGNORECASE),
    "coasting": re.compile(r"\bcoast", re.IGNORECASE),
    "hesitation": re.compile(r"\bhesitat", re.IGNORECASE),
    "trail braking": re.compile(r"\btrail(?:-|\s)?brak", re.IGNORECASE),
    "ERS / DRS": re.compile(r"\bERS\b|\bDRS\b|\bbattery\b|\bdeploy", re.IGNORECASE),
    "weather": re.compile(r"\brain\b|\bwet\b|\bweather\b|\btemperatures?\b", re.IGNORECASE),
    "gears": re.compile(r"\bgears?\b|\bshift(?:s|ing|ed)?\b|\bdownshift", re.IGNORECASE),
    "kerbs": re.compile(r"\bkerbs?\b|\bcurbs?\b|\btrack limits\b", re.IGNORECASE),
    "racing line": re.compile(r"\bracing line\b|\bturn-?in point\b", re.IGNORECASE),
}


@dataclass
class Violation:
    kind: str  # citation | number | direction | lap | corner | estimate | topic | structure
    where: str
    detail: str
    repair: str | None = None  # fact id whose citation would ground it

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Grounding:
    """Everything the check needs: the facts, the corners of the track, the known laps."""

    facts: dict[str, Fact]
    segments: list[tuple[str, str]]  # (id, name)
    lap_numbers: set[int]
    _mentions: list[tuple[re.Pattern[str], str]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        pats: list[tuple[str, str]] = []
        for seg_id, name in self.segments:
            pats.append((name, seg_id))
            if re.fullmatch(r"c\d+", seg_id):
                pats.append((seg_id, seg_id))
            label = re.search(r"\(([^)]*T\d[^)]*)\)", name)
            if label:
                pats.append((label.group(1), seg_id))
                for t in re.findall(r"T\d+", label.group(1)):
                    pats.append((t, seg_id))
                    pats.append(("turn " + t[1:], seg_id))
        pats.sort(key=lambda p: -len(p[0]))
        self._mentions = [
            (re.compile(r"(?<![\w-])" + re.escape(text) + r"(?![\w-])", re.IGNORECASE), sid)
            for text, sid in pats
        ]

    @property
    def names(self) -> list[str]:
        return [name for _, name in self.segments]

    def mentioned_segments(self, text: str) -> set[str]:
        found: set[str] = set()
        rest = text
        for pattern, sid in self._mentions:
            if pattern.search(rest):
                found.add(sid)
                rest = pattern.sub(" ", rest)
        return found


def _sentences(text: str) -> list[tuple[int, int]]:
    spans = []
    start = 0
    for m in re.finditer(r"[.!?;](?:\s+|$)|\n+", text):
        spans.append((start, m.end()))
        start = m.end()
    if start < len(text):
        spans.append((start, len(text)))
    return spans


def _supports(num: Num, facts: list[Fact]) -> list[Fact]:
    return [f for f in facts if any(num.matches(n) for n in f.numbers)]


def check_text(text: str, cites: list[str], g: Grounding, where: str) -> list[Violation]:
    out: list[Violation] = []
    cited: list[Fact] = []
    for cid in cites:
        f = g.facts.get(cid)
        if f is None:
            out.append(Violation("citation", where, f"cites {cid!r}, which is not a fact"))
        else:
            cited.append(f)
    all_facts = list(g.facts.values())

    for n in lap_labels(text):
        if n not in g.lap_numbers:
            out.append(Violation("lap", where, f"mentions lap {n}, which is not in the data"))

    sentences = _sentences(text)
    for num in parse_numbers(text, g.names):
        if not num.unit and num.decimals == 0 and 0 <= num.value <= COUNT_MAX:
            continue  # a count ("two corners", "3 things")
        support = _supports(num, cited)
        written = text[num.start : num.end].strip()
        if not support:
            elsewhere = _supports(num, all_facts)
            if elsewhere:
                out.append(
                    Violation(
                        "number",
                        where,
                        f"{written!r} is in fact {elsewhere[0].id}, which is not cited",
                        repair=elsewhere[0].id,
                    )
                )
            else:
                out.append(
                    Violation("number", where, f"{written!r} is not in the facts (no calculating)")
                )
            continue
        sent = next((text[a:b] for a, b in sentences if a <= num.start < b), text)
        if all(f.estimate for f in support) and not HEDGE.search(sent):
            out.append(
                Violation(
                    "estimate",
                    where,
                    f"{written!r} comes from real-F1 data: say 'about' (it is an estimate)",
                )
            )
        if num.unit == "s":
            gain, loss = bool(GAIN_WORDS.search(sent)), bool(LOSS_WORDS.search(sent))
            signs = {n.sign for f in support for n in f.numbers if num.matches(n) and n.sign}
            if gain and not loss and signs == {1}:
                out.append(
                    Violation("direction", where, f"{written!r} is a loss in the facts, not a gain")
                )
            if loss and not gain and signs == {-1}:
                out.append(
                    Violation("direction", where, f"{written!r} is a gain in the facts, not a loss")
                )

    allowed = {f.segment_id for f in cited if f.segment_id}
    for f in cited:
        allowed |= g.mentioned_segments(f.text)
    for sid in sorted(g.mentioned_segments(text) - allowed):
        name = next((n for i, n in g.segments if i == sid), sid)
        repair = next(
            (f.id for f in all_facts if f.segment_id == sid and f.kind in ("finding", "segment")),
            None,
        )
        out.append(Violation("corner", where, f"names {name}, but cites no fact about it", repair))

    for topic, pattern in TOPICS.items():
        if pattern.search(text) and not any(pattern.search(f.text) for f in cited):
            out.append(
                Violation("topic", where, f"talks about {topic}, which no cited fact covers")
            )
    return out


def _unit(obj: Any, where: str, out: list[Violation]) -> tuple[str, list[str]] | None:
    if not isinstance(obj, dict):
        out.append(Violation("structure", where, "missing"))
        return None
    text = obj.get("text")
    cites = obj.get("cites")
    if not isinstance(text, str) or not text.strip():
        out.append(Violation("structure", where, "no text"))
        return None
    if not isinstance(cites, list) or not all(isinstance(c, str) for c in cites):
        out.append(Violation("structure", where, "cites must be a list of fact ids"))
        return None
    if len(text) > MAX_TEXT_CHARS:
        out.append(Violation("structure", where, f"longer than {MAX_TEXT_CHARS} characters"))
    return text, cites


def check_narrative(obj: Any, g: Grounding) -> list[Violation]:
    out: list[Violation] = []
    if not isinstance(obj, dict):
        return [Violation("structure", "answer", "not a JSON object")]
    units: list[tuple[str, Any]] = [("summary", obj.get("summary"))]
    sections = obj.get("sections") or []
    focus = obj.get("focus") or []
    if not isinstance(sections, list) or not isinstance(focus, list):
        return [Violation("structure", "answer", "sections and focus must be lists")]
    if len(sections) > MAX_SECTIONS:
        out.append(Violation("structure", "sections", f"more than {MAX_SECTIONS} sections"))
    if len(focus) > MAX_FOCUS:
        out.append(Violation("structure", "focus", f"more than {MAX_FOCUS} focus items"))
    units += [(f"sections[{i}]", s) for i, s in enumerate(sections)]
    units += [(f"focus[{i}]", s) for i, s in enumerate(focus)]
    for where, unit in units:
        parsed = _unit(unit, where, out)
        if parsed is None:
            continue
        text, cites = parsed
        if where == "summary" and len(text) > MAX_SUMMARY_CHARS:
            out.append(Violation("structure", where, "summary too long"))
        if not cites:
            out.append(Violation("citation", where, "cites no fact"))
        if where.startswith("focus") and not any(
            g.facts.get(c) is not None and g.facts[c].kind == "finding" for c in cites
        ):
            out.append(Violation("structure", where, "a focus item must cite a finding"))
        title = unit.get("title") if isinstance(unit, dict) else None
        if isinstance(title, str) and title:
            text = f"{title}. {text}"
        out += check_text(text, cites, g, where)
    return out


def check_answer(obj: Any, g: Grounding) -> list[Violation]:
    if not isinstance(obj, dict):
        return [Violation("structure", "answer", "not a JSON object")]
    out: list[Violation] = []
    parsed = _unit({"text": obj.get("answer"), "cites": obj.get("cites")}, "answer", out)
    if parsed is None:
        return out
    text, cites = parsed
    if not obj.get("refused") and not cites:
        out.append(Violation("citation", "answer", "an answer must cite the facts it uses"))
    return out + check_text(text, cites, g, "answer")


def repair_citations(obj: dict[str, Any], violations: list[Violation]) -> bool:
    """Add the citations that ground otherwise-correct numbers (a number written in a fact
    that was not cited). True when every violation was of that kind (and was repaired).
    Named corners are never repaired: a claim about a corner needs the AI's own citation."""
    if not violations or not all(v.kind == "number" and v.repair for v in violations):
        return False
    for v in violations:
        unit = _locate(obj, v.where)
        if unit is None:
            return False
        cites = unit.setdefault("cites", [])
        if v.repair not in cites:
            cites.append(v.repair)
    return True


def _locate(obj: dict[str, Any], where: str) -> dict[str, Any] | None:
    if where in ("summary", "answer"):
        return obj.get("summary") if where == "summary" else obj
    m = re.fullmatch(r"(sections|focus)\[(\d+)\]", where)
    if not m:
        return None
    items = obj.get(m.group(1)) or []
    i = int(m.group(2))
    return items[i] if i < len(items) and isinstance(items[i], dict) else None
