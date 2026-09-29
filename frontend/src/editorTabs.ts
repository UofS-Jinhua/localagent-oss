import type { FileSource } from "./api";

// The editor area's tabs. A tab names what it shows; its contents are fetched when it is shown.
//   file     a file of a tree (the user's checkout, or a chat's worktree), changed lines marked
//   diff     the same file against the tree's base (HEAD, or where the chat started), side by side
//   changes  everything a chat's branch changed, file after file (what Merge would bring in)

export type TabKind = "file" | "diff" | "changes";

export interface EditorTab {
  id: string;
  kind: TabKind;
  /** The file, relative to the tree; "" for a changes tab. */
  path: string;
  source: FileSource;
  /** The chat's branch when the tree is a chat's worktree (or the changes tab's chat), for titles. */
  branch: string;
  /** Opened with a single click: the next single click replaces it (shown in italics). */
  preview: boolean;
}

export function tabId(kind: TabKind, source: FileSource, path: string): string {
  return `${kind}\n${source.chatId ?? `dir:${source.workDir}`}\n${path}`;
}

export interface OpenOptions {
  /** Keep the tab (a double click, or anything opened on purpose rather than while browsing). */
  pin?: boolean;
  branch?: string;
}

/**
 * Where a path the agent named (in a Read, Write or Edit) is, as a tree and a path in it. The agent works in its
 * chat's worktree (``<repo>/.la-worktrees/<chat>/…``) or in the folder itself; a relative path is relative to
 * whichever it worked in (``chat`` undefined). Null when the path is outside both.
 */
export function treePath(path: string, workDir: string, repoRoot: string | null): { path: string; chat?: string | null } | null {
  const norm = (p: string) => p.replace(/\\/g, "/").replace(/\/+$/, "");
  const same = (a: string, b: string) => a.toLowerCase() === b.toLowerCase(); // Windows ignores case
  const p = norm(path);
  if (!/^([a-zA-Z]:)?\//.test(p)) return { path: p.replace(/^\.\//, "") };
  const worktree = p.match(/^(.*)\/\.la-worktrees\/([^/]+)\/(.+)$/);
  if (worktree) {
    // A folder inside the repository is the same folder inside the worktree.
    const inside = repoRoot ? norm(workDir).slice(norm(repoRoot).length).replace(/^\//, "") : "";
    let rest = worktree[3];
    if (inside) {
      if (!same(rest.slice(0, inside.length + 1), `${inside}/`)) return null;
      rest = rest.slice(inside.length + 1);
    }
    return { path: rest, chat: worktree[2] };
  }
  const root = norm(workDir);
  return same(p.slice(0, root.length + 1), `${root}/`) ? { path: p.slice(root.length + 1), chat: null } : null;
}

/** The tab list after opening one: an open tab is reused, a preview tab is replaced by the next preview. */
export function withTab(tabs: EditorTab[], kind: TabKind, source: FileSource, path: string,
                        options: OpenOptions = {}): { tabs: EditorTab[]; id: string } {
  const id = tabId(kind, source, path);
  const existing = tabs.find((t) => t.id === id);
  if (existing) {
    return { id, tabs: options.pin && existing.preview ? tabs.map((t) => (t.id === id ? { ...t, preview: false } : t)) : tabs };
  }
  const tab: EditorTab = { id, kind, path, source, branch: options.branch ?? "", preview: !options.pin };
  const previewAt = tab.preview ? tabs.findIndex((t) => t.preview) : -1;
  if (previewAt >= 0) return { id, tabs: tabs.map((t, i) => (i === previewAt ? tab : t)) };
  return { id, tabs: [...tabs, tab] };
}
