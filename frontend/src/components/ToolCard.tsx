import { createContext, useContext, useState } from "react";
import { describeCall, type ToolItem } from "../timeline";
import { Codicon } from "./Codicon";
import { CopyButton } from "./CopyButton";
import { Icon } from "./Icon";

/** Opens a file a tool call named in the editor area: the file itself after a Read, its diff after an edit. */
export const OpenFileContext = createContext<((path: string, edited: boolean) => void) | null>(null);

function verdict(tool: ToolItem): { text: string; tone: string } | null {
  if (!tool.done) return { text: "running…", tone: "running" };
  if (tool.isError) return { text: "error", tone: "error" };
  const first = tool.output.split("\n", 1)[0];
  if (tool.name === "RunTests") {
    return { text: first.replace(/^\[tests /, "").replace(/\].*$/, ""), tone: first.includes("PASSED") ? "ok" : "error" };
  }
  const exit = tool.output.match(/\[exit code (-?\d+)\]\s*$/);
  if (exit) return { text: `exit ${exit[1]}`, tone: exit[1] === "0" ? "ok" : "error" };
  return null;
}

/** The call in full, as the user would want to paste it: the command itself, or the arguments. */
function callText(tool: ToolItem): string {
  if (tool.name === "Bash" && typeof tool.args.command === "string") return tool.args.command;
  if (["Read", "Write", "Edit"].includes(tool.name)) return String(tool.args.file_path ?? "");
  return JSON.stringify(tool.args, null, 2);
}

export function ToolCard({ tool }: { tool: ToolItem }) {
  return tool.name === "Plan" && tool.plan ? <PlanCard plan={tool.plan} /> : <CallCard tool={tool} />;
}

const PLAN_MARKS = { completed: "✓", in_progress: "▸", pending: "○" };

/**
 * A Plan call: the checklist as the model recorded it at this point, open; the notes on request.
 * No "n done" count: models rarely mark steps done later, so a count would read as progress it is not.
 */
function PlanCard({ plan }: { plan: NonNullable<ToolItem["plan"]> }) {
  const [notesOpen, setNotesOpen] = useState(false);
  return (
    <div className="tool-card plan-card">
      <button className="tool-head" onClick={() => setNotesOpen(!notesOpen)} disabled={!plan.notes} aria-expanded={notesOpen}>
        {plan.notes && <span className={`chev ${notesOpen ? "open" : ""}`}><Icon name="chevron" size={12} /></span>}
        <span className="tool-name">Plan</span>
        <span className="tool-args">{plan.steps.length} steps{plan.notes ? " · notes" : ""}</span>
      </button>
      <ol className="plan-steps">
        {plan.steps.map((s, i) => (
          <li key={i} className={`plan-step ${s.status}`}>
            <span className="plan-mark">{PLAN_MARKS[s.status] ?? PLAN_MARKS.pending}</span>
            <span>{s.step}</span>
          </li>
        ))}
      </ol>
      {notesOpen && plan.notes && <pre className="plan-notes">{plan.notes}</pre>}
    </div>
  );
}

function CallCard({ tool }: { tool: ToolItem }) {
  const [open, setOpen] = useState(false);
  const openFile = useContext(OpenFileContext);
  const filePath = ["Read", "Write", "Edit"].includes(tool.name) && typeof tool.args.file_path === "string" ? tool.args.file_path : "";
  const result = verdict(tool);
  const body = tool.done ? tool.output : tool.streamed;
  const expanded = open || (!tool.done && tool.streamed.length > 0);
  return (
    <div className={`tool-card ${tool.isError ? "is-error" : ""}`}>
      <div className="tool-head-row">
        <button className="tool-head" onClick={() => setOpen(!open)} aria-expanded={expanded}>
          <span className={`chev ${expanded ? "open" : ""}`}><Icon name="chevron" size={12} /></span>
          <span className="tool-name">{tool.name}</span>
          <span className="tool-args">{describeCall(tool.name, tool.args)}</span>
          {result && <span className={`tool-verdict ${result.tone}`}>{result.text}</span>}
          {tool.elapsed !== undefined && tool.elapsed >= 1 && <span className="dim small">{tool.elapsed.toFixed(1)}s</span>}
        </button>
        {openFile && filePath && tool.done && (
          <button className="action tool-open" title={tool.name === "Read" ? "Open the file" : "Open the change"}
            aria-label={tool.name === "Read" ? "Open the file" : "Open the change"}
            onClick={() => openFile(filePath, tool.name !== "Read")}>
            <Codicon name={tool.name === "Read" ? "go-to-file" : "git-compare"} />
          </button>
        )}
      </div>
      {expanded && (
        <div className="tool-body">
          <div className="tool-call copyable">
            <pre>{callText(tool)}</pre>
            <CopyButton text={callText(tool)} className="floating" title="Copy the call" />
          </div>
          {body && (
            <div className="copyable">
              <pre className="tool-output">{body}</pre>
              <CopyButton text={body} className="floating" title="Copy the output" />
            </div>
          )}
        </div>
      )}
    </div>
  );
}
