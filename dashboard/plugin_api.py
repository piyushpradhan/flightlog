"""Read-only API for the dashboard tab and the desktop page, mounted at /api/plugins/flightlog/."""

import importlib.util
import json
from pathlib import Path

from fastapi import APIRouter, HTTPException

_ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(f"flightlog_{name}", _ROOT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


store, rules = _load("store"), _load("rules")
router = APIRouter()


def _q(fn, *args, **kw):
    conn = store.connect()
    try:
        return fn(conn, *args, **kw)
    finally:
        conn.close()


def _finding(f):
    return {**f, "headline": rules.HEADLINES.get(f["rule"], f["rule"]), "impact": rules.IMPACT.get(f["rule"], ""),
            "evidence": json.loads(f["evidence"] or "[]"),
            "report": f["report"] if f["report"] != f["summary"] + " " + rules.IMPACT.get(f["rule"], "") else None}


@router.get("/runs")
def runs(limit: int = 50, job: str | None = None):
    rows = _q(store.recent_runs, min(limit, 500), job)
    for r in rows:
        r["headline"] = rules.HEADLINES.get(r["top_rule"]) if r["top_rule"] else None
    return {"runs": rows, "summary": _q(store.recent_summary)}


@router.get("/runs/{run_id}")
def run(run_id: str):
    found = _q(store.find_runs, run_id)
    if not found:
        raise HTTPException(404, "no such run")
    r = found[-1]
    order = list(rules.HEADLINES)
    fs = sorted(_q(store.findings, run_id=r["run_id"], include_acked=True), key=lambda f: order.index(f["rule"]))
    return {"run": r, "events": _q(store.events, r["run_id"]), "findings": [_finding(f) for f in fs]}


@router.get("/findings")
def findings():
    return {"findings": [_finding(f) for f in _q(store.findings)]}


@router.get("/health")
def health():
    """Fleet card: last 24h of runs."""
    return _q(store.recent_summary)
