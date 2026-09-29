import { useEffect, useMemo, useRef, useState } from "react";

// "@" in the composer offers the folder's files, so the user can point at one
// exactly instead of describing it (and the agent does not have to guess).

export interface MentionState {
  query: string;
  start: number; // index of the "@"
}

/** The "@word" being typed just before the caret, if any. */
export function mentionAt(text: string, caret: number): MentionState | null {
  const before = text.slice(0, caret);
  const at = before.lastIndexOf("@");
  if (at < 0) return null;
  if (at > 0 && !/\s/.test(before[at - 1])) return null; // part of an email or path
  const query = before.slice(at + 1);
  if (/\s/.test(query)) return null;
  return { query, start: at };
}

function score(path: string, query: string): number {
  if (!query) return 0;
  const lower = path.toLowerCase();
  const name = lower.split("/").pop() ?? lower;
  if (name.startsWith(query)) return 0;
  if (name.includes(query)) return 1;
  if (lower.includes(query)) return 2;
  return -1;
}

export function rankFiles(files: string[], query: string, limit = 8): string[] {
  const lower = query.toLowerCase();
  return files
    .map((path) => ({ path, rank: score(path.toLowerCase(), lower) }))
    .filter((entry) => entry.rank >= 0)
    .sort((a, b) => a.rank - b.rank || a.path.length - b.path.length)
    .slice(0, limit)
    .map((entry) => entry.path);
}

export function MentionPicker({ files, query, onPick, onClose }: {
  files: string[];
  query: string;
  onPick: (path: string) => void;
  onClose: () => void;
}) {
  const [active, setActive] = useState(0);
  const matches = useMemo(() => rankFiles(files, query), [files, query]);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => setActive(0), [query]);
  useEffect(() => {
    // The textarea keeps focus, so the keys are handled there; this closes on a click elsewhere.
    const away = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) onClose();
    };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, [onClose]);

  useEffect(() => {
    const keys = (e: KeyboardEvent) => {
      if (!matches.length) return;
      if (e.key === "ArrowDown") setActive((i) => (i + 1) % matches.length);
      else if (e.key === "ArrowUp") setActive((i) => (i - 1 + matches.length) % matches.length);
      else if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        e.stopPropagation();
        onPick(matches[active]);
      } else if (e.key === "Escape") onClose();
      else return;
      if (e.key !== "Enter" && e.key !== "Tab") e.preventDefault();
    };
    document.addEventListener("keydown", keys, true);
    return () => document.removeEventListener("keydown", keys, true);
  }, [matches, active, onPick, onClose]);

  if (!matches.length) return null;
  return (
    <div className="mentions" ref={box}>
      {matches.map((path, index) => (
        <button key={path} className={index === active ? "active" : ""} onMouseEnter={() => setActive(index)}
          onClick={() => onPick(path)}>
          <span className="mention-name">{path.split("/").pop()}</span>
          <span className="dim small">{path}</span>
        </button>
      ))}
    </div>
  );
}
