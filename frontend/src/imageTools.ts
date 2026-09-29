// Image helpers for attachments: load, downscale, export.
// Screenshots from high-DPI screens are large; vision models pay per pixel (a local
// model's prefill especially), so images are capped at MAX_EDGE on their longest side.

export const MAX_EDGE = 1600;

/** Load *source* into a fully decoded image, so drawing it paints every row.
 *
 * `decode()` (not `onload`) is what guarantees the bitmap is ready, and the
 * object URL has to stay alive: revoking it makes later draws come out blank.
 */
export async function loadImage(source: Blob): Promise<HTMLImageElement> {
  const image = new Image();
  image.src = URL.createObjectURL(source);
  try {
    await image.decode();
  } catch {
    URL.revokeObjectURL(image.src);
    throw new Error("could not read the image");
  }
  return image;
}

export function fittedSize(width: number, height: number, maxEdge = MAX_EDGE): [number, number] {
  const scale = Math.min(1, maxEdge / Math.max(width, height));
  return [Math.round(width * scale), Math.round(height * scale)];
}

export function canvasToBlob(canvas: HTMLCanvasElement, type = "image/png"): Promise<Blob> {
  return new Promise((resolve, reject) =>
    canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error("could not export the image"))), type, 0.92));
}

/** The image, downscaled to MAX_EDGE if it is larger (GIFs and small images are kept as they are). */
export async function prepareImage(file: Blob): Promise<Blob> {
  if (file.type === "image/gif") return file;
  const image = await loadImage(file);
  const [width, height] = fittedSize(image.naturalWidth, image.naturalHeight);
  if (width === image.naturalWidth && height === image.naturalHeight) return file;
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  canvas.getContext("2d")!.drawImage(image, 0, 0, width, height);
  return canvasToBlob(canvas, file.type === "image/jpeg" ? "image/jpeg" : "image/png");
}
