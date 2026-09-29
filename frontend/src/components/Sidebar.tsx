import type { ChatRecord, PendingBranch } from "../api";
import { ActionButton, ViewPane } from "./ActivityBar";
import { Codicon } from "./Codicon";

function when(timestamp: number): string {
  const date = new Date(timestamp * 1000);
  const today = new Date();
  return date.toDateString() === today.toDateString()
    ? date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : date.toLocaleDateString([], { month: "short", day: "numeric" });
}

/** The Chats view: the folder's chats, newest first, and a new one. */
export function ChatsView({ chats, pending, selected, runningChat, onSelect, onDelete, onRefresh }: {
  chats: ChatRecord[];
  pending: PendingBranch[];
  selected: string | null;
  runningChat: string | null;
  onSelect: (id: string | null) => void;
  onDelete: (chat: ChatRecord) => void;
  onRefresh: () => void;
}) {
  const pendingByChat = new Map(pending.map((p) => [p.chat_id, p]));
  return (
    <ViewPane title="Chats" actions={<>
      <ActionButton icon="add" title="New Chat" onClick={() => onSelect(null)} />
      <ActionButton icon="refresh" title="Refresh" onClick={onRefresh} />
    </>}>
      <div className="list chat-list" role="listbox" aria-label="Chats in this folder">
        <button role="option" aria-selected={selected === null} className={`list-row new-chat ${selected === null ? "selected" : ""}`}
          onClick={() => onSelect(null)}>
          <Codicon name="add" /> <span>New chat</span>
        </button>
        {chats.length === 0 && <div className="view-empty">No chats in this folder yet.</div>}
        {chats.map((chat) => {
          const branch = pendingByChat.get(chat.id);
          const running = runningChat === chat.id;
          return (
            <div key={chat.id} role="option" aria-selected={selected === chat.id} tabIndex={0}
              className={`list-row chat-row ${selected === chat.id ? "selected" : ""}`}
              onClick={() => onSelect(chat.id)} onKeyDown={(e) => e.key === "Enter" && onSelect(chat.id)}>
              <span className="chat-icon">
                {running ? <Codicon name="loading" spin title="running" /> : <Codicon name="comment" />}
              </span>
              <span className="chat-text">
                <span className="chat-title">{chat.title || chat.last_task || "(untitled)"}</span>
                <span className="chat-meta">
                  {when(chat.created)} · {chat.task_count} task{chat.task_count === 1 ? "" : "s"}
                  {branch ? (
                    <span className="chip branch" title={`${branch.branch} ← ${branch.base}`}>
                      <Codicon name="git-branch" /> {branch.commits}
                    </span>
                  ) : chat.branch ? <span className="chip muted" title={chat.branch}>closed</span> : null}
                </span>
              </span>
              {!running && (
                <span className="chat-actions">
                  <button className="action" title="Delete Chat" aria-label="Delete this chat"
                    onClick={(e) => { e.stopPropagation(); onDelete(chat); }}>
                    <Codicon name="trash" />
                  </button>
                </span>
              )}
            </div>
          );
        })}
      </div>
    </ViewPane>
  );
}
