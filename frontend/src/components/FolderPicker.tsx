import { useEffect, useState } from "react";
import { api, type DirListing } from "../api";
import { Icon } from "./Icon";

/** Folder browser for browser mode (the desktop window uses the native dialog instead). */
export function FolderPicker({ start, onPick, onClose }: { start: string; onPick: (path: string) => void; onClose: () => void }) {
  const [listing, setListing] = useState<DirListing | null>(null);
  const [error, setError] = useState("");
  const [recent, setRecent] = useState<string[]>([]);

  const open = (path: string) => {
    setError("");
    api.fs(path).then((l) => {
      setListing(l);
      if (l.recent) setRecent(l.recent);
    }).catch((e) => setError(String(e.message ?? e)));
  };

  useEffect(() => {
    api.fs("").then((l) => setRecent(l.recent ?? [])).catch(() => undefined);
    open(start);
  }, [start]);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal folder-picker" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <strong>Choose a working folder</strong>
          <div className="spacer" />
          <button className="icon-button" onClick={onClose}><Icon name="x" /></button>
        </div>
        <div className="path-row">
          <button onClick={() => listing?.parent !== null && open(listing?.parent ?? "")} disabled={!listing?.path}>
            <Icon name="up" /> Up
          </button>
          <code className="current-path">{listing?.path || "Places"}</code>
        </div>
        {error && <div className="info tone-error">{error}</div>}
        <div className="folder-columns">
          {recent.length > 0 && (
            <div className="recent">
              <div className="section-title">Recent</div>
              {recent.map((path) => (
                <button key={path} className="folder-entry recent-entry" onClick={() => onPick(path)} title={path}>
                  <strong>{path.split(/[\\/]/).filter(Boolean).pop()}</strong>
                  <span className="dim small">{path}</span>
                </button>
              ))}
            </div>
          )}
          <div className="entries">
            {listing?.entries.map((entry) => (
              <button key={entry.path} className="folder-entry" onDoubleClick={() => onPick(entry.path)} onClick={() => open(entry.path)}>
                <Icon name={entry.repo ? "branch" : "folder"} size={14} /> {entry.name}
              </button>
            ))}
            {listing && listing.entries.length === 0 && <div className="empty">No subfolders.</div>}
          </div>
        </div>
        <div className="modal-actions">
          <span className="dim small">Click to open, double-click to choose.</span>
          <div className="spacer" />
          <button onClick={onClose}>Cancel</button>
          <button className="primary" disabled={!listing?.path} onClick={() => listing?.path && onPick(listing.path)}>
            Use this folder
          </button>
        </div>
      </div>
    </div>
  );
}
