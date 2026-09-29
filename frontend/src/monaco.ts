// Monaco, the editor inside VS Code, for reading files and diffs (never for editing: the agent edits).
// This entry takes the editor, its read-only features and the syntax colouring of every language, and
// leaves out the language services (TypeScript, JSON, CSS, HTML): they need their own workers and several
// MB, and a viewer has no use for completions or errors. Loaded on demand, the first time a tab opens.
import "@vscode/codicons/dist/codicon.css"; // the icon font; Monaco draws its icons from it too
import * as monaco from "monaco-editor/editor/editor.api.js";
import "monaco-editor/editor/browser/widget/codeEditor/codeEditorWidget.js";
import "monaco-editor/editor/browser/widget/diffEditor/diffEditor.contribution.js";
import "monaco-editor/editor/browser/coreCommands.js";
import "monaco-editor/editor/contrib/bracketMatching/browser/bracketMatching.js";
import "monaco-editor/editor/contrib/clipboard/browser/clipboard.js";
import "monaco-editor/editor/contrib/contextmenu/browser/contextmenu.js";
import "monaco-editor/features/find/register.js";
import "monaco-editor/editor/contrib/find/browser/findController.js";
import "monaco-editor/editor/contrib/folding/browser/folding.js";
import "monaco-editor/editor/contrib/hover/browser/hoverContribution.js";
import "monaco-editor/editor/contrib/multicursor/browser/multicursor.js";
import "monaco-editor/editor/contrib/readOnlyMessage/browser/contribution.js";
import "monaco-editor/editor/contrib/stickyScroll/browser/stickyScrollContribution.js";
import "monaco-editor/editor/contrib/wordHighlighter/browser/wordHighlighter.js";
import "monaco-editor/editor/contrib/wordOperations/browser/wordOperations.js";
import "monaco-editor/editor/standalone/browser/quickAccess/standaloneGotoLineQuickAccess.js";
import "monaco-editor/editor/common/standaloneStrings.js";
import "monaco-editor/languages/definitions/register.all.js";
import EditorWorker from "monaco-editor/editor/editor.worker.js?worker";

// The diff editor computes its diff in a worker; nothing else here needs one.
(globalThis as any).MonacoEnvironment = { getWorker: () => new EditorWorker() };

// JSON's own support is one of the language services left out; JavaScript's colouring reads it well.
monaco.languages.register({ id: "json", extensions: [".json", ".jsonc", ".jsonl", ".code-workspace"],
  filenames: [".babelrc", ".eslintrc", "tsconfig.json"], aliases: ["JSON"] });
monaco.languages.onLanguage("json", async () => {
  const js = await import("monaco-editor/languages/definitions/javascript/javascript.js");
  monaco.languages.setMonarchTokensProvider("json", js.language);
  monaco.languages.setLanguageConfiguration("json", js.conf);
});

// VS Code's Dark Modern and Light Modern backgrounds, so the editor sits flush with the rest of the window.
monaco.editor.defineTheme("la-dark", {
  base: "vs-dark", inherit: true, rules: [],
  colors: {
    "editor.background": "#1f1f1f", "editorGutter.background": "#1f1f1f", "editorLineNumber.foreground": "#6e7681",
    "editorLineNumber.activeForeground": "#cccccc", "editor.lineHighlightBorder": "#282828",
    "editorStickyScroll.background": "#1f1f1f", "editorWidget.background": "#202020",
  },
});
monaco.editor.defineTheme("la-light", {
  base: "vs", inherit: true, rules: [],
  colors: {
    "editor.background": "#ffffff", "editorGutter.background": "#ffffff", "editorLineNumber.foreground": "#6e7681",
    "editorLineNumber.activeForeground": "#171184", "editor.lineHighlightBorder": "#eeeeee",
    "editorStickyScroll.background": "#ffffff", "editorWidget.background": "#f8f8f8",
  },
});

let byName: Map<string, string> | null = null;
let byExtension: Map<string, string> | null = null;

/** Monaco's language for a file, from its name (Dockerfile) or extension; plain text otherwise. */
export function languageFor(path: string): string {
  if (!byName || !byExtension) {
    byName = new Map();
    byExtension = new Map();
    for (const language of monaco.languages.getLanguages()) {
      for (const name of language.filenames ?? []) byName.set(name.toLowerCase(), language.id);
      for (const extension of language.extensions ?? []) {
        if (!byExtension.has(extension.toLowerCase())) byExtension.set(extension.toLowerCase(), language.id);
      }
    }
  }
  const name = (path.split("/").pop() ?? path).toLowerCase();
  if (byName.has(name)) return byName.get(name)!;
  for (let dot = name.indexOf("."); dot >= 0; dot = name.indexOf(".", dot + 1)) {
    const found = byExtension.get(name.slice(dot));
    if (found) return found;
  }
  return "plaintext";
}

export { monaco };
