import type { ReactNode } from "react";
import { REMOTE_NAME } from "../api";
import { Codicon } from "./Codicon";

/** The top strip: the folder menu on the left, the command center in the middle, layout toggles on the right. */
export function TitleBar({ project, folderName, onCommandCenter, sidebarOpen, onToggleSidebar, editorOpen, onToggleEditor }: {
  /** The folder button and its recent projects (see ProjectMenu.tsx). */
  project: ReactNode;
  folderName: string;
  onCommandCenter: () => void;
  sidebarOpen: boolean;
  onToggleSidebar: () => void;
  editorOpen: boolean;
  onToggleEditor: () => void;
}) {
  return (
    <header className="titlebar">
      <div className="titlebar-left">
        <img className="app-icon" src="icon.png" alt="" width={16} height={16} />
        <span className="app-name">LocalAgent</span>
        {project}
      </div>
      <button className="command-center" onClick={onCommandCenter} title="Search files by name (Ctrl+P), or type > for commands (Ctrl+Shift+P)">
        <Codicon name="search" />
        <span>{REMOTE_NAME ? `[${REMOTE_NAME}] ` : ""}{folderName || "LocalAgent"}</span>
      </button>
      <div className="titlebar-right">
        <button className="action" onClick={onToggleSidebar} aria-pressed={sidebarOpen}
          title="Toggle Primary Side Bar (Ctrl+B)" aria-label="Toggle Primary Side Bar">
          <Codicon name={sidebarOpen ? "layout-sidebar-left" : "layout-sidebar-left-off"} />
        </button>
        <button className="action" onClick={onToggleEditor} aria-pressed={editorOpen}
          title="Toggle Editor Area (files and diffs)" aria-label="Toggle Editor Area">
          <Codicon name={editorOpen ? "layout-sidebar-right" : "layout-sidebar-right-off"} />
        </button>
      </div>
    </header>
  );
}
