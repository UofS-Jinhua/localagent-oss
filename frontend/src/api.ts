// REST and WebSocket access to the local LocalAgent server.
// The token arrives in the URL fragment (#token=...), which browsers never send to servers.

export type AgentEvent = { type: string; [key: string]: any };

export interface PendingBranch {
  chat_id: string;
  branch: string;
  base: string;
  commits: number;
  files: number;
  checked_out: boolean;
}

export interface RepoState {
  repo: string | null;
  branch: string | null;
  dirty_files: number;
  pending: PendingBranch[];
  branches: string[];
}

export interface ChatRecord {
  id: string;
  created: number;
  work_dir: string;
  title: string;
  task_count: number;
  last_task: string;
  last_reason: string;
  branch: string;
  base: string;
}

export interface TaskRecord {
  id: string;
  chat_id: string;
  created: number;
  finished: number | null;
  task: string;
  result: string;
  success: boolean | null;
  reason: string;
  model: string;
  steps: number;
  events: AgentEvent[];
}

export interface ModelInfo {
  name: string;
  provider: string;
  context_length: number;
  thinking: string;
  notes: string;
  vision: boolean;
  switchable: boolean;
  local: boolean;
  loaded: boolean; // the server currently holds it in memory (LM Studio)
}

export interface ModelsPayload {
  error?: string;
  models: ModelInfo[];
  roles: Record<string, { model: string; thinking: boolean | null }>;
  /** What the "auto" role target resolves to right now (the model the server has loaded). */
  auto_model?: string | null;
}

export interface AttachmentInfo {
  id: string;
  name: string;
  mime: string;
  kind: "image" | "pdf" | "text" | "file";
  size: number;
  pages: number;
  chars: number;
  lines: number;
  path?: string; // where the agent finds it, in events
}

export interface ProviderView {
  name: string;
  base_url: string;
  type: string;
  api_key_env: string;
  has_key: boolean;
  max_concurrency: number;
  discovery: string;
  local: boolean;
  timeout: number;
}

export interface ModelSpec {
  name: string;
  provider: string;
  model_id: string;
  context_length: number;
  continue_at: number;
  thinking: "toggle" | "always" | "never";
  thinking_off: Record<string, unknown>;
  max_output_tokens: number;
  max_output_tokens_first: number; // the first step of a session (planning); 0 = the same
  max_thinking_tokens: number; // how far one reply may think before it is stopped; 0 = unbounded
  price_input: number; // USD per million tokens
  price_output: number;
  extra_params: Record<string, unknown>;
  notes: string;
  vision: boolean;
  switchable: boolean;
  max_subtask_context: number;
  max_parallel_subtasks: number;
  local?: boolean;
  loaded?: boolean | null;
  registered?: boolean;
}

export interface Preset {
  id: string;
  label: string;
  base_url: string;
  api_key_env: string;
  max_concurrency: number;
  discovery: string;
  key_url: string;
  note: string;
}

export interface RegistryView {
  path: string;
  providers: ProviderView[];
  models: ModelSpec[];
  roles: Record<string, { model: string; thinking: boolean | null }>;
  role_names: string[];
  thinking_modes: string[];
  presets: Preset[];
  auto_model: string | null;
  auto_label: string; // "auto"
}

export interface ProbeResult {
  name: string;
  ok: boolean;
  detail: string;
}

export interface Changes {
  branch: string | null;
  base?: string;
  checked_out?: boolean;
  commits?: [string, string][];
  stat?: string;
  diff?: string;
  truncated?: boolean;
}

export interface MergeResult {
  status: "merged" | "nothing" | "conflict" | "failed";
  message: string;
  commit: string | null;
  notes: string[];
}

export interface DirListing {
  path: string;
  parent: string | null;
  entries: { name: string; path: string; repo: boolean }[];
  recent?: string[];
  repo?: boolean;
}

export interface RecentFolders {
  folders: string[];
  scratch: string;
}

/** Which tree the Files view shows: the folder itself, or a chat's worktree (``chatId``). */
export interface FileSource {
  workDir: string;
  chatId: string | null;
}

/** Git status of a path: M, A, D, T, or U (untracked); "" when unchanged or outside git. */
export interface TreeEntry {
  name: string;
  path: string;
  dir: boolean;
  size: number;
  status: string;
  ignored: boolean;
  deleted: boolean;
}

export interface TreeListing {
  root: string;
  git: boolean;
  label: string;
  entries: TreeEntry[];
  truncated: boolean;
}

export interface ChangedFiles {
  root: string;
  git: boolean;
  label: string;
  files: { path: string; status: string }[];
  truncated: boolean;
}

export interface FileContent {
  path: string;
  root: string;
  git: boolean;
  label: string;
  status: string;
  size: number;
  kind: "text" | "image" | "binary" | "missing";
  text: string;
  encoding: string;
  truncated: boolean;
  diff: string;
  diff_truncated: boolean;
  /** The text at the base (HEAD, or where the chat started), for a side-by-side diff; "" when there was none. */
  base_text: string;
  base_truncated: boolean;
}

function readFragment(): URLSearchParams {
  return new URLSearchParams(window.location.hash.slice(1));
}

const fragment = readFragment();

function remembered(key: string, value: string | null): string {
  try {
    if (value) sessionStorage.setItem(key, value);
    return value ?? sessionStorage.getItem(key) ?? "";
  } catch {
    return value ?? "";
  }
}

export const TOKEN = remembered("la-token", fragment.get("token"));
export const INITIAL_DIR = fragment.get("dir") ?? "";
/** Set when this page is another machine's UI, opened from the machine switcher of this computer's app. */
export const REMOTE_NAME = fragment.get("remote") ?? "";

async function request<T>(method: string, path: string, query?: Record<string, string>, body?: unknown): Promise<T> {
  const url = new URL(`/api${path}`, window.location.origin);
  for (const [k, v] of Object.entries(query ?? {})) url.searchParams.set(k, v);
  const headers: Record<string, string> = { Authorization: `Bearer ${TOKEN}` };
  let payload: BodyInit | undefined;
  if (body instanceof Blob) {
    payload = body;
    headers["Content-Type"] = body.type || "application/octet-stream";
  } else if (body !== undefined) {
    payload = JSON.stringify(body);
    headers["Content-Type"] = "application/json";
  }
  const response = await fetch(url, { method, headers, body: payload });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch {
      /* not JSON */
    }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return response.json() as Promise<T>;
}

const blobUrls = new Map<string, Promise<string>>();

/** An object URL for a stored attachment (fetched with the token header, so the token never appears in a URL). */
export function attachmentUrl(id: string): Promise<string> {
  let url = blobUrls.get(id);
  if (!url) {
    url = fetch(`/api/attachments/${id}`, { headers: { Authorization: `Bearer ${TOKEN}` } })
      .then((r) => {
        if (!r.ok) throw new Error(r.statusText);
        return r.blob();
      })
      .then((blob) => URL.createObjectURL(blob));
    url.catch(() => blobUrls.delete(id));
    blobUrls.set(id, url);
  }
  return url;
}

const enc = encodeURIComponent;

function sourceQuery(source: FileSource, path: string): Record<string, string> {
  return source.chatId ? { chat_id: source.chatId, path } : { work_dir: source.workDir, path };
}

/** An object URL for an image in the project tree (fetched with the token header, like attachments). */
export async function projectFileUrl(source: FileSource, path: string): Promise<string> {
  const url = new URL("/api/file/raw", window.location.origin);
  for (const [k, v] of Object.entries(sourceQuery(source, path))) url.searchParams.set(k, v);
  const response = await fetch(url, { headers: { Authorization: `Bearer ${TOKEN}` } });
  if (!response.ok) throw new Error(response.statusText);
  return URL.createObjectURL(await response.blob());
}

export const api = {
  upload: (file: Blob, name: string) => request<AttachmentInfo>("POST", "/attachments", { name }, file),
  registry: () => request<RegistryView>("GET", "/registry"),
  remotes: () => request<RemotesView>("GET", "/remotes"),
  putRemote: (name: string, body: { host: string; forwards: string[]; ui: string; commands: Record<string, string> }) =>
    request<RemotesView>("PUT", `/remotes/${enc(name)}`, undefined, body),
  deleteRemote: (name: string) => request<RemotesView>("DELETE", `/remotes/${enc(name)}`),
  connectRemote: (name: string) => request<RemotesView>("POST", `/remotes/${enc(name)}/connect`),
  /** Run one of the machine's named commands over ssh. Only names it already has are accepted. */
  runRemote: (name: string, command: string) => request<RemoteRun>("POST", `/remotes/${enc(name)}/run`, undefined, { command }),
  disconnectRemote: (name: string) => request<RemotesView>("POST", `/remotes/${enc(name)}/disconnect`),
  /** Hand ssh the answer (null cancels). It goes straight to the waiting ssh process and is not kept. */
  answerSshPrompt: (id: string, answer: string | null) =>
    request<{ ok: boolean }>("POST", `/ssh-prompts/${enc(id)}`, undefined, answer === null ? { cancel: true } : { answer }),
  skills: (workDir: string) => request<SkillList>("GET", "/skills", { work_dir: workDir }),
  web: () => request<WebView>("GET", "/web"),
  putWeb: (body: Record<string, unknown>) => request<WebView>("PUT", "/web", undefined, body),
  tryWebSearch: (query: string) => request<{ results: { title: string; url: string; snippet: string }[] }>(
    "POST", "/web/search", undefined, { query }),
  putProvider: (name: string, body: Record<string, unknown>) => request<RegistryView>("PUT", `/providers/${enc(name)}`, undefined, body),
  deleteProvider: (name: string, forgetKey: boolean) =>
    request<RegistryView>("DELETE", `/providers/${enc(name)}`, { forget_key: String(forgetKey) }),
  discover: (name: string) => request<{ models: ModelSpec[] }>("GET", `/providers/${enc(name)}/discover`),
  putModel: (name: string, body: Partial<ModelSpec>) => request<RegistryView>("PUT", `/models/${enc(name)}`, undefined, body),
  deleteModel: (name: string) => request<RegistryView>("DELETE", `/models/${enc(name)}`),
  putRoles: (body: Record<string, { model: string; thinking: boolean | null } | null>) =>
    request<RegistryView>("PUT", "/roles", undefined, body),
  testModel: (name: string) => request<{ results: ProbeResult[] }>("POST", `/models/${enc(name)}/test`),
  scratch: () => request<{ path: string }>("GET", "/scratch"),
  files: (workDir: string) => request<{ files: string[]; truncated: boolean }>("GET", "/files", { work_dir: workDir }),
  state: () => request<{ busy: boolean; chat_id?: string; version: string }>("GET", "/state"),
  models: () => request<ModelsPayload>("GET", "/models"),
  repo: (workDir: string) => request<RepoState>("GET", "/repo", { work_dir: workDir }),
  fs: (path: string) => request<DirListing>("GET", "/fs", { path }),
  /** What the page remembers between starts; the desktop window's storage cannot (a new port each time). */
  settings: () => request<Record<string, any>>("GET", "/settings"),
  putSettings: (values: Record<string, unknown>) => request<Record<string, any>>("PUT", "/settings", undefined, values),
  recent: () => request<RecentFolders>("GET", "/recent"),
  /** Put a folder first on the recent list (the next start opens it). */
  openFolder: (path: string) => request<RecentFolders>("POST", "/recent", undefined, { path }),
  forgetFolder: (path: string) => request<RecentFolders>("DELETE", "/recent", { path }),
  tree: (source: FileSource, path: string) => request<TreeListing>("GET", "/tree", sourceQuery(source, path)),
  treeChanges: (source: FileSource) => request<ChangedFiles>("GET", "/tree/changes",
    source.chatId ? { chat_id: source.chatId } : { work_dir: source.workDir }),
  file: (source: FileSource, path: string) => request<FileContent>("GET", "/file", sourceQuery(source, path)),
  chats: (workDir: string) => request<ChatRecord[]>("GET", "/chats", { work_dir: workDir }),
  orphanedChats: () => request<ChatRecord[]>("GET", "/orphaned-chats"),
  deleteOrphanedChats: (ids: string[]) =>
    request<{ chats: number; tasks: number; attachments: number }>("POST", "/orphaned-chats/delete", undefined, { ids }),
  chat: (id: string) => request<{ chat: ChatRecord; tasks: TaskRecord[] }>("GET", `/chats/${id}`),
  changes: (id: string) => request<Changes>("GET", `/chats/${id}/changes`),
  /** Delete a chat and its transcript; a chat whose branch still holds commits needs discardBranch. */
  deleteChat: (id: string, discardBranch = false) =>
    request<{ tasks: number; branch: string | null; notes: string[] }>(
      "DELETE", `/chats/${id}`, { discard_branch: String(discardBranch) }),
  merge: (id: string) => request<MergeResult>("POST", `/chats/${id}/merge`),
  keep: (id: string) => request<{ notes: string[] }>("POST", `/chats/${id}/keep`),
  discard: (id: string) => request<{ notes: string[] }>("POST", `/chats/${id}/discard`),
};

export interface Socket {
  send: (command: Record<string, unknown>) => void;
  close: () => void;
}

/** Connect to the event stream, reconnecting after drops. Events are delivered in batches per frame. */
export function connect(onEvents: (events: AgentEvent[]) => void, onStatus: (online: boolean) => void): Socket {
  let socket: WebSocket | null = null;
  let closed = false;
  let retry = 500;
  let pending: AgentEvent[] = [];
  let scheduled = false;

  const flush = () => {
    scheduled = false;
    const batch = pending;
    pending = [];
    if (batch.length) onEvents(batch);
  };

  const open = () => {
    const scheme = window.location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(`${scheme}://${window.location.host}/ws?token=${encodeURIComponent(TOKEN)}`);
    socket.onopen = () => {
      retry = 500;
      onStatus(true);
    };
    socket.onmessage = (message) => {
      pending.push(JSON.parse(message.data));
      if (!scheduled) {
        scheduled = true;
        requestAnimationFrame(flush);
      }
    };
    socket.onclose = () => {
      onStatus(false);
      if (!closed) {
        setTimeout(open, retry);
        retry = Math.min(retry * 2, 8000);
      }
    };
  };
  open();

  return {
    send: (command) => {
      if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(command));
    },
    close: () => {
      closed = true;
      socket?.close();
    },
  };
}

export interface SkillInfo {
  name: string;
  description: string;
  source: string;
  folder: string;
}

/** The WebSearch/WebFetch settings (~/.localagent/web.yaml). The key itself never comes back. */
export interface WebView {
  enabled: boolean;
  search: string;
  url: string;
  api_key_env: string;
  key_env: string;
  has_key: boolean;
  backends: string[];
}

export interface SkillList {
  skills: SkillInfo[];
  user_dir: string;
  project_dir: string;
}

export interface RemoteInfo {
  name: string;
  host: string;
  forwards: string[];
  ui: string;
  state: "disconnected" | "connecting" | "connected" | "failed";
  detail: string;
  ui_url: string;
  /** How the copy of this computer's skills on the machine stands ("2 skills copied", or why not). */
  skills: string;
  /** Named commands the machine knows (remotes.yaml): name -> the shell command run over ssh. */
  commands: Record<string, string>;
  /** Closed because another user listens on one of its forwarded ports there (detail says which). */
  refused: boolean;
}

/** Something ssh is asking while logging in: a password (secret) or a yes/no question such as a new host key. */
export interface SshPrompt {
  id: string;
  remote: string;
  prompt: string;
  kind: "secret" | "confirm";
}

/** What a named command printed on the machine. */
export interface RemoteRun {
  command: string;
  line: string;
  exit_code: number | null;
  output: string;
  elapsed?: number;
}

export interface RemotesView {
  remotes: RemoteInfo[];
  prompts: SshPrompt[];
  ssh_hosts: string[];
  askpass_installed: boolean;
}

/** The desktop window's bridge (pywebview), when running inside it. */
export async function desktopPickFolder(): Promise<string | null | undefined> {
  const bridge = (window as any).pywebview?.api;
  if (!bridge?.pick_folder) return undefined;
  return bridge.pick_folder();
}
