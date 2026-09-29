import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api, type ChatRecord } from "../api";
import { CleanupDialog } from "./CleanupDialog";
import { Icon } from "./Icon";
import { useDismiss } from "./Menu";

export function basename(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] ?? path;
}

/** The folder button: recent projects (kept by the server, newest first), another folder, or the scratch folder. */
export function ProjectMenu({ workDir, recent, scratch, onOpen, onBrowse, onForget }: {
  workDir: string;
  recent: string[];
  scratch: string;
  onOpen: (path: string) => void;
  onBrowse: () => void;
  onForget: (path: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [orphans, setOrphans] = useState<ChatRecord[]>([]); // chats whose folder is gone
  const [cleaning, setCleaning] = useState(false);
  const box = useRef<HTMLDivElement>(null);
  const close = useCallback(() => setOpen(false), []);
  useDismiss(box, open, close);
  const isScratch = !!workDir && workDir === scratch;

  useEffect(() => {
    if (open) api.orphanedChats().then(setOrphans).catch(() => setOrphans([]));
  }, [open]);

  const pick = (action: () => void) => {
    setOpen(false);
    action();
  };

  return (
    <div className="project-menu" ref={box}>
      <button className="folder-button" onClick={() => setOpen(!open)} aria-expanded={open}
        title={workDir || "Choose a working folder"}>
        <Icon name="folder" />
        <span>{!workDir ? "Choose a folder" : isScratch ? "scratch (no project)" : basename(workDir)}</span>
        <span className={`menu-chev ${open ? "open" : ""}`}><Icon name="chevron" size={12} /></span>
      </button>
      {open && (
        <div className="menu project-list">
          <div className="menu-heading">Recent projects</div>
          {recent.length === 0 && <div className="menu-empty dim small">Folders you open show up here.</div>}
          {recent.map((path) => (
            <div key={path} className={`recent-project ${path === workDir ? "selected" : ""}`}>
              <button className="menu-item" onClick={() => pick(() => onOpen(path))} title={path}>
                <Icon name="folder" size={14} />
                <span className="recent-text">
                  <strong>{basename(path)}</strong>
                  <span className="dim small">{path}</span>
                </span>
                {path === workDir && <Icon name="check" size={14} />}
              </button>
              {path !== workDir && (
                <button className="icon-button recent-forget" title="Remove from this list (its chats are kept)"
                  onClick={() => onForget(path)}>
                  <Icon name="x" size={12} />
                </button>
              )}
            </div>
          ))}
          <div className="menu-separator" />
          <button className="menu-item" onClick={() => pick(onBrowse)}>
            <Icon name="plus" size={14} /> Open another folder…
          </button>
          {scratch && !isScratch && (
            <button className="menu-item" onClick={() => pick(() => onOpen(scratch))}
              title="A folder for questions and one-off scripts; never a git repository">
              <Icon name="file" size={14} /> Scratch folder (no project)
            </button>
          )}
          {orphans.length > 0 && (
            <button className="menu-item" onClick={() => pick(() => setCleaning(true))}
              title="Chats whose folder was deleted or moved: review them and delete the ones you no longer need">
              <Icon name="trash" size={14} /> Clean up chats of deleted folders ({orphans.length})
            </button>
          )}
        </div>
      )}
      {cleaning && createPortal(
        <CleanupDialog chats={orphans} onClose={() => setCleaning(false)}
          onDone={() => { setCleaning(false); setOrphans([]); }} />,
        document.body,
      )}
    </div>
  );
}
