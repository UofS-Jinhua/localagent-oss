import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import {
  api, connect, desktopPickFolder, INITIAL_DIR, REMOTE_NAME,
  type AgentEvent, type AttachmentInfo, type ChatRecord, type FileSource, type ModelsPayload, type RemoteInfo,
  type RecentFolders, type RemotesView, type RepoState, type Socket, type TaskRecord,
} from "./api";
import { ActivityBar, type MenuEntry, type SideView, ViewPane } from "./components/ActivityBar";
import { ApprovalDialog, type ApprovalRequest } from "./components/ApprovalDialog";
import { AttachmentViewer } from "./components/Attachments";
import { ChatHeader, type NewChatOptions } from "./components/ChatHeader";
import { Codicon } from "./components/Codicon";
import { Composer } from "./components/Composer";
import { EditorGroup } from "./components/EditorGroup";
import { FileTree } from "./components/FileTree";
import { FolderPicker } from "./components/FolderPicker";
import { MachinesDialog, SshPromptDialog, STATE_LABEL } from "./components/Machines";
import { ModelDot, MODEL_GROUPS, modelDetails, modelGroup } from "./components/ModelPicker";
import { ModelsDialog } from "./components/ModelsDialog";
import { basename, ProjectMenu } from "./components/ProjectMenu";
import { QuestionDialog, type Question } from "./components/QuestionDialog";
import { filterItems, QuickInput, type QuickItem, type QuickSource } from "./components/QuickInput";
import { Sash } from "./components/Sash";
import { ScmView } from "./components/ScmView";
import { ChatsView } from "./components/Sidebar";
import { StatusBar } from "./components/StatusBar";
import { TitleBar } from "./components/TitleBar";
import { OpenFileContext } from "./components/ToolCard";
import { Transcript, type TaskView } from "./components/Transcript";
import { treePath, withTab, type EditorTab, type OpenOptions, type TabKind } from "./editorTabs";
import { dirnameOf, fileIcon } from "./fileIcons";
import { fuzzyPath } from "./fuzzy";
import { applyTheme, useDark, type ThemeChoice } from "./theme";
import { pendingRequests } from "./timeline";

interface Live {
  chatId: string;
  taskId: string | null;
  task: string;
  running: boolean;
  events: AgentEvent[];
  /** Unix seconds, for the timer: the server says when a task started, so a reloaded page agrees. */
  started: number;
  finished: number | null;
}

/** Fold a batch of server events into the live task (the one running, or the one that just ran). */
function liveReducer(state: Live | null, batch: AgentEvent[]): Live | null {
  let live = state;
  let events = live ? live.events.slice() : [];
  for (const e of batch) {
    if (e.type === "hello") {
      if (e.state?.busy) {
        live = { chatId: e.state.chat_id, taskId: e.state.task_id ?? null, task: e.state.task ?? "", running: true,
                 events: [], started: e.state.started ?? Date.now() / 1000, finished: null };
        events = [...(e.backlog ?? [])];
      }
      continue;
    }
    if (e.type === "task_started") {
      live = { chatId: e.chat_id, taskId: null, task: e.task, running: true, events: [],
               started: e.started ?? Date.now() / 1000, finished: null };
      events = [e];
      continue;
    }
    if (!live || e.chat_id !== live.chatId) continue;
    if (e.type === "task_record") live = { ...live, taskId: e.task_id };
    if (e.type === "task_finished") live = { ...live, running: false, finished: Date.now() / 1000 };
    events.push(e);
  }
  return live ? { ...live, events } : live;
}

type QuickKind = "palette" | "models" | "machines" | "recent" | "theme" | "steps";

function readSetting<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw === null ? fallback : (JSON.parse(raw) as T);
  } catch {
    return fallback;
  }
}

function writeSetting(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* storage unavailable */
  }
}

export function App() {
  // The folder named in the address, or else the one opened last (the server keeps that list: the desktop
  // window's port changes on every start, and the page's own storage with it).
  const [workDir, setWorkDir] = useState<string>(INITIAL_DIR);
  const [recent, setRecent] = useState<RecentFolders | null>(null);
  const [repo, setRepo] = useState<RepoState | null>(null);
  const [models, setModels] = useState<ModelsPayload | null>(null);
  const [chats, setChats] = useState<ChatRecord[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [stored, setStored] = useState<TaskRecord[]>([]);
  const [live, dispatchLive] = useReducer(liveReducer, null);
  const [running, setRunning] = useState<string | null>(null);
  const [approval, setApproval] = useState<ApprovalRequest | null>(null);
  const [question, setQuestion] = useState<Question | null>(null);
  const [online, setOnline] = useState(false);
  // What the page remembers between starts. It lives on the server: the desktop window opens on a new port
  // every time and browser storage is per origin, so the page's own storage cannot carry anything over.
  const [settings, setSettings] = useState<Record<string, any>>(() => readSetting("la-settings", {}));
  const [options, setOptions] = useState<NewChatOptions>({ isolate: true, base: "" });
  // The editor area: its tabs, the one shown, and whether the area is open at all.
  const [tabs, setTabs] = useState<EditorTab[]>([]);
  const [activeTab, setActiveTab] = useState<string | null>(null);
  const [editorOpen, setEditorOpen] = useState(false);
  // A pane's width while its sash is dragged; the settings keep it once let go.
  const [dragWidth, setDragWidth] = useState<{ sidebar?: number; editor?: number }>({});
  const [quick, setQuick] = useState<{ kind: QuickKind; initial: string } | null>(null);
  const [toastHidden, setToastHidden] = useState(false);
  // With a chat selected that has its own branch, Files shows that branch's files unless the user says otherwise.
  const [filesFromChat, setFilesFromChat] = useState(true);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [modelsOpen, setModelsOpen] = useState(false);
  const [viewing, setViewing] = useState<AttachmentInfo | null>(null);
  const [files, setFiles] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [refreshTick, setRefreshTick] = useState(0);
  const [remotes, setRemotes] = useState<RemotesView | null>(null);
  const [switching, setSwitching] = useState<string | null>(null);
  const [machinesOpen, setMachinesOpen] = useState(false);
  // Only the desktop app's own server says so: a machine's UI shown in this window lists that machine's folders.
  const [nativeDialogs, setNativeDialogs] = useState(false);
  const switchingRef = useRef(switching);
  switchingRef.current = switching;
  const socket = useRef<Socket | null>(null);
  const selectedRef = useRef(selected);
  selectedRef.current = selected;

  const refresh = useCallback(() => setRefreshTick((t) => t + 1), []);
  /** Change settings: shown at once, cached for this window, and stored for the next start. */
  const remember = useCallback((values: Record<string, unknown>) => {
    setSettings((current) => {
      const next = { ...current, ...values };
      writeSetting("la-settings", next);
      return next;
    });
    api.putSettings(values).catch(() => undefined);
  }, []);
  const loadModels = useCallback(() => {
    api.models().then(setModels).catch((e) => setError(String(e.message ?? e)));
  }, []);
  // A machine's own page lists no machines of its own: this computer's app is the one holding the ssh tunnels.
  // A machine closed for someone else listening on its ports is said out loud: the page may have been on
  // that machine's UI when it happened, so its event never reached this one.
  const loadRemotes = useCallback(() => {
    if (!REMOTE_NAME) api.remotes().then((view) => {
      setRemotes(view);
      const refused = view.remotes.filter((m) => m.refused);
      if (refused.length) setError(refused.map((m) => `${m.name}: ${m.detail}`).join("; "));
    }).catch(() => undefined);
  }, []);

  /** Show a connected machine's LocalAgent in this window; the browser history is the way back. */
  const openMachine = useCallback((machine: RemoteInfo) => {
    setSwitching(null);
    if (!machine.ui_url) {
      setError(`Connected to ${machine.name}. It has no UI command, so only its ports are forwarded.`);
      return;
    }
    window.location.assign(`${machine.ui_url}&remote=${encodeURIComponent(machine.name)}`);
  }, []);

  // Event stream.
  useEffect(() => {
    const s = connect((batch) => {
      dispatchLive(batch);
      for (const e of batch) {
        switch (e.type) {
          case "hello": {
            loadRemotes(); // after a reload or reconnect: ssh may be waiting for a password
            setNativeDialogs(Boolean(e.state?.native_dialogs) && !REMOTE_NAME);
            setRunning(e.state?.busy ? e.state.chat_id : null);
            // A reload (or a reconnect) must not lose a dialog the agent is waiting on.
            const waiting = pendingRequests(e.backlog ?? []);
            setApproval(waiting.approval);
            setQuestion(waiting.question);
            break;
          }
          case "task_started":
            setRunning(e.chat_id);
            break;
          case "accepted":
            if (selectedRef.current === null) setSelected(e.chat_id);
            refresh();
            break;
          case "task_finished":
            setRunning(null);
            setApproval(null);
            setQuestion(null);
            refresh();
            break;
          case "approval_request":
            setApproval({ id: e.id, command: e.command, reason: e.reason });
            break;
          case "approval_resolved":
            setApproval((current) => (current?.id === e.id ? null : current));
            break;
          case "question_request":
            setQuestion({ id: e.id, question: e.question, options: e.options ?? [] });
            break;
          case "question_resolved":
            setQuestion((current) => (current?.id === e.id ? null : current));
            break;
          case "repo":
            refresh();
            break;
          case "remote_status": {
            const machine = { name: e.name, state: e.state, detail: e.detail, ui_url: e.ui_url,
              refused: e.refused } as Partial<RemoteInfo>;
            setRemotes((view) => view && {
              ...view, remotes: view.remotes.map((m) => (m.name === e.name ? { ...m, ...machine } : m)),
            });
            if (e.name === switchingRef.current) {
              if (e.state === "connected") openMachine({ ...(machine as RemoteInfo) });
              if (e.state === "failed" || e.state === "disconnected") {
                setSwitching(null); // stay on this computer: the login failed or was cancelled
                if (e.state === "failed" && !e.refused) setError(`${e.name}: ${e.detail}`);
              }
            }
            if (e.refused) setError(`${e.name}: ${e.detail}`);
            break;
          }
          case "ssh_prompt":
            setRemotes((view) => view && {
              ...view, prompts: [...view.prompts.filter((p) => p.id !== e.id),
                { id: e.id, remote: e.remote, prompt: e.prompt, kind: e.kind }],
            });
            break;
          case "ssh_prompt_resolved":
            setRemotes((view) => view && { ...view, prompts: view.prompts.filter((p) => p.id !== e.id) });
            break;
          case "error":
            if (!e.chat_id) setError(e.message);
            break;
        }
      }
    }, setOnline);
    socket.current = s;
    return () => s.close();
  }, [refresh, loadRemotes, openMachine]);

  useEffect(loadModels, [loadModels]);

  useEffect(() => {
    api.settings().then((stored) => {
      setSettings(stored);
      writeSetting("la-settings", stored);
    }).catch(() => undefined);
  }, []);

  useEffect(() => {
    const known = INITIAL_DIR ? api.openFolder(INITIAL_DIR).catch(() => api.recent()) : api.recent();
    known.then((r) => {
      setRecent(r);
      if (!INITIAL_DIR && r.folders.length) setWorkDir((dir) => dir || r.folders[0]);
    }).catch(() => setRecent({ folders: [], scratch: "" }));
  }, []);

  // Folder-level data: branch status and chats. Polled so branch switches made outside the app show up.
  useEffect(() => {
    if (!workDir) return;
    let cancelled = false;
    const load = () => {
      api.repo(workDir).then((r) => !cancelled && setRepo(r)).catch((e) => !cancelled && setError(String(e.message ?? e)));
      api.chats(workDir).then((c) => !cancelled && setChats(c)).catch(() => undefined);
    };
    load();
    const timer = setInterval(load, 10000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [workDir, refreshTick]);

  // The folder's files, for @-mentions in the composer.
  useEffect(() => {
    if (!workDir) {
      setFiles([]);
      return;
    }
    let cancelled = false;
    api.files(workDir).then((r) => !cancelled && setFiles(r.files)).catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [workDir, refreshTick]);

  // The selected chat's stored tasks and, when open, its changes.
  useEffect(() => {
    if (selected === null) {
      setStored([]);
      return;
    }
    api.chat(selected).then((d) => setStored(d.tasks)).catch((e) => setError(String(e.message ?? e)));
  }, [selected, refreshTick]);

  const chooseDir = (path: string) => {
    setWorkDir(path);
    api.openFolder(path).then(setRecent).catch(() => undefined);
    setSelected(null);
    setRepo(null);
    setChats([]);
    setTabs([]); // they show the old folder's files
    setActiveTab(null);
    setEditorOpen(false);
    setPickerOpen(false);
  };

  const useScratch = async () => {
    try {
      chooseDir((await api.scratch()).path);
    } catch (e: any) {
      setError(String(e.message ?? e));
    }
  };

  const pickFolder = async () => {
    // The window's native dialog shows this computer's disks, which is wrong for another machine's UI.
    const picked = nativeDialogs ? await desktopPickFolder() : undefined;
    if (picked === undefined) setPickerOpen(true);
    else if (picked) chooseDir(picked);
  };

  const thinking: boolean = settings.thinking ?? true;
  const allowCloud: boolean = settings.allow_cloud ?? false;
  const review: boolean = settings.review ?? false;
  const maxSteps: number = Number(settings.max_steps) || 100;
  // The side bar's view (older windows only had "Chats | Files"), whether it is shown, and the panes' widths.
  const sideView: SideView = ["chats", "explorer", "scm"].includes(settings.sidebar_view) ? settings.sidebar_view
    : settings.sidebar_tab === "files" ? "explorer" : "chats";
  const sidebarOpen: boolean = !settings.sidebar_hidden;
  const sidebarWidth: number = dragWidth.sidebar ?? (Number(settings.sidebar_width) || 260);
  const editorWidth: number = dragWidth.editor ?? (Number(settings.editor_width) || Math.round(window.innerWidth * 0.4));
  const sideBySide: boolean = settings.diff_side_by_side ?? true;
  const themeChoice: ThemeChoice = settings.theme === "dark" || settings.theme === "light" ? settings.theme : "system";
  const dark = useDark(themeChoice);
  useEffect(() => applyTheme(themeChoice), [themeChoice]);
  useEffect(() => {
    if (error) setToastHidden(false);
  }, [error]);
  // The model is remembered per folder: a local model for one project and a cloud one for another is common.
  const model: string = (workDir && settings[`model:${workDir}`]) || "";

  const selectedChat = chats.find((c) => c.id === selected) ?? null;
  const pending = repo?.pending.find((p) => p.chat_id === selected) ?? null;
  const chatHasFiles = !!pending?.checked_out;
  const fileSource: FileSource = useMemo(() => ({ workDir, chatId: chatHasFiles && filesFromChat ? selected : null }),
    [workDir, selected, chatHasFiles, filesFromChat]);

  const tasks: TaskView[] = useMemo(() => {
    const views: TaskView[] = stored.map((t) => ({ id: t.id, task: t.task, events: t.events, live: false,
                                                   started: t.created, finished: t.finished }));
    if (live && live.chatId === selected) {
      const view: TaskView = { id: live.taskId ?? "live", task: live.task, events: live.events, live: live.running,
                               started: live.started, finished: live.running ? null : live.finished };
      const index = views.findIndex((v) => v.id === live.taskId);
      if (index >= 0) views[index] = view;
      else views.push(view);
    }
    return views;
  }, [stored, live, selected]);

  const mode = !workDir ? "no-folder" : running === null ? "idle"
    : running === selected || (selected === null && live?.chatId === running) ? "running-here" : "running-elsewhere";

  const mainRole = models?.roles?.main?.model ?? "";
  const mainModel = mainRole.startsWith("auto") ? models?.auto_model ?? "" : mainRole;
  const autoUnresolved = mainRole.startsWith("auto") && !mainModel;
  const currentModel = models?.models.find((m) => m.name === (model || mainModel)) ?? null;
  const visionModels = (models?.models ?? []).filter((m) => m.vision).map((m) => m.name);
  const hasCloudSwitch = (models?.models ?? []).some((m) => m.switchable && !m.local);

  const run = (text: string, attachments: AttachmentInfo[]) => {
    socket.current?.send({
      type: "run", work_dir: workDir, task: text, chat_id: selected ?? undefined,
      isolate: options.isolate, base: options.base || undefined, model: model || undefined, thinking,
      attachments: attachments.map((a) => a.id), allow_cloud: allowCloud, review, max_steps: maxSteps,
    });
  };
  const stop = () => socket.current?.send({ type: "stop" });

  // ---- the editor area's tabs
  const openTab = (kind: TabKind, source: FileSource, path: string, open: OpenOptions = {}) => {
    const next = withTab(tabs, kind, source, path, open);
    setTabs(next.tabs);
    setActiveTab(next.id);
    setEditorOpen(true);
  };
  const closeTabs = (keep: (tab: EditorTab) => boolean) => {
    const at = tabs.findIndex((t) => t.id === activeTab);
    const next = tabs.filter(keep);
    setTabs(next);
    if (!next.some((t) => t.id === activeTab)) {
      const before = tabs.slice(0, Math.max(0, at)).filter(keep);
      setActiveTab((before[before.length - 1] ?? next[0])?.id ?? null);
    }
    if (!next.length) setEditorOpen(false);
  };
  const closeTab = (id: string) => closeTabs((t) => t.id !== id);
  /** A chat's worktree is gone once its branch is merged, kept or discarded, and with it what its tabs showed. */
  const closeChatTabs = (chatId: string) => closeTabs((t) => t.source.chatId !== chatId);
  const shownTab = tabs.find((t) => t.id === activeTab) ?? tabs[tabs.length - 1] ?? null;
  /** A file a tool call of this chat named: the file after a Read, its change after a Write or an Edit. */
  const openFromTool = (path: string, edited: boolean) => {
    const found = treePath(path, workDir, repo?.repo ?? null);
    if (!found) return setError(`${path} is outside this folder, so it cannot be shown here.`);
    // A relative path is in the tree the agent worked in: the chat's worktree while it has one.
    const chatId = found.chat === undefined ? (pending?.checked_out ? selected : null) : found.chat;
    const branch = chatId ? repo?.pending.find((p) => p.chat_id === chatId)?.branch ?? "" : "";
    if (chatId && !repo?.pending.some((p) => p.chat_id === chatId && p.checked_out)) {
      return setError("That chat's worktree is gone (its branch was merged, kept or discarded); open the file from the Explorer.");
    }
    openTab(edited ? "diff" : "file", { workDir, chatId }, found.path, { pin: true, branch });
  };
  const sameTree = (a: FileSource, b: FileSource) => a.workDir === b.workDir && a.chatId === b.chatId;

  // ---- the side bar
  const showView = (view: SideView) => remember({ sidebar_view: view, sidebar_hidden: false });
  const clickView = (view: SideView) => (sidebarOpen && view === sideView ? remember({ sidebar_hidden: true }) : showView(view));
  const toggleSidebar = () => remember({ sidebar_hidden: sidebarOpen });
  const openChanges = () => {
    if (!pending) return;
    showView("scm");
    openTab("changes", { workDir, chatId: pending.chat_id }, "", { pin: true, branch: pending.branch });
  };

  /** Delete a chat. The server refuses while its branch still holds commits, which becomes the second question. */
  const deleteChat = async (chat: ChatRecord) => {
    const name = chat.title || chat.last_task || "this chat";
    if (!confirm(`Delete "${name}" and everything it recorded?`)) return;
    try {
      await api.deleteChat(chat.id);
    } catch (e) {
      const detail = String((e as Error).message ?? e);
      // Only the refusal about this chat's branch turns into a second question; anything else is an error.
      if (!detail.includes(`la/${chat.id}`)) return setError(detail);
      if (!confirm(`${detail}\n\nDelete the chat and throw the branch away?`)) return;
      try {
        await api.deleteChat(chat.id, true);
      } catch (second) {
        return setError(String((second as Error).message ?? second));
      }
    }
    if (selected === chat.id) setSelected(null);
    closeChatTabs(chat.id);
    refresh();
  };

  /** The model for new tasks in this folder, and a live switch for a running task. */
  const chooseModel = (name: string) => {
    if (workDir) remember({ [`model:${workDir}`]: name || null });
    if (mode === "running-here") socket.current?.send({ type: "set_model", model: name || mainModel });
  };

  /** Pick a machine: connect if needed (ssh asks its questions in a dialog), then open its UI. */
  const switchMachine = (name: string) => {
    if (!name) return setSwitching(null);
    const machine = remotes?.remotes.find((m) => m.name === name);
    if (machine?.state === "connected") return openMachine(machine);
    setSwitching(name);
    api.connectRemote(name).then(setRemotes).catch((e) => {
      setSwitching(null);
      setError(String(e.message ?? e));
    });
  };
  const sshPrompt = remotes?.prompts[0] ?? null;

  const branchAction = async <T,>(chatId: string, action: () => Promise<T>): Promise<T> => {
    const result = await action();
    closeChatTabs(chatId);
    refresh();
    return result;
  };

  // ---- the quick input: files (Ctrl+P), commands (Ctrl+Shift+P, or ">"), and the pickers
  const openQuick = (kind: QuickKind, initial = "") => {
    if (kind === "models") loadModels(); // which models are loaded may have changed
    setQuick({ kind, initial });
  };
  const commands: { id: string; label: string; keys?: string; run: () => void }[] = [
    { id: "chat.new", label: "Chat: New Chat", run: () => { setSelected(null); showView("chats"); } },
    ...(selectedChat ? [{ id: "chat.delete", label: "Chat: Delete This Chat", run: () => void deleteChat(selectedChat) }] : []),
    ...(pending ? [{ id: "scm.changes", label: "Source Control: Open This Chat's Changes", run: openChanges }] : []),
    ...(mode === "running-here" ? [{ id: "agent.stop", label: "Agent: Stop the Running Task", run: stop }] : []),
    { id: "agent.thinking", label: `Agent: Turn ${thinking ? "Off" : "On"} Thinking`, run: () => remember({ thinking: !thinking }) },
    { id: "agent.review", label: `Agent: Turn ${review ? "Off" : "On"} Review`, run: () => remember({ review: !review }) },
    ...(hasCloudSwitch ? [{ id: "agent.cloud", label: `Agent: ${allowCloud ? "Keep Tasks off" : "Let Tasks Move to"} Cloud Models`,
      run: () => remember({ allow_cloud: !allowCloud }) }] : []),
    { id: "agent.steps", label: "Agent: Set the Step Limit…", run: () => openQuick("steps", String(maxSteps)) },
    { id: "models.pick", label: "Models: Select Model…", run: () => openQuick("models") },
    { id: "models.configure", label: "Models: Configure Models…", run: () => setModelsOpen(true) },
    { id: "file.open", label: "File: Open Folder…", run: () => void pickFolder() },
    { id: "file.recent", label: "File: Open Recent…", run: () => openQuick("recent") },
    { id: "file.scratch", label: "File: Use the Scratch Folder", run: () => void useScratch() },
    { id: "go.file", label: "Go to File…", keys: "Ctrl+P", run: () => openQuick("palette") },
    { id: "view.chats", label: "View: Show Chats", run: () => showView("chats") },
    { id: "view.explorer", label: "View: Show Explorer", keys: "Ctrl+Shift+E", run: () => showView("explorer") },
    { id: "view.scm", label: "View: Show Source Control", keys: "Ctrl+Shift+G", run: () => showView("scm") },
    { id: "view.sidebar", label: "View: Toggle Primary Side Bar Visibility", keys: "Ctrl+B", run: toggleSidebar },
    { id: "view.editor", label: "View: Toggle Editor Area", run: () => setEditorOpen(!editorOpen) },
    ...(tabs.length ? [{ id: "view.closeAll", label: "View: Close All Editors", run: () => closeTabs(() => false) }] : []),
    { id: "view.diff", label: `View: Show Diffs ${sideBySide ? "Inline" : "Side by Side"}`, run: () => remember({ diff_side_by_side: !sideBySide }) },
    { id: "prefs.theme", label: "Preferences: Color Theme…", run: () => openQuick("theme") },
    ...(REMOTE_NAME ? [] : [
      { id: "remote.switch", label: "Remote: Switch Machine…", run: () => openQuick("machines") },
      { id: "remote.manage", label: "Remote: Manage Machines…", run: () => setMachinesOpen(true) },
    ]),
  ];
  // The quick input's lists are built once per opening; what an item does reads the page as it is when chosen.
  const latest = useRef({ commands, openTab, fileSource, branch: pending?.branch ?? "", chooseModel, chooseDir, switchMachine,
    pickFolder, useScratch, openQuick, toggleSidebar, showView, remember });
  latest.current = { commands, openTab, fileSource, branch: pending?.branch ?? "", chooseModel, chooseDir, switchMachine,
    pickFolder, useScratch, openQuick, toggleSidebar, showView, remember };

  const quickSource = useMemo<QuickSource | null>(() => {
    if (!quick) return null;
    const icon = (name: string, color?: string) => <span style={color ? { color } : undefined}><Codicon name={name} /></span>;
    switch (quick.kind) {
      case "palette": {
        const fileItems = (text: string): QuickItem[] => {
          const toItem = (path: string, positions: number[] = []): QuickItem => {
            const start = path.lastIndexOf("/") + 1;
            const kind = fileIcon(path);
            return {
              id: `file:${path}`, label: path.slice(start), description: dirnameOf(path), icon: icon(kind.icon, kind.color),
              highlights: positions.filter((p) => p >= start).map((p) => p - start),
              run: () => latest.current.openTab("file", latest.current.fileSource, path,
                { pin: true, branch: latest.current.fileSource.chatId ? latest.current.branch : "" }),
            };
          };
          const query = text.trim();
          if (!query) return files.slice(0, 200).map((path) => toItem(path));
          const scored: { path: string; score: number; positions: number[] }[] = [];
          for (const path of files) {
            const match = fuzzyPath(query, path);
            if (match) scored.push({ path, ...match });
          }
          return scored.sort((a, b) => b.score - a.score).slice(0, 200).map((s) => toItem(s.path, s.positions));
        };
        const commandItems = (text: string) => filterItems(latest.current.commands.map((c) => ({
          id: `cmd:${c.id}`, label: c.label, keys: c.keys, run: c.run,
        })), text);
        return {
          placeholder: "Search files by name, or type > for commands",
          items: (text) => (text.startsWith(">") ? commandItems(text.slice(1)) : fileItems(text)),
        };
      }
      case "models": {
        const all = models?.models ?? [];
        const others = all.filter((m) => m.name !== mainModel)
          .sort((a, b) => MODEL_GROUPS.indexOf(modelGroup(a)) - MODEL_GROUPS.indexOf(modelGroup(b)));
        const main = all.find((m) => m.name === mainModel);
        const items: QuickItem[] = [
          { id: "model:", label: mainModel || (autoUnresolved ? "auto · no model loaded" : "main role"),
            description: modelDetails(main), icon: <ModelDot model={main} />, group: "main role", checked: model === "",
            run: () => latest.current.chooseModel("") },
          ...others.map((m) => ({
            id: `model:${m.name}`, label: m.name, description: modelDetails(m), detail: m.notes || undefined,
            icon: <ModelDot model={m} />, group: modelGroup(m), checked: model === m.name,
            run: () => latest.current.chooseModel(m.name),
          })),
          { id: "model:configure", label: "Configure Models…", icon: icon("settings-gear"), group: " ", run: () => setModelsOpen(true) },
        ];
        return {
          placeholder: mode === "running-here" ? "Move the running task to a model" : "Select the model for new tasks in this folder",
          items: (text) => filterItems(items, text),
        };
      }
      case "machines": {
        const items: QuickItem[] = REMOTE_NAME ? [
          ...(window.history.length > 1 ? [{ id: "machine:back", label: "Back to This Computer", icon: icon("arrow-left"),
            run: () => window.history.back() }] : []),
          { id: "machine:here", label: REMOTE_NAME, description: "this window shows that machine", icon: icon("remote"),
            checked: true, run: () => undefined },
        ] : [
          { id: "machine:local", label: "This Computer", icon: icon("device-desktop"), checked: true, run: () => undefined },
          ...(remotes?.remotes ?? []).map((m) => ({
            id: `machine:${m.name}`, label: m.name, icon: icon("remote"),
            description: switching === m.name && m.state !== "failed" ? "connecting…" : STATE_LABEL[m.state],
            detail: m.state === "failed" ? m.detail : undefined, run: () => latest.current.switchMachine(m.name),
          })),
          { id: "machine:manage", label: remotes?.remotes.length ? "Manage Machines…" : "Add a Machine…",
            icon: icon("settings-gear"), group: " ", run: () => setMachinesOpen(true) },
        ];
        return { placeholder: "Select the machine this window works on", items: (text) => filterItems(items, text) };
      }
      case "recent": {
        const items: QuickItem[] = [
          ...(recent?.folders ?? []).map((path) => ({
            id: `recent:${path}`, label: basename(path), description: path, icon: icon("folder"), group: "recent",
            checked: path === workDir, run: () => latest.current.chooseDir(path),
          })),
          { id: "recent:browse", label: "Open Folder…", icon: icon("folder-opened"), group: " ", run: () => void latest.current.pickFolder() },
          { id: "recent:scratch", label: "Scratch Folder (no project)", icon: icon("note"), run: () => void latest.current.useScratch() },
        ];
        return { placeholder: "Select a folder to open", items: (text) => filterItems(items, text) };
      }
      case "theme": {
        const choices: [ThemeChoice, string, string][] = [
          ["system", "Follow the System", "color-mode"], ["dark", "Dark Modern", "circle-filled"], ["light", "Light Modern", "circle-outline"],
        ];
        const items: QuickItem[] = choices.map(([value, label, name]) => ({
          id: `theme:${value}`, label, icon: icon(name), checked: themeChoice === value,
          run: () => latest.current.remember({ theme: value === "system" ? null : value }),
        }));
        return { placeholder: "Select Color Theme", items: (text) => filterItems(items, text) };
      }
      case "steps":
        return {
          placeholder: "Step limit", items: () => [],
          prompt: "How many tool calls one task may make before it stops and writes notes to continue from (1–999). "
            + "Press Enter to confirm or Escape to cancel.",
          validate: (text) => (/^\d+$/.test(text.trim()) && +text >= 1 && +text <= 999 ? "" : "Enter a whole number from 1 to 999."),
          accept: (text) => latest.current.remember({ max_steps: Number(text) }),
        };
    }
  }, [quick, files, models, remotes, recent, switching]);

  // VS Code's keys for what this window has: quick open, the command palette, and the side bar.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const ctrl = e.ctrlKey || e.metaKey;
      const key = e.key.toLowerCase();
      const act = latest.current;
      if (e.key === "F1" || (ctrl && e.shiftKey && key === "p")) act.openQuick("palette", ">");
      else if (ctrl && !e.shiftKey && !e.altKey && key === "p") act.openQuick("palette");
      else if (ctrl && !e.shiftKey && !e.altKey && key === "b") act.toggleSidebar();
      else if (ctrl && e.shiftKey && key === "e") act.showView("explorer");
      else if (ctrl && e.shiftKey && key === "g") act.showView("scm");
      else return;
      e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const gearMenu: (MenuEntry | "-")[] = [
    { label: "Command Palette…", keys: "Ctrl+Shift+P", onClick: () => openQuick("palette", ">") },
    { label: "Go to File…", keys: "Ctrl+P", onClick: () => openQuick("palette") },
    "-",
    { label: "Models…", onClick: () => setModelsOpen(true) },
    ...(REMOTE_NAME ? [] : [{ label: "Machines…", onClick: () => setMachinesOpen(true) }]),
    { label: "Color Theme…", onClick: () => openQuick("theme") },
  ];

  const scmCount = (pending?.files ?? 0) + (repo?.dirty_files ?? 0);
  const liveStarted = live && live.chatId === running && live.running ? live.started : null;
  const editorMax = Math.max(300, window.innerWidth - 48 - (sidebarOpen ? sidebarWidth : 0) - 360);
  const shownEditorWidth = Math.min(editorWidth, editorMax);

  const sideContent = sideView === "explorer" ? (
    workDir ? (
      <FileTree source={fileSource} refreshKey={refreshTick}
        openPath={shownTab && shownTab.kind !== "changes" && sameTree(shownTab.source, fileSource) ? shownTab.path : null}
        onOpen={(path, pin) => openTab("file", fileSource, path, { pin, branch: fileSource.chatId ? pending?.branch : "" })}
        sourceChoice={chatHasFiles && pending
          ? { branch: pending.branch, fromChat: filesFromChat, onChange: setFilesFromChat } : undefined} />
    ) : (
      <ViewPane title="Explorer">
        <div className="view-empty">
          You have not opened a folder yet.
          <button className="primary wide" onClick={() => void pickFolder()}>Open Folder</button>
        </div>
      </ViewPane>
    )
  ) : sideView === "scm" ? (
    <ScmView workDir={workDir} repo={repo} chatTitle={selectedChat?.title || selectedChat?.last_task || ""}
      pending={pending} busy={running === selected && selected !== null} refreshKey={refreshTick}
      activePath={shownTab?.kind === "diff" ? `${shownTab.source.chatId ?? ""}\n${shownTab.path}` : ""}
      onOpenDiff={(source, path, branch) => openTab("diff", source, path, { pin: true, branch })}
      onOpenChanges={(chatId, branch) => openTab("changes", { workDir, chatId }, "", { pin: true, branch })}
      onMerge={() => branchAction(selected!, () => api.merge(selected!))}
      onKeep={() => branchAction(selected!, () => api.keep(selected!))}
      onDiscard={() => branchAction(selected!, () => api.discard(selected!))}
      onRefresh={refresh} />
  ) : (
    <ChatsView chats={chats} pending={repo?.pending ?? []} selected={selected} runningChat={running}
      onSelect={setSelected} onDelete={deleteChat} onRefresh={refresh} />
  );

  return (
    <div className="app">
      <TitleBar
        project={<ProjectMenu workDir={workDir} recent={recent?.folders ?? []} scratch={recent?.scratch ?? ""}
          onOpen={chooseDir} onBrowse={pickFolder}
          onForget={(path) => api.forgetFolder(path).then(setRecent).catch((e) => setError(String(e.message ?? e)))} />}
        folderName={workDir ? (workDir === recent?.scratch ? "scratch" : basename(workDir)) : ""}
        onCommandCenter={() => openQuick("palette")}
        sidebarOpen={sidebarOpen} onToggleSidebar={toggleSidebar}
        editorOpen={editorOpen} onToggleEditor={() => setEditorOpen(!editorOpen)} />
      <div className="workbench">
        <ActivityBar view={sideView} sidebarOpen={sidebarOpen} onView={clickView} running={running !== null}
          badges={{ scm: scmCount }} menu={gearMenu} />
        {sidebarOpen && (
          <>
            <aside className="sidebar" style={{ width: sidebarWidth }}>{sideContent}</aside>
            <Sash side="left" width={sidebarWidth} min={170} max={640}
              onWidth={(w) => setDragWidth((d) => ({ ...d, sidebar: w }))}
              onDone={(w) => { remember({ sidebar_width: w }); setDragWidth((d) => ({ ...d, sidebar: undefined })); }}
              onReset={() => remember({ sidebar_width: null })} />
          </>
        )}
        <main className="main">
          <ChatHeader chat={selectedChat} pending={pending} repo={repo} options={options} onOptions={setOptions}
            onOpenChanges={openChanges} />
          <OpenFileContext.Provider value={workDir ? openFromTool : null}>
            <Transcript key={selected ?? "new"} tasks={tasks} onOpenAttachment={setViewing}
              empty={recent === null && !workDir ? null
                : <Welcome workDir={workDir} repo={repo} recent={recent?.folders ?? []} onOpen={chooseDir}
                    onPickFolder={pickFolder} onScratch={useScratch} />} />
          </OpenFileContext.Provider>
          <Composer mode={mode} onRun={run} files={files}
            model={currentModel} visionModels={visionModels} onPickModel={chooseModel}
            onSteer={(text, attachments) => socket.current?.send({ type: "steer", text, attachments: attachments.map((a) => a.id) })}
            onStop={stop} />
        </main>
        {editorOpen && (
          <>
            <Sash side="right" width={shownEditorWidth} min={260} max={editorMax}
              onWidth={(w) => setDragWidth((d) => ({ ...d, editor: w }))}
              onDone={(w) => { remember({ editor_width: w }); setDragWidth((d) => ({ ...d, editor: undefined })); }}
              onReset={() => remember({ editor_width: null })} />
            <div className="editor-part" style={{ width: shownEditorWidth }}>
              <EditorGroup tabs={tabs} activeId={shownTab?.id ?? null} refreshKey={refreshTick} dark={dark}
                sideBySide={sideBySide} onSideBySide={(on) => remember({ diff_side_by_side: on })}
                onActivate={setActiveTab} onClose={closeTab} onCloseAll={() => closeTabs(() => false)}
                onPin={(id) => setTabs(tabs.map((t) => (t.id === id ? { ...t, preview: false } : t)))}
                onOpen={openTab} />
            </div>
          </>
        )}
      </div>
      <StatusBar
        machine={REMOTE_NAME || switching || ""} connecting={!!switching} onMachine={() => openQuick("machines")}
        online={online} workDir={workDir} repo={repo} chatBranch={pending} onBranch={() => showView("scm")}
        running={running !== null ? { started: liveStarted ?? Date.now() / 1000, here: mode === "running-here" } : null}
        onRunning={() => { if (running) setSelected(running); showView("chats"); }}
        modelName={models?.error ? "No models" : currentModel?.name ?? (mainModel || (autoUnresolved ? "auto · none loaded" : "main role"))}
        model={currentModel ?? undefined}
        modelWarning={models?.error ? `No models configured: ${models.error}`
          : autoUnresolved ? "The role follows the loaded model, but the server reports none (is it running?)" : ""}
        switching={mode === "running-here"} onModel={() => openQuick("models")}
        thinking={thinking} onThinking={() => remember({ thinking: !thinking })}
        review={review} onReview={() => remember({ review: !review })}
        cloud={hasCloudSwitch ? allowCloud : undefined} onCloud={() => remember({ allow_cloud: !allowCloud })}
        steps={maxSteps} onSteps={() => openQuick("steps", String(maxSteps))}
        notification={!!error} onNotifications={() => setToastHidden(!toastHidden)} />
      {error && !toastHidden && (
        <div className="notification" role="alert">
          <Codicon name="error" className="notification-icon" />
          <div className="notification-text">{error}</div>
          <button className="action" title="Clear Notification" aria-label="Clear Notification" onClick={() => setError("")}>
            <Codicon name="close" />
          </button>
        </div>
      )}
      {quick && quickSource && (
        <QuickInput key={`${quick.kind}:${quick.initial}`} source={quickSource} initial={quick.initial} onClose={() => setQuick(null)} />
      )}
      {pickerOpen && <FolderPicker start={workDir} onPick={chooseDir} onClose={() => setPickerOpen(false)} />}
      {modelsOpen && <ModelsDialog onClose={() => setModelsOpen(false)} onChanged={loadModels} workDir={workDir} />}
      {viewing && <AttachmentViewer attachment={viewing} onClose={() => setViewing(null)} />}
      {question && (
        <QuestionDialog request={question} onAnswer={(text) => {
          socket.current?.send({ type: "answer", id: question.id, text });
          setQuestion(null);
        }} />
      )}
      {machinesOpen && remotes && (
        <MachinesDialog view={remotes} onView={setRemotes} onClose={() => setMachinesOpen(false)} />
      )}
      {sshPrompt && (
        <SshPromptDialog key={sshPrompt.id} prompt={sshPrompt} onDone={(answer) => {
          setRemotes((view) => view && { ...view, prompts: view.prompts.filter((p) => p.id !== sshPrompt.id) });
          api.answerSshPrompt(sshPrompt.id, answer).catch((e) => setError(String(e.message ?? e)));
        }} />
      )}
      {approval && (
        <ApprovalDialog request={approval} onAnswer={(allow) => {
          socket.current?.send({ type: "approval", id: approval.id, allow });
          setApproval(null);
        }} />
      )}
    </div>
  );
}

function Welcome({ workDir, repo, recent, onOpen, onPickFolder, onScratch }: {
  workDir: string;
  repo: RepoState | null;
  recent: string[];
  onOpen: (path: string) => void;
  onPickFolder: () => void;
  onScratch: () => void;
}) {
  if (!workDir) {
    return (
      <div className="welcome start">
        <div className="welcome-title">
          <img src="icon.png" alt="" width={56} height={56} />
          <div>
            <h1>LocalAgent</h1>
            <p className="dim">Your own models, reading, editing and testing your code.</p>
          </div>
        </div>
        <div className="welcome-columns">
          <section>
            <h3>Start</h3>
            <button className="link-row" onClick={onPickFolder}><Codicon name="folder-opened" /> Open Folder…</button>
            <button className="link-row" onClick={onScratch} title="A folder for questions and one-off scripts; never a git repository">
              <Codicon name="note" /> Use the Scratch Folder
            </button>
          </section>
          {recent.length > 0 && (
            <section>
              <h3>Recent</h3>
              {recent.slice(0, 6).map((path) => (
                <button key={path} className="link-row" onClick={() => onOpen(path)} title={path}>
                  <span>{basename(path)}</span> <span className="dim">{path}</span>
                </button>
              ))}
            </section>
          )}
        </div>
      </div>
    );
  }
  return (
    <div className="welcome">
      <h2>What should the agent do?</h2>
      {repo?.repo ? (
        <p>
          This chat will work on its own branch, starting from <strong>{repo.branch ?? "the current commit"}</strong>.
          Your checkout stays untouched until you review the changes and merge them.
        </p>
      ) : (
        <p>This folder is not a git repository, so the agent will edit files in place.</p>
      )}
      <p className="dim">
        Examples: "the tests fail, find and fix the bug" · paste a screenshot and ask what looks wrong
        · attach a paper (PDF) and ask for an implementation plan
      </p>
      <p className="dim small welcome-keys">
        <kbd>Ctrl</kbd>+<kbd>P</kbd> open a file · <kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>P</kbd> all commands
        · type <kbd>@</kbd> in the box below to mention a file
      </p>
    </div>
  );
}
