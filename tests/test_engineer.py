"""Stage 5: the race engineer. Deterministic report on synthetic laps with known mistakes,
the grounding check (adversarial cases), and the AI write-up and Q&A against a mock Ollama."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from pitwall.engineer.facts import Fact, parse_numbers
from pitwall.engineer.llm import LLMUnavailable, OllamaProvider
from pitwall.engineer.narrator import deterministic_output, narrate
from pitwall.engineer.report import render_text
from pitwall.engineer.service import ask_lap, build_context, grounding_for, narrate_lap
from pitwall.engineer.store import row_payload
from pitwall.engineer.validate import (
    Grounding,
    check_answer,
    check_narrative,
    check_text,
    repair_citations,
)
from pitwall.ingest.pipeline import DB_NAME, ingest_capture
from pitwall.storage import db
from pitwall.synth.session import LapPlan, SessionSynth
from pitwall.synth.track import DriverProfile, build_track
from tests.mock_ollama import MockOllama, text_reply, tool_reply
from tests.test_ingest import write_capture

GOLDEN = Path(__file__).parent / "golden"
BASE = DriverProfile()
SCENARIOS = {
    "base": BASE,  # lap index 1 (the reference)
    "twin": BASE,  # 2
    "brake_early": DriverProfile(brake_early_m={0: 15.0}),  # 3
    "late_throttle": DriverProfile(throttle_delay_m={3: 30.0}),  # 4
    "lock_spin": DriverProfile(lockup_m={2: 6.0}, wheelspin_m={5: 10.0}),  # 5
}
INDEX = {name: i + 1 for i, name in enumerate(SCENARIOS)}


@pytest.fixture(scope="module")
def setup(tmp_path_factory):
    plans = [LapPlan(kind="out", driver=BASE)] + [LapPlan(driver=d) for d in SCENARIOS.values()]
    synth = SessionSynth(track=build_track(), track_id=11).run(plans, final_partial=0.2)
    data = tmp_path_factory.mktemp("eng")
    cap = write_capture(data / "raw", "20261115T100000Z", synth.records)
    conn = db.connect(data / DB_NAME)
    key = ingest_capture(cap, data, conn).sessions[0].session_key

    def ctx(name: str):
        return build_context(conn, data, key, INDEX[name], ref_kind="lap", ref_lap=INDEX["base"])

    return {"conn": conn, "data": data, "key": key, "ctx": ctx}


# ------------------------------------------------------------------ 5a: the report


def test_report_focus_follows_the_measured_mistakes(setup):
    r = setup["ctx"]("brake_early").report
    assert [f.fact_id for f in r.focus] == ["find:c1:EARLY_BRAKE"]
    assert "later" in r.focus[0].instruction and "15 m earlier" in r.focus[0].measured
    assert r.losses[0].segment_id == "c1" and r.losses[0].causes == ["find:c1:EARLY_BRAKE"]
    assert r.headline.startswith("Lap 4: 1:")
    late = setup["ctx"]("late_throttle").report
    assert late.focus[0].fact_id == "find:c4:LATE_THROTTLE"
    events = setup["ctx"]("lock_spin").report
    assert {f.fact_id for f in events.focus} == {"find:c3:LOCKUP", "find:c6:WHEELSPIN"}


def test_report_says_so_when_there_is_nothing_to_change(setup):
    r = setup["ctx"]("twin").report
    assert r.focus == [] and r.losses == []
    assert "note:no-findings" in r.notes
    assert "No segment loses a measurable amount of time" in r.headline
    same = build_context(
        setup["conn"], setup["data"], setup["key"], INDEX["base"], ref_kind="lap", ref_lap=1
    ).report
    assert "note:same-lap" in same.notes


def test_every_fact_number_is_readable_and_the_deterministic_text_is_grounded(setup):
    for name in SCENARIOS:
        ctx = setup["ctx"](name)
        for f in ctx.report.facts:
            assert parse_numbers(f.text, ctx.report.segment_names) == f.numbers
        # the deterministic report, in the narrative's shape, passes the same check
        assert check_narrative(deterministic_output(ctx.report), grounding_for(ctx)) == []


def test_golden_report(setup):
    text = render_text(setup["ctx"]("late_throttle").report)
    path = GOLDEN / "engineer-report-late-throttle.txt"
    if os.environ.get("PITWALL_UPDATE_GOLDEN") == "1" or not path.exists():
        path.parent.mkdir(exist_ok=True)
        path.write_text(text)
    assert text == path.read_text(), "report changed: review, then PITWALL_UPDATE_GOLDEN=1"


# ------------------------------------------------------------------ grounding check

SEGMENTS = [("c1", "Variante del Rettifilo (T1-T2)"), ("s1", "Straight to C2"), ("c2", "C2")]
NAMES = [n for _, n in SEGMENTS]


def _g(*facts: Fact) -> Grounding:
    return Grounding({f.id: f.bind(NAMES) for f in facts}, SEGMENTS, {2, 3, 4, 17})


LAP = Fact("lap", "lap", "Lap 4 at Monza: 1:12.012, +0.386 s against the lap 2 (1:11.626).")
SEG = Fact(
    "seg:c1",
    "segment",
    "Variante del Rettifilo (T1-T2): +0.386 s against the reference.",
    segment_id="c1",
)
FIND = Fact(
    "find:c1:EARLY_BRAKE",
    "finding",
    "Variante del Rettifilo (T1-T2): Brakes earlier than the reference. Braking starts 15 m "
    "earlier (326 m vs 341 m into the lap) with similar brake pressure. Costs +0.386 s (high "
    "confidence). Suggestion: Try braking about 7 m later (half the gap).",
    segment_id="c1",
    code="EARLY_BRAKE",
)
GAIN = Fact("strength:c2", "strength", "C2: -0.138 s, faster than the reference here.", "c2")
LOCK = Fact(
    "find:c2:LOCKUP",
    "finding",
    "C2: Front wheel lock-up. A front wheel locks for 6 m under braking.",
    "c2",
    code="LOCKUP",
)
PRO = Fact(
    "pro:c2:LOW_MIN_SPEED",
    "finding",
    "C2: Lower minimum speed than VER. Minimum speed 145 vs about 154 km/h; after the "
    "systematic difference this corner is 7 km/h (± 1) slower than your norm.",
    "c2",
    basis="pro_normalised",
    estimate=True,
    code="LOW_MIN_SPEED",
)
G = _g(LAP, SEG, FIND, GAIN, LOCK, PRO)


def _kinds(text: str, cites: list[str]) -> list[str]:
    return [v.kind for v in check_text(text, cites, G, "t")]


def test_grounded_text_passes_with_rounding_and_other_forms():
    assert _kinds("You brake 15 m early into the T1-T2 chicane and lose 0.39 s.", [FIND.id]) == []
    assert _kinds("That costs about 0.4 s, or 386 ms, at Turn 1.", [FIND.id]) == []
    assert _kinds("Your 1:12.0 was 0.386 s slower than lap 2.", [LAP.id]) == []
    assert _kinds("Two things matter, 3 at most.", [LAP.id]) == []  # counts


@pytest.mark.parametrize(
    ("text", "cites", "kind"),
    [
        ("You lose 0.52 s at T1-T2.", [FIND.id], "number"),  # invented
        ("In total you lost 0.772 s at T1-T2.", [FIND.id, SEG.id], "number"),  # calculated
        ("You brake 15 m early and lose 0.39 s in C2.", [FIND.id], "corner"),  # wrong corner
        ("You were 0.386 s faster at T1-T2.", [SEG.id], "direction"),  # a loss, not a gain
        ("Your minimum speed in C2 is 7 km/h below your norm.", [PRO.id], "estimate"),
        ("Your tyres were too cold in C2.", [LOCK.id], "topic"),
        ("You lock up in T1-T2.", [FIND.id], "topic"),
        ("Compared with lap 99 you lost 0.386 s.", [LAP.id], "lap"),
        ("You lost 0.386 s.", ["nope"], "citation"),
    ],
)
def test_unsupported_claims_are_rejected(text, cites, kind):
    assert kind in _kinds(text, cites)


def test_estimates_and_events_pass_when_worded_right():
    assert _kinds("In C2 you are about 7 km/h slower than your norm vs VER.", [PRO.id]) == []
    assert _kinds("In C2 a front wheel locks for 6 m.", [LOCK.id]) == []


def test_a_correct_number_with_a_missing_citation_is_repaired():
    obj = {"answer": "You gained 0.14 s.", "cites": [FIND.id], "refused": False}
    violations = check_answer(obj, G)
    assert [v.kind for v in violations] == ["number"] and violations[0].repair == GAIN.id
    assert repair_citations(obj, violations) and check_answer(obj, G) == []
    # an invented number is never repaired
    bad = {"answer": "You gained 0.52 s.", "cites": [FIND.id], "refused": False}
    assert not repair_citations(bad, check_answer(bad, G))


def test_narrative_structure_rules():
    focus = [{"text": "Brake later at T1-T2.", "cites": [FIND.id]}] * 4
    obj = {
        "summary": {"text": "Lap 4: 1:12.012.", "cites": [LAP.id]},
        "sections": [],
        "focus": focus,
    }
    assert "structure" in {v.kind for v in check_narrative(obj, G)}
    no_finding = {
        "summary": {"text": "Lap 4.", "cites": [LAP.id]},
        "sections": [],
        "focus": [{"text": "Lap 4 was 0.386 s slower.", "cites": [LAP.id]}],
    }
    assert any(v.kind == "structure" for v in check_narrative(no_finding, G))
    assert check_narrative("not json", G)[0].kind == "structure"


# ------------------------------------------------------------------ 5b: AI write-up


def _good(report) -> dict:
    f = report.focus[0]
    return {
        "summary": {"text": report.fact("lap").text, "cites": ["lap"]},
        "sections": [
            {
                "title": "Where the time goes",
                "text": report.fact(f"seg:{f.segment_id}").text,
                "cites": [f"seg:{f.segment_id}"],
            }
        ],
        "focus": [{"text": f"{f.segment_name}: {f.instruction}", "cites": [f.fact_id]}],
    }


def _bad(report) -> dict:
    out = _good(report)
    out["summary"] = {"text": "You lost 9.99 s at C1.", "cites": ["seg:c1"]}
    return out


def test_narration_is_checked_and_stored(setup):
    ctx = setup["ctx"]("brake_early")
    with MockOllama() as mock:
        mock.replies = [text_reply(_good(ctx.report))]
        provider = OllamaProvider(base_url=mock.url)
        result, nid = narrate_lap(setup["conn"], ctx, provider)
    assert result.status == "grounded" and result.attempts == 1
    req = mock.requests[0]
    assert req["stream"] is False and req["think"] is False and req["model"] == "qwen3:14b"
    assert req["options"]["temperature"] == 0.2
    enum = req["format"]["properties"]["summary"]["properties"]["cites"]["items"]["enum"]
    assert set(enum) == {f.id for f in ctx.report.facts}
    sent = json.loads(req["messages"][1]["content"])
    assert {f["id"] for f in sent["facts"]} == set(enum)  # facts only, no telemetry
    rows = db.list_narratives(setup["conn"], setup["key"], INDEX["brake_early"])
    stored = row_payload(rows[0])
    assert stored["narrative_id"] == nid and stored["status"] == "grounded"
    assert stored["prompt_version"] == "narrator-1" and stored["model"] == "qwen3:14b"
    assert stored["output"] == result.output


def test_one_retry_with_the_problems_then_the_measured_report(setup):
    ctx = setup["ctx"]("brake_early")
    with MockOllama() as mock:
        mock.replies = [text_reply(_bad(ctx.report)), text_reply(_good(ctx.report))]
        retried = narrate(ctx.report, OllamaProvider(base_url=mock.url), grounding_for(ctx))
        assert retried.status == "grounded" and retried.attempts == 2
        retry_msg = mock.requests[1]["messages"][-1]["content"]
        assert "rejected" in retry_msg and "9.99" in retry_msg
        mock.replies = [text_reply(_bad(ctx.report)), text_reply("{not json")]
        failed = narrate(ctx.report, OllamaProvider(base_url=mock.url), grounding_for(ctx))
    assert failed.status == "fallback" and failed.attempts == 2
    assert failed.output == deterministic_output(ctx.report)
    assert failed.log[0]["violations"] and failed.log[1]["violations"]


def test_without_ollama_the_measured_report_is_shown(setup):
    ctx = setup["ctx"]("brake_early")
    down = OllamaProvider(base_url="http://127.0.0.1:9", timeout_s=2)
    result = narrate(ctx.report, down, grounding_for(ctx))
    assert result.status == "unavailable" and "ollama serve" in (result.error or "")
    assert result.output == deterministic_output(ctx.report)
    st = down.status()
    assert st["reachable"] is False and st["error"]
    with MockOllama(models=("llama3.1:8b",)) as mock:
        provider = OllamaProvider(base_url=mock.url)
        st = provider.status()
        assert st["reachable"] and st["installed"] == ["llama3.1:8b"]
        assert st["model_installed"] is False and "ollama pull qwen3:14b" in st["error"]
        with pytest.raises(LLMUnavailable, match="ollama pull"):
            provider.chat([{"role": "user", "content": "hi"}])


# ------------------------------------------------------------------ 5c: questions


def test_question_with_a_tool_call(setup):
    ctx = setup["ctx"]("brake_early")
    find = ctx.report.fact("find:c1:EARLY_BRAKE")
    answer = {
        "answer": "At C1 you brake 15 m earlier than the reference; that costs 0.39 s.",
        "cites": [find.id],
        "refused": False,
    }
    with MockOllama() as mock:
        mock.replies = [
            tool_reply(("get_corner", {"corner": "c1"})),
            text_reply("C1 is the problem."),
            text_reply(answer),
        ]
        result, _ = ask_lap(
            setup["conn"], setup["data"], ctx, OllamaProvider(base_url=mock.url), "Where do I lose?"
        )
    assert result.status == "grounded", result.log
    assert result.tool_calls == [{"name": "get_corner", "arguments": {"corner": "c1"}}]
    tool_msg = mock.requests[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and tool_msg["tool_name"] == "get_corner"
    assert find.id in tool_msg["content"]
    assert "tools" in mock.requests[0] and "format" in mock.requests[2]


def test_compare_with_another_lap_through_a_tool(setup):
    ctx = setup["ctx"]("brake_early")
    with MockOllama() as mock:

        def final(body: dict) -> dict:
            # quote the lap delta exactly as the compare tool wrote it
            tool = next(m for m in body["messages"] if m["role"] == "tool")
            fact = next(f for f in json.loads(tool["content"])["facts"] if f["id"] == "vs5:lap")
            delta = next(n for n in parse_numbers(fact["text"]) if n.sign)
            return text_reply(
                {
                    "answer": f"Against lap 5 you were {abs(delta.value):.3f} s "
                    + ("slower." if delta.sign > 0 else "faster."),
                    "cites": ["vs5:lap"],
                    "refused": False,
                }
            )

        mock.replies = [tool_reply(("compare_laps", {"lap_number": 5})), text_reply(""), final]
        result, _ = ask_lap(
            setup["conn"],
            setup["data"],
            ctx,
            OllamaProvider(base_url=mock.url),
            "How did this lap compare with lap 5?",
        )
    assert result.status == "grounded", result.log


def test_questions_the_data_cannot_answer_are_refused(setup):
    ctx = setup["ctx"]("brake_early")
    with MockOllama() as mock:
        mock.replies = [
            tool_reply(("get_history", {})),
            text_reply(""),
            text_reply(
                {
                    "answer": "Your progress over past sessions is not available yet.",
                    "cites": ["note:no-history"],
                    "refused": True,
                }
            ),
        ]
        refused, _ = ask_lap(
            setup["conn"], setup["data"], ctx, OllamaProvider(base_url=mock.url), "Am I improving?"
        )
        assert refused.status == "refused"
        invented = {"answer": "Your tyres were 95 degrees.", "cites": [], "refused": False}
        mock.replies = [text_reply(""), text_reply(invented), text_reply(invented)]
        fallback, _ = ask_lap(
            setup["conn"], setup["data"], ctx, OllamaProvider(base_url=mock.url), "Tyre temps?"
        )
    assert fallback.status == "fallback" and fallback.output["refused"] is True
    assert "can't answer" in fallback.output["answer"]
    rows = db.list_narratives(setup["conn"], setup["key"], INDEX["brake_early"])
    assert {r["kind"] for r in rows} >= {"answer"}


# ------------------------------------------------------------------ API and CLI


def test_engineer_api(setup):
    from fastapi.testclient import TestClient

    from pitwall.api.app import create_app

    ctx = setup["ctx"]("brake_early")
    with MockOllama() as mock:
        app = create_app(setup["data"], live=None, llm=OllamaProvider(base_url=mock.url))
        c = TestClient(app)
        st = c.get("/api/engineer/status").json()
        assert st["reachable"] and st["model_installed"] and st["prompt_versions"]["report"]
        params = {"session": setup["key"], "lap": INDEX["brake_early"], "ref": "lap", "ref_lap": 1}
        rep = c.get("/api/engineer/report", params=params).json()
        assert rep["focus"][0]["fact_id"] == "find:c1:EARLY_BRAKE" and "Focus" in rep["text"]
        assert {f["id"] for f in rep["facts"]} >= {"lap", "seg:c1"}
        mock.replies = [text_reply(_good(ctx.report))]
        nar = c.post("/api/engineer/narrate", params=params).json()
        assert nar["status"] == "grounded" and nar["narrative_id"]
        mock.replies = [
            text_reply(""),
            text_reply({"answer": "Not measured.", "cites": [], "refused": True}),
        ]
        ans = c.post("/api/engineer/ask", json={**params, "question": "Tyre pressures?"}).json()
        assert ans["status"] == "refused"
        hist = c.get("/api/engineer/report", params=params).json()["history"]
        assert [h["kind"] for h in hist[:2]] == ["answer", "report"]
        bad = c.get("/api/engineer/report", params={**params, "lap": 0})
        assert bad.status_code == 422
        assert c.get("/api/engineer/report", params={**params, "session": "x"}).status_code == 404
        empty = c.post("/api/engineer/ask", json={**params, "question": ""})
        assert empty.status_code == 422


def test_engineer_cli(setup, tmp_path):
    from typer.testing import CliRunner

    from pitwall.cli import app

    ctx = setup["ctx"]("brake_early")
    with MockOllama() as mock:
        cfg = tmp_path / "config.toml"
        cfg.write_text(f'data_dir = "{setup["data"]}"\n[engineer]\nbase_url = "{mock.url}"\n')
        runner = CliRunner(env={"COLUMNS": "200", "PITWALL_CONFIG": str(cfg)})
        st = runner.invoke(app, ["engineer", "status"])
        assert st.exit_code == 0 and "qwen3:14b is installed" in st.output, st.output
        args = ["--lap", str(INDEX["brake_early"]), "--ref", "lap", "--ref-lap", "1"]
        rep = runner.invoke(app, ["engineer", "report", setup["key"], *args])
        assert rep.exit_code == 0 and "Focus for the next lap" in rep.output, rep.output
        mock.replies = [text_reply(_good(ctx.report))]
        nar = runner.invoke(app, ["engineer", "narrate", setup["key"], *args])
        assert nar.exit_code == 0 and "every number and corner traced" in nar.output, nar.output
        mock.replies = [text_reply(""), text_reply(_bad(ctx.report)), text_reply(_bad(ctx.report))]
        nar2 = runner.invoke(app, ["engineer", "narrate", setup["key"], *args])
        assert "failed the fact check twice" in nar2.output
        mock.replies = [
            text_reply(""),
            text_reply({"answer": "Not measured here.", "cites": [], "refused": True}),
        ]
        ask = runner.invoke(app, ["engineer", "ask", setup["key"], "Tyre temps?", *args])
        assert ask.exit_code == 0 and "the data cannot answer this" in ask.output, ask.output
        mock.replies = [lambda body: text_reply(_good(ctx.report))] * 10
        ev = runner.invoke(app, ["engineer", "eval", "--limit", "2"])
        assert ev.exit_code == 0, ev.output
        sheets = list((setup["data"] / "engineer").glob("eval-*.md"))
        assert sheets and "Your rating" in sheets[0].read_text()
    down = tmp_path / "down.toml"
    down.write_text('[engineer]\nbase_url = "http://127.0.0.1:9"\n')
    off = CliRunner(env={"PITWALL_CONFIG": str(down)}).invoke(app, ["engineer", "status"])
    assert off.exit_code == 1 and "ollama pull" in off.output
