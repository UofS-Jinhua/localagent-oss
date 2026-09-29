// A file's icon in the tree and on its tab: a codicon and a colour by kind, in the spirit of VS Code's
// file icon theme (which is a font of its own; these are close enough to tell files apart at a glance).

interface FileIcon {
  icon: string;
  color: string;
}

const CODE = "file-code";
const byExtension: Record<string, FileIcon> = {
  py: { icon: CODE, color: "#4b8bbe" }, pyi: { icon: CODE, color: "#4b8bbe" }, ipynb: { icon: "notebook", color: "#e37933" },
  ts: { icon: CODE, color: "#3178c6" }, tsx: { icon: CODE, color: "#3178c6" }, mts: { icon: CODE, color: "#3178c6" },
  js: { icon: CODE, color: "#cbcb41" }, jsx: { icon: CODE, color: "#cbcb41" }, mjs: { icon: CODE, color: "#cbcb41" },
  cjs: { icon: CODE, color: "#cbcb41" }, json: { icon: "json", color: "#cbcb41" }, jsonl: { icon: "json", color: "#cbcb41" },
  html: { icon: CODE, color: "#e37933" }, htm: { icon: CODE, color: "#e37933" }, vue: { icon: CODE, color: "#8dc149" },
  css: { icon: "symbol-color", color: "#519aba" }, scss: { icon: "symbol-color", color: "#f55385" }, less: { icon: "symbol-color", color: "#519aba" },
  md: { icon: "markdown", color: "#519aba" }, mdx: { icon: "markdown", color: "#519aba" }, rst: { icon: "note", color: "#8a8a8a" },
  txt: { icon: "note", color: "#8a8a8a" }, log: { icon: "output", color: "#8a8a8a" }, csv: { icon: "table", color: "#8dc149" },
  tsv: { icon: "table", color: "#8dc149" }, xlsx: { icon: "table", color: "#8dc149" },
  yaml: { icon: "settings", color: "#a074c4" }, yml: { icon: "settings", color: "#a074c4" }, toml: { icon: "settings", color: "#a074c4" },
  ini: { icon: "settings", color: "#8a8a8a" }, cfg: { icon: "settings", color: "#8a8a8a" }, env: { icon: "settings", color: "#cbcb41" },
  lock: { icon: "lock", color: "#8a8a8a" },
  sh: { icon: "terminal", color: "#8dc149" }, bash: { icon: "terminal", color: "#8dc149" }, ps1: { icon: "terminal", color: "#519aba" },
  cmd: { icon: "terminal", color: "#cbcb41" }, bat: { icon: "terminal", color: "#cbcb41" },
  c: { icon: CODE, color: "#599eff" }, h: { icon: CODE, color: "#a074c4" }, cpp: { icon: CODE, color: "#599eff" },
  hpp: { icon: CODE, color: "#a074c4" }, cu: { icon: CODE, color: "#8dc149" }, rs: { icon: CODE, color: "#dea584" },
  go: { icon: CODE, color: "#519aba" }, java: { icon: CODE, color: "#cc3e44" }, kt: { icon: CODE, color: "#a074c4" },
  cs: { icon: CODE, color: "#8dc149" }, rb: { icon: CODE, color: "#cc3e44" }, php: { icon: CODE, color: "#a074c4" },
  swift: { icon: CODE, color: "#e37933" }, lua: { icon: CODE, color: "#519aba" }, r: { icon: CODE, color: "#519aba" },
  sql: { icon: "database", color: "#f55385" }, db: { icon: "database", color: "#f55385" }, sqlite: { icon: "database", color: "#f55385" },
  tex: { icon: CODE, color: "#8dc149" }, bib: { icon: "book", color: "#8dc149" },
  png: { icon: "file-media", color: "#a074c4" }, jpg: { icon: "file-media", color: "#a074c4" }, jpeg: { icon: "file-media", color: "#a074c4" },
  gif: { icon: "file-media", color: "#a074c4" }, webp: { icon: "file-media", color: "#a074c4" }, svg: { icon: "file-media", color: "#e37933" },
  ico: { icon: "file-media", color: "#cbcb41" }, bmp: { icon: "file-media", color: "#a074c4" },
  pdf: { icon: "file-pdf", color: "#cc3e44" }, zip: { icon: "file-zip", color: "#8a8a8a" }, gz: { icon: "file-zip", color: "#8a8a8a" },
  tar: { icon: "file-zip", color: "#8a8a8a" }, whl: { icon: "file-zip", color: "#8a8a8a" },
  exe: { icon: "file-binary", color: "#8a8a8a" }, dll: { icon: "file-binary", color: "#8a8a8a" }, bin: { icon: "file-binary", color: "#8a8a8a" },
  gguf: { icon: "file-binary", color: "#8a8a8a" }, pt: { icon: "file-binary", color: "#8a8a8a" },
};

const byName: Record<string, FileIcon> = {
  dockerfile: { icon: "package", color: "#519aba" }, makefile: { icon: "tools", color: "#e37933" },
  license: { icon: "law", color: "#cbcb41" }, ".gitignore": { icon: "git-commit", color: "#e37933" },
  ".gitattributes": { icon: "git-commit", color: "#e37933" }, "package.json": { icon: "package", color: "#8dc149" },
  "pyproject.toml": { icon: "package", color: "#4b8bbe" }, "readme.md": { icon: "info", color: "#519aba" },
  "claude.md": { icon: "sparkle", color: "#d97757" }, "agents.md": { icon: "sparkle", color: "#d97757" },
};

export function fileIcon(path: string): FileIcon {
  const name = (path.split("/").pop() ?? path).toLowerCase();
  if (byName[name]) return byName[name];
  const dot = name.lastIndexOf(".");
  return (dot >= 0 && byExtension[name.slice(dot + 1)]) || { icon: "file", color: "var(--dim)" };
}

export function basenameOf(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] ?? path;
}

export function dirnameOf(path: string): string {
  const parts = path.split("/");
  return parts.slice(0, -1).join("/");
}
