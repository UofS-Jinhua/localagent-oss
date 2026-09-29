import { useEffect, useState, type ReactNode } from "react";
import type { ModelInfo, PendingBranch, RepoState } from "../api";
import { Codicon } from "./Codicon";
import { ModelDot } from "./ModelPicker";

function elapsed(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const pad = (n: number) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(s % 60)}` : `${pad(m)}:${pad(s % 60)}`;
}

function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!active) return;
    setNow(Date.now() / 1000);
    const timer = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  return now;
}

function Item({ children, title, onClick, className = "" }: {
  children: ReactNode; title: string; onClick?: () => void; className?: string;
}) {
  return onClick ? (
    <button className={`status-item ${className}`} title={title} onClick={onClick}>{children}</button>
  ) : <span className={`status-item ${className}`} title={title}>{children}</span>;
}

/** The bottom strip, VS Code style: where you are on the left, the agent's settings on the right. */
export function StatusBar(props: {
  /** "" on this computer; the machine's name when this window shows another machine, or one being connected. */
  machine: string;
  connecting: boolean;
  onMachine: () => void;
  online: boolean;
  workDir: string;
  repo: RepoState | null;
  /** The selected chat's branch, when it has one waiting. */
  chatBranch: PendingBranch | null;
  onBranch: () => void;
  /** When the running task started (unix seconds), and whether it is the selected chat's. */
  running: { started: number; here: boolean } | null;
  onRunning: () => void;
  modelName: string;
  model: ModelInfo | undefined;
  modelWarning: string;
  /** Picking a model while the chat's task runs moves the task to it. */
  switching: boolean;
  onModel: () => void;
  thinking: boolean;
  onThinking: () => void;
  review: boolean;
  onReview: () => void;
  /** Only set when the registry has cloud models the agent could move a task to. */
  cloud?: boolean;
  onCloud: () => void;
  steps: number;
  onSteps: () => void;
  notification: boolean;
  onNotifications: () => void;
}) {
  const now = useNow(props.running !== null);
  const { repo, chatBranch } = props;
  return (
    <footer className="statusbar">
      <div className="status-left">
        <Item className={`remote ${props.machine ? "on" : ""}`} onClick={props.onMachine}
          title={props.machine ? `This window works on ${props.machine}. Click to switch machines.` : "Open a window on another machine (ssh)"}>
          <Codicon name={props.connecting ? "loading" : "remote"} spin={props.connecting} />
          {props.machine && <span>{props.connecting ? `Connecting to ${props.machine}…` : props.machine}</span>}
        </Item>
        {!props.online && (
          <Item className="warning" title="The connection to LocalAgent's server dropped; reconnecting">
            <Codicon name="debug-disconnect" /> Reconnecting…
          </Item>
        )}
        {props.workDir && repo?.repo && (
          <Item onClick={props.onBranch} title={`Your checkout: ${repo.repo}${repo.dirty_files ? ` · ${repo.dirty_files} uncommitted` : ""}`}>
            <Codicon name="git-branch" /> {repo.branch ?? "detached"}{repo.dirty_files > 0 ? "*" : ""}
          </Item>
        )}
        {chatBranch && (
          <Item onClick={props.onBranch} title={`This chat works on ${chatBranch.branch}, from ${chatBranch.base}`}>
            <Codicon name="git-pull-request" /> {chatBranch.branch} · {chatBranch.commits}↑
          </Item>
        )}
        {props.workDir && repo && !repo.repo && (
          <Item title="Tasks edit this folder in place"><Codicon name="folder" /> not a git repository</Item>
        )}
        {props.running && (
          <Item onClick={props.onRunning} title={props.running.here ? "This chat's task is running" : "A task is running in another chat: show it"}>
            <Codicon name="loading" spin /> {props.running.here ? "Running" : "Running elsewhere"} {elapsed(now - props.running.started)}
          </Item>
        )}
      </div>
      <div className="status-right">
        <Item onClick={props.onSteps} title="How many tool calls one task may make before it stops and writes notes to continue from">
          <Codicon name="list-ordered" /> {props.steps} steps
        </Item>
        <Item className={props.review ? "" : "off"} onClick={props.onReview}
          title={`Review ${props.review ? "on" : "off"}: after the task, a second pass reads the diff and fixes what it finds`}>
          <Codicon name="checklist" /> Review
        </Item>
        {props.cloud !== undefined && (
          <Item className={props.cloud ? "" : "off"} onClick={props.onCloud}
            title={`Cloud ${props.cloud ? "allowed" : "off"}: let the agent move a task to a cloud model itself`}>
            <Codicon name="cloud" /> Cloud
          </Item>
        )}
        <Item className={props.thinking ? "" : "off"} onClick={props.onThinking}
          title={`Thinking ${props.thinking ? "on" : "off"}: let the model think before it answers (slower, usually better)`}>
          <Codicon name="lightbulb" /> Thinking
        </Item>
        <Item className={`model ${props.modelWarning ? "warning" : ""}`} onClick={props.onModel}
          title={props.modelWarning || (props.switching ? "Move the running task to another model" : "Model for new tasks")}>
          <ModelDot model={props.model} /> {props.modelName}
        </Item>
        <Item onClick={props.onNotifications} title={props.notification ? "Show the notification" : "No notifications"}>
          <Codicon name={props.notification ? "bell-dot" : "bell"} />
        </Item>
      </div>
    </footer>
  );
}
