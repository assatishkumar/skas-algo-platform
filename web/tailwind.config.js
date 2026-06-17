/** @type {import('tailwindcss').Config} */

// The whole UI uses the `slate` ramp as its grayscale. Routing it through CSS variables lets us
// flip light/dark by swapping the variable values (see index.css) — no component changes needed.
const slate = Object.fromEntries(
  [50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950].map((n) => [
    n,
    `rgb(var(--slate-${n}) / <alpha-value>)`,
  ]),
);

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  // `.dark` on <html> (toggled in lib/theme.ts) drives both the slate vars and the dark:
  // variants used by colored pills/badges (accent colors aren't variable-driven).
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        brand: {
          DEFAULT: "#0f766e",
          light: "#14b8a6",
        },
        slate,
      },
    },
  },
  plugins: [],
};
