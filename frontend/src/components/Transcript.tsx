import { useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import type { AgentEvent, AttachmentInfo } from "../api";
import { Markdown } from "../Markdown";
import { buildTimeline, taskAttachments, type Item, type StepItem } from "../timeline";
import { AttachmentList } from "./Attachments";
import { ContextGauge, lastUsage } from "./ContextGauge";
import { CopyButton } from "./CopyButton";
import { Icon } from "./Icon";
import { ToolCard } from "./ToolCard";

type OpenAttachment = (a: AttachmentInfo) => void;

export interface TaskView {
  id: string;
  task: string;
  events: AgentEvent[];
  live: boolean;
  /** Unix seconds; the clock the user sees, not the agent's own elapsed time. */
  started?: number;
  finished?: number | null;
}

function clock(seconds: number): string {
  return new Date(seconds * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function duration(seconds: number): string {
  const whole = Math.max(0, Math.round(seconds));
  const [h, m, s] = [Math.floor(whole / 3600), Math.floor(whole / 60) % 60, whole % 60];
  return h ? `${h}h ${String(m).padStart(2, "0")}m ${String(s).padStart(2, "0")}s` : m ? `${m}m ${String(s).padStart(2, "0")}s` : `${s}s`;
}

/** When the task started, when it ended, and how long it took — ticking while it runs. */
function TaskTimer({ started, finished, live }: { started?: number; finished?: number | null; live: boolean }) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    if (!live) return;
    const timer = window.setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => window.clearInterval(timer);
  }, [live]);
  if (!started) return null;
  const end = finished ?? (live ? now : null);
  return (
    <div className="task-time dim small" title={`Started ${new Date(started * 1000).toLocaleString()}`}>
      <Icon name="clock" size={12} />
      {clock(started)}
      {end !== null && <> → {live ? "running" : clock(end)} · <strong>{duration(end - started)}</strong></>}
    </div>
  );
}

function kilo(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}K` : String(n);
}

function Thinking({ text, live }: { text: string; live: boolean }) {
  const [open, setOpen] = useState(false);
  if (!text) return null;
  return (
    <div className="thinking">
      <button className="thinking-head" onClick={() => setOpen(!open)}>
        {live ? "thinking…" : `thought ${text.length.toLocaleString()} characters`} {open ? "▾" : "▸"}
      </button>
      {open && (
        <div className="copyable">
          <pre className="thinking-body">{text}</pre>
          <CopyButton text={text} className="floating" />
        </div>
      )}
    </div>
  );
}

function Step({ item, live }: { item: StepItem; live: boolean }) {
  const usage = item.usage;
  return (
    <div className="step">
      <Thinking text={item.reasoning} live={live && !item.text && item.tools.length === 0} />
      {item.text.trim() && (
        <div className="copyable">
          <Markdown text={item.text} className="assistant-text" />
          <CopyButton text={item.text} className="floating bottom" title="Copy this reply (Markdown)" />
        </div>
      )}
      {item.tools.map((tool) => <ToolCard key={tool.id} tool={tool} />)}
      {usage && (
        <div className="usage">
          step {usage.step} · context {kilo(usage.context_tokens)}/{kilo(usage.context_limit)}
          {usage.ttft != null && ` · first token ${usage.ttft.toFixed(1)}s`}
          {usage.tokens_per_second && ` · ${Math.round(usage.tokens_per_second)} tok/s`}
          {usage.cost > 0 && ` · $${usage.cost.toFixed(4)}`}
        </div>
      )}
    </div>
  );
}

function Entry({ item, live, onOpen }: { item: Item; live: boolean; onOpen: OpenAttachment }) {
  switch (item.kind) {
    case "step":
      return <Step item={item} live={live} />;
    case "info":
      return <div className={`info tone-${item.tone}`}>{item.text}</div>;
    case "handoff":
      return (
        <details className="handoff" open={!item.done}>
          <summary>Handoff notes {item.source === "digest" ? "(automatic record)" : ""} {item.done ? "" : "— writing…"}</summary>
          <div className="copyable">
            <Markdown text={item.notes} />
            <CopyButton text={item.notes} className="floating bottom" />
          </div>
        </details>
      );
    case "continuation":
      return <div className="continuation">session {item.session} · fresh context, continuing from the notes</div>;
    case "steer":
      return (
        <div className="user-message">
          {item.text && <UserBubble text={item.text} kind="steer" />}
          <AttachmentList attachments={item.attachments} onOpen={onOpen} />
        </div>
      );
    case "switch":
      return (
        <div className="info tone-switch">
          <Icon name="swap" size={13} />{" "}
          {item.by === "user" ? "you moved the task" : item.by === "fallback" ? "fell back" : "the agent switched"} from {item.previous} to{" "}
          <strong>{item.model}</strong> ({item.local ? "local" : "cloud"}{item.vision ? ", sees images" : ""})
          {item.reason && <span> — {item.reason}</span>}
        </div>
      );
    case "approval":
      return (
        <div className="info tone-warn">
          approval {item.allowed === undefined ? "requested" : item.allowed ? "granted" : "denied"}: <code>{item.command}</code>
        </div>
      );
    case "question":
      return (
        <div className="asked">
          <div className="asked-q"><Icon name="chat" size={14} /> {item.question}</div>
          {item.answer !== undefined && (
            <div className="asked-a">{item.answer || "(no answer; the agent carried on)"}</div>
          )}
        </div>
      );
    case "subtask":
      return (
        <details className="subtask">
          <summary>
            <Icon name="branch" size={13} /> sub-agent: {item.headline}
            {item.done
              ? <span className="dim small"> · {item.steps} steps · {kilo(item.tokens)} tokens of its own context</span>
              : <span className="dim small"> · working…</span>}
          </summary>
          {item.instructions && (
            // What it was asked is the other half of the story: the report only makes sense next to it.
            <div className="subtask-brief small dim">{item.instructions}</div>
          )}
          {item.done ? (
            <div className="copyable">
              <Markdown text={item.summary} />
              <CopyButton text={item.summary} className="floating bottom" />
            </div>
          ) : null}
        </details>
      );
    case "review":
      return item.clean ? (
        <div className="info tone-ok">⚖ {item.model} reviewed the change and found no problems</div>
      ) : (
        <details className="handoff review" open>
          <summary>⚖ {item.model} found problems — fixing them</summary>
          <div className="copyable">
            <Markdown text={item.findings} />
            <CopyButton text={item.findings} className="floating bottom" />
          </div>
        </details>
      );
    case "finish":
      return (
        <div className={`finish ${item.success ? "ok" : "failed"}`}>
          <div className="finish-head">
            <strong>{item.success ? "Done" : `Not completed (${item.reason})`}</strong>
            <span className="dim small">
              {item.steps} steps{item.sessions > 1 ? ` · ${item.sessions} sessions` : ""} · {kilo(item.tokens)} tokens
              {item.cost > 0 ? ` · $${item.cost.toFixed(2)}` : ""} · {Math.round(item.elapsed)}s
              {item.summary && <CopyButton text={item.summary} title="Copy the answer (Markdown)" />}
            </span>
          </div>
          <Markdown text={item.summary} />
        </div>
      );
    case "error":
      return <div className="info tone-error">{item.text}</div>;
  }
}

/** The user's message, with a copy button beside it on hover. */
function UserBubble({ text, kind }: { text: string; kind: "user" | "steer" }) {
  return (
    <div className="bubble-row">
      <CopyButton text={text} />
      <div className={`bubble ${kind}`}>{text}</div>
    </div>
  );
}

function TaskBlock({ task, onOpen }: { task: TaskView; onOpen: OpenAttachment }) {
  const items = useMemo(() => buildTimeline(task.events), [task.events]);
  const attachments = useMemo(() => taskAttachments(task.events), [task.events]);
  const lastStepIndex = items.map((i) => i.kind).lastIndexOf("step");
  return (
    <section className="task">
      <div className="user-message">
        <UserBubble text={task.task} kind="user" />
        <AttachmentList attachments={attachments} onOpen={onOpen} />
        <TaskTimer started={task.started} finished={task.finished} live={task.live} />
      </div>
      {items.map((item, index) => <Entry key={index} item={item} live={task.live && index === lastStepIndex} onOpen={onOpen} />)}
      {task.live && !items.some((i) => i.kind === "finish") && <div className="working">working…</div>}
    </section>
  );
}

/** How close to the end counts as "at the bottom" (a pixel or two is lost to rounding and zoom). */
const BOTTOM_SLACK = 24;
const SCROLL_UP_KEYS = new Set(["ArrowUp", "PageUp", "Home"]);

/**
 * The chat. While a task runs it follows the new output, but only while the view is at the bottom:
 * scrolling up (wheel, keys, scrollbar) stops following and leaves the view where it was; scrolling back
 * to the bottom, or the "latest" button, follows again.
 */
export function Transcript({ tasks, empty, onOpenAttachment }: { tasks: TaskView[]; empty: ReactNode; onOpenAttachment: OpenAttachment }) {
  const scroller = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const lastTop = useRef(0);
  const [atBottom, setAtBottom] = useState(true);
  const [scrollable, setScrollable] = useState(false);
  const current = tasks[tasks.length - 1];
  const usage = current ? lastUsage(current.events) : null;
  const live = current?.live ?? false;

  const measure = () => {
    const el = scroller.current;
    if (!el) return;
    setScrollable(el.scrollHeight > el.clientHeight + 4);
    setAtBottom(el.scrollHeight - el.scrollTop - el.clientHeight <= BOTTOM_SLACK);
  };
  const toBottom = () => {
    const el = scroller.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    lastTop.current = el.scrollTop;
  };

  // After every render (new output) and whenever the content changes size (an image loads, a card opens).
  useLayoutEffect(() => {
    if (follow.current) toBottom();
    measure();
  });
  useEffect(() => {
    const el = content.current;
    if (!el) return;
    const observer = new ResizeObserver(() => {
      if (follow.current) toBottom();
      measure();
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const onScroll = () => {
    const el = scroller.current;
    if (!el) return;
    const bottom = el.scrollHeight - el.scrollTop - el.clientHeight <= BOTTOM_SLACK;
    // Moving up is always the user (following only ever moves down); reaching the end going down follows again.
    if (el.scrollTop < lastTop.current - 1) follow.current = false;
    else if (bottom) follow.current = true;
    lastTop.current = el.scrollTop;
    setAtBottom(bottom);
  };
  // Smooth scrolling reports its first small step after the next render, which would pull the view back down:
  // stop following as soon as the wheel or a key asks to go up.
  const stopFollowing = () => {
    follow.current = false;
  };

  const jump = (top: number) => {
    follow.current = false;
    scroller.current?.scrollTo({ top, behavior: "smooth" });
  };
  const toTurnStart = () => {
    const sections = scroller.current?.querySelectorAll<HTMLElement>("section.task");
    const last = sections?.[sections.length - 1];
    if (last) jump(last.offsetTop - 12);
  };
  const toLatest = () => {
    follow.current = true;
    toBottom();
    measure();
  };

  return (
    <>
      {usage && <ContextGauge usage={usage} live={current.live} />}
      <div className="transcript-frame">
        <div
          className="transcript"
          ref={scroller}
          onScroll={onScroll}
          tabIndex={-1}
          onWheel={(e) => e.deltaY < 0 && stopFollowing()}
          onKeyDown={(e: KeyboardEvent) => SCROLL_UP_KEYS.has(e.key) && stopFollowing()}
          onPointerDown={(e) => {
            // A press on the scrollbar itself: dragging it must not be pulled back down by new output.
            const el = e.currentTarget;
            if (e.clientX >= el.getBoundingClientRect().left + el.clientWidth) stopFollowing();
          }}
        >
          <div ref={content}>
            {tasks.length === 0 ? empty : tasks.map((task) => <TaskBlock key={task.id} task={task} onOpen={onOpenAttachment} />)}
          </div>
        </div>
        {scrollable && tasks.length > 0 && (
          <div className="scroll-nav" role="toolbar" aria-label="Scroll the chat">
            <button onClick={() => jump(0)} title="Top of the chat"><Icon name="toTop" size={15} /></button>
            <button onClick={toTurnStart} title="Start of the latest turn (your last message)"><Icon name="up" size={15} /></button>
            <button onClick={toLatest} className={!atBottom && live ? "attention" : ""}
              title={live ? "Latest output, and keep following it" : "Bottom of the chat"}>
              <Icon name="toBottom" size={15} />
              {!atBottom && live && <span className="new-output" />}
            </button>
          </div>
        )}
      </div>
    </>
  );
}
