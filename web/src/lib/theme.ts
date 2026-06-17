// Light/dark theming. Light is the default; the `.dark` class on <html> swaps the slate ramp
// (see index.css). Choice is persisted in localStorage.

export type Theme = "light" | "dark";
const KEY = "skas-theme";

export function getTheme(): Theme {
  return localStorage.getItem(KEY) === "dark" ? "dark" : "light";
}

export function applyTheme(theme: Theme): void {
  document.documentElement.classList.toggle("dark", theme === "dark");
  localStorage.setItem(KEY, theme);
}
