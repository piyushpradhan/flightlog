"""Flightlog — flight recorder for Hermes Agent.

Hooks record every tool call, model call, subagent and approval into local SQLite (via a background
writer), judge each finished run against its cron job's baseline, and explain what went wrong.
Every callback is fail-open: an exception in Flightlog never reaches the agent.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import re
import threading
import time
import uuid
from collections import Counter
from typing import Any, Dict, List, Optional

from . import rules, store

logger = logging.getLogger(__name__)

_ctx: Any = None
_cfg: Dict[str, Any] = {"rules": {}, "tripwire": False, "break_silence": True}
_runs: Dict[str, "Run"] = {}
_lock = threading.Lock()
_loop_counts: Counter = Counter()  # tripwire: (session, tool, argkey) -> calls
_read_conn = None
_read_lock = threading.Lock()

MAX_EVENTS_PER_RUN = 5000
_CRON_SESSION = re.compile(r"^cron_([0-9a-f]+)_\d{8}_\d{6}")


def _safe(fn):
    """Fail open: log and swallow everything. Hooks must never break the agent."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            logger.debug("flightlog: %s failed", fn.__name__, exc_info=True)
            return None
    return wrapper


# ---------------------------------------------------------------- in-memory run state

class Run:
    def __init__(self, session_id: str, platform: str = ""):
        self.run_id = uuid.uuid4().hex[:8]
        self.session_id = session_id
        self.platform = platform
        m = _CRON_SESSION.match(session_id or "")
        self.job_id = m.group(1) if m else None
        self.model: Optional[str] = None
        self.started = time.time()
        self.seq = 0
        self.events: List[Dict[str, Any]] = []  # compact copies for the rules
        self.tool_calls = self.tool_errors = self.tokens = 0
        self.ok = True
        self.answer = ""
        self.findings: Optional[List[Dict[str, Any]]] = None

    @property
    def is_cron(self) -> bool:
        return self.job_id is not None or self.platform == "cron"

    def record(self, kind: str, name: str = "", status: str = "ok", duration_ms: Any = None,
               tokens: Any = None, preview: str = "", argkey: str = "", target: str = "", detail: str = "",
               **extra: Any) -> None:
        self.seq += 1
        ev = {"seq": self.seq, "kind": kind, "name": name, "status": status, "argkey": argkey,
              "preview": preview, "duration_ms": duration_ms, "target": target, "detail": detail}
        if len(self.events) < MAX_EVENTS_PER_RUN:
            self.events.append(ev)
        store.put("event", {**ev, **extra, "run_id": self.run_id, "ts": time.time(), "tokens": tokens})

    def as_rule_input(self) -> Dict[str, Any]:
        return {"run_id": self.run_id, "ok": self.ok, "is_cron": self.is_cron, "tool_calls": self.tool_calls,
                "tokens": self.tokens, "answer": self.answer, "events": self.events}


def _key(session_id: Any = "", task_id: Any = "", **_: Any) -> str:
    return str(session_id or task_id or "default")


def _run(session_id: Any = "", task_id: Any = "", platform: str = "", create: bool = True, **_: Any):
    key = _key(session_id, task_id)
    with _lock:
        run = _runs.get(key)
        if run is None and not session_id and not task_id and _runs:
            run = max(_runs.values(), key=lambda r: r.started)  # hook with no ids: newest open run
        if run is None and create:
            run = _runs[key] = Run(key, platform)
        if run is not None and platform and not run.platform:
            run.platform = platform
        return run


def _preview(value: Any, limit: int = 200) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    return text[:limit]


def _argkey(args: Any) -> str:
    """Near-identical args hash: key order, case and whitespace ignored."""
    text = json.dumps(args, sort_keys=True, default=str) if not isinstance(args, str) else args
    return hashlib.md5(re.sub(r"\s+", " ", text.lower()).encode()).hexdigest()[:12]


_TARGET_KEYS = ("path", "file_path", "command", "query", "q", "url", "pattern", "name", "code")


def _target(args: Any) -> str:
    """The one argument a human cares about: the path, command, query or URL."""
    if isinstance(args, dict):
        val = next((args[k] for k in _TARGET_KEYS if isinstance(args.get(k), str) and args[k]), None)
        val = val or next((v for v in args.values() if isinstance(v, str) and v), "")
        return " ".join(str(val).split())[:80]
    return " ".join(str(args or "").split())[:80]


def _error_detail(result: Any, message: Any = None) -> str:
    text = message
    if not text:
        try:
            parsed = json.loads(result) if isinstance(result, str) else result
            text = parsed.get("error") if isinstance(parsed, dict) else None
            if not text and isinstance(parsed, dict) and parsed.get("exit_code") not in (None, 0):
                tail = (str(parsed.get("output") or "").strip().splitlines() or [""])[-1]
                text = f"exit {parsed['exit_code']}" + (f": {tail}" if tail else "")
        except (ValueError, TypeError):
            text = None
    text = str(text or result or "").strip()
    return (text.splitlines() or [""])[0][:120]


def _tool_status(result: Any) -> str:
    try:
        parsed = json.loads(result) if isinstance(result, str) else result
    except (ValueError, TypeError):
        return "ok"
    if isinstance(parsed, dict) and (parsed.get("error") or parsed.get("success") is False
                                     or str(parsed.get("status", "")).lower() in ("error", "failed")
                                     or parsed.get("exit_code") not in (None, 0)):
        return "error"
    return "ok"


# ---------------------------------------------------------------- recording hooks

@_safe
def on_session_start(session_id: str = "", platform: str = "", model: str = "", **kw: Any) -> None:
    run = _run(session_id, platform=platform)
    run.model = run.model or model
    run.record("session", "start", preview=platform)


@_safe
def post_tool_call(tool_name: str = "", args: Any = None, result: Any = None, duration_ms: Any = None,
                   **kw: Any) -> None:
    run = _run(**kw)
    status = "ok" if (kw.get("status") or _tool_status(result)) == "ok" else "error"  # Hermes passes status; older builds don't
    run.tool_calls += 1
    run.tool_errors += status == "error"
    run.record("tool", tool_name, status, duration_ms=duration_ms, argkey=_argkey(args), target=_target(args),
               detail=_error_detail(result, kw.get("error_message")) if status == "error" else "",
               preview=f"{_preview(args, 100)} -> {_preview(kw.get('error_message') or result, 100)}")


@_safe
def post_api_request(model: str = "", provider: str = "", base_url: str = "", api_duration: float = 0.0,
                     finish_reason: str = "", usage: Any = None, **kw: Any) -> None:
    run = _run(**kw)
    run.model = model or run.model
    tokens = int((usage or {}).get("total_tokens") or 0)
    run.tokens += tokens
    run.record("model", model, finish_reason or "ok", duration_ms=int((api_duration or 0) * 1000), tokens=tokens,
               usage=usage, model=model, provider=provider, base_url=base_url)  # cost priced in the writer


@_safe
def api_request_error(model: str = "", api_duration: float = 0.0, status_code: Any = None, reason: Any = None,
                      error: Any = None, **kw: Any) -> None:
    _run(**kw).record("model", model or "api", "error", duration_ms=int((api_duration or 0) * 1000),
                      detail=_preview(f"{status_code or ''} {reason or error or ''}".strip(), 120))


@_safe
def subagent_start(parent_session_id: Any = None, child_role: str = "", child_goal: Any = None, **kw: Any) -> None:
    _run(parent_session_id or "").record("subagent", child_role or "child", "started", preview=_preview(child_goal))


@_safe
def subagent_stop(parent_session_id: Any = None, child_role: Any = None, child_status: Any = None,
                  child_summary: Any = None, duration_ms: Any = None, **kw: Any) -> None:
    _run(parent_session_id or "").record("subagent", child_role or "child", str(child_status or "unknown"),
                                         duration_ms=duration_ms, preview=_preview(child_summary))


@_safe
def pre_approval_request(command: str = "", description: str = "", session_key: str = "", **kw: Any) -> None:
    run = _run(session_key, create=bool(session_key))
    if run:
        run.record("approval", "request", "pending", preview=_preview(f"{description}: {command}"))


@_safe
def post_approval_response(choice: str = "", command: str = "", session_key: str = "", **kw: Any) -> None:
    run = _run(session_key, create=bool(session_key))
    if run:
        run.record("approval", choice, "denied" if choice in ("deny", "timeout", "smart_deny") else "ok",
                   preview=_preview(command))


# ---------------------------------------------------------------- judgement

def _conn():
    global _read_conn
    if _read_conn is None:
        _read_conn = store.connect()
    return _read_conn


def _judge(run: Run) -> List[Dict[str, Any]]:
    if run.findings is None:
        baseline, silenced = None, frozenset()
        if run.job_id:
            with _read_lock:
                baseline = rules.baseline_from(store.healthy_runs(_conn(), run.job_id))
                silenced = store.silenced_rules(_conn(), run.job_id)
        run.findings = rules.evaluate(run.as_rule_input(), baseline, _cfg["rules"], silenced)
    return run.findings


@_safe
def transform_llm_output(response_text: str = "", session_id: str = "", platform: str = "", **kw: Any):
    run = _run(session_id, platform=platform, create=False)
    if run is None:
        return None
    run.ok = not response_text.lstrip().startswith("[CRON_FAILURE]")
    run.answer = response_text
    found = _judge(run)
    if not found:
        return None
    note = rules.footer(found, run.run_id)
    post = getattr(_ctx, "post_message", None)
    if not run.is_cron and callable(post):
        post(note.strip())  # interactive: live notice, answer untouched
        return None
    first_line = response_text.strip().splitlines()[0].strip().upper() if response_text.strip() else ""
    if first_line in ("[SILENT]", "SILENT", "NO_REPLY", "NO REPLY"):
        # A silent job that swallowed errors is exactly what Flightlog exists to surface.
        return note.lstrip("\n-") if _cfg["break_silence"] else None
    return response_text + "\n" + note  # first line untouched, so [CRON_FAILURE] keeps working


@_safe
def on_session_end(session_id: str = "", completed: bool = True, interrupted: bool = False, platform: str = "",
                   model: str = "", **kw: Any) -> None:
    key = _key(session_id, kw.get("task_id"))
    with _lock:
        run = _runs.pop(key, None)
        for k in [k for k in _loop_counts if k[0] == key]:
            del _loop_counts[k]
    if run is None:
        return
    run.platform = run.platform or platform
    run.model = run.model or model
    outcome = "interrupted" if interrupted else ("ok" if completed and run.ok else "failed")
    run.ok = outcome == "ok"
    found = _judge(run)
    store.put("run", {"run_id": run.run_id, "session_id": run.session_id, "platform": run.platform,
                      "job_id": run.job_id, "model": run.model, "started": run.started, "ended": time.time(),
                      "outcome": outcome, "tool_calls": run.tool_calls, "tool_errors": run.tool_errors,
                      "tokens": run.tokens})
    template = rules.template_report(found) if found else None
    for f in found:
        store.put("finding", {**f, "finding_id": uuid.uuid4().hex[:8], "run_id": run.run_id, "job_id": run.job_id,
                              "report": template})
    if found:
        threading.Thread(target=_explain, args=(run, found), name="flightlog-report", daemon=True).start()


def _explain(run: Run, found: List[Dict[str, Any]]) -> None:
    """Replace the template report with a model-written one. Failure keeps the template."""
    try:
        seqs = {s for f in found for s in f["evidence"]}
        evidence = [{k: e[k] for k in ("seq", "kind", "name", "status", "duration_ms", "target", "detail")}
                    | {"preview": store._redact(e["preview"])} for e in run.events if e["seq"] in seqs][:40]
        prompt = ("An AI agent finished a run and reported success. Automated checks flagged it:\n"
                  + "\n".join(f"- {rules.HEADLINES[f['rule']]}: {f['summary']}" for f in found)
                  + "\n\nEvidence (tool and model events):\n" + json.dumps(evidence, default=str)
                  + "\n\nWrite two short sentences for the person who relies on this agent, not an engineer: "
                    "can they trust the answer, and what exactly should they check or fix. No preamble, no lists.")
        result = _ctx.llm.complete([{"role": "user", "content": prompt}], max_tokens=250, timeout=60,
                                   purpose="flightlog incident report")
        text = (getattr(result, "text", "") or "").strip()
        if text:
            store.put("report", {"run_id": run.run_id, "report": text})
    except Exception:
        logger.debug("flightlog: explanation failed, template kept", exc_info=True)


# ---------------------------------------------------------------- tripwire (opt-in)

@_safe
def pre_tool_call(tool_name: str = "", args: Any = None, **kw: Any):
    """In-memory counter only — no I/O, so it can never hit the fail-closed hook timeout."""
    key = (_key(**kw), tool_name, _argkey(args))
    with _lock:
        _loop_counts[key] += 1
        n = _loop_counts[key]
    limit = int(_cfg["rules"].get("loop_repeats", rules.DEFAULTS["loop_repeats"]))
    if n < limit:
        return None
    return {"action": "approve", "rule_key": f"flightlog:loop:{tool_name}",
            "message": f"Flightlog: {tool_name} has been called {n} times with the same arguments this turn. "
                       f"Let it continue?"}


# ---------------------------------------------------------------- views
# Slash output lands in the desktop as a plain-text, proportional-font system line, so: verdict first,
# one idea per line, no column alignment. The rich view is the desktop Flightlog page.

RED, RESET = "\033[31m", "\033[0m"
BAD = ("error", "failed", "denied", "timeout")
MAX_STEPS = 25


def _ago(ts: Optional[float]) -> str:
    if not ts:
        return "-"
    s = int(time.time() - ts)
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{s // n}{unit} ago"
    return "just now" if s < 10 else f"{s}s ago"


def _dur(ms: Any) -> str:
    if ms is None:
        return ""
    return f"{ms / 1000:.1f} s" if ms >= 1000 else f"{ms} ms"


def _n(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _tok(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _who(r: Dict[str, Any]) -> str:
    if r.get("job_name"):
        return f'cron job "{r["job_name"]}"'
    return f"cron job {r['job_id']}" if r.get("job_id") else "chat"


def _verdict(rule: Optional[str], outcome: str = "ok") -> str:
    if rule:
        return f"⚠ {rules.HEADLINES.get(rule, rule)}"
    return "✓ Looks fine" if outcome == "ok" else f"✗ Ended {outcome}"


def _step(e: Dict[str, Any]) -> Optional[str]:
    bad = e["status"] in BAD
    mark, dur = ("✗" if bad else "✓"), _dur(e["duration_ms"])
    if e["kind"] == "tool":
        text = f"{e['name']} {e.get('target') or ''}".strip()
    elif e["kind"] == "model":
        text = "model call failed" if bad else ("model call, answered" if e["status"] == "stop" else "model call")
    elif e["kind"] == "subagent":
        mark, text = ("✗" if bad else "↳"), f"helper {e['name']} {e['status']}"
    elif e["kind"] == "approval":
        mark, text = ("✗" if bad else "?"), ("asked for approval" if e["name"] == "request" else f"approval: {e['name']}")
    else:
        return None
    detail = e.get("detail") if bad else ""
    return f"{mark} {text}" + (f" · {dur}" if dur else "") + (f" — {detail}" if detail else "")


def _steps(events: List[Dict[str, Any]], keep: set, limit: int = MAX_STEPS) -> List[tuple]:
    """(event, text) pairs; long runs keep flagged steps plus the first and last few."""
    steps = [(e, t) for e in events if (t := _step(e))]
    if len(steps) <= limit:
        return steps
    show = {i for i, (e, _) in enumerate(steps) if e["seq"] in keep or e["status"] in BAD}
    show |= set(range(5)) | set(range(len(steps) - 5, len(steps)))
    out, last = [], -1
    for i in sorted(show):
        if i != last + 1:
            out.append((None, f"… {i - last - 1} more steps"))
        out.append(steps[i])
        last = i
    return out


def report(conn, run: Dict[str, Any], color: bool = False) -> str:
    order = list(rules.HEADLINES)
    found = sorted(store.findings(conn, run_id=run["run_id"], include_acked=True), key=lambda f: order.index(f["rule"]))
    events = store.events(conn, run["run_id"])
    secs = run["ended"] - run["started"]
    models = sum(e["kind"] == "model" for e in events)
    if found:
        lead = found[0]
        lines = [_verdict(lead["rule"]), lead["summary"], f"Why it matters: {rules.IMPACT[lead['rule']]}"]
        lines += [f"Also: {rules.HEADLINES[f['rule']].lower()}. {f['summary']}" for f in found[1:]]
        if lead["report"] and lead["report"] != rules.template_report(found):
            lines += ["", lead["report"]]
    else:
        lines = [_verdict(None, run["outcome"]), "No problems found in this run."]
    lines += ["", f"What happened · {secs:.1f} s · {_n(models, 'model call')} · {_n(run['tool_calls'], 'tool')} · "
                  f"{_tok(run['tokens'])} tokens" + (f" · ${run['cost_usd']:.3f}" if run.get("cost_usd") else "")]
    keep = {s for f in found for s in json.loads(f["evidence"] or "[]")}
    for e, text in _steps(events, keep):
        line = "  " + (f"+{e['ts'] - run['started']:.1f}s  " if color and e else "") + text
        lines.append(f"{RED}{line}{RESET}" if color and e and e["status"] in BAD else line)
    if found:
        lines += ["", f"False alarm? hermes flightlog ack {found[0]['finding_id']}"]
    return "\n".join(lines)


def overview(conn, limit: int = 5) -> str:
    recent = store.recent_runs(conn, limit)
    if not recent:
        return ("Flightlog has no runs yet. A run is recorded when an agent turn ends.\n"
                "Just enabled it? Restart Hermes fully (desktop: Cmd+Q) so the agent backend loads the plugin.")
    s = store.recent_summary(conn)
    head = f"Flightlog · last 24h: {s['runs']} run{'s' if s['runs'] != 1 else ''}, "
    head += f"{s['flagged']} need{'s' if s['flagged'] == 1 else ''} a look" if s["flagged"] else "all look fine"
    lines = [head, ""]
    for r in recent:
        lines += [_verdict(r.get("top_rule"), r["outcome"]), f"   {_who(r)} · {_ago(r['ended'])} · run {r['run_id']}"]
    flagged = next((r for r in recent if r.get("top_rule")), None)
    lines += ["", f"Open one: /flightlog why {(flagged or recent[0])['run_id']}"]
    if store.dropped:
        lines.append(f"{store.dropped} events were dropped (queue full).")
    return "\n".join(lines)


def slash(raw_args: str = "") -> str:
    argv = (raw_args or "").split()
    conn = store.connect()
    try:
        if argv[:1] != ["why"]:
            return overview(conn)
        if len(argv) < 2:
            return "Which run? /flightlog why <run id>. Run /flightlog to list recent runs."
        runs = store.find_runs(conn, argv[1])
        return report(conn, runs[-1]) if runs else f"No run {argv[1]}. Run /flightlog to list recent runs."
    finally:
        conn.close()


def _cli_setup(parser) -> None:
    sub = parser.add_subparsers(dest="flightlog_action")
    sub.add_parser("runs", help="Recent runs with health marks").add_argument("--job")
    sub.add_parser("show", help="Timeline for a run or session").add_argument("ref")
    sub.add_parser("jobs", help="Each cron job's baseline")
    sub.add_parser("ack", help="Acknowledge a finding and silence its rule for that job").add_argument("finding")
    sub.add_parser("purge", help="Delete all recorded data").add_argument("--yes", action="store_true")


def _cli(args) -> None:
    action = getattr(args, "flightlog_action", None) or "runs"
    conn = store.connect()
    try:
        if action == "runs":
            for r in store.recent_runs(conn, 20, getattr(args, "job", None)):
                print(f"{_verdict(r.get('top_rule'), r['outcome'])}  ·  {_who(r)} · {_ago(r['ended'])} · "
                      f"{r['tool_calls']} tools · {_tok(r['tokens'])} tok · run {r['run_id']}")
        elif action == "show":
            runs = store.find_runs(conn, args.ref)
            print("\n\n".join(report(conn, r, color=True) for r in runs) or f"No run matching {args.ref}.")
        elif action == "jobs":
            for j in store.jobs(conn):
                b = rules.baseline_from(store.healthy_runs(conn, j["job_id"]))
                base = (f"median {b['median_tools']:g} tools, {int(b['median_tokens']):,} tok, "
                        f"{b['median_duration_ms'] / 1000:.0f}s over {b['n']} healthy runs") if b else "no healthy runs yet"
                print(f"{(j['job_name'] or j['job_id'])[:28]:<28} {j['runs']:>4} runs  {base}")
        elif action == "ack":
            f = store.ack(conn, args.finding)
            print(f"Acked {f['finding_id']} ({f['rule']}); silenced for this job." if f else "No such finding.")
        elif action == "purge":
            if not args.yes and input("Delete all Flightlog data? [y/N] ").strip().lower() != "y":
                return
            store.purge(conn)
            print("Purged.")
    finally:
        conn.close()


# ---------------------------------------------------------------- registration

RECORDING_HOOKS = (on_session_start, post_tool_call, post_api_request, api_request_error, subagent_start,
                   subagent_stop, pre_approval_request, post_approval_response, transform_llm_output, on_session_end)


def register(ctx) -> None:
    global _ctx
    _ctx = ctx
    try:
        _cfg["rules"] = dict(ctx.get_config("rules", {}) or {})
        _cfg["tripwire"] = bool(ctx.get_config("tripwire", False))
        _cfg["break_silence"] = bool(ctx.get_config("break_silence", True))
    except Exception:
        pass
    for fn in RECORDING_HOOKS:
        ctx.register_hook(fn.__name__, fn)
    if _cfg["tripwire"]:  # only registered when on: pre_tool_call fails closed on timeout
        ctx.register_hook("pre_tool_call", pre_tool_call)
    ctx.register_command("flightlog", handler=slash, description="Flight recorder: recent runs and findings",
                         args_hint="[why <run>]")
    ctx.register_cli_command("flightlog", help="Flightlog flight recorder", setup_fn=_cli_setup, handler_fn=_cli)
