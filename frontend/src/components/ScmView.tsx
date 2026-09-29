import { useEffect, useState } from "react";
import { api, type ChangedFiles, type Changes, type FileSource, type MergeResult, type PendingBranch, type RepoState } from "../api";
import { basenameOf, dirnameOf, fileIcon } from "../fileIcons";
import { ActionButton, ViewPane, ViewSection } from "./ActivityBar";
import { Codicon } from "./Codicon";
import { parse } from "./DiffView";

// Source Control: what the selected chat's branch would bring in (and the buttons that decide it),
// then what is uncommitted in your own checkout. A file opens as a diff in the editor area.

const STATUS_LABEL: Record<string, string> = {
  M: "Modified", A: "Added", D: "Deleted", T: "Type changed", U: "Untracked",
};

interface Row {
  path: string;
  status: string;
  added?: number;
  removed?: number;
}

function ChangeRow({ row, active, onOpen }: { row: Row; active: boolean; onOpen: (path: string) => void }) {
  const folder = row.path.endsWith("/");
  const path = row.path.replace(/\/$/, "");
  const icon = folder ? { icon: "folder", color: "var(--dim)" } : fileIcon(path);
  return (
    <button className={`list-row change-row git-${row.status} ${active ? "selected" : ""}`} disabled={folder}
      title={folder ? `${row.path}: a new folder (open its files from the Explorer)` : `${row.path} · ${STATUS_LABEL[row.status] ?? row.status}`}
      onClick={() => onOpen(path)}>
      <span className="file-icon" style={{ color: icon.color }}><Codicon name={icon.icon} /></span>
      <span className="row-name">{basenameOf(path)}</span>
      <span className="row-description">{dirnameOf(path)}</span>
      {row.added !== undefined && (
        <span className="row-counts"><span className="add-count">+{row.added}</span> <span className="del-count">−{row.removed}</span></span>
      )}
      <span className={`status-letter git-${row.status}`}>{row.status}</span>
    </button>
  );
}

export function ScmView({ workDir, repo, chatTitle, pending, busy, refreshKey, activePath, onOpenDiff, onOpenChanges,
  onMerge, onKeep, onDiscard, onRefresh }: {
  workDir: string;
  repo: RepoState | null;
  chatTitle: string;
  /** The selected chat's branch, when it has one waiting. */
  pending: PendingBranch | null;
  /** The selected chat is running: its branch cannot be merged yet. */
  busy: boolean;
  refreshKey: number;
  /** The file shown in the editor area, as "<chat or empty>\n<path>", to mark its row. */
  activePath: string;
  onOpenDiff: (source: FileSource, path: string, branch: string) => void;
  onOpenChanges: (chatId: string, branch: string) => void;
  onMerge: () => Promise<MergeResult>;
  onKeep: () => Promise<unknown>;
  onDiscard: () => Promise<unknown>;
  onRefresh: () => void;
}) {
  const [chatChanges, setChatChanges] = useState<Changes | null>(null);
  const [checkout, setCheckout] = useState<ChangedFiles | null>(null);
  const [working, setWorking] = useState("");
  const [message, setMessage] = useState<{ tone: string; text: string } | null>(null);
  const chatId = pending?.chat_id ?? null;

  useEffect(() => {
    setMessage(null);
  }, [chatId]);

  useEffect(() => {
    if (!chatId) return setChatChanges(null);
    let cancelled = false;
    api.changes(chatId).then((c) => !cancelled && setChatChanges(c)).catch(() => !cancelled && setChatChanges(null));
    return () => {
      cancelled = true;
    };
  }, [chatId, refreshKey]);

  useEffect(() => {
    if (!workDir || !repo?.repo) return setCheckout(null);
    let cancelled = false;
    api.treeChanges({ workDir, chatId: null }).then((c) => !cancelled && setCheckout(c)).catch(() => !cancelled && setCheckout(null));
    return () => {
      cancelled = true;
    };
  }, [workDir, repo?.repo, refreshKey]);

  const act = async (label: string, action: () => Promise<unknown>) => {
    setWorking(label);
    setMessage(null);
    try {
      const result = (await action()) as Partial<MergeResult> | undefined;
      if (result && "status" in result && result.status) {
        const tone = result.status === "merged" || result.status === "nothing" ? "ok" : "error";
        setMessage({ tone, text: [result.message, ...(result.notes ?? [])].join(" ") });
      } else {
        const notes = result && "notes" in result ? result.notes ?? [] : [];
        const done = label === "keep" ? "Kept the branch; its worktree folder was removed." : "Discarded the branch.";
        setMessage({ tone: notes.length ? "warn" : "ok", text: [done, ...notes].join(" ") });
      }
    } catch (e) {
      setMessage({ tone: "error", text: String((e as Error).message ?? e) });
    } finally {
      setWorking("");
    }
  };

  if (!workDir) return <ViewPane title="Source Control"><div className="view-empty">Open a folder first.</div></ViewPane>;
  if (repo && !repo.repo) {
    return (
      <ViewPane title="Source Control">
        <div className="view-empty">
          This folder is not a git repository. The agent edits its files in place, so there is no branch to review.
        </div>
      </ViewPane>
    );
  }

  const chatRows: Row[] = parse(chatChanges?.diff ?? "").map((f) => ({ path: f.path, status: f.status, added: f.added, removed: f.removed }));
  const checkoutRows: Row[] = checkout?.files ?? [];
  const chatSource: FileSource = { workDir, chatId };

  return (
    <ViewPane title="Source Control" actions={<ActionButton icon="refresh" title="Refresh" onClick={onRefresh} />}>
      {pending && (
        <ViewSection title={<><Codicon name="git-branch" /> {pending.branch}</>}
          actions={<ActionButton icon="diff-multiple" title="Open All Changes" onClick={() => onOpenChanges(pending.chat_id, pending.branch)} />}>
          <div className="scm-intro" title={chatTitle}>
            {chatTitle || "This chat"}: {pending.commits} commit{pending.commits === 1 ? "" : "s"} on its own branch, from {pending.base}.
          </div>
          <div className="scm-actions">
            <button className="primary wide" disabled={busy || !!working} onClick={() => act("merge", onMerge)}
              title={`Squash the chat's commits into ${pending.base} as one commit`}>
              <Codicon name="git-merge" /> {working === "merge" ? "Merging…" : `Merge into ${pending.base}`}
            </button>
            <div className="scm-secondary">
              <button disabled={busy || !!working} onClick={() => act("keep", onKeep)}
                title="Keep the branch for later and remove its worktree folder">
                <Codicon name="archive" /> Keep
              </button>
              <button className="danger" disabled={busy || !!working}
                onClick={() => { if (confirm(`Throw away ${pending.branch} and all its changes?`)) void act("discard", onDiscard); }}
                title="Throw the branch and its changes away">
                <Codicon name="discard" /> Discard
              </button>
            </div>
            {busy && <div className="scm-note warn">The chat is running; its branch can be merged when it finishes.</div>}
            {message && <div className={`scm-note ${message.tone}`}>{message.text}</div>}
          </div>
          <ViewSection title="Changes" count={chatRows.length}>
            {!chatChanges ? <div className="view-empty">Loading…</div>
              : chatRows.length === 0 ? <div className="view-empty">No changes yet.</div>
              : chatRows.map((row) => (
                <ChangeRow key={row.path} row={row} active={activePath === `${chatId}\n${row.path}`}
                  onOpen={(path) => (pending.checked_out ? onOpenDiff(chatSource, path, pending.branch)
                    : onOpenChanges(pending.chat_id, pending.branch))} />
              ))}
            {chatChanges?.truncated && <div className="view-empty">(the diff was cut off; more files may follow)</div>}
          </ViewSection>
          {(chatChanges?.commits ?? []).length > 0 && (
            <ViewSection title="Commits" count={chatChanges!.commits!.length} defaultOpen={false}>
              {chatChanges!.commits!.map(([sha, subject]) => (
                <div key={sha} className="list-row commit-row" title={`${sha} ${subject}`}>
                  <Codicon name="git-commit" /> <span className="row-name">{subject}</span> <code className="row-description">{sha}</code>
                </div>
              ))}
            </ViewSection>
          )}
        </ViewSection>
      )}
      <ViewSection title={<><Codicon name="repo" /> {repo?.branch ?? "Your checkout"}</>} count={checkoutRows.length}>
        <div className="scm-intro">Uncommitted changes in your own checkout (the chats never touch it).</div>
        {!checkout ? <div className="view-empty">Loading…</div>
          : checkoutRows.length === 0 ? <div className="view-empty">Nothing uncommitted.</div>
          : checkoutRows.map((row) => (
            <ChangeRow key={row.path} row={row} active={activePath === `\n${row.path}`}
              onOpen={(path) => onOpenDiff({ workDir, chatId: null }, path, "")} />
          ))}
        {checkout?.truncated && <div className="view-empty">(more changes not shown)</div>}
      </ViewSection>
    </ViewPane>
  );
}
