(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  const { React } = SDK;
  const { useState, useEffect } = SDK.hooks;
  const { Card, CardHeader, CardTitle, CardContent, Badge } = SDK.components;
  const h = React.createElement;
  const API = "/api/plugins/blackbox";
  const BAD = { error: 1, failed: 1, denied: 1, timeout: 1 };

  const LABELS = {
    unverified_claim: "Unverified claim", swallowed_errors: "Swallowed tool errors", loop: "Loop", cost_spike: "Cost spike",
    effort_collapse: "Effort collapse", failed_child: "Failed child", empty_work: "Empty work",
  };

  function mark(r) {
    if (r.findings) return ["amber", "flagged"];
    return r.outcome === "ok" ? ["green", "ok"] : ["red", r.outcome];
  }

  function useJSON(url, deps) {
    const [data, setData] = useState(null);
    const [err, setErr] = useState(null);
    useEffect(() => {
      if (!url) return;
      let live = true;
      SDK.fetchJSON(url).then((d) => live && setData(d)).catch((e) => live && setErr(String(e)));
      return () => { live = false; };
    }, deps);
    return [data, err];
  }

  function RunsList({ runs, selected, onSelect, job, setJob }) {
    const jobs = Array.from(new Set(runs.map((r) => r.job_name || r.job_id).filter(Boolean)));
    const shown = job ? runs.filter((r) => (r.job_name || r.job_id) === job) : runs;
    return h(Card, null,
      h(CardHeader, null, h(CardTitle, null, "Runs")),
      h(CardContent, null,
        h("select", { className: "bb-select", value: job, onChange: (e) => setJob(e.target.value), "aria-label": "Filter by job" },
          h("option", { value: "" }, "All runs"),
          jobs.map((j) => h("option", { key: j, value: j }, j))),
        shown.length === 0 && h("p", { className: "text-sm text-muted-foreground" }, "No runs recorded yet."),
        h("ul", { className: "bb-runs" }, shown.map((r) => {
          const [color, label] = mark(r);
          return h("li", { key: r.run_id },
            h("button", { className: "bb-run" + (selected === r.run_id ? " bb-sel" : ""), onClick: () => onSelect(r.run_id) },
              h("span", { className: "bb-dot bb-" + color, title: label }),
              h("span", { className: "bb-who" }, r.job_name || (r.job_id ? "cron " + r.job_id : r.platform || "session")),
              h("span", { className: "bb-meta" }, r.tool_calls + " tools · " + SDK.utils.timeAgo(r.ended))));
        }))));
  }

  function Timeline({ runId }) {
    const [data, err] = useJSON(runId ? API + "/runs/" + runId : null, [runId]);
    if (!runId) return h(Card, null, h(CardContent, { className: "text-sm text-muted-foreground py-6" }, "Select a run."));
    if (err) return h(Card, null, h(CardContent, { className: "py-6" }, "Failed to load run: " + err));
    if (!data) return h(Card, null, h(CardContent, { className: "py-6" }, "Loading…"));
    const { run, events, findings } = data;
    const maxDur = Math.max(1, ...events.map((e) => e.duration_ms || 0));
    return h(Card, null,
      h(CardHeader, null,
        h(CardTitle, null, (run.job_name || run.session_id) + " · " + run.run_id),
        h("p", { className: "text-sm text-muted-foreground" },
          run.outcome + " · " + ((run.ended - run.started)).toFixed(1) + "s · " + run.tool_calls + " tools · " +
          run.tokens.toLocaleString() + " tokens" + (run.cost_usd ? " · $" + run.cost_usd.toFixed(3) : ""))),
      h(CardContent, null,
        findings.map((f) => h("div", { key: f.finding_id, className: "bb-finding" },
          h(Badge, null, LABELS[f.rule] || f.rule), " ", f.summary,
          f.report && h("p", { className: "bb-report" }, f.report))),
        h("ol", { className: "bb-timeline" }, events.map((e) =>
          h("li", { key: e.seq, className: "bb-ev" + (BAD[e.status] ? " bb-bad" : "") },
            h("span", { className: "bb-t" }, "+" + (e.ts - run.started).toFixed(2) + "s"),
            h("span", { className: "bb-kind" }, e.kind),
            h("span", { className: "bb-name" }, e.name || ""),
            h("span", { className: "bb-bar-wrap" },
              e.duration_ms != null && h("span", { className: "bb-bar", style: { width: Math.max(2, 100 * e.duration_ms / maxDur) + "%" } })),
            h("span", { className: "bb-dur" }, e.duration_ms != null ? e.duration_ms + "ms" : ""),
            h("span", { className: "bb-preview", title: e.preview || "" }, e.preview || ""))))));
  }

  function BlackboxPage() {
    const [data, err] = useJSON(API + "/runs?limit=200", []);
    const [selected, setSelected] = useState(null);
    const [job, setJob] = useState("");
    if (err) return h("p", { className: "p-4" }, "Blackbox API unavailable: " + err);
    if (!data) return h("p", { className: "p-4" }, "Loading…");
    const runs = data.runs;
    const current = selected || (runs.find((r) => r.findings) || runs[0] || {}).run_id;
    return h("div", { className: "bb-page" },
      h(RunsList, { runs, selected: current, onSelect: setSelected, job, setJob }),
      h(Timeline, { runId: current }));
  }

  function FleetCard() {
    const [d] = useJSON(API + "/health", []);
    if (!d) return null;
    const color = d.flagged || d.failed ? "amber" : "green";
    return h(Card, null, h(CardContent, { className: "py-2 text-sm bb-fleet" },
      h("span", { className: "bb-dot bb-" + color }),
      "Blackbox, last 24h: " + d.runs + " runs, " + d.flagged + " need a look. " +
      "Timelines in the Blackbox tab."));
  }

  window.__HERMES_PLUGINS__.register("blackbox", BlackboxPage);
  window.__HERMES_PLUGINS__.registerSlot("blackbox", "cron:top", FleetCard);
})();
