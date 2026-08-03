/** The Skais brand — mark + wordmark per the design handoff (design_handoff_skais_logo,
 * 2026-08-03). HIGH-FIDELITY: geometry and colours are final — a 1px drift or an
 * approximated red is a defect. Do not round caps/joins, add effects, or move the fork.
 *
 * Mark: a sheared S from two square brackets + the K's fork, three stroked open paths on
 * a 120×120 grid, butt caps + miter joins. Wordmark: live text, Archivo 800, SKAIS with
 * the I in signal red (never more than that one character). In-app header lockup: mark
 * 24px + wordmark 17px. DISPLAY-ONLY brand — code identifiers (skas_algo, SKAS_* env)
 * keep the old name. */

export const SKAIS_RED = "#ec3013";
export const SKAIS_INK = "#201e1d";

export function SkaisMark({
  size = 24,
  letter = "#ffffff", // mark-on-dark default (our navbar/login are dark surfaces)
  fork = SKAIS_RED,
}: {
  size?: number;
  letter?: string;
  fork?: string;
}) {
  return (
    <svg viewBox="0 0 120 120" width={size} height={size} role="img" aria-label="Skais">
      <g fill="none" strokeLinecap="butt" strokeLinejoin="miter">
        <path d="M106 24 L34 24 L34 69" stroke={letter} strokeWidth={18} />
        <path d="M86 51 L86 96 L14 96" stroke={letter} strokeWidth={18} />
        <path d="M70 44 L54 60 L70 76" stroke={fork} strokeWidth={10} />
      </g>
    </svg>
  );
}

export function SkaisWordmark({
  size = 17,
  color = "#ffffff", // on dark; pass SKAIS_INK on light surfaces
}: {
  size?: number;
  color?: string;
}) {
  return (
    <span
      style={{
        fontFamily: "Archivo, sans-serif",
        fontWeight: 800,
        fontSize: size,
        letterSpacing: "-0.03em",
        lineHeight: 1,
        color,
        textTransform: "uppercase",
      }}
    >
      SKA<span style={{ color: SKAIS_RED }}>I</span>S
    </span>
  );
}

/** Horizontal lockup for the in-app header: mark 24 + wordmark 17 (handoff ratios),
 *  gap 0.41 × font-size. `suffix` is a muted product label outside the brand lockup. */
export default function Brand({
  size = 17,
  suffix = "Algo",
}: {
  size?: number;
  suffix?: string | null;
}) {
  const mark = Math.round(size * 1.41); // 24 at the 17px header reference
  const gap = Math.round(size * 0.41);
  return (
    <span className="inline-flex items-center" style={{ gap }}>
      <SkaisMark size={mark} />
      <SkaisWordmark size={size} />
      {suffix ? (
        <span className="font-medium text-slate-400" style={{ fontSize: size - 2 }}>
          {suffix}
        </span>
      ) : null}
    </span>
  );
}
