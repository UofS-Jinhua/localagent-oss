import { useEffect, useLayoutEffect, useRef, useState, type RefObject } from "react";
import { api, type AttachmentInfo } from "../api";
import { prepareImage } from "../imageTools";
import { Annotator } from "./Annotator";
import { describeAttachment } from "./Attachments";
import { Icon } from "./Icon";
import { MentionPicker, mentionAt } from "./Mentions";

interface Props {
  mode: "idle" | "running-here" | "running-elsewhere" | "no-folder";
  onRun: (text: string, attachments: AttachmentInfo[]) => void;
  onSteer: (text: string, attachments: AttachmentInfo[]) => void;
  onStop: () => void;
  /** The model that will read the message, to warn when it cannot see images. */
  model: { name: string; vision: boolean } | null;
  /** Vision models the user could switch to instead. */
  visionModels: string[];
  onPickModel: (name: string) => void;
  /** Files of the working folder, offered after "@". */
  files: string[];
}

interface Pending {
  key: number;
  name: string;
  blob: Blob;
  preview: string | null; // object URL for images
  info: AttachmentInfo | null;
  error: string;
}

const placeholders = {
  "idle": "Describe a task… (Enter to run, Shift+Enter for a new line; paste or drop screenshots and documents)",
  "running-here": "Message the running task — it reads this before its next step",
  "running-elsewhere": "Another chat is running; wait for it or stop it",
  "no-folder": "Choose a working folder first",
};

let nextKey = 1;

function pastedName(blob: Blob): string {
  const stamp = new Date().toISOString().slice(0, 19).replace(/[-:T]/g, "");
  return `pasted-${stamp}.${blob.type === "image/jpeg" ? "jpg" : "png"}`;
}

/** Grow the box with its text, up to a share of the window, so a long task stays readable while it is typed. */
function useAutoGrow(textarea: RefObject<HTMLTextAreaElement | null>, text: string): void {
  useLayoutEffect(() => {
    const box = textarea.current;
    if (!box) return;
    box.style.height = "auto";
    box.style.height = `${Math.min(box.scrollHeight, Math.max(160, window.innerHeight * 0.4))}px`;
  }, [textarea, text]);
}

export function Composer({ mode, onRun, onSteer, onStop, model, visionModels, onPickModel, files }: Props) {
  const [text, setText] = useState("");
  const [mention, setMention] = useState<{ query: string; start: number } | null>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);
  useAutoGrow(textarea, text);
  const [items, setItems] = useState<Pending[]>([]);
  const [editing, setEditing] = useState<Pending | null>(null);
  const [dragging, setDragging] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const disabled = mode === "running-elsewhere" || mode === "no-folder";
  const uploading = items.some((i) => !i.info && !i.error);
  const failed = items.filter((i) => i.error);

  useEffect(() => () => items.forEach((i) => i.preview && URL.revokeObjectURL(i.preview)), []); // eslint-disable-line react-hooks/exhaustive-deps

  const upload = (key: number, blob: Blob, name: string) => {
    api.upload(blob, name)
      .then((info) => setItems((all) => all.map((i) => (i.key === key ? { ...i, info, error: "" } : i))))
      .catch((e) => setItems((all) => all.map((i) => (i.key === key ? { ...i, error: String(e.message ?? e) } : i))));
  };

  const add = async (files: File[] | Blob[]) => {
    for (const original of files) {
      const isImage = original.type.startsWith("image/");
      const name = original instanceof File && original.name && original.name !== "image.png" ? original.name : isImage ? pastedName(original) : "attachment";
      let blob: Blob = original;
      if (isImage) {
        try {
          blob = await prepareImage(original);
        } catch {
          /* not decodable here: send as it is */
        }
      }
      const item: Pending = { key: nextKey++, name, blob, preview: isImage ? URL.createObjectURL(blob) : null, info: null, error: "" };
      setItems((all) => [...all, item]);
      upload(item.key, blob, name);
    }
  };

  const replace = (item: Pending, blob: Blob) => {
    const name = item.name.replace(/(\.[^.]+)?$/, (ext) => (item.name.includes("-marked") ? ext : "-marked.png"));
    if (item.preview) URL.revokeObjectURL(item.preview);
    const updated: Pending = { ...item, name, blob, preview: URL.createObjectURL(blob), info: null, error: "" };
    setItems((all) => all.map((i) => (i.key === item.key ? updated : i)));
    upload(item.key, blob, name);
    setEditing(null);
  };

  const remove = (item: Pending) => {
    if (item.preview) URL.revokeObjectURL(item.preview);
    setItems((all) => all.filter((i) => i.key !== item.key));
  };

  const retry = (item: Pending) => {
    setItems((all) => all.map((i) => (i.key === item.key ? { ...i, error: "" } : i)));
    upload(item.key, item.blob, item.name);
  };

  /** Replace the "@query" being typed with the chosen path. */
  const completeMention = (path: string) => {
    if (!mention) return;
    const caret = (textarea.current?.selectionStart ?? text.length);
    const next = `${text.slice(0, mention.start)}${path} ${text.slice(caret)}`;
    setText(next);
    setMention(null);
    requestAnimationFrame(() => {
      const at = mention.start + path.length + 1;
      textarea.current?.focus();
      textarea.current?.setSelectionRange(at, at);
    });
  };

  const submit = () => {
    if (mention) return; // Enter belongs to the file picker
    const value = text.trim();
    const ready = items.filter((i) => i.info).map((i) => i.info!);
    // Never send silently without an attachment the user added: they must retry or remove it.
    if (disabled || uploading || failed.length || (!value && !(mode === "running-here" && ready.length))) return;
    if (mode === "running-here") onSteer(value, ready);
    else onRun(value, ready);
    setText("");
    items.forEach((i) => i.preview && URL.revokeObjectURL(i.preview));
    setItems([]);
  };

  const images = items.filter((i) => i.blob.type.startsWith("image/"));
  const blind = model && !model.vision && images.length > 0;

  return (
    <div
      className={`composer ${dragging ? "dragging" : ""}`}
      onDragOver={(e) => {
        if (disabled || !e.dataTransfer.types.includes("Files")) return;
        e.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={(e) => {
        setDragging(false);
        if (disabled || !e.dataTransfer.files.length) return;
        e.preventDefault();
        add(Array.from(e.dataTransfer.files));
      }}
    >
      <div className="composer-main">
        {items.length > 0 && (
          <div className="pending-attachments">
            {items.map((item) => (
              <div key={item.key} className={`pending ${item.error ? "failed" : ""}`} title={item.error || item.name}>
                {item.preview ? (
                  <button className="thumb" onClick={() => setEditing(item)} title="Click to mark up the image">
                    <img src={item.preview} alt={item.name} />
                    <span className="thumb-hint"><Icon name="pen" size={12} /></span>
                  </button>
                ) : (
                  <span className="doc-chip">
                    <Icon name="file" size={14} />
                    <span className="doc-name">{item.name}</span>
                    {item.info && <span className="dim small">{describeAttachment(item.info)}</span>}
                  </span>
                )}
                <span className="pending-state small">
                  {item.error ? <button className="link-button" onClick={() => retry(item)}>failed — retry</button>
                    : item.info ? "" : "uploading…"}
                </span>
                <button className="remove" onClick={() => remove(item)} aria-label={`Remove ${item.name}`}><Icon name="x" size={12} /></button>
              </div>
            ))}
          </div>
        )}
        {blind && (
          <div className="vision-warning small">
            <Icon name="eye" size={14} />
            <span>{model!.name} cannot view images; it only gets their file paths.</span>
            {visionModels.slice(0, 3).map((name) => (
              <button key={name} className="link-button" onClick={() => onPickModel(name)}>use {name}</button>
            ))}
          </div>
        )}
        {mention && files.length > 0 && (
          <MentionPicker files={files} query={mention.query} onPick={completeMention} onClose={() => setMention(null)} />
        )}
        <textarea
          ref={textarea}
          value={text}
          disabled={disabled}
          placeholder={placeholders[mode]}
          rows={2}
          onChange={(e) => {
            setText(e.target.value);
            setMention(mentionAt(e.target.value, e.target.selectionStart ?? e.target.value.length));
          }}
          onClick={(e) => setMention(mentionAt(text, e.currentTarget.selectionStart ?? 0))}
          onPaste={(e) => {
            const files = Array.from(e.clipboardData.files);
            if (files.length && !disabled) {
              e.preventDefault();
              add(files);
            }
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              submit();
            }
          }}
        />
      </div>
      <div className="composer-actions">
        {mode === "running-here" && (
          <button className="danger" onClick={onStop} title="Stop the running task">
            <Icon name="stop" /> Stop
          </button>
        )}
        <button onClick={() => fileInput.current?.click()} disabled={disabled} title="Attach images or documents (PDF, text)">
          <Icon name="clip" /> Attach
        </button>
        <input ref={fileInput} type="file" multiple hidden onChange={(e) => {
          add(Array.from(e.target.files ?? []));
          e.target.value = "";
        }} />
        <button className="primary" onClick={submit}
          title={failed.length ? "An attachment failed to upload: retry or remove it" : ""}
          disabled={disabled || uploading || failed.length > 0 || (!text.trim() && !(mode === "running-here" && items.length))}>
          <Icon name="send" /> {mode === "running-here" ? "Send" : "Run"}
        </button>
      </div>
      {editing && (
        <Annotator image={editing.blob} name={editing.name} onClose={() => setEditing(null)}
          onSave={(blob) => replace(editing, blob)} />
      )}
    </div>
  );
}
