import type { AgentEvent } from "../api";

// What a local model costs you, at a glance: how full the window is, where the
// handoff will happen, and how fast this step ran. A long context is not just a
// limit -- every later step prefills more and answers slower -- so the numbers
// stay visible instead of scrolling away with the step they came from.

function kilo(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}K` : String(n);
}

/** The newest usage event of a task, or null when it has not run a step yet. */
export function lastUsage(events: AgentEvent[]): AgentEvent | null {
  for (let i = events.length - 1; i >= 0; i--) if (events[i].type === "usage") return events[i];
  return null;
}

export function ContextGauge({ usage, live }: { usage: AgentEvent; live: boolean }) {
  const limit = usage.context_limit || 1;
  const used = usage.context_tokens || 0;
  const handoff = usage.handoff_at || 0;
  const share = Math.min(1, used / limit);
  const tone = handoff && used >= handoff ? "hot" : handoff && used >= handoff * 0.85 ? "warm" : "";

  return (
    <div className={`gauge ${live ? "live" : ""}`}>
      <div className="gauge-bar" title={`${used.toLocaleString()} of ${limit.toLocaleString()} tokens`}>
        <div className={`gauge-fill ${tone}`} style={{ width: `${share * 100}%` }} />
        {handoff > 0 && handoff < limit && (
          <div className="gauge-mark" style={{ left: `${(handoff / limit) * 100}%` }}
            title={`a fresh session takes over at ${handoff.toLocaleString()} tokens`} />
        )}
      </div>
      <div className="gauge-text small dim">
        <span className={tone === "hot" ? "warn-text" : ""}>
          context {kilo(used)}/{kilo(limit)}
        </span>
        {handoff > 0 && <span> · hands off at {kilo(handoff)}</span>}
        <span> · step {usage.step}</span>
        {usage.ttft != null && <span> · first token {usage.ttft.toFixed(1)}s</span>}
        {usage.tokens_per_second ? <span> · {Math.round(usage.tokens_per_second)} tok/s</span> : null}
        {usage.total_tokens ? <span> · {kilo(usage.total_tokens)} tokens this task</span> : null}
        {usage.total_cost ? <span> · ${usage.total_cost.toFixed(3)}</span> : null}
        {usage.model && <span> · {usage.model}</span>}
      </div>
    </div>
  );
}
