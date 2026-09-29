/// <reference types="vite/client" />

// Monaco's JavaScript colouring, borrowed for JSON (see monaco.ts); the package ships no types for it.
declare module "monaco-editor/languages/definitions/javascript/javascript.js" {
  import type { languages } from "monaco-editor/editor/editor.api.js";
  export const conf: languages.LanguageConfiguration;
  export const language: languages.IMonarchLanguage;
}
