import { useMemo, useState } from "react";

export interface FileDiff {
  path: string;
  /** A (new file), D (deleted) or M. */
  status: string;
  lines: string[];
  added: number;
  removed: number;
}

/** The files of a unified diff, each with its lines and counts. */
export function parse(diff: string): FileDiff[] {
  const files: FileDiff[] = [];
  let current: FileDiff | null = null;
  for (const line of diff.split("\n")) {
    if (line.startsWith("diff --git ")) {
      const match = line.match(/ b\/(.*)$/);
      current = { path: match ? match[1] : line, status: "M", lines: [], added: 0, removed: 0 };
      files.push(current);
    } else if (current && current.lines.length === 0 && line.startsWith("new file")) {
      current.status = "A";
    } else if (current && current.lines.length === 0 && line.startsWith("deleted file")) {
      current.status = "D";
    } else if (current && !line.startsWith("index ") && !line.startsWith("--- ") && !line.startsWith("+++ ")) {
      current.lines.push(line);
      if (line.startsWith("+")) current.added++;
      else if (line.startsWith("-")) current.removed++;
    }
  }
  return files;
}

function lineClass(line: string): string {
  if (line.startsWith("@@")) return "hunk";
  if (line.startsWith("+")) return "add";
  if (line.startsWith("-")) return "del";
  return "";
}

function File({ file }: { file: FileDiff }) {
  const [open, setOpen] = useState(true);
  return (
    <div className="diff-file">
      <button className="diff-file-head" onClick={() => setOpen(!open)}>
        <span>{open ? "▾" : "▸"} {file.path}</span>
        <span><span className="add-count">+{file.added}</span> <span className="del-count">−{file.removed}</span></span>
      </button>
      {open && (
        <pre className="diff-body">
          {file.lines.map((line, i) => <div key={i} className={lineClass(line)}>{line || " "}</div>)}
        </pre>
      )}
    </div>
  );
}

export function DiffView({ diff }: { diff: string }) {
  const files = useMemo(() => parse(diff), [diff]);
  if (!files.length) return <div className="empty">No changes yet.</div>;
  return <div className="diff">{files.map((f) => <File key={f.path} file={f} />)}</div>;
}
