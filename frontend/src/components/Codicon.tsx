import "@vscode/codicons/dist/codicon.css";

/** One of VS Code's own icons (https://microsoft.github.io/vscode-codicons/dist/codicon.html), by name. */
export function Codicon({ name, className = "", title, spin = false }: {
  name: string;
  className?: string;
  title?: string;
  spin?: boolean;
}) {
  return (
    <i className={`codicon codicon-${name} ${spin ? "codicon-modifier-spin" : ""} ${className}`}
      title={title} aria-hidden={title ? undefined : true} />
  );
}
