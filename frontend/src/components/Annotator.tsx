import { useEffect, useRef, useState } from "react";
import { canvasToBlob, fittedSize, loadImage } from "../imageTools";
import { Icon } from "./Icon";

// Mark up a screenshot before sending it: circle what is wrong, point at it.
// Shapes are kept in image coordinates and flattened into the PNG on save.

type Tool = "pen" | "rect" | "arrow";
interface Shape {
  tool: Tool;
  color: string;
  points: [number, number][];
}

const COLORS = ["#e5322d", "#f2b705", "#2f6fde", "#1f8a4c"];

function drawArrow(ctx: CanvasRenderingContext2D, [x1, y1]: [number, number], [x2, y2]: [number, number], width: number) {
  const angle = Math.atan2(y2 - y1, x2 - x1);
  const head = width * 5;
  ctx.beginPath();
  ctx.moveTo(x1, y1);
  ctx.lineTo(x2, y2);
  ctx.stroke();
  ctx.beginPath();
  ctx.moveTo(x2, y2);
  ctx.lineTo(x2 - head * Math.cos(angle - Math.PI / 7), y2 - head * Math.sin(angle - Math.PI / 7));
  ctx.lineTo(x2 - head * Math.cos(angle + Math.PI / 7), y2 - head * Math.sin(angle + Math.PI / 7));
  ctx.closePath();
  ctx.fill();
}

function drawShape(ctx: CanvasRenderingContext2D, shape: Shape, width: number) {
  ctx.strokeStyle = ctx.fillStyle = shape.color;
  ctx.lineWidth = width;
  ctx.lineCap = ctx.lineJoin = "round";
  const [first, last] = [shape.points[0], shape.points[shape.points.length - 1]];
  if (!first) return;
  if (shape.tool === "pen") {
    ctx.beginPath();
    ctx.moveTo(...first);
    for (const p of shape.points.slice(1)) ctx.lineTo(...p);
    ctx.stroke();
  } else if (shape.tool === "rect") {
    ctx.strokeRect(first[0], first[1], last[0] - first[0], last[1] - first[1]);
  } else {
    drawArrow(ctx, first, last, width);
  }
}

export function Annotator({ image, name, onSave, onClose }: {
  image: Blob;
  name: string;
  onSave: (blob: Blob) => void;
  onClose: () => void;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [picture, setPicture] = useState<HTMLImageElement | null>(null);
  const [shapes, setShapes] = useState<Shape[]>([]);
  const [drawing, setDrawing] = useState<Shape | null>(null);
  const [tool, setTool] = useState<Tool>("rect");
  const [color, setColor] = useState(COLORS[0]);
  const [error, setError] = useState("");

  useEffect(() => {
    loadImage(image).then(setPicture).catch((e) => setError(String(e.message ?? e)));
  }, [image]);

  const lineWidth = picture ? Math.max(3, Math.round(Math.max(...fittedSize(picture.naturalWidth, picture.naturalHeight)) / 250)) : 3;

  useEffect(() => {
    const el = canvas.current;
    if (!el || !picture) return;
    const [width, height] = fittedSize(picture.naturalWidth, picture.naturalHeight);
    if (el.width !== width) el.width = width;
    if (el.height !== height) el.height = height;
    const ctx = el.getContext("2d")!;
    ctx.drawImage(picture, 0, 0, width, height);
    for (const shape of drawing ? [...shapes, drawing] : shapes) drawShape(ctx, shape, lineWidth);
  }, [picture, shapes, drawing, lineWidth]);

  const point = (e: React.PointerEvent<HTMLCanvasElement>): [number, number] => {
    const rect = e.currentTarget.getBoundingClientRect();
    return [(e.clientX - rect.left) * (e.currentTarget.width / rect.width),
      (e.clientY - rect.top) * (e.currentTarget.height / rect.height)];
  };

  const save = async () => {
    if (!canvas.current) return;
    onSave(await canvasToBlob(canvas.current));
  };

  return (
    <div className="modal-backdrop">
      <div className="modal annotator" role="dialog" aria-label={`Mark up ${name}`}>
        <div className="modal-head">
          <strong>Mark up {name}</strong>
          <span className="dim small">draw on the image to show the agent what you mean</span>
          <div className="spacer" />
          <button className="icon-button" onClick={onClose} aria-label="Close"><Icon name="x" /></button>
        </div>
        <div className="annotator-tools">
          {(["rect", "arrow", "pen"] as Tool[]).map((t) => (
            <button key={t} className={tool === t ? "active" : ""} onClick={() => setTool(t)} title={t === "rect" ? "Box" : t === "arrow" ? "Arrow" : "Pen"}>
              <Icon name={t === "rect" ? "square" : t} size={14} /> {t === "rect" ? "Box" : t === "arrow" ? "Arrow" : "Pen"}
            </button>
          ))}
          <span className="color-swatches">
            {COLORS.map((c) => (
              <button key={c} className={`swatch ${c === color ? "active" : ""}`} style={{ background: c }}
                onClick={() => setColor(c)} aria-label={`colour ${c}`} />
            ))}
          </span>
          <button onClick={() => setShapes(shapes.slice(0, -1))} disabled={!shapes.length}><Icon name="undo" size={14} /> Undo</button>
          <button onClick={() => setShapes([])} disabled={!shapes.length}>Clear</button>
        </div>
        <div className="annotator-canvas">
          {error ? <div className="info tone-error">{error}</div> : (
            <canvas
              ref={canvas}
              onPointerDown={(e) => {
                e.currentTarget.setPointerCapture(e.pointerId);
                const p = point(e);
                setDrawing({ tool, color, points: [p, p] });
              }}
              onPointerMove={(e) => {
                if (!drawing) return;
                const p = point(e);
                setDrawing({ ...drawing, points: drawing.tool === "pen" ? [...drawing.points, p] : [drawing.points[0], p] });
              }}
              onPointerUp={() => {
                if (drawing) setShapes([...shapes, drawing]);
                setDrawing(null);
              }}
            />
          )}
        </div>
        <div className="modal-actions">
          <div className="spacer" />
          <button onClick={onClose}>Cancel</button>
          <button className="primary" onClick={save} disabled={!picture}><Icon name="check" size={14} /> Use this image</button>
        </div>
      </div>
    </div>
  );
}
