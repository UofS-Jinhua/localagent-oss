import type { ModelInfo } from "../api";
import { Codicon } from "./Codicon";

// On a one-GPU server, whether a model is loaded is the thing to know: picking one that is not makes
// the server swap it in. The model quick pick (App.tsx) shows it with these dots.

/** Loaded on its server right now: a breathing green dot. Local, not loaded: an empty ring. Cloud: a cloud. */
export function ModelDot({ model }: { model: ModelInfo | undefined }) {
  if (!model) return <span className="model-dot idle" />;
  if (!model.local) {
    return <span className="model-dot cloud" title="cloud model"><Codicon name="cloud" /></span>;
  }
  return model.loaded
    ? <span className="model-dot loaded" title="loaded on its server now" />
    : <span className="model-dot idle" title="not loaded: choosing it makes the server load it" />;
}

export const MODEL_GROUPS = ["Loaded now", "Local, not loaded", "Cloud"];

export function modelGroup(model: ModelInfo | undefined): string {
  return !model ? "" : !model.local ? "Cloud" : model.loaded ? "Loaded now" : "Local, not loaded";
}

export function modelDetails(model: ModelInfo | undefined): string {
  if (!model) return "";
  const context = model.context_length >= 1000 ? `${Math.round(model.context_length / 1000)}K` : `${model.context_length}`;
  return [`${context} context`, model.vision ? "sees images" : "", model.provider].filter(Boolean).join(" · ");
}
