"""The AI write-up of a report (5b): facts in, checked text out.

1. The model gets only the report's facts (ids, sentences, which ones are estimates) and the
   deterministic focus ranking; never telemetry.
2. Its answer must follow a JSON schema; citations can only be fact ids of this report.
3. The grounding check (:mod:`validate`) runs on the answer. Citations that only miss a fact
   whose number was quoted correctly are added automatically.
4. If anything else fails, the model gets one second chance with the list of problems.
5. If that fails too, the deterministic report is shown instead, saying so.

Every result records the provider, model, prompt version and a hash of the facts it saw.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from pitwall.engineer.facts import fmt_s
from pitwall.engineer.llm import LLMError, LLMProvider, LLMUnavailable
from pitwall.engineer.report import EngineerReport
from pitwall.engineer.validate import Grounding, Violation, check_narrative, repair_citations

PROMPT_VERSION = "narrator-2"
MAX_ATTEMPTS = 2

SYSTEM_PROMPT = """You are the race engineer of a sim racer who drives F1 25 (a video game) on a \
PS5, with a controller and no assists. After a lap you explain briefly and concretely what the \
measurements show and what to try on the next lap.

Rules. A program checks your answer against the facts; if a rule is broken, the answer is \
thrown away.
1. Use only the facts given. Add nothing about the track, the car, tyres, fuel, set-up, \
weather or driving technique that the facts do not say.
2. Every number you write must be written in one of the facts you cite. You may round it \
(0.386 s can be 0.39 s). Never calculate new numbers: no sums, differences, averages or \
percentages of your own.
3. List the ids of the facts you use in "cites", for every part of the answer.
4. Facts with "estimate": true come from real Formula 1 data: write "about" before their \
numbers.
5. Name only corners that appear in the facts you cite.
6. "focus": at most 3 items, the most useful changes for the next lap, based on the findings \
and their suggestions (the list "suggested_focus" is the measured ranking). Each focus item \
cites its finding.
7. If there are no findings, say that the lap matched the reference within what can be \
measured, and give no advice.
8. Plain English, second person ("you"), short sentences. The summary has at most 3 \
sentences. Sections are optional; use at most 4.
9. Facts of kind "history" describe earlier sessions at this track (trend, recurring habits, \
how the last recommendations worked out). Use them only to put this lap in context, for \
example when a focus item is a recurring habit; this lap's findings come first."""

RETRY_PROMPT = """The fact check rejected your answer:
{problems}
Write the whole answer again. Quote numbers exactly as they are written in the facts you \
cite (rounding is fine), do not calculate, name only corners from the cited facts, and write \
"about" before numbers from facts marked as estimates."""


def schema_for(fact_ids: list[str]) -> dict[str, Any]:
    cites = {"type": "array", "items": {"type": "string", "enum": fact_ids}}
    part = {
        "type": "object",
        "properties": {"text": {"type": "string"}, "cites": cites},
        "required": ["text", "cites"],
    }
    section = {
        "type": "object",
        "properties": {"title": {"type": "string"}, "text": {"type": "string"}, "cites": cites},
        "required": ["title", "text", "cites"],
    }
    return {
        "type": "object",
        "properties": {
            "summary": part,
            "sections": {"type": "array", "items": section, "maxItems": 4},
            "focus": {"type": "array", "items": part, "maxItems": 3},
        },
        "required": ["summary", "sections", "focus"],
    }


def facts_payload(report: EngineerReport) -> dict[str, Any]:
    """What the model sees: facts only."""
    return {
        "lap": report.headline,
        "reference": report.reference["label"],
        "facts": [
            {"id": f.id, "kind": f.kind, "text": f.text, "estimate": f.estimate}
            for f in report.facts
        ],
        "suggested_focus": [f.fact_id for f in report.focus],
    }


def deterministic_output(report: EngineerReport) -> dict[str, Any]:
    """The deterministic report in the narrative's shape (shown when the AI text fails)."""
    summary_cites = ["lap"] + ([report.losses[0].fact_id] if report.losses else [])
    sections = []
    if report.losses:
        sections.append(
            {
                "title": "Where the time goes",
                "text": " ".join(f"{x.segment_name}: {fmt_s(x.loss_s)}." for x in report.losses),
                "cites": [x.fact_id for x in report.losses],
            }
        )
    notes = report.notes
    if notes:
        sections.append(
            {
                "title": "Notes",
                "text": " ".join(report.fact(n).text for n in notes),
                "cites": list(notes),
            }
        )
    return {
        "summary": {"text": report.headline, "cites": summary_cites},
        "sections": sections,
        "focus": [
            {"text": f"{f.segment_name}: {f.instruction}", "cites": [f.fact_id]}
            for f in report.focus
        ],
    }


@dataclass
class Narrative:
    status: str  # grounded | fallback | unavailable
    output: dict[str, Any]
    attempts: int
    log: list[dict[str, Any]]
    provider: str
    model: str
    prompt_version: str
    facts_hash: str
    duration_s: float
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "output": self.output,
            "attempts": self.attempts,
            "log": self.log,
            "provider": self.provider,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "facts_hash": self.facts_hash,
            "duration_s": round(self.duration_s, 2),
            "error": self.error,
            **self.extra,
        }


def facts_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _problems(violations: list[Violation]) -> str:
    return "\n".join(f"- {v.where}: {v.detail}" for v in violations[:15])


def narrate(report: EngineerReport, provider: LLMProvider, grounding: Grounding) -> Narrative:
    payload = facts_payload(report)
    digest = facts_hash(payload)
    schema = schema_for([f.id for f in report.facts])
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    log: list[dict[str, Any]] = []
    t0 = time.monotonic()

    def result(status: str, output: dict[str, Any], error: str | None = None) -> Narrative:
        return Narrative(
            status=status,
            output=output,
            attempts=len(log),
            log=log,
            provider=provider.name,
            model=provider.model,
            prompt_version=PROMPT_VERSION,
            facts_hash=digest,
            duration_s=time.monotonic() - t0,
            error=error,
        )

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            res = provider.chat(messages, schema=schema)
        except LLMUnavailable as exc:
            return result("unavailable", deterministic_output(report), str(exc))
        except LLMError as exc:
            log.append({"attempt": attempt, "error": str(exc), "violations": []})
            continue
        try:
            obj = json.loads(res.content)
        except ValueError:
            obj = None
        violations = check_narrative(obj, grounding)
        repaired = False
        if violations and isinstance(obj, dict) and repair_citations(obj, violations):
            repaired = True
            violations = check_narrative(obj, grounding)
        log.append(
            {
                "attempt": attempt,
                "violations": [v.to_dict() for v in violations],
                "repaired_citations": repaired,
                "duration_s": round(res.duration_s, 2),
                "output_tokens": res.output_tokens,
            }
        )
        if not violations and isinstance(obj, dict):
            return result("grounded", obj)
        messages += [
            {"role": "assistant", "content": res.content},
            {"role": "user", "content": RETRY_PROMPT.format(problems=_problems(violations))},
        ]
    return result(
        "fallback",
        deterministic_output(report),
        "the AI text failed the fact check twice; showing the measured report",
    )


def render_narrative(output: dict[str, Any]) -> str:
    """A narrative (or its deterministic stand-in) as plain text, with its citations."""

    def cites(part: dict[str, Any]) -> str:
        return f" [{', '.join(part.get('cites') or [])}]" if part.get("cites") else ""

    lines = [output["summary"]["text"] + cites(output["summary"]), ""]
    for sec in output.get("sections") or []:
        lines.append(f"{sec.get('title', '')}: {sec['text']}{cites(sec)}")
    if output.get("focus"):
        lines += ["", "Focus for the next lap:"]
        for i, f in enumerate(output["focus"], 1):
            lines.append(f" {i}. {f['text']}{cites(f)}")
    return "\n".join(lines).strip() + "\n"
