/** The Skais wordmark — "ai" in bright brand teal (owner rebrand 2026-08-01, option A).
 *
 * DISPLAY-ONLY rebrand: code identifiers (the skas_algo package, SKAS_* env vars, repo,
 * DB) deliberately keep the old name — renaming those would break the VPS deploy and
 * every .env. Prose references ("SKAS never sees your password") say "Skais".
 * Self-colored for dark surfaces (navbar / login card). */
export default function Brand({ suffix = "Algo" }: { suffix?: string | null }) {
  return (
    <span className="font-semibold">
      <span className="text-slate-100">
        Sk<span className="text-[#2dd4bf]">ai</span>s
      </span>
      {suffix ? <span className="font-medium text-slate-400"> {suffix}</span> : null}
    </span>
  );
}
