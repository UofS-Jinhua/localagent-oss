import { useEffect, useRef, useState } from "react";
import { Icon } from "./Icon";

/** Put *text* on the clipboard; falls back to the old selection trick where the Clipboard API is refused. */
export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const copied = document.execCommand("copy");
    area.remove();
    return copied;
  }
}

export function CopyButton({ text, label, title = "Copy", className = "" }: {
  text: string;
  label?: string;
  title?: string;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  return (
    <button className={`copy-button ${className}`} title={copied ? "Copied" : title}
      onClick={async (e) => {
        e.stopPropagation();
        if (await copyText(text)) {
          setCopied(true);
          window.clearTimeout(timer.current);
          timer.current = window.setTimeout(() => setCopied(false), 1200);
        }
      }}>
      <Icon name={copied ? "check" : "copy"} size={13} />
      {label && <span>{copied ? "Copied" : label}</span>}
    </button>
  );
}
