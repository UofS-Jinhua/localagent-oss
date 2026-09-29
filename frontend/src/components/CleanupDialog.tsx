import { useMemo, useState } from "react";
import { api, type ChatRecord } from "../api";
import { Icon } from "./Icon";

/**
 * Chats whose folder no longer exists, grouped by folder and all ticked; the ticked ones are deleted on
 * request. Never automatic: a folder that is only out of reach for now looks exactly like a deleted one.
 */
export function CleanupDialog({ chats, onClose, onDone }: {
  chats: ChatRecord[];
  onClose: () => void;
  onDone: (deleted: number) => void;
}) {
  const [picked, setPicked] = useState<Set<string>>(() => new Set(chats.map((c) => c.id)));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const folders = useMemo(() => {
    const byFolder = new Map<string, ChatRecord[]>();
    for (const chat of chats) byFolder.set(chat.work_dir, [...(byFolder.get(chat.work_dir) ?? []), chat]);
    return [...byFolder.entries()];
  }, [chats]);

  const toggle = (ids: string[], on: boolean) => {
    const next = new Set(picked);
    for (const id of ids) {
      if (on) next.add(id);
      else next.delete(id);
    }
    setPicked(next);
  };

  const remove = () => {
    setBusy(true);
    setError("");
    api.deleteOrphanedChats([...picked])
      .then((result) => onDone(result.chats))
      .catch((e) => {
        setError(String(e.message ?? e));
        setBusy(false);
      });
  };

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal cleanup-dialog" role="dialog" aria-labelledby="cleanup-title" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <strong id="cleanup-title">Chats of folders that no longer exist</strong>
          <div className="spacer" />
          <button className="icon-button" onClick={onClose}><Icon name="x" /></button>
        </div>
        <p className="dim small cleanup-note">
          These folders are gone, so their chats cannot be opened any more. If a folder is only out of reach right
          now (an unplugged disk, a network drive, an expired login), untick it: its chats come back with the
          folder. Deleting removes the transcripts; git branches are left alone.
        </p>
        <div className="cleanup-list">
          {folders.map(([folder, list]) => (
            <div key={folder} className="cleanup-folder">
              <label className="cleanup-row cleanup-folder-head">
                <input type="checkbox" checked={list.every((c) => picked.has(c.id))}
                  onChange={(e) => toggle(list.map((c) => c.id), e.target.checked)} />
                <Icon name="folder" size={14} />
                <strong>{folder.split(/[\\/]/).filter(Boolean).pop() ?? folder}</strong>
                <span className="dim small cleanup-path" title={folder}>{folder}</span>
              </label>
              {list.map((chat) => (
                <label key={chat.id} className="cleanup-row cleanup-chat">
                  <input type="checkbox" checked={picked.has(chat.id)} onChange={(e) => toggle([chat.id], e.target.checked)} />
                  <span className="cleanup-title">{chat.title || "(untitled)"}</span>
                  <span className="dim small">
                    {chat.task_count} task{chat.task_count === 1 ? "" : "s"} · {new Date(chat.created * 1000).toLocaleDateString()}
                  </span>
                </label>
              ))}
            </div>
          ))}
        </div>
        {error && <div className="info tone-error">{error}</div>}
        <div className="modal-actions">
          <div className="spacer" />
          <button onClick={onClose}>Cancel</button>
          <button className="danger" disabled={busy || picked.size === 0} onClick={remove}>
            <Icon name="trash" size={14} /> Delete {picked.size} chat{picked.size === 1 ? "" : "s"}
          </button>
        </div>
      </div>
    </div>
  );
}
