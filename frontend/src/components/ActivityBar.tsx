import { useCallback, useRef, useState, type ReactNode } from "react";
import { Codicon } from "./Codicon";
import { useDismiss } from "./Menu";

export type SideView = "chats" | "explorer" | "scm";

const VIEWS: { id: SideView; icon: string; title: string }[] = [
  { id: "chats", icon: "comment-discussion", title: "Chats" },
  { id: "explorer", icon: "files", title: "Explorer (Ctrl+Shift+E)" },
  { id: "scm", icon: "source-control", title: "Source Control (Ctrl+Shift+G)" },
];

export interface MenuEntry {
  label: string;
  keys?: string;
  onClick: () => void;
}

/** The strip of view icons at the far left. Clicking the open view again hides the side bar, as in VS Code. */
export function ActivityBar({ view, sidebarOpen, badges, running, onView, menu }: {
  view: SideView;
  sidebarOpen: boolean;
  badges: Partial<Record<SideView, number>>;
  /** A task is running (in any chat): the Chats icon says so. */
  running: boolean;
  onView: (view: SideView) => void;
  /** The gear's menu. */
  menu: (MenuEntry | "-")[];
}) {
  return (
    <nav className="activity-bar" aria-label="Views">
      {VIEWS.map((v) => (
        <button key={v.id} className={`activity ${sidebarOpen && view === v.id ? "active" : ""}`} title={v.title}
          aria-label={v.title} aria-pressed={sidebarOpen && view === v.id} onClick={() => onView(v.id)}>
          <Codicon name={v.icon} />
          {badges[v.id] ? <span className="activity-badge">{badges[v.id]! > 99 ? "99+" : badges[v.id]}</span> : null}
          {v.id === "chats" && running && <span className="activity-running" title="A task is running" />}
        </button>
      ))}
      <div className="spacer" />
      <GearMenu entries={menu} />
    </nav>
  );
}

function GearMenu({ entries }: { entries: (MenuEntry | "-")[] }) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const close = useCallback(() => setOpen(false), []);
  useDismiss(box, open, close);
  return (
    <div className="gear" ref={box}>
      <button className={`activity ${open ? "active" : ""}`} title="Manage" aria-label="Manage" onClick={() => setOpen(!open)}>
        <Codicon name="settings-gear" />
      </button>
      {open && (
        <div className="menu gear-menu" role="menu">
          {entries.map((entry, i) => entry === "-" ? <div key={i} className="menu-separator" /> : (
            <button key={i} role="menuitem" className="menu-item" onClick={() => { setOpen(false); entry.onClick(); }}>
              <span className="menu-label">{entry.label}</span>
              {entry.keys && <span className="menu-keys">{entry.keys}</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/** A side bar view: its title in capitals, its actions on the right, then its contents. */
export function ViewPane({ title, actions, children }: { title: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <div className="view-pane">
      <div className="view-title">
        <h2>{title}</h2>
        <div className="view-actions">{actions}</div>
      </div>
      <div className="view-content">{children}</div>
    </div>
  );
}

/** A collapsible section inside a view, like "CHANGES" in Source Control. */
export function ViewSection({ title, count, actions, children, defaultOpen = true }: {
  title: ReactNode;
  count?: number;
  actions?: ReactNode;
  children: ReactNode;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <section className={`view-section ${open ? "open" : ""}`}>
      <div className="section-head" role="button" tabIndex={0} aria-expanded={open}
        onClick={() => setOpen(!open)} onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && setOpen(!open)}>
        <Codicon name={open ? "chevron-down" : "chevron-right"} />
        <span className="section-name">{title}</span>
        {count !== undefined && count > 0 && <span className="count-badge">{count}</span>}
        <div className="section-actions" onClick={(e) => e.stopPropagation()}>{actions}</div>
      </div>
      {open && <div className="section-body">{children}</div>}
    </section>
  );
}

export function ActionButton({ icon, title, onClick, disabled = false }: {
  icon: string; title: string; onClick: () => void; disabled?: boolean;
}) {
  return (
    <button className="action" title={title} aria-label={title} disabled={disabled} onClick={onClick}>
      <Codicon name={icon} />
    </button>
  );
}
