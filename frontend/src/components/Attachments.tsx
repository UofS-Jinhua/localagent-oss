import { useEffect, useState } from "react";
import { attachmentUrl, type AttachmentInfo } from "../api";
import { Icon } from "./Icon";

export function formatSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

export function describeAttachment(a: Pick<AttachmentInfo, "kind" | "size" | "pages" | "lines">): string {
  if (a.kind === "pdf") return a.pages ? `PDF · ${a.pages} pages` : "PDF";
  if (a.kind === "text") return `${a.lines.toLocaleString()} lines`;
  return formatSize(a.size);
}

/** An object URL for a stored attachment, or null while it loads. */
export function useAttachmentUrl(id: string | undefined): string | null {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!id) return;
    let alive = true;
    attachmentUrl(id).then((u) => alive && setUrl(u)).catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [id]);
  return url;
}

/** A stored attachment in the transcript: image thumbnail (click to enlarge) or a document chip. */
export function AttachmentView({ attachment, onOpen }: { attachment: AttachmentInfo; onOpen: (a: AttachmentInfo) => void }) {
  const url = useAttachmentUrl(attachment.kind === "image" ? attachment.id : undefined);
  const title = `${attachment.path ?? attachment.name} · ${describeAttachment(attachment)}`;
  if (attachment.kind === "image") {
    return (
      <button className="thumb" title={title} onClick={() => onOpen(attachment)}>
        {url ? <img src={url} alt={attachment.name} /> : <span className="thumb-placeholder" />}
      </button>
    );
  }
  return (
    <button className="doc-chip" title={title} onClick={() => onOpen(attachment)}>
      <Icon name="file" size={14} />
      <span className="doc-name">{attachment.name}</span>
      <span className="dim small">{describeAttachment(attachment)}</span>
    </button>
  );
}

export function AttachmentList({ attachments, onOpen }: { attachments: AttachmentInfo[]; onOpen: (a: AttachmentInfo) => void }) {
  if (!attachments.length) return null;
  return (
    <div className="attachment-list">
      {attachments.map((a) => <AttachmentView key={a.id} attachment={a} onOpen={onOpen} />)}
    </div>
  );
}

/** Full-size view of an image attachment; documents open in a new tab. */
export function AttachmentViewer({ attachment, onClose }: { attachment: AttachmentInfo; onClose: () => void }) {
  const url = useAttachmentUrl(attachment.id);
  useEffect(() => {
    if (attachment.kind !== "image" && url) {
      window.open(url, "_blank");
      onClose();
    }
  }, [attachment.kind, url, onClose]);
  if (attachment.kind !== "image") return null;
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="viewer" onClick={(e) => e.stopPropagation()}>
        <div className="modal-head">
          <strong>{attachment.name}</strong>
          <span className="dim small">{attachment.path}</span>
          <div className="spacer" />
          <button className="icon-button" onClick={onClose} aria-label="Close"><Icon name="x" /></button>
        </div>
        {url && <img src={url} alt={attachment.name} />}
      </div>
    </div>
  );
}
