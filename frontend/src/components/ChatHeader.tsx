import type { ChatRecord, PendingBranch, RepoState } from "../api";
import { Codicon } from "./Codicon";

export interface NewChatOptions {
  isolate: boolean;
  base: string;
}

interface Props {
  chat: ChatRecord | null;
  pending: PendingBranch | null;
  repo: RepoState | null;
  options: NewChatOptions;
  onOptions: (options: NewChatOptions) => void;
  onOpenChanges: () => void;
}

/** Above the transcript, like an editor's breadcrumbs: which branch this chat works on, or how a new chat will start. */
export function ChatHeader({ chat, pending, repo, options, onOptions, onOpenChanges }: Props) {
  if (chat === null) {
    if (!repo?.repo) {
      return (
        <div className="chat-header">
          <span className="crumb"><Codicon name="comment-discussion" /> New chat</span>
          <span className="crumb dim">works directly in this folder (not a git repository)</span>
        </div>
      );
    }
    return (
      <div className="chat-header">
        <span className="crumb"><Codicon name="comment-discussion" /> New chat</span>
        <label className="toggle">
          <input type="checkbox" checked={options.isolate}
            onChange={(e) => onOptions({ ...options, isolate: e.target.checked })} />
          <span>work on a separate branch</span>
        </label>
        {options.isolate && (
          <label className="field">
            <span>from</span>
            <select value={options.base} onChange={(e) => onOptions({ ...options, base: e.target.value })}>
              <option value="">{repo.branch ?? "current branch"} (current)</option>
              {repo.branches.filter((b) => b !== repo.branch && !b.startsWith("la/")).map((b) => (
                <option key={b} value={b}>{b}</option>
              ))}
            </select>
          </label>
        )}
      </div>
    );
  }
  return (
    <div className="chat-header">
      <span className="crumb chat-name" title={chat.title}><Codicon name="comment-discussion" /> {chat.title || "(untitled)"}</span>
      {pending ? (
        <span className="crumb branch" title={`Worktree ${pending.checked_out ? "present" : "removed (kept branch)"}`}>
          <Codicon name="chevron-right" className="crumb-sep" />
          <Codicon name="git-branch" /> {pending.branch}
          <span className="dim"> ← {pending.base} · {pending.commits} commit{pending.commits === 1 ? "" : "s"} · {pending.files} file{pending.files === 1 ? "" : "s"}</span>
        </span>
      ) : (
        <span className="crumb dim">
          <Codicon name="chevron-right" className="crumb-sep" />
          {chat.branch ? `${chat.branch} (merged or discarded)` : "works directly in the folder"}
        </span>
      )}
      <div className="spacer" />
      {pending && (
        <button className="action labeled" onClick={onOpenChanges} title="Review what this chat changed, and merge it">
          <Codicon name="diff-multiple" /> Changes
        </button>
      )}
    </div>
  );
}
