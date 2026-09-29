import { useEffect, useState } from "react";

// The colour theme: the system's (the default), or dark or light whatever the system says.
// The choice is a setting the server keeps; CSS reads it from <html data-theme>.

export type ThemeChoice = "system" | "dark" | "light";

const darkQuery = typeof window !== "undefined" ? window.matchMedia("(prefers-color-scheme: dark)") : null;

export function applyTheme(choice: ThemeChoice): void {
  const root = document.documentElement;
  if (choice === "system") delete root.dataset.theme;
  else root.dataset.theme = choice;
}

/** Whether the window is dark right now, following the setting and, under "system", the system. */
export function useDark(choice: ThemeChoice): boolean {
  const [systemDark, setSystemDark] = useState(() => darkQuery?.matches ?? false);
  useEffect(() => {
    if (!darkQuery) return;
    const update = () => setSystemDark(darkQuery.matches);
    darkQuery.addEventListener("change", update);
    return () => darkQuery.removeEventListener("change", update);
  }, []);
  return choice === "system" ? systemDark : choice === "dark";
}
