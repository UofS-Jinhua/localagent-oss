import { forwardRef, useEffect, useImperativeHandle, useRef, useState } from "react";

// Read-only Monaco views: a file with its changed lines marked in the gutter (VS Code's "dirty diff"),
// and a diff of a file against its base. Monaco is loaded the first time one of them is shown.

type MonacoModule = typeof import("../monaco");
type CodeEditor = import("monaco-editor/editor/editor.api.js").editor.IStandaloneCodeEditor;
type DiffEditor = import("monaco-editor/editor/editor.api.js").editor.IStandaloneDiffEditor;
type ViewState = import("monaco-editor/editor/editor.api.js").editor.ICodeEditorViewState;

let loading: Promise<MonacoModule> | null = null;
export const loadMonaco = () => (loading ??= import("../monaco"));

function useMonaco(): MonacoModule | null {
  const [module, setModule] = useState<MonacoModule | null>(null);
  useEffect(() => {
    let alive = true;
    loadMonaco().then((m) => alive && setModule(m));
    return () => {
      alive = false;
    };
  }, []);
  return module;
}

// Where each tab was scrolled to, so switching tabs (which remounts the view) comes back to the same place.
const viewStates = new Map<string, ViewState | null>();

const BASE_OPTIONS = {
  readOnly: true,
  automaticLayout: true,
  fontFamily: "'Cascadia Code', 'JetBrains Mono', Consolas, 'Microsoft YaHei UI', monospace",
  fontSize: 13,
  scrollBeyondLastLine: false,
  renderLineHighlight: "line" as const,
  smoothScrolling: true,
  stickyScroll: { enabled: true },
  unicodeHighlight: { ambiguousCharacters: false, invisibleCharacters: false }, // Chinese text is not a mistake
  scrollbar: { verticalScrollbarSize: 12, horizontalScrollbarSize: 12 },
};

export interface LineChanges {
  added: number[];
  modified: number[];
  /** Lines below which something was deleted. */
  deleted: number[];
}

/** Which lines of the current file a unified diff adds, changes, or deletes below. */
export function lineChanges(diff: string): LineChanges {
  const changes: LineChanges = { added: [], modified: [], deleted: [] };
  let line = 0;
  let inHunk = false;
  let removed = 0; // "-" lines not yet paired with a "+" line
  const flush = () => {
    if (removed > 0) changes.deleted.push(Math.max(1, line - 1));
    removed = 0;
  };
  for (const text of diff.split("\n")) {
    const hunk = text.match(/^@@ -\d+(?:,\d+)? \+(\d+)/);
    if (hunk) {
      flush();
      line = Number(hunk[1]);
      inHunk = true;
    } else if (text.startsWith("diff --git ")) {
      flush();
      inHunk = false;
    } else if (!inHunk || text.startsWith("\\")) {
      continue;
    } else if (text.startsWith("-")) {
      removed++;
    } else if (text.startsWith("+")) {
      if (removed > 0) {
        changes.modified.push(line);
        removed--;
      } else changes.added.push(line);
      line++;
    } else {
      flush();
      line++;
    }
  }
  flush();
  return changes;
}

export function FileEditor({ viewKey, path, text, changes, dark }: {
  viewKey: string;
  path: string;
  text: string;
  changes: LineChanges;
  dark: boolean;
}) {
  const m = useMonaco();
  const box = useRef<HTMLDivElement>(null);
  const editor = useRef<CodeEditor | null>(null);
  const decorations = useRef<{ clear: () => void } | null>(null);

  useEffect(() => {
    if (!m || !box.current) return;
    const created = m.monaco.editor.create(box.current, { ...BASE_OPTIONS, theme: dark ? "la-dark" : "la-light" });
    editor.current = created;
    return () => {
      viewStates.set(viewKey, created.saveViewState());
      decorations.current = null;
      created.getModel()?.dispose();
      created.dispose();
      editor.current = null;
    };
  }, [m, viewKey]);

  useEffect(() => {
    if (m) m.monaco.editor.setTheme(dark ? "la-dark" : "la-light");
  }, [m, dark]);

  // New text (the file changed after a task, or a reload) keeps the scroll position and the cursor.
  useEffect(() => {
    const view = editor.current;
    if (!m || !view) return;
    const language = m.languageFor(path);
    const model = view.getModel();
    if (model && model.getLanguageId() === language) {
      if (model.getValue() !== text) {
        const state = view.saveViewState();
        model.setValue(text);
        view.restoreViewState(state);
      }
    } else {
      model?.dispose();
      view.setModel(m.monaco.editor.createModel(text, language));
      const saved = viewStates.get(viewKey);
      if (saved) view.restoreViewState(saved);
    }
    decorations.current?.clear();
    const mark = (lines: number[], kind: string, ruler: string) => lines.map((n) => ({
      range: new m.monaco.Range(n, 1, n, 1),
      options: {
        isWholeLine: true, linesDecorationsClassName: `dirty-${kind}`,
        overviewRuler: { color: ruler, position: m.monaco.editor.OverviewRulerLane.Left },
      },
    }));
    decorations.current = view.createDecorationsCollection([
      ...mark(changes.added, "added", "#2ea04399"), ...mark(changes.modified, "modified", "#0078d499"),
      ...mark(changes.deleted, "deleted", "#f8514999"),
    ]);
  }, [m, text, path, changes, viewKey]);

  return <div className="monaco-box" ref={box}>{!m && <div className="empty">Loading the editor…</div>}</div>;
}

export interface DiffEditorHandle {
  next: () => void;
  previous: () => void;
}

export const DiffEditorView = forwardRef<DiffEditorHandle, {
  viewKey: string;
  path: string;
  original: string;
  modified: string;
  sideBySide: boolean;
  dark: boolean;
}>(function DiffEditorView({ viewKey, path, original, modified, sideBySide, dark }, ref) {
  const m = useMonaco();
  const box = useRef<HTMLDivElement>(null);
  const editor = useRef<DiffEditor | null>(null);

  useImperativeHandle(ref, () => ({
    next: () => editor.current?.goToDiff("next"),
    previous: () => editor.current?.goToDiff("previous"),
  }), []);

  useEffect(() => {
    if (!m || !box.current) return;
    const created = m.monaco.editor.createDiffEditor(box.current, {
      ...BASE_OPTIONS, theme: dark ? "la-dark" : "la-light", originalEditable: false, renderSideBySide: sideBySide,
      useInlineViewWhenSpaceIsLimited: true, renderSideBySideInlineBreakpoint: 640,
      hideUnchangedRegions: { enabled: true, contextLineCount: 3, minimumLineCount: 5, revealLineCount: 20 },
      renderOverviewRuler: true, ignoreTrimWhitespace: false, diffAlgorithm: "advanced",
    });
    editor.current = created;
    return () => {
      viewStates.set(viewKey, created.getModifiedEditor().saveViewState());
      const models = created.getModel();
      created.dispose();
      models?.original.dispose();
      models?.modified.dispose();
      editor.current = null;
    };
  }, [m, viewKey]);

  useEffect(() => {
    if (m) m.monaco.editor.setTheme(dark ? "la-dark" : "la-light");
  }, [m, dark]);

  useEffect(() => {
    editor.current?.updateOptions({ renderSideBySide: sideBySide });
  }, [sideBySide, m]);

  useEffect(() => {
    const view = editor.current;
    if (!m || !view) return;
    const language = m.languageFor(path);
    const current = view.getModel();
    if (current && current.original.getLanguageId() === language) {
      if (current.original.getValue() !== original) current.original.setValue(original);
      if (current.modified.getValue() !== modified) current.modified.setValue(modified);
      return;
    }
    view.setModel({
      original: m.monaco.editor.createModel(original, language),
      modified: m.monaco.editor.createModel(modified, language),
    });
    current?.original.dispose();
    current?.modified.dispose();
    const saved = viewStates.get(viewKey);
    if (saved) view.getModifiedEditor().restoreViewState(saved);
  }, [m, path, original, modified, viewKey]);

  return <div className="monaco-box" ref={box}>{!m && <div className="empty">Loading the editor…</div>}</div>;
});
