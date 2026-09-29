// Turn a task's event stream (live, or replayed from the database) into renderable items.
import type { AgentEvent, AttachmentInfo } from "./api";

export interface PlanStep {
  step: string;
  status: "pending" | "in_progress" | "completed";
}

export interface ToolItem {
  kind: "tool";
  id: string;
  name: string;
  args: Record<string, any>;
  streamed: string;
  output: string;
  done: boolean;
  isError: boolean;
  elapsed?: number;
  plan?: { steps: PlanStep[]; notes: string }; // a Plan call, as the agent recorded it
}

export interface StepItem {
  kind: "step";
  step: number;
  reasoning: string;
  text: string;
  tools: ToolItem[];
  usage?: AgentEvent;
}

export type Item =
  | StepItem
  | { kind: "info"; tone: "muted" | "warn" | "branch" | "commit"; text: string }
  | { kind: "handoff"; source: string; notes: string; done: boolean }
  | { kind: "continuation"; session: number }
  | { kind: "steer"; text: string; attachments: AttachmentInfo[] }
  | { kind: "switch"; model: string; previous: string; by: string; reason: string; local: boolean; vision: boolean }
  | { kind: "approval"; id: string; command: string; reason: string; allowed?: boolean }
  | { kind: "question"; id: string; question: string; answer?: string }
  | { kind: "review"; model: string; clean: boolean; findings: string }
  | { kind: "subtask"; id: string; headline: string; instructions: string; done: boolean; success: boolean; steps: number; tokens: number; summary: string }
  | { kind: "finish"; success: boolean; summary: string; reason: string; steps: number; sessions: number; tokens: number; elapsed: number; cost: number }
  | { kind: "error"; text: string };

export function buildTimeline(events: AgentEvent[]): Item[] {
  const items: Item[] = [];
  const tools = new Map<string, ToolItem>();
  let step: StepItem | null = null;
  let handoff: Extract<Item, { kind: "handoff" }> | null = null;

  const currentStep = (): StepItem => {
    if (!step) {
      step = { kind: "step", step: 0, reasoning: "", text: "", tools: [] };
      items.push(step);
    }
    return step;
  };

  for (const e of events) {
    switch (e.type) {
      case "task_start":
        items.push({ kind: "info", tone: "muted", text: `${e.model}${e.vision ? " · sees images" : ""} · thinking ${e.thinking ? "on" : "off"}` });
        break;
      case "worktree": {
        const state = e.created ? "new branch" : e.restored ? "restored worktree" : "continuing on";
        const extra = e.created && e.carries_user_changes ? " · includes your uncommitted changes" : "";
        items.push({ kind: "info", tone: "branch", text: `${state} ${e.branch} (from ${e.base})${extra}` });
        break;
      }
      case "step_start":
        step = { kind: "step", step: e.step, reasoning: "", text: "", tools: [] };
        items.push(step);
        break;
      case "reasoning_delta":
        if (!handoff) currentStep().reasoning += e.text;
        break;
      case "content_delta":
        if (handoff) handoff.notes += e.text;
        else currentStep().text += e.text;
        break;
      case "assistant": {
        const s = currentStep();
        s.text = e.text ?? s.text;
        s.reasoning = e.reasoning || s.reasoning;
        break;
      }
      case "usage":
        currentStep().usage = e;
        break;
      case "tool_call": {
        const tool: ToolItem = { kind: "tool", id: e.id, name: e.name, args: e.args ?? {}, streamed: "", output: "", done: false, isError: false };
        tools.set(e.id, tool);
        currentStep().tools.push(tool);
        break;
      }
      case "tool_output": {
        const tool = tools.get(e.id);
        if (tool) tool.streamed += e.text;
        break;
      }
      case "plan": {
        const tool = tools.get(e.id);
        if (tool) tool.plan = { steps: e.steps ?? [], notes: e.notes ?? "" };
        break;
      }
      case "tool_result": {
        let tool = tools.get(e.id);
        if (!tool) {
          tool = { kind: "tool", id: e.id, name: e.name, args: {}, streamed: "", output: "", done: false, isError: false };
          currentStep().tools.push(tool);
        }
        tool.output = e.output;
        tool.isError = e.is_error;
        tool.done = true;
        tool.elapsed = e.elapsed;
        break;
      }
      case "handoff_start":
        handoff = { kind: "handoff", source: "model", notes: "", done: false };
        items.push(handoff);
        break;
      case "handoff":
        if (handoff) {
          handoff.notes = e.notes;
          handoff.source = e.source;
          handoff.done = true;
        } else {
          items.push({ kind: "handoff", source: e.source, notes: e.notes, done: true });
        }
        handoff = null;
        step = null;
        break;
      case "continuation":
        items.push({ kind: "continuation", session: e.session });
        step = null;
        break;
      case "steer":
        items.push({ kind: "steer", text: e.text, attachments: e.attachments ?? [] });
        break;
      case "model_switch":
        items.push({ kind: "switch", model: e.model, previous: e.previous, by: e.by, reason: e.reason ?? "",
          local: e.local, vision: e.vision });
        break;
      case "approval_request":
        items.push({ kind: "approval", id: e.id, command: e.command, reason: e.reason });
        break;
      case "review":
        items.push({ kind: "review", model: e.model, clean: e.clean, findings: e.findings });
        break;
      case "subtask_start":
        items.push({ kind: "subtask", id: e.task_id, headline: e.headline, instructions: e.instructions ?? "",
          done: false, success: false, steps: 0, tokens: 0, summary: "" });
        break;
      case "subtask_finish": {
        const started = items.find((i) => i.kind === "subtask" && i.id === e.task_id);
        const finished = { done: true, success: e.success, steps: e.steps, tokens: e.total_tokens, summary: e.summary };
        if (started && started.kind === "subtask") Object.assign(started, finished);
        else items.push({ kind: "subtask", id: e.task_id, headline: e.headline, instructions: "", ...finished });
        break;
      }
      case "question_request":
        items.push({ kind: "question", id: e.id, question: e.question });
        break;
      case "question_resolved": {
        const asked = items.find((i) => i.kind === "question" && i.id === e.id);
        if (asked && asked.kind === "question") asked.answer = e.answer;
        break;
      }
      case "approval_resolved": {
        const item = items.find((i) => i.kind === "approval" && i.id === e.id);
        if (item && item.kind === "approval") item.allowed = e.allowed;
        break;
      }
      case "task_committed":
        items.push({ kind: "info", tone: "commit", text: `committed ${String(e.commit).slice(0, 8)} ${e.subject}` });
        break;
      case "notice":
        items.push({ kind: "info", tone: "warn", text: e.text });
        break;
      case "retry":
        items.push({ kind: "info", tone: "warn", text: `retry ${e.attempt} in ${e.delay}s: ${e.error}` });
        break;
      case "finish":
        items.push({
          kind: "finish", success: e.success, summary: e.summary, reason: e.reason, steps: e.steps,
          sessions: e.sessions ?? 1, tokens: e.total_tokens, elapsed: e.elapsed, cost: e.cost ?? 0,
        });
        break;
      case "error":
        items.push({ kind: "error", text: e.message });
        break;
    }
  }
  return items;
}

/** The files attached to a task (from its task_start event). */
export function taskAttachments(events: AgentEvent[]): AttachmentInfo[] {
  const start = events.find((e) => e.type === "task_start") ?? events.find((e) => e.type === "task_started");
  return start?.attachments ?? [];
}

/** A one-line description of a tool call. */
export function describeCall(name: string, args: Record<string, any>): string {
  switch (name) {
    case "Bash":
      return `$ ${args.command ?? ""}`;
    case "RunTests":
      return args.command || args.target || "detected test command";
    case "Read":
    case "Write":
    case "Edit":
      return String(args.file_path ?? "");
    case "Grep":
    case "Glob":
      return `${args.pattern ?? ""}${args.path ? ` in ${args.path}` : ""}`;
    case "WebSearch":
      return String(args.query ?? "");
    case "WebFetch":
      return `${args.url ?? ""}${args.start ? ` from ${args.start}` : ""}`;
    case "set_model":
      return `${args.model ?? ""}${args.reason ? ` — ${args.reason}` : ""}`;
    case "Plan":
      return Array.isArray(args.steps) ? `${args.steps.length} steps` : "";
    default:
      return JSON.stringify(args).slice(0, 160);
  }
}

/** Requests in *events* that are still waiting for the user (a reloaded page must show them again). */
export function pendingRequests(events: AgentEvent[]): {
  approval: { id: string; command: string; reason: string } | null;
  question: { id: string; question: string; options: string[] } | null;
} {
  let approval: { id: string; command: string; reason: string } | null = null;
  let question: { id: string; question: string; options: string[] } | null = null;
  for (const e of events) {
    if (e.type === "approval_request") approval = { id: e.id, command: e.command, reason: e.reason };
    if (e.type === "approval_resolved" && approval?.id === e.id) approval = null;
    if (e.type === "question_request") question = { id: e.id, question: e.question, options: e.options ?? [] };
    if (e.type === "question_resolved" && question?.id === e.id) question = null;
  }
  return { approval, question };
}
