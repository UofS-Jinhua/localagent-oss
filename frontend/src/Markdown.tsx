import DOMPurify from "dompurify";
import { marked } from "marked";
import { useEffect, useMemo, useRef, type MouseEvent } from "react";
import { copyText } from "./components/CopyButton";

marked.setOptions({ gfm: true, breaks: true });

/** Render model-written Markdown; the HTML is sanitized because the model can emit anything. */
export function Markdown({ text, className }: { text: string; className?: string }) {
  const html = useMemo(() => DOMPurify.sanitize(marked.parse(text, { async: false }) as string), [text]);
  const box = useRef<HTMLDivElement>(null);

  // A copy button on every code block, added after rendering so the sanitized HTML stays as it is.
  useEffect(() => {
    for (const pre of box.current?.querySelectorAll("pre") ?? []) {
      if (pre.querySelector(":scope > .copy-code")) continue;
      const button = document.createElement("button");
      button.className = "copy-code";
      button.type = "button";
      button.textContent = "Copy";
      pre.appendChild(button);
    }
  }, [html]);

  const onClick = async (e: MouseEvent<HTMLDivElement>) => {
    const button = (e.target as HTMLElement).closest(".copy-code");
    const pre = button?.parentElement;
    if (!button || !pre) return;
    const code = pre.querySelector("code");
    if (await copyText((code ?? pre).textContent ?? "")) {
      button.textContent = "Copied";
      window.setTimeout(() => (button.textContent = "Copy"), 1200);
    }
  };

  return <div ref={box} className={`markdown ${className ?? ""}`} onClick={onClick} dangerouslySetInnerHTML={{ __html: html }} />;
}
