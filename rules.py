"""Drift rules: pure functions over one run's events plus its job baseline.

A run is a dict: {"run_id", "ok", "is_cron", "tool_calls", "tokens", "answer", "events": [...]}.
An event is a dict: {"seq", "kind", "name", "status", "argkey", "target", "detail"}.
A baseline is a dict: {"n", "median_tools", "median_tokens", "median_duration_ms"} or None.
Each rule returns a finding dict {"rule", "summary", "evidence": [seq, ...]} or None.
"""

from __future__ import annotations

import re
from collections import Counter
from statistics import median
from typing import Any, Dict, List, Optional

DEFAULTS = {
    "swallowed_errors": 1,  # tool errors in an "ok" run
    "loop_repeats": 5,  # same tool + near-identical args in one run
    "cost_spike_x": 3.0,  # tokens over N x job median
    "effort_collapse_pct": 25,  # tool calls under N% of job median
    "min_baseline_runs": 3,  # baseline rules stay silent below this
}

# What the user reads: a verdict, then why it matters. Plain words, no rule jargon.
HEADLINES = {
    "unverified_claim": "Claimed it works, but never proved it",
    "swallowed_errors": "Said done, but a step failed",
    "loop": "Stuck in a loop",
    "cost_spike": "Cost far above normal",
    "effort_collapse": "Did far less work than usual",
    "failed_child": "A helper failed, the parent said done",
    "empty_work": "Answered without doing any work",
}
IMPACT = {
    "unverified_claim": "The change may be broken; check it before you merge or ship it.",
    "swallowed_errors": "The answer may be built on data the agent never got.",
    "loop": "It repeated itself instead of making progress, burning time and tokens.",
    "cost_spike": "Something made this run much more expensive than this job usually is.",
    "effort_collapse": "It may have skipped steps or answered from memory.",
    "failed_child": "Part of the task may never have been done.",
    "empty_work": "It probably answered from memory instead of checking.",
}
LABELS = HEADLINES  # back-compat name used by views

# ponytail: keyword check for "the answer admits the failure"; upgrade to a model judgement if it misfires.
_ADMITS = re.compile(r"\b(errors?|fail(ed|s|ure)?|couldn'?t|could not|cannot|can'?t|unable|not found|"
                     r"does ?n[o']t exist|missing|no such|denied|timed? ?out|rate.?limit\w*|unavailable)\b", re.I)


# ponytail: regexes for "a check command" and "the answer claims success"; extend the lists as misses show up.
_CHECK = re.compile(r"\b(pytest|unittest|tox|nox|jest|vitest|mocha|playwright|cargo (test|build|check|clippy)|"
                    r"go (test|build|vet)|(npm|pnpm|yarn|bun)( run)? (test|build|lint|typecheck|check)|"
                    r"make( \w+)?|tsc|mypy|pyright|ruff|eslint|gradle|mvn|swift (test|build)|xcodebuild|rspec|"
                    r"phpunit|dotnet (test|build))\b", re.I)
_CLAIMS = re.compile(r"\b(tests? (now )?pass(es|ed|ing)?|all (tests )?(pass|green)|(build|it) (now )?"
                     r"(succeeds|passes|works)|works now|verified|fixed|everything (is )?green)\b", re.I)
_EDITS = ("write_file", "patch", "edit_file", "str_replace")


def _tool_events(run):
    return [e for e in run["events"] if e["kind"] == "tool"]


def _times(n):
    return "once" if n == 1 else f"{n} times"


def swallowed_errors(run, baseline, cfg):
    if not run["ok"]:
        return None
    errs = [e for e in _tool_events(run) if e["status"] == "error"]
    if len(errs) < cfg["swallowed_errors"] or _ADMITS.search(run.get("answer") or ""):
        return None  # an answer that says what failed is honest, not swallowed
    name, n = Counter(e["name"] for e in errs).most_common(1)[0]
    detail = next((e.get("detail") for e in errs if e["name"] == name and e.get("detail")), "")
    what = f"{name} failed {_times(n)}" + (f" ({detail[:80]})" if detail else "")
    others = len(errs) - n
    if others:
        what += f", plus {others} other error{'s' if others > 1 else ''}"
    return {"rule": "swallowed_errors", "evidence": [e["seq"] for e in errs],
            "summary": f"{what}, and the answer never mentions it."}


def loop(run, baseline, cfg):
    tools = _tool_events(run)
    counts = Counter((e["name"], e["argkey"]) for e in tools)
    if not counts:
        return None
    (name, key), n = counts.most_common(1)[0]
    if n < cfg["loop_repeats"]:
        return None
    ev = [e for e in tools if (e["name"], e["argkey"]) == (name, key)]
    target = ev[0].get("target") or ""
    return {"rule": "loop", "evidence": [e["seq"] for e in ev],
            "summary": f"Called {name}" + (f" ({target[:60]})" if target else "")
                       + f" {n} times with the same arguments."}


def unverified_claim(run, baseline, cfg):
    tools = _tool_events(run)
    edits = [e for e in tools if e["name"] in _EDITS]
    claim = _CLAIMS.search(run.get("answer") or "")
    if not run["ok"] or not edits or not claim:
        return None
    checks = [e for e in tools if e["name"] in ("terminal", "execute_code") and _CHECK.search(e.get("target") or "")]
    after = [e for e in checks if e["seq"] > edits[-1]["seq"]]
    said = f'The answer says "{claim.group(0)}"'
    if not after:
        return {"rule": "unverified_claim", "evidence": [edits[-1]["seq"]],
                "summary": f"{said}, but no test or build ran after the last edit ({edits[-1].get('target') or edits[-1]['name']})."}
    last = after[-1]
    if last["status"] != "error":
        return None
    return {"rule": "unverified_claim", "evidence": [last["seq"]],
            "summary": f"{said}, but the last check failed: {last.get('target')}"
                       + (f" ({last['detail'][:80]})" if last.get("detail") else "") + "."}


def failed_child(run, baseline, cfg):
    if not run["ok"]:
        return None
    bad = [e for e in run["events"] if e["kind"] == "subagent" and e["status"] in ("failed", "error")]
    if not bad:
        return None
    return {"rule": "failed_child", "evidence": [e["seq"] for e in bad],
            "summary": f"{len(bad)} subagent{'s' if len(bad) > 1 else ''} stopped as failed, "
                       f"but the run still reported success."}


def _has_baseline(baseline, cfg):
    return bool(baseline) and baseline["n"] >= cfg["min_baseline_runs"]


def cost_spike(run, baseline, cfg):
    # ponytail: tokens, not dollars — cost is missing for some providers; dollars shown when known.
    if not _has_baseline(baseline, cfg) or not baseline["median_tokens"]:
        return None
    ratio = run["tokens"] / baseline["median_tokens"]
    if ratio < cfg["cost_spike_x"]:
        return None
    return {"rule": "cost_spike", "evidence": [e["seq"] for e in run["events"] if e["kind"] == "model"],
            "summary": f"Used {run['tokens']:,} tokens, {ratio:.1f}x this job's usual "
                       f"{int(baseline['median_tokens']):,}."}


def effort_collapse(run, baseline, cfg):
    if not _has_baseline(baseline, cfg) or not baseline["median_tools"] or run["tool_calls"] == 0:
        return None  # zero tool calls is empty_work's job
    if run["tool_calls"] * 100 >= baseline["median_tools"] * cfg["effort_collapse_pct"]:
        return None
    return {"rule": "effort_collapse", "evidence": [e["seq"] for e in _tool_events(run)],
            "summary": f"Made {run['tool_calls']} tool call{'s' if run['tool_calls'] > 1 else ''}; "
                       f"this job usually makes {baseline['median_tools']:g}."}


def empty_work(run, baseline, cfg):
    if not _has_baseline(baseline, cfg) or not baseline["median_tools"] or run["tool_calls"]:
        return None
    return {"rule": "empty_work", "evidence": [],
            "summary": f"Made no tool calls; this job usually makes {baseline['median_tools']:g}."}


BASELINE_FREE = (unverified_claim, swallowed_errors, loop, failed_child)
WITH_BASELINE = (cost_spike, effort_collapse, empty_work)


def evaluate(run: Dict[str, Any], baseline: Optional[Dict[str, Any]], cfg: Optional[Dict[str, Any]] = None,
             silenced: frozenset = frozenset()) -> List[Dict[str, Any]]:
    cfg = {**DEFAULTS, **(cfg or {})}
    rules = BASELINE_FREE + (WITH_BASELINE if run["is_cron"] else ())
    return [f for f in (r(run, baseline, cfg) for r in rules) if f and f["rule"] not in silenced]


def baseline_from(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """rows: recent healthy runs of one job, each with tool_calls, tokens, duration_ms."""
    if not rows:
        return None
    return {"n": len(rows),
            "median_tools": median(r["tool_calls"] for r in rows),
            "median_tokens": median(r["tokens"] for r in rows),
            "median_duration_ms": median(r["duration_ms"] for r in rows)}


def template_report(findings: List[Dict[str, Any]]) -> str:
    """Fallback explanation when the model call fails. Never empty."""
    return " ".join(f"{f['summary']} {IMPACT[f['rule']]}" for f in findings)


def footer(findings: List[Dict[str, Any]], run_ref: str) -> str:
    """Markdown appended to the agent's answer. First line of the answer is never touched."""
    lead = findings[0]
    lines = ["", "---", f"**⚠ Flightlog: {HEADLINES[lead['rule']].lower()}.** {lead['summary']} {IMPACT[lead['rule']]}"]
    lines += [f"- **{HEADLINES[f['rule']]}.** {f['summary']}" for f in findings[1:]]
    lines += ["", f"Details: `/flightlog why {run_ref}`"]
    return "\n".join(lines)
