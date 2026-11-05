"""Run: python3 tests/test_blackbox.py  (no Hermes install, no pytest needed)."""

import importlib.util
import json
import os
import random
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ["BLACKBOX_DB"] = str(Path(tempfile.mkdtemp()) / "bb.db")
spec = importlib.util.spec_from_file_location("blackbox", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
bb = importlib.util.module_from_spec(spec)
sys.modules["blackbox"] = bb
spec.loader.exec_module(bb)
rules, store = bb.rules, bb.store

OK = json.dumps({"success": True, "data": "fine"})
ERR = json.dumps({"error": "429 rate limited"})


# ---------------------------------------------------------------- rules on fixtures

def ev(seq, kind="tool", name="web_search", status="ok", argkey=None):
    return {"seq": seq, "kind": kind, "name": name, "status": status, "argkey": argkey or f"k{seq}", "preview": ""}


def run(events, ok=True, is_cron=True, tokens=10_000):
    return {"run_id": "r", "ok": ok, "is_cron": is_cron, "events": events, "tokens": tokens,
            "tool_calls": sum(e["kind"] == "tool" for e in events)}


BASE = {"n": 7, "median_tools": 11, "median_tokens": 10_000, "median_duration_ms": 30_000}
HEALTHY = [ev(i, name=f"t{i % 4}") for i in range(11)]

PLANTED = {
    "swallowed_errors": run([ev(1, status="error"), ev(2, status="error")] + HEALTHY[2:]),
    "loop": run([ev(i, argkey="same") for i in range(6)] + HEALTHY[6:]),
    "cost_spike": run(HEALTHY, tokens=40_000),
    "effort_collapse": run(HEALTHY[:2]),
    "failed_child": run(HEALTHY + [ev(99, kind="subagent", name="leaf", status="failed")]),
    "empty_work": run([]),
}


def test_each_rule_fires_on_its_planted_fixture():
    for rule, fixture in PLANTED.items():
        assert rule in [f["rule"] for f in rules.evaluate(fixture, BASE)], rule


def test_clean_replays_stay_quiet():
    rnd = random.Random(7)
    alarms = 0
    for _ in range(20):
        n = rnd.randint(8, 15)
        events = [ev(i, name=f"t{i % 5}") for i in range(n)]
        alarms += bool(rules.evaluate(run(events, tokens=rnd.randint(6_000, 18_000)), BASE))
    assert alarms <= 1


def code_run(*events, answer="Fixed the bug, tests pass."):
    r = run(list(events), is_cron=False)
    r["answer"] = answer
    return r


def test_unverified_claim():
    edit = {**ev(1, name="patch"), "target": "src/auth.py"}
    test_ok = {**ev(2, name="terminal"), "target": "pytest -q"}
    test_bad = {**test_ok, "status": "error", "detail": "exit 1: 2 failed"}
    fired = lambda r: "unverified_claim" in [f["rule"] for f in rules.evaluate(r, None)]
    assert fired(code_run(edit))  # edited, never ran a check
    assert fired(code_run(edit, test_bad))  # last check failed
    assert not fired(code_run(edit, test_ok))
    assert not fired(code_run({**test_ok, "seq": 0}, edit, test_ok))
    assert not fired(code_run(edit, answer="Patched it, haven't run anything yet."))
    assert fired(code_run({**test_ok, "seq": 0}, edit))  # check ran before the edit only
    assert bb._tool_status(json.dumps({"output": "2 failed", "exit_code": 1, "error": None})) == "error"
    assert bb._error_detail(json.dumps({"output": "x\n2 failed", "exit_code": 1})) == "exit 1: 2 failed"


def test_baseline_rules_silent_until_three_runs():
    assert rules.evaluate(PLANTED["empty_work"], {**BASE, "n": 2}) == []


def test_interactive_gets_baseline_free_rules_only():
    assert rules.evaluate(run([], is_cron=False), BASE) == []
    assert rules.evaluate({**PLANTED["swallowed_errors"], "is_cron": False}, None)[0]["rule"] == "swallowed_errors"


def test_honest_answer_is_not_swallowed():
    honest = {**PLANTED["swallowed_errors"], "answer": "notes.txt could not be read because it does not exist."}
    assert "swallowed_errors" not in [f["rule"] for f in rules.evaluate(honest, BASE)]
    hidden = {**PLANTED["swallowed_errors"], "answer": "Here is your brief: markets are up."}
    assert "swallowed_errors" in [f["rule"] for f in rules.evaluate(hidden, BASE)]


def test_declared_failure_is_not_swallowed():
    assert rules.evaluate({**PLANTED["swallowed_errors"], "ok": False}, BASE) == []


def test_silenced_rule_is_skipped():
    assert rules.evaluate(PLANTED["loop"], BASE, silenced=frozenset({"loop"})) == []


# ---------------------------------------------------------------- hooks end to end

def cron_run(job, i, tools, result=OK, answer="Brief: all good."):
    sid = f"cron_{job}_20261105_0{i}0000"
    bb.on_session_start(session_id=sid, platform="cron", model="m")
    for t in range(tools):
        bb.post_tool_call(tool_name="web_search", args={"q": f"news {t}"}, result=result, duration_ms=40,
                          session_id=sid, task_id=sid)
    bb.post_api_request(session_id=sid, model="m", usage={"total_tokens": 10_000}, api_duration=1.2)
    out = bb.transform_llm_output(response_text=answer, session_id=sid, platform="cron")
    bb.on_session_end(session_id=sid, completed=True, interrupted=False, platform="cron")
    store.flush()
    return sid, out


def test_green_but_wrong_cron_run_is_flagged_end_to_end():
    for i in range(7):
        assert cron_run("abc123def456", i, 11)[1] is None
    sid, out = cron_run("abc123def456", 8, 2, result=ERR, answer="Brief: AI news today.\nMore text.")
    assert out.splitlines()[0] == "Brief: AI news today."  # first line untouched
    assert "said done, but a step failed" in out and "Did far less work than usual" in out
    assert "web_search failed twice" not in out and "web_search failed 2 times (429 rate limited)" in out
    conn = store.connect()
    r = store.find_runs(conn, sid)[0]
    assert (r["job_id"], r["tool_calls"], r["tool_errors"], r["outcome"]) == ("abc123def456", 2, 2, "ok")
    assert {f["rule"] for f in store.findings(conn, run_id=r["run_id"])} == {"swallowed_errors", "effort_collapse"}
    assert len(store.events(conn, r["run_id"])) == 4  # session start, 2 tools, 1 model call
    why = bb.slash(f"why {r['run_id']}")
    assert why.startswith("⚠ Said done, but a step failed") and "✗ web_search news 0 · 40 ms — 429 rate limited" in why
    assert bb.slash("").startswith("Blackbox · last 24h")
    b = rules.baseline_from(store.healthy_runs(conn, "abc123def456"))
    assert b["n"] == 7 and b["median_tools"] == 11  # the bad run is not in the baseline


def test_cron_failure_marker_is_respected():
    for i in range(3):
        cron_run("fff000fff000", i, 3)
    _, out = cron_run("fff000fff000", 5, 3, result=ERR, answer="[CRON_FAILURE] search down")
    assert out is None


def test_silent_job_breaks_silence_when_flagged():
    _, out = cron_run("eee000eee000", 1, 2, result=ERR, answer="[SILENT]")
    assert out.startswith("**⚠ Blackbox") and "[SILENT]" not in out


def test_ack_silences_rule_for_job():
    _, out = cron_run("ddd000ddd000", 1, 1, result=ERR)
    conn = store.connect()
    f = store.findings(conn)[0]
    store.ack(conn, f["finding_id"])
    assert cron_run("ddd000ddd000", 2, 1, result=ERR)[1] is None


# ---------------------------------------------------------------- reliability

def test_hooks_fail_open_on_garbage():
    for fn in bb.RECORDING_HOOKS + (bb.pre_tool_call,):
        fn(session_id=object(), tool_name=None, args=object(), result=b"\xff", usage="nope", response_text=None)
        fn()


def test_tool_hook_latency_p95_under_5ms():
    big = {"content": "x" * 50_000}
    times = []
    for i in range(1000):
        t = time.perf_counter()
        bb.post_tool_call(tool_name="write_file", args=big, result=OK, duration_ms=1, session_id="lat")
        times.append((time.perf_counter() - t) * 1000)
    times.sort()
    assert times[949] < 5, times[949]
    bb.on_session_end(session_id="lat")


def test_tripwire_asks_human_on_fifth_repeat():
    out = [bb.pre_tool_call(tool_name="terminal", args={"command": "ls"}, session_id="tw") for _ in range(5)]
    assert out[:4] == [None] * 4 and out[4]["action"] == "approve"
    bb.on_session_end(session_id="tw")
    assert bb.pre_tool_call(tool_name="terminal", args={"command": "ls"}, session_id="tw") is None


if __name__ == "__main__":
    tests = [(n, f) for n, f in list(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        fn()
        print("ok  ", name)
    print(f"{len(tests)} passed")
