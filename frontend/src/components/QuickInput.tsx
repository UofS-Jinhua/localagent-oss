import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { fuzzy } from "../fuzzy";

// VS Code's quick input: a box at the top with a filtered list below. It serves the quick open
// (files), the command palette (text starting with ">"), and the pickers (model, machine, theme),
// and asks for a single value (the step limit).

export interface QuickItem {
  id: string;
  label: string;
  description?: string;
  detail?: string;
  icon?: ReactNode;
  /** A keyboard shortcut, shown on the right. */
  keys?: string;
  /** A separator with this label starts where the group changes (only while nothing is typed). */
  group?: string;
  /** Matched characters of the label, to highlight. */
  highlights?: number[];
  checked?: boolean;
  run: () => void;
}

export interface QuickSource {
  placeholder: string;
  items: (text: string) => QuickItem[];
  /** Asking for a value instead of offering a list: Enter hands the text over. */
  accept?: (text: string) => void;
  /** Said under the box while asking for a value; what is wrong with the text, when validate says so. */
  prompt?: string;
  validate?: (text: string) => string;
}

/** Keep the items matching *text* (on the label, else the description), best first, with highlights. */
export function filterItems(items: QuickItem[], text: string): QuickItem[] {
  const query = text.trim();
  if (!query) return items;
  const scored: { item: QuickItem; score: number }[] = [];
  for (const item of items) {
    const onLabel = fuzzy(query, item.label);
    if (onLabel) {
      scored.push({ item: { ...item, highlights: onLabel.positions, group: undefined }, score: onLabel.score + 10 });
      continue;
    }
    const onDescription = item.description ? fuzzy(query, `${item.label} ${item.description}`) : null;
    if (onDescription) scored.push({ item: { ...item, highlights: [], group: undefined }, score: onDescription.score });
  }
  return scored.sort((a, b) => b.score - a.score).map((s) => s.item);
}

export function Highlighted({ text, positions }: { text: string; positions?: number[] }) {
  if (!positions?.length) return <>{text}</>;
  const marked = new Set(positions);
  const parts: ReactNode[] = [];
  let run = "";
  let runMarked = false;
  const push = (key: number) => {
    if (run) parts.push(runMarked ? <mark key={key}>{run}</mark> : <span key={key}>{run}</span>);
    run = "";
  };
  for (let i = 0; i < text.length; i++) {
    const m = marked.has(i);
    if (m !== runMarked) {
      push(i);
      runMarked = m;
    }
    run += text[i];
  }
  push(text.length);
  return <>{parts}</>;
}

export function QuickInput({ source, initial, onClose }: { source: QuickSource; initial: string; onClose: () => void }) {
  const [text, setText] = useState(initial);
  const [active, setActive] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const box = useRef<HTMLDivElement>(null);
  const items = useMemo(() => source.items(text), [source, text]);
  const problem = source.validate?.(text) ?? "";

  useEffect(() => {
    input.current?.focus();
    // The ">" of the command palette stays; the cursor goes after it.
    input.current?.setSelectionRange(initial.length, initial.length);
  }, []);
  useEffect(() => setActive(0), [text]);
  useEffect(() => {
    list.current?.querySelector(".quick-row.active")?.scrollIntoView({ block: "nearest" });
  }, [active, items]);
  useEffect(() => {
    const away = (e: MouseEvent) => {
      if (box.current && !box.current.contains(e.target as Node)) onClose();
    };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, [onClose]);

  const choose = (item: QuickItem | undefined) => {
    if (source.accept && !items.length) {
      if (problem) return;
      onClose();
      source.accept(text);
      return;
    }
    if (!item) return;
    onClose();
    item.run();
  };

  const onKey = (e: KeyboardEvent) => {
    const page = 10;
    if (e.key === "Escape") {
      e.preventDefault();
      onClose();
    } else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (items.length) setActive((i) => (i + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length);
    } else if (e.key === "PageDown" || e.key === "PageUp") {
      e.preventDefault();
      setActive((i) => Math.max(0, Math.min(items.length - 1, i + (e.key === "PageDown" ? page : -page))));
    } else if (e.key === "Enter" && !e.nativeEvent.isComposing) {
      e.preventDefault();
      choose(items[active]);
    }
  };

  return (
    <div className="quick-input" ref={box} role="dialog" aria-label={source.placeholder}>
      <input ref={input} className="quick-box" value={text} placeholder={source.placeholder} spellCheck={false}
        onChange={(e) => setText(e.target.value)} onKeyDown={onKey} aria-activedescendant={items[active]?.id}
        aria-controls="quick-list" role="combobox" aria-expanded={items.length > 0} />
      {source.accept && !items.length ? (
        <div className={`quick-prompt ${problem ? "error" : ""}`}>{problem || source.prompt}</div>
      ) : (
        <div className="quick-list" id="quick-list" ref={list} role="listbox">
          {items.length === 0 && <div className="quick-empty">No matching results</div>}
          {items.slice(0, 300).map((item, i) => {
            const separator = item.group && (i === 0 || items[i - 1].group !== item.group) ? item.group : "";
            return (
              <div key={item.id} id={item.id} role="option" aria-selected={i === active}
                className={`quick-row ${i === active ? "active" : ""} ${separator && i > 0 ? "separated" : ""}`}
                onMouseMove={() => i !== active && setActive(i)} onMouseDown={(e) => e.preventDefault()}
                onClick={() => choose(item)}>
                {item.icon !== undefined && <span className="quick-icon">{item.icon}</span>}
                <span className="quick-text">
                  <span className="quick-label"><Highlighted text={item.label} positions={item.highlights} /></span>
                  {item.description && <span className="quick-description">{item.description}</span>}
                  {item.detail && <span className="quick-detail">{item.detail}</span>}
                </span>
                {separator && <span className="quick-group">{separator}</span>}
                {item.checked && <span className="quick-check codicon codicon-check" />}
                {item.keys && <span className="quick-keys">{item.keys.split("+").map((k, n) => <kbd key={n}>{k}</kbd>)}</span>}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
