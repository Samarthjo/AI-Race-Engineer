"""Questions and answers (5c): the model may look things up with read-only tools, then
answers in a checked format.

Tools (all read-only; each returns facts with ids, like the report):

* ``list_corners``: the corners and straights of the track;
* ``get_corner``: everything measured about one corner of this lap;
* ``compare_laps``: this lap against another lap of the session;
* ``get_history``: progress over past sessions; not available before Stage 7, and it says so.

The answer is a JSON object ``{answer, cites, refused}`` and goes through the same grounding
check as the report text (numbers, corners, estimates, topics), against the report's facts
plus every fact a tool returned. The model is told to refuse when the data cannot answer; if
its answer fails the check twice, Pitwall answers with a refusal itself.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pitwall.engineer.facts import Fact
from pitwall.engineer.llm import LLMError, LLMProvider, LLMUnavailable
from pitwall.engineer.narrator import facts_hash
from pitwall.engineer.validate import Grounding, Violation, check_answer, repair_citations

QA_PROMPT_VERSION = "qa-1"
MAX_TOOL_ROUNDS = 3
MAX_ATTEMPTS = 2
REFUSAL = "I can't answer that reliably from the measured data of this lap."

QA_SYSTEM = """You are the race engineer of a sim racer who drives F1 25 (a video game) on a \
PS5 with a controller and no assists. You answer questions about one lap, using only measured \
facts: the facts below and what your tools return.

Rules (a program checks your answer; if a rule is broken, the answer is thrown away):
1. Every number you write must be written in a fact you cite (you may round). Never calculate.
2. Cite the ids of the facts you use.
3. Facts with "estimate": true come from real Formula 1 data: write "about" before their \
numbers.
4. Name only corners from the facts you cite.
5. If the facts and tools cannot answer the question (for example tyres, fuel, set-up, \
steering, other cars, past sessions), set "refused" to true and say briefly what is not \
measured. Never guess.
6. Answer in plain English, second person, at most 4 sentences.

Facts of this lap:
{facts}"""

FINAL_PROMPT = """Now give your final answer as JSON: {"answer": ..., "cites": [...], \
"refused": true|false}."""

RETRY_PROMPT = """The fact check rejected your answer:
{problems}
Answer again, quoting numbers exactly as written in the facts you cite, or refuse."""

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_corners",
            "description": "The corners and straights of this track, with their ids and names.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_corner",
            "description": (
                "Everything measured about one corner or straight of this lap: time against "
                "the reference, findings, consistency, and the real-F1 comparison if selected."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "corner": {"type": "string", "description": "Corner id (e.g. c3) or name"}
                },
                "required": ["corner"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_laps",
            "description": (
                "This lap against another lap of the same session: lap times, sectors, "
                "corners and findings."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "lap_number": {"type": "integer", "description": "The other lap's number"}
                },
                "required": ["lap_number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_history",
            "description": "Progress over past sessions and recurring patterns.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


@dataclass
class QATools:
    """Implementations of the tools for one lap. ``compare`` builds facts for this lap
    against another lap number (or raises LookupError / ValueError with a reason)."""

    facts: dict[str, Fact]
    segments: list[tuple[str, str]]
    compare: Callable[[int], list[Fact]]
    added: dict[str, Fact] = field(default_factory=dict)

    def _out(self, facts: list[Fact], note: str | None = None) -> str:
        for f in facts:
            self.added[f.id] = f
        body: dict[str, Any] = {
            "facts": [{"id": f.id, "text": f.text, "estimate": f.estimate} for f in facts]
        }
        if note:
            body["note"] = note
        return json.dumps(body, ensure_ascii=False)

    def call(self, name: str, args: dict[str, Any]) -> str:
        if name == "list_corners":
            return json.dumps(
                {"corners": [{"id": i, "name": n} for i, n in self.segments]}, ensure_ascii=False
            )
        if name == "get_corner":
            key = str(args.get("corner", "")).strip().lower()
            seg = next(
                (i for i, n in self.segments if key in (i.lower(), n.lower())),
                None,
            )
            if seg is None:
                return json.dumps({"error": f"no corner {args.get('corner')!r}; use list_corners"})
            found = [f for f in self.facts.values() if f.segment_id == seg]
            return self._out(found, None if found else "nothing measured above the thresholds here")
        if name == "compare_laps":
            try:
                n = int(args.get("lap_number"))  # type: ignore[arg-type]
                return self._out(self.compare(n))
            except (LookupError, ValueError, TypeError) as exc:
                return json.dumps({"error": str(exc)})
        if name == "get_history":
            note = Fact(
                "note:no-history",
                "note",
                "History over past sessions is not available yet (Stage 7); only this session "
                "has been analysed.",
            )
            return self._out([note])
        return json.dumps({"error": f"unknown tool {name!r}"})


@dataclass
class Answer:
    status: str  # grounded | refused | fallback | unavailable
    question: str
    output: dict[str, Any]
    attempts: int
    tool_calls: list[dict[str, Any]]
    log: list[dict[str, Any]]
    provider: str
    model: str
    prompt_version: str
    facts_hash: str
    duration_s: float
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "question": self.question,
            "output": self.output,
            "attempts": self.attempts,
            "tool_calls": self.tool_calls,
            "log": self.log,
            "provider": self.provider,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "facts_hash": self.facts_hash,
            "duration_s": round(self.duration_s, 2),
            "error": self.error,
        }


def answer_schema(fact_ids: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "cites": {"type": "array", "items": {"type": "string", "enum": fact_ids}},
            "refused": {"type": "boolean"},
        },
        "required": ["answer", "cites", "refused"],
    }


def ask(
    question: str,
    tools: QATools,
    grounding: Grounding,
    provider: LLMProvider,
) -> Answer:
    base = [{"id": f.id, "text": f.text, "estimate": f.estimate} for f in tools.facts.values()]
    digest = facts_hash({"facts": base, "question": question})
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": QA_SYSTEM.format(facts=json.dumps(base, ensure_ascii=False))},
        {"role": "user", "content": question},
    ]
    calls: list[dict[str, Any]] = []
    log: list[dict[str, Any]] = []
    t0 = time.monotonic()

    def done(status: str, output: dict[str, Any], error: str | None = None) -> Answer:
        return Answer(
            status=status,
            question=question,
            output=output,
            attempts=len(log),
            tool_calls=calls,
            log=log,
            provider=provider.name,
            model=provider.model,
            prompt_version=QA_PROMPT_VERSION,
            facts_hash=digest,
            duration_s=time.monotonic() - t0,
            error=error,
        )

    refusal = {"answer": REFUSAL, "cites": [], "refused": True}
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            res = provider.chat(messages, tools=TOOLS)
            if not res.tool_calls:
                break
            messages.append(
                {
                    "role": "assistant",
                    "content": res.content,
                    "tool_calls": res.raw_message.get("tool_calls")
                    or [
                        {"function": {"name": c.name, "arguments": c.arguments}}
                        for c in res.tool_calls
                    ],
                }
            )
            for call in res.tool_calls:
                output = tools.call(call.name, call.arguments)
                calls.append({"name": call.name, "arguments": call.arguments})
                messages.append({"role": "tool", "tool_name": call.name, "content": output})
    except LLMUnavailable as exc:
        return done("unavailable", refusal, str(exc))
    except LLMError as exc:
        log.append({"attempt": 0, "error": str(exc), "violations": []})

    pool = {**tools.facts, **tools.added}
    g = Grounding(pool, grounding.segments, grounding.lap_numbers)
    schema = answer_schema(sorted(pool))
    messages.append({"role": "user", "content": FINAL_PROMPT})
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            res = provider.chat(messages, schema=schema)
        except LLMUnavailable as exc:
            return done("unavailable", refusal, str(exc))
        except LLMError as exc:
            log.append({"attempt": attempt, "error": str(exc), "violations": []})
            continue
        try:
            obj = json.loads(res.content)
        except ValueError:
            obj = None
        violations: list[Violation] = check_answer(obj, g)
        repaired = False
        if violations and isinstance(obj, dict) and repair_citations(obj, violations):
            repaired = True
            violations = check_answer(obj, g)
        log.append(
            {
                "attempt": attempt,
                "violations": [v.to_dict() for v in violations],
                "repaired_citations": repaired,
                "duration_s": round(res.duration_s, 2),
            }
        )
        if not violations and isinstance(obj, dict):
            return done("refused" if obj.get("refused") else "grounded", obj)
        problems = "\n".join(f"- {v.detail}" for v in violations[:10])
        messages += [
            {"role": "assistant", "content": res.content},
            {"role": "user", "content": RETRY_PROMPT.format(problems=problems)},
        ]
    return done("fallback", refusal, "the answer failed the fact check twice")
