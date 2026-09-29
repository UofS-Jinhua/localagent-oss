import { useEffect, useMemo, useRef, useState, type MouseEvent, type RefObject } from "react";
import { api, projectFileUrl, type Changes, type FileContent, type FileSource } from "../api";
import type { EditorTab, OpenOptions, TabKind } from "../editorTabs";
import { basenameOf, dirnameOf, fileIcon } from "../fileIcons";
import { DiffEditorView, FileEditor, lineChanges, type DiffEditorHandle } from "./CodeEditor";
import { Codicon } from "./Codicon";
import { copyText } from "./CopyButton";
import { DiffView } from "./DiffView";

// The editor area, right of the chat: tabs of files, diffs and a chat's changes, like VS Code's editor group.

const STATUS_LABEL: Record<string, string> = {
  M: "Modified", A: "Added", D: "Deleted", T: "Type changed", U: "Untracked",
};

function size(bytes: number): string {
  return bytes < 1024 ? `${bytes} B` : bytes < 1024 * 1024 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1048576).toFixed(1)} MB`;
}

/** "(Working Tree)" for the checkout, the branch for a chat's tree: what the right-hand side of a diff is. */
function sideName(tab: EditorTab): string {
  return tab.source.chatId ? tab.branch || "chat" : "Working Tree";
}

function tabTitle(tab: EditorTab): string {
  if (tab.kind === "changes") return `Changes · ${tab.branch || "chat"}`;
  const name = basenameOf(tab.path);
  return tab.kind === "diff" ? `${name} (${sideName(tab)})` : name;
}

function useFile(source: FileSource, path: string, reload: number): { file: FileContent | null; error: string } {
  // Kept with the request it answers (not the path the server returns, whose case Windows may change).
  const key = `${source.workDir}\n${source.chatId ?? ""}\n${path}`;
  const [loaded, setLoaded] = useState<{ key: string; file: FileContent } | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let cancelled = false;
    setError("");
    api.file(source, path).then((file) => !cancelled && setLoaded({ key, file })).catch((e) => {
      if (cancelled) return;
      setLoaded(null);
      setError(String(e.message ?? e));
    });
    return () => {
      cancelled = true;
    };
  }, [key, reload]);
  return { file: loaded?.key === key ? loaded.file : null, error };
}

function ImagePreview({ source, path, version }: { source: FileSource; path: string; version: number }) {
  const [url, setUrl] = useState("");
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let revoked = false;
    let made = "";
    projectFileUrl(source, path).then((u) => {
      made = u;
      if (revoked) URL.revokeObjectURL(u);
      else setUrl(u);
    }).catch(() => setFailed(true));
    return () => {
      revoked = true;
      if (made) URL.revokeObjectURL(made);
    };
  }, [source.workDir, source.chatId, path, version]);
  if (failed) return <div className="editor-message">The image could not be loaded.</div>;
  return url ? <div className="image-preview"><img src={url} alt={path} /></div> : <div className="editor-message">Loading…</div>;
}

function Action({ icon, title, onClick, disabled = false, active = false }: {
  icon: string; title: string; onClick: () => void; disabled?: boolean; active?: boolean;
}) {
  return (
    <button className={`action ${active ? "checked" : ""}`} title={title} aria-label={title} disabled={disabled} onClick={onClick}>
      <Codicon name={icon} />
    </button>
  );
}

export function EditorGroup({ tabs, activeId, refreshKey, dark, sideBySide, onSideBySide, onActivate, onClose, onCloseAll, onPin, onOpen }: {
  tabs: EditorTab[];
  activeId: string | null;
  refreshKey: number;
  dark: boolean;
  sideBySide: boolean;
  onSideBySide: (on: boolean) => void;
  onActivate: (id: string) => void;
  onClose: (id: string) => void;
  onCloseAll: () => void;
  onPin: (id: string) => void;
  onOpen: (kind: TabKind, source: FileSource, path: string, options?: OpenOptions) => void;
}) {
  const active = tabs.find((t) => t.id === activeId) ?? tabs[tabs.length - 1] ?? null;
  const [reloads, setReloads] = useState(0);
  const strip = useRef<HTMLDivElement>(null);
  const diff = useRef<DiffEditorHandle>(null); // the shown diff, for the arrows right of the tabs

  // Two tabs with the same file name say which folder each is in, as VS Code does.
  const names = useMemo(() => {
    const count = new Map<string, number>();
    for (const t of tabs) count.set(tabTitle(t), (count.get(tabTitle(t)) ?? 0) + 1);
    return count;
  }, [tabs]);

  useEffect(() => {
    strip.current?.querySelector(".tab.active")?.scrollIntoView({ block: "nearest", inline: "nearest" });
  }, [active?.id]);

  if (!active) {
    return (
      <section className="editor-group">
        <div className="editor-watermark">
          <img src="icon.png" alt="" width={96} height={96} />
          <dl>
            <dt>Show All Commands</dt><dd><kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>P</kbd></dd>
            <dt>Go to File</dt><dd><kbd>Ctrl</kbd>+<kbd>P</kbd></dd>
            <dt>Toggle Side Bar</dt><dd><kbd>Ctrl</kbd>+<kbd>B</kbd></dd>
            <dt>Open a file</dt><dd>click it in the Explorer</dd>
          </dl>
        </div>
      </section>
    );
  }

  return (
    <section className="editor-group">
      <div className="tabs-bar">
        <div className="tabs" ref={strip} role="tablist" onWheel={(e) => {
          if (strip.current && e.deltaY) strip.current.scrollLeft += e.deltaY;
        }}>
          {tabs.map((tab) => {
            const title = tabTitle(tab);
            const icon = tab.kind === "changes" ? { icon: "diff-multiple", color: "var(--branch)" }
              : tab.kind === "diff" ? { icon: "git-compare", color: "var(--accent)" } : fileIcon(tab.path);
            const where = (names.get(title) ?? 0) > 1 ? dirnameOf(tab.path) : "";
            const close = (e: MouseEvent) => {
              e.stopPropagation();
              onClose(tab.id);
            };
            return (
              <div key={tab.id} role="tab" aria-selected={tab.id === active.id}
                className={`tab ${tab.id === active.id ? "active" : ""} ${tab.preview ? "preview" : ""}`}
                title={`${tab.path || title}${tab.source.chatId ? ` · ${tab.branch || "chat"}` : ""}`}
                onClick={() => onActivate(tab.id)} onDoubleClick={() => onPin(tab.id)}
                onMouseDown={(e) => e.button === 1 && e.preventDefault()}
                onAuxClick={(e) => e.button === 1 && close(e)}>
                <span className="tab-icon" style={{ color: icon.color }}><Codicon name={icon.icon} /></span>
                <span className="tab-label">{title}</span>
                {where && <span className="tab-description">{where}</span>}
                <button className="tab-close" title="Close" aria-label={`Close ${title}`} onClick={close}>
                  <Codicon name="close" />
                </button>
              </div>
            );
          })}
        </div>
        <div className="editor-actions" key={active.id}>
          <TabActions tab={active} sideBySide={sideBySide} onSideBySide={onSideBySide} onOpen={onOpen}
            onReload={() => setReloads((n) => n + 1)} onPrevious={() => diff.current?.previous()}
            onNext={() => diff.current?.next()} />
          <Action icon="close-all" title="Close All Editors" onClick={onCloseAll} />
        </div>
      </div>
      <Breadcrumbs tab={active} />
      <div className="editor-body">
        {active.kind === "changes"
          ? <ChangesBody key={active.id} chatId={active.source.chatId ?? ""} reload={refreshKey + reloads} />
          : <FileBody key={active.id} tab={active} reload={refreshKey + reloads} dark={dark} sideBySide={sideBySide}
              onOpen={onOpen} diffRef={diff} />}
      </div>
    </section>
  );
}

/** The buttons right of the tabs: they belong to the tab shown. */
function TabActions({ tab, sideBySide, onSideBySide, onOpen, onReload, onPrevious, onNext }: {
  tab: EditorTab;
  sideBySide: boolean;
  onSideBySide: (on: boolean) => void;
  onOpen: (kind: TabKind, source: FileSource, path: string, options?: OpenOptions) => void;
  onReload: () => void;
  onPrevious: () => void;
  onNext: () => void;
}) {
  const options = { pin: true, branch: tab.branch };
  return (
    <>
      {tab.kind === "diff" && (
        <>
          <Action icon="arrow-up" title="Previous Change" onClick={onPrevious} />
          <Action icon="arrow-down" title="Next Change" onClick={onNext} />
          <Action icon={sideBySide ? "diff-single" : "diff"} title={sideBySide ? "Inline View" : "Side by Side View"}
            onClick={() => onSideBySide(!sideBySide)} />
          <Action icon="go-to-file" title="Open File" onClick={() => onOpen("file", tab.source, tab.path, options)} />
        </>
      )}
      {tab.kind === "file" && (
        <Action icon="git-compare" title="Open Changes" onClick={() => onOpen("diff", tab.source, tab.path, options)} />
      )}
      {tab.path && <Action icon="copy" title="Copy Path" onClick={() => void copyText(tab.path)} />}
      <Action icon="refresh" title="Reload" onClick={onReload} />
    </>
  );
}

function Breadcrumbs({ tab }: { tab: EditorTab }) {
  const parts = tab.path ? tab.path.split("/") : [];
  return (
    <div className="breadcrumbs">
      <span className="crumb root" title={tab.source.chatId ? "The chat's own worktree" : tab.source.workDir}>
        <Codicon name={tab.source.chatId ? "git-branch" : "root-folder"} />
        {tab.source.chatId ? tab.branch || "chat" : basenameOf(tab.source.workDir)}
      </span>
      {parts.map((part, i) => (
        <span key={i} className="crumb">
          <Codicon name="chevron-right" className="crumb-sep" />
          {i === parts.length - 1 && <span style={{ color: fileIcon(part).color }}><Codicon name={fileIcon(part).icon} /></span>}
          {part}
        </span>
      ))}
      {tab.kind === "diff" && <span className="crumb dim">{tab.source.chatId ? "vs. where the chat started" : "vs. HEAD"}</span>}
    </div>
  );
}

function FileBody({ tab, reload, dark, sideBySide, onOpen, diffRef }: {
  tab: EditorTab;
  reload: number;
  dark: boolean;
  sideBySide: boolean;
  onOpen: (kind: TabKind, source: FileSource, path: string, options?: OpenOptions) => void;
  diffRef: RefObject<DiffEditorHandle | null>;
}) {
  const { file, error } = useFile(tab.source, tab.path, reload);
  const changes = useMemo(() => lineChanges(file?.diff ?? ""), [file]);

  if (error) return <div className="editor-message error"><Codicon name="error" /> {error}</div>;
  if (!file) return <div className="editor-message">Loading…</div>;

  const notes = [
    file.status && STATUS_LABEL[file.status],
    file.kind !== "missing" && size(file.size),
    file.encoding && file.encoding !== "utf-8" && file.encoding.toUpperCase(),
    file.truncated && "only the first part is shown",
  ].filter(Boolean);
  const footer = notes.length > 0 && <div className="editor-footer">{notes.join(" · ")}</div>;

  if (tab.kind === "diff") {
    if (!file.git) return <div className="editor-message">Not a git repository: there is nothing to compare with.</div>;
    if (!file.status) {
      return (
        <div className="editor-message">
          No changes in this file.{" "}
          <button className="link-button" onClick={() => onOpen("file", tab.source, tab.path, { pin: true, branch: tab.branch })}>Open the file</button>
        </div>
      );
    }
    const text = file.kind === "text" || file.kind === "missing";
    if (!text) {
      // A binary or image file: git's own summary of the change says as much as there is to say.
      return file.diff ? <div className="editor-scroll"><DiffView diff={file.diff} /></div>
        : <div className="editor-message">A new {file.kind} file that git has not seen yet.</div>;
    }
    return (
      <>
        {(file.truncated || file.base_truncated) && <div className="editor-banner warn">The file is large: only its first part is compared.</div>}
        <DiffEditorView ref={diffRef} viewKey={tab.id} path={tab.path} original={file.base_text}
          modified={file.kind === "missing" ? "" : file.text} sideBySide={sideBySide} dark={dark} />
        {footer}
      </>
    );
  }

  if (file.kind === "text") {
    return (
      <>
        <FileEditor viewKey={tab.id} path={tab.path} text={file.text} changes={changes} dark={dark} />
        {footer}
      </>
    );
  }
  if (file.kind === "image") {
    return <><div className="editor-scroll"><ImagePreview source={tab.source} path={tab.path} version={reload} /></div>{footer}</>;
  }
  if (file.kind === "binary") return <div className="editor-message">A binary file ({size(file.size)}); nothing to show as text.</div>;
  return (
    <div className="editor-message">
      This file was deleted.{" "}
      <button className="link-button" onClick={() => onOpen("diff", tab.source, tab.path, { pin: true, branch: tab.branch })}>See what it contained</button>
    </div>
  );
}

function ChangesBody({ chatId, reload }: { chatId: string; reload: number }) {
  const [changes, setChanges] = useState<Changes | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let cancelled = false;
    api.changes(chatId).then((c) => !cancelled && setChanges(c)).catch((e) => !cancelled && setError(String(e.message ?? e)));
    return () => {
      cancelled = true;
    };
  }, [chatId, reload]);
  if (error) return <div className="editor-message error"><Codicon name="error" /> {error}</div>;
  if (!changes) return <div className="editor-message">Loading…</div>;
  if (!changes.branch) return <div className="editor-message">This chat has no branch waiting to be merged.</div>;
  return (
    <div className="editor-scroll changes-body">
      <div className="changes-summary">
        <Codicon name="git-branch" /> <strong>{changes.branch}</strong> <span className="dim">→ {changes.base}</span>
      </div>
      {(changes.commits ?? []).length > 0 && (
        <div className="commits">
          {(changes.commits ?? []).map(([sha, subject]) => (
            <div key={sha} className="commit"><Codicon name="git-commit" /> <code>{sha}</code> {subject}</div>
          ))}
        </div>
      )}
      <DiffView diff={changes.diff ?? ""} />
      {changes.truncated && <div className="editor-banner warn">The diff is too long and was cut off here.</div>}
    </div>
  );
}
