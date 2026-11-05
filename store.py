"""SQLite store and the background writer. The agent thread only ever calls ``put()``."""

from __future__ import annotations

import atexit
import json
import re
import logging
import os
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

QUEUE_MAX = 10_000
RETENTION_DAYS = 30

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, session_id TEXT, platform TEXT, job_id TEXT, job_name TEXT, model TEXT,
  started REAL, ended REAL, outcome TEXT, tool_calls INTEGER DEFAULT 0, tool_errors INTEGER DEFAULT 0,
  tokens INTEGER DEFAULT 0, cost_usd REAL);
CREATE INDEX IF NOT EXISTS runs_job ON runs(job_id, ended);
CREATE INDEX IF NOT EXISTS runs_session ON runs(session_id);
CREATE TABLE IF NOT EXISTS events (
  run_id TEXT, seq INTEGER, ts REAL, kind TEXT, name TEXT, status TEXT, duration_ms INTEGER,
  tokens INTEGER, cost_usd REAL, preview TEXT, target TEXT, detail TEXT, PRIMARY KEY (run_id, seq));
CREATE TABLE IF NOT EXISTS findings (
  finding_id TEXT PRIMARY KEY, run_id TEXT, job_id TEXT, rule TEXT, summary TEXT, evidence TEXT,
  report TEXT, acked INTEGER DEFAULT 0, created REAL);
CREATE INDEX IF NOT EXISTS findings_run ON findings(run_id);
CREATE TABLE IF NOT EXISTS silenced (job_id TEXT, rule TEXT, PRIMARY KEY (job_id, rule));
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def db_path() -> Path:
    override = os.environ.get("FLIGHTLOG_DB")
    if override:
        return Path(override)
    try:
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home())
    except Exception:
        home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return home / "flightlog" / "flightlog.db"


def connect(path: Optional[Path] = None) -> sqlite3.Connection:
    path = path or db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(20):  # first-open WAL switch and schema race other processes; retry ~2s
        conn = sqlite3.connect(str(path), timeout=5, check_same_thread=False)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(SCHEMA)
            for col in ("target", "detail"):  # migrate databases made by 0.1.0
                try:
                    conn.execute(f"ALTER TABLE events ADD COLUMN {col} TEXT")
                except sqlite3.OperationalError:
                    pass
            return conn
        except sqlite3.OperationalError:
            conn.close()
            if attempt == 19:
                raise
            time.sleep(0.1)
    raise AssertionError("unreachable")


# ---------------------------------------------------------------- writer

_q: "queue.Queue[tuple]" = queue.Queue(maxsize=QUEUE_MAX)
_thread: Optional[threading.Thread] = None
_start_lock = threading.Lock()
dropped = 0
paused = False  # set when the DB is unusable; recording stops, agent carries on


def put(op: str, data: Dict[str, Any]) -> None:
    """Enqueue one write. Never blocks, never raises."""
    global dropped
    if paused:
        return
    _ensure_writer()
    try:
        _q.put_nowait((op, data))
    except queue.Full:
        try:  # drop the oldest, keep the newest
            _q.get_nowait()
            _q.put_nowait((op, data))
        except Exception:
            pass
        dropped += 1


def _ensure_writer() -> None:
    global _thread
    if _thread is not None:
        return
    with _start_lock:
        if _thread is None:
            _thread = threading.Thread(target=_writer_loop, name="flightlog-writer", daemon=True)
            _thread.start()
            atexit.register(flush)


def flush(timeout: float = 3.0) -> None:
    """Wait for the queue to drain (used at exit so short cron processes keep their tail)."""
    end = time.monotonic() + timeout
    while _q.unfinished_tasks and time.monotonic() < end:
        time.sleep(0.01)


def _writer_loop() -> None:
    global paused
    try:
        conn = connect()
    except Exception as exc:
        paused = True
        logger.warning("flightlog: database unavailable, recording paused (%s)", exc)
        return
    last_prune = 0.0
    while True:
        batch = [_q.get()]
        while len(batch) < 500:  # batched commits
            try:
                batch.append(_q.get_nowait())
            except queue.Empty:
                break
        try:
            with conn:
                for op, data in batch:
                    _apply(conn, op, data)
            if time.time() - last_prune > 3600:
                prune(conn)
                last_prune = time.time()
        except Exception as exc:
            logger.warning("flightlog: write failed, %d ops lost (%s)", len(batch), exc)
        finally:
            for _ in batch:
                _q.task_done()


# ponytail: fallback only if Hermes's private redactor moves; catches common key shapes, not everything.
_SECRETS = re.compile(r"((?i:bearer)\s+[\w.~+/-]{8,}|sk-[\w-]{16,}|gh[pousr]_\w{20,}|xox[abp]-[\w-]{10,}|AKIA[0-9A-Z]{16}|eyJ[\w-]{10,}\.[\w-]+\.[\w-]+|"
                      r"(?i:(?:api[_-]?key|token|secret|password|authorization)[\"']?\s*[:=]\s*[\"']?(?:bearer\s+)?)[^\s\"',]{6,})")


def _redact(text: Optional[str]) -> Optional[str]:
    if not text:
        return text
    try:
        from agent.redact import redact_sensitive_text
        return redact_sensitive_text(text, force=True)[:200]
    except Exception:
        return _SECRETS.sub("[REDACTED]", text)[:200]


def _event_cost(data: Dict[str, Any]) -> Optional[float]:
    usage = data.pop("usage", None)
    if not usage:
        return None
    try:
        from agent.usage_pricing import CanonicalUsage, estimate_usage_cost
        cu = CanonicalUsage(output_tokens=usage.get("output_tokens", 0) or 0,
                            **{k: usage.get(k, 0) or 0 for k in
                               ("input_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")})
        amount = estimate_usage_cost(data.pop("model", ""), cu, provider=data.pop("provider", None),
                                     base_url=data.pop("base_url", None)).amount_usd
        return float(amount) if amount is not None else None
    except Exception:
        return None


def _apply(conn: sqlite3.Connection, op: str, data: Dict[str, Any]) -> None:
    if op == "event":
        cost = _event_cost(data)
        conn.execute("INSERT OR REPLACE INTO events (run_id, seq, ts, kind, name, status, duration_ms, tokens, "
                     "cost_usd, preview, target, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (data["run_id"], data["seq"], data["ts"], data["kind"], data.get("name"), data.get("status"),
                      data.get("duration_ms"), data.get("tokens"), cost, _redact(data.get("preview")),
                      _redact(data.get("target")), _redact(data.get("detail"))))
    elif op == "run":
        cost = conn.execute("SELECT SUM(cost_usd) FROM events WHERE run_id=?", (data["run_id"],)).fetchone()[0]
        conn.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (data["run_id"], data["session_id"], data["platform"], data["job_id"],
                      data.get("job_name") or _job_name(data["job_id"]), data.get("model"), data["started"],
                      data["ended"], data["outcome"], data["tool_calls"], data["tool_errors"], data["tokens"], cost))
    elif op == "finding":
        conn.execute("INSERT OR REPLACE INTO findings VALUES (?,?,?,?,?,?,?,0,?)",
                     (data["finding_id"], data["run_id"], data.get("job_id"), data["rule"], data["summary"],
                      json.dumps(data["evidence"]), data.get("report"), time.time()))
    elif op == "report":
        conn.execute("UPDATE findings SET report=? WHERE run_id=?", (data["report"], data["run_id"]))


def _job_name(job_id: Optional[str]) -> Optional[str]:
    if not job_id:
        return None
    try:
        from cron.jobs import load_jobs
        return next((j.get("name") for j in load_jobs() if j.get("id") == job_id), None)
    except Exception:
        return None


def prune(conn: sqlite3.Connection, days: int = RETENTION_DAYS) -> None:
    cutoff = time.time() - days * 86400
    old = "SELECT run_id FROM runs WHERE ended < ?"
    with conn:
        conn.execute(f"DELETE FROM events WHERE run_id IN ({old})", (cutoff,))
        conn.execute(f"DELETE FROM findings WHERE run_id IN ({old})", (cutoff,))
        conn.execute("DELETE FROM runs WHERE ended < ?", (cutoff,))


# ---------------------------------------------------------------- reads (CLI, slash command, dashboard)

def _rows(cur) -> List[Dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]


def healthy_runs(conn, job_id: str, limit: int = 7) -> List[Dict[str, Any]]:
    """Last N ok runs of a job with no unacked findings — the baseline sample."""
    return _rows(conn.execute(
        """SELECT tool_calls, tokens, (ended - started) * 1000 AS duration_ms FROM runs r
           WHERE job_id=? AND outcome='ok'
             AND NOT EXISTS (SELECT 1 FROM findings f WHERE f.run_id=r.run_id AND f.acked=0)
           ORDER BY ended DESC LIMIT ?""", (job_id, limit)))


def silenced_rules(conn, job_id: Optional[str]) -> frozenset:
    if not job_id:
        return frozenset()
    return frozenset(r[0] for r in conn.execute("SELECT rule FROM silenced WHERE job_id=?", (job_id,)))


def recent_runs(conn, limit: int = 20, job_id: Optional[str] = None) -> List[Dict[str, Any]]:
    where, args = ("WHERE job_id=?", (job_id,)) if job_id else ("", ())
    return _rows(conn.execute(
        f"""SELECT r.*, (SELECT COUNT(*) FROM findings f WHERE f.run_id=r.run_id AND f.acked=0) AS findings,
                   (SELECT rule FROM findings f WHERE f.run_id=r.run_id AND f.acked=0 ORDER BY created LIMIT 1) AS top_rule
            FROM runs r {where} ORDER BY ended DESC LIMIT ?""", (*args, limit)))


def find_runs(conn, ref: str) -> List[Dict[str, Any]]:
    """A run id, a session id (all its runs), or a unique prefix of either."""
    return _rows(conn.execute(
        "SELECT * FROM runs WHERE run_id=? OR session_id=? OR run_id LIKE ? OR session_id LIKE ? ORDER BY started",
        (ref, ref, ref + "%", ref + "%")))


def events(conn, run_id: str) -> List[Dict[str, Any]]:
    return _rows(conn.execute("SELECT * FROM events WHERE run_id=? ORDER BY seq", (run_id,)))


def recent_summary(conn, hours: int = 24) -> Dict[str, int]:
    row = conn.execute(
        """SELECT COUNT(*), SUM(EXISTS (SELECT 1 FROM findings f WHERE f.run_id=r.run_id AND f.acked=0))
           FROM runs r WHERE ended > ?""", (time.time() - hours * 3600,)).fetchone()
    return {"runs": row[0] or 0, "flagged": row[1] or 0}


def findings(conn, run_id: Optional[str] = None, since: float = 0, include_acked: bool = False):
    q = "SELECT * FROM findings WHERE created > ?"
    args: list = [since]
    if run_id:
        q += " AND run_id=?"
        args.append(run_id)
    if not include_acked:
        q += " AND acked=0"
    return _rows(conn.execute(q + " ORDER BY created DESC LIMIT 200", args))


def jobs(conn) -> List[Dict[str, Any]]:
    return _rows(conn.execute(
        """SELECT job_id, MAX(job_name) AS job_name, COUNT(*) AS runs, MAX(ended) AS last_run
           FROM runs WHERE job_id IS NOT NULL GROUP BY job_id ORDER BY last_run DESC"""))


def ack(conn, finding_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT * FROM findings WHERE finding_id LIKE ?", (finding_id + "%",)).fetchone()
    if row is None:
        return None
    with conn:
        conn.execute("UPDATE findings SET acked=1 WHERE finding_id=?", (row["finding_id"],))
        if row["job_id"]:
            conn.execute("INSERT OR IGNORE INTO silenced VALUES (?,?)", (row["job_id"], row["rule"]))
    return dict(row)


def purge(conn) -> None:
    with conn:
        for t in ("events", "findings", "runs", "silenced", "meta"):
            conn.execute(f"DELETE FROM {t}")


def get_meta(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(conn, key: str, value: str) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))
