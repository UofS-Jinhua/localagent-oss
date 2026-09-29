import { useRef } from "react";

/**
 * The thin handle between two panes: dragging it resizes the pane on its *side*. The width follows the
 * pointer while dragging and *onDone* gets the final one, to be remembered for the next start; a double
 * click goes back to the default (*onReset*).
 */
export function Sash({ side, width, min, max, onWidth, onDone, onReset }: {
  /** Which pane this handle resizes: the one on its left (the side bar) or on its right (the editor area). */
  side: "left" | "right";
  width: number;
  min: number;
  max: number;
  onWidth: (width: number) => void;
  onDone: (width: number) => void;
  onReset: () => void;
}) {
  const start = useRef<{ x: number; width: number; last: number } | null>(null);
  const clamp = (w: number) => Math.round(Math.min(max, Math.max(min, w)));
  return (
    <div className="sash" role="separator" aria-orientation="vertical" aria-valuenow={width}
      onPointerDown={(e) => {
        e.preventDefault();
        e.currentTarget.setPointerCapture(e.pointerId);
        start.current = { x: e.clientX, width, last: width };
        document.body.classList.add("resizing");
      }}
      onPointerMove={(e) => {
        if (!start.current) return;
        const moved = e.clientX - start.current.x;
        const next = clamp(start.current.width + (side === "left" ? moved : -moved));
        start.current.last = next;
        onWidth(next);
      }}
      onPointerUp={(e) => {
        if (!start.current) return;
        e.currentTarget.releasePointerCapture(e.pointerId);
        document.body.classList.remove("resizing");
        onDone(start.current.last);
        start.current = null;
      }}
      onDoubleClick={onReset} />
  );
}
