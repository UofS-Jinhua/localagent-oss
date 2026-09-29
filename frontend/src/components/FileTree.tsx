import { useEffect, useRef, useState } from "react";
import { api, type FileSource, type TreeEntry, type TreeListing } from "../api";
import { basenameOf, fileIcon } from "../fileIcons";
import { ActionButton, ViewPane } from "./ActivityBar";
import { Codicon } from "./Codicon";

const STATUS_LABEL: Record<string, string> = {
  M: "Modified", A: "Added", D: "Deleted", T: "Type changed", U: "Untracked",
};

const INDENT = 12;

/** The Explorer: a folder's files and subfolders, opened one level at a time, coloured by their git status. */
export function FileTree({ source, sourceChoice, refreshKey, openPath, onOpen }: {
  source: FileSource;
  /** Lets the user switch between the folder and the selected chat's branch, when the chat has one. */
  sourceChoice?: { branch: string; fromChat: boolean; onChange: (fromChat: boolean) => void };
  refreshKey: number;
  /** The file shown in the editor area, when it is from this tree. */
  openPath: string | null;
  /** A single click previews the file; a double click keeps its tab. */
  onOpen: (path: string, pin: boolean) => void;
}) {
  const [dirs, setDirs] = useState<Record<string, TreeListing>>({});
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [error, setError] = useState("");
  const [reloads, setReloads] = useState(0);
  const sourceKey = `${source.workDir}\n${source.chatId ?? ""}`;
  const shownKey = useRef(sourceKey);

  const load = (path: string, current = () => true) => api.tree(source, path).then((listing) => {
    if (!current()) return; // the tree changed while this was on its way
    setDirs((shown) => ({ ...shown, [path]: listing }));
    setError("");
  });

  // Load the root and every open folder again: after a task, a merge, or the refresh button.
  // A different tree (another folder, or the chat's branch instead) starts collapsed.
  useEffect(() => {
    let cancelled = false;
    let paths = ["", ...expanded];
    if (shownKey.current !== sourceKey) {
      shownKey.current = sourceKey;
      paths = [""];
      setDirs({});
      setExpanded(new Set());
    }
    for (const path of paths) {
      load(path, () => !cancelled).catch((e) => {
        if (cancelled) return;
        // A folder that no longer exists just closes.
        if (path) setExpanded((current) => new Set([...current].filter((p) => p !== path)));
        else setError(String(e.message ?? e));
      });
    }
    return () => {
      cancelled = true;
    };
  }, [sourceKey, refreshKey, reloads]);

  const toggle = (path: string) => {
    const next = new Set(expanded);
    if (next.has(path)) next.delete(path);
    else {
      next.add(path);
      if (!dirs[path]) load(path).catch((e) => setError(String(e.message ?? e)));
    }
    setExpanded(next);
  };

  const root = dirs[""];
  const guides = (depth: number) => Array.from({ length: depth }, (_, i) => (
    <span key={i} className="indent-guide" style={{ left: 16 + i * INDENT }} />
  ));

  const note = (text: string, depth: number) => (
    <div className="tree-row note" style={{ paddingLeft: 8 + depth * INDENT + 16 }}>{guides(depth)}{text}</div>
  );

  const renderEntries = (path: string, depth: number) => {
    const listing = dirs[path];
    if (!listing) return note("Loading…", depth);
    return (
      <>
        {listing.entries.map((entry) => renderRow(entry, depth))}
        {listing.truncated && note("(more entries not shown)", depth)}
        {listing.entries.length === 0 && note("(empty)", depth)}
      </>
    );
  };

  const renderRow = (entry: TreeEntry, depth: number) => {
    const open = expanded.has(entry.path);
    const icon = entry.dir ? { icon: open ? "folder-opened" : "folder", color: "var(--folder)" } : fileIcon(entry.name);
    const status = entry.status;
    const classes = ["tree-row", entry.ignored ? "ignored" : "", entry.deleted ? "deleted" : "",
      status ? `git-${entry.dir && status === "M" ? "inside" : status}` : "", openPath === entry.path ? "selected" : ""].join(" ");
    const title = `${entry.path}${status ? ` · ${entry.dir && status === "M" ? "contains changes" : STATUS_LABEL[status] ?? status}` : ""}${entry.ignored ? " · ignored by git" : ""}`;
    return (
      <div key={entry.path} role="none">
        <div className={classes} role="treeitem" aria-expanded={entry.dir ? open : undefined} tabIndex={0}
          style={{ paddingLeft: 8 + depth * INDENT }} title={title}
          onClick={() => (entry.dir && !entry.deleted ? toggle(entry.path) : onOpen(entry.path, false))}
          onDoubleClick={() => !entry.dir && onOpen(entry.path, true)}
          onKeyDown={(e) => e.key === "Enter" && (entry.dir && !entry.deleted ? toggle(entry.path) : onOpen(entry.path, true))}>
          {guides(depth)}
          <span className="twistie">{entry.dir && <Codicon name={open ? "chevron-down" : "chevron-right"} />}</span>
          <span className="file-icon" style={{ color: icon.color }}><Codicon name={icon.icon} /></span>
          <span className="row-name">{entry.name}</span>
          {status && <span className={`status-letter git-${status}`}>{entry.dir && status === "M" ? "•" : status}</span>}
        </div>
        {entry.dir && open && renderEntries(entry.path, depth + 1)}
      </div>
    );
  };

  const rootName = source.chatId && sourceChoice ? sourceChoice.branch : basenameOf(source.workDir);
  return (
    <ViewPane title="Explorer" actions={<>
      <ActionButton icon="refresh" title="Refresh Explorer" onClick={() => setReloads((n) => n + 1)} />
      <ActionButton icon="collapse-all" title="Collapse Folders in Explorer" onClick={() => setExpanded(new Set())} />
    </>}>
      <div className="tree-root-head" title={root?.root}>
        <Codicon name="chevron-down" />
        <span className="tree-root-name">{rootName}</span>
        {sourceChoice && (
          <select value={sourceChoice.fromChat ? "chat" : "folder"} onClick={(e) => e.stopPropagation()}
            title={sourceChoice.fromChat ? `${sourceChoice.branch}: the files this chat works on` : "Your checkout"}
            onChange={(e) => sourceChoice.onChange(e.target.value === "chat")}>
            <option value="chat">chat branch</option>
            <option value="folder">your checkout</option>
          </select>
        )}
        {!sourceChoice && root && <span className="dim">{root.git ? root.label : "no git"}</span>}
      </div>
      {error && <div className="view-empty error"><Codicon name="error" /> {error}</div>}
      <div className="tree" role="tree">
        {root ? renderEntries("", 0) : !error && <div className="view-empty">Loading…</div>}
      </div>
    </ViewPane>
  );
}
