import type { ReactNode } from "react";

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <div className={`rounded-xl border border-slate-800 bg-slate-900/50 p-4 ${className}`}>
      {children}
    </div>
  );
}

export function MetricCard({
  label,
  value,
  tone = "default",
}: {
  label: string;
  value: ReactNode;
  tone?: "default" | "good" | "bad";
}) {
  const color =
    tone === "good" ? "text-emerald-400" : tone === "bad" ? "text-rose-400" : "text-slate-100";
  return (
    <Card>
      <div className="text-xs uppercase tracking-wide text-slate-400">{label}</div>
      <div className={`mt-1 text-xl font-semibold ${color}`}>{value}</div>
    </Card>
  );
}

export function Spinner({ label = "Loading…" }: { label?: string }) {
  return <div className="text-slate-400 py-8 text-center">{label}</div>;
}

export function ErrorBox({ message }: { message: string }) {
  return (
    <div className="rounded-lg border border-rose-800 bg-rose-950/40 text-rose-300 p-3 text-sm">
      {message}
    </div>
  );
}

export function Badge({ children }: { children: ReactNode }) {
  return (
    <span className="inline-block rounded-full bg-slate-800 px-2 py-0.5 text-xs text-slate-300">
      {children}
    </span>
  );
}
