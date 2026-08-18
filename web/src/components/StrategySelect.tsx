import { groupedStrategyOptions } from "../lib/strategyMeta";

/** THE strategy dropdown — grouped (intraday / positional / equity) with human names,
 *  shared by the backtest forms and the deploy page so the organisation can't drift.
 *  Values stay the raw strategy ids; only the labels are humanised. */
export default function StrategySelect({ value, onChange, ids, className }: {
  value: string; onChange: (id: string) => void; ids: string[]; className: string;
}) {
  const groups = groupedStrategyOptions(ids);
  return (
    <select className={className} value={value} onChange={(e) => onChange(e.target.value)}>
      {groups.map((g) => (
        <optgroup key={g.label} label={g.label}>
          {g.options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
        </optgroup>
      ))}
    </select>
  );
}
