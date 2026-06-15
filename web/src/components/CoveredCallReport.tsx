import { useMemo, useState } from "react";
import {
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ResponsiveContainer,
  Scatter,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { formatInr } from "../lib/format";
import type { Campaign, CampaignCall, EquityTranche, OptionsReportData } from "../types";

function pnlClass(v: number): string {
  return v > 0 ? "text-emerald-400" : v < 0 ? "text-rose-400" : "text-slate-400";
}

const kFmt = (v: number) => (Math.abs(v) >= 1000 ? `${(v / 1000).toFixed(0)}k` : `${v}`);

type ChartPoint = {
  date: string;
  underlying: number;
  strike: number | null;
  buy: number | null;
  called: number | null;
};

/** Build the timeline series: underlying price line, the active (short) call strike as a
 *  step line, tranche-buy dots, and called-away dots — all on the shared index axis. */
function buildChart(
  prices: { date: string; close: number }[],
  calls: CampaignCall[],
  tranches: EquityTranche[],
  calledDates: Set<string>,
): ChartPoint[] {
  const buyDates = new Set(tranches.map((t) => t.date));
  return prices.map((p) => {
    let strike: number | null = null;
    let bestEntry = "";
    for (const c of calls) {
      const live = c.entry_date <= p.date && (!c.exit_date || p.date <= c.exit_date);
      if (live && c.entry_date >= bestEntry) {
        strike = c.strike;
        bestEntry = c.entry_date;
      }
    }
    return {
      date: p.date,
      underlying: p.close,
      strike,
      buy: buyDates.has(p.date) ? p.close : null,
      called: calledDates.has(p.date) ? p.close : null,
    };
  });
}

function TimelineChart({
  prices,
  calls,
  tranches,
  calledDates,
  height = 240,
}: {
  prices: { date: string; close: number }[];
  calls: CampaignCall[];
  tranches: EquityTranche[];
  calledDates: Set<string>;
  height?: number;
}) {
  const data = useMemo(
    () => buildChart(prices, calls, tranches, calledDates),
    [prices, calls, tranches, calledDates],
  );
  if (data.length === 0) return null;
  return (
    <ResponsiveContainer width="100%" height={height}>
      <ComposedChart data={data} margin={{ top: 5, right: 10, bottom: 0, left: 10 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#1e293b" />
        <XAxis dataKey="date" tick={{ fontSize: 11, fill: "#94a3b8" }} minTickGap={40} />
        <YAxis
          tick={{ fontSize: 11, fill: "#94a3b8" }}
          width={52}
          domain={["auto", "auto"]}
          tickFormatter={kFmt}
        />
        <Tooltip
          contentStyle={{ background: "#0f172a", border: "1px solid #334155" }}
          formatter={(v: number, name: string) => [Math.round(v), name]}
        />
        <Legend wrapperStyle={{ fontSize: 11 }} />
        <Line
          type="monotone"
          dataKey="underlying"
          name="Underlying"
          stroke="#38bdf8"
          dot={false}
          strokeWidth={1.5}
        />
        <Line
          type="stepAfter"
          dataKey="strike"
          name="Short call strike"
          stroke="#f59e0b"
          dot={false}
          strokeWidth={1.5}
          connectNulls={false}
        />
        <Scatter dataKey="buy" name="Tranche buy" fill="#10b981" shape="circle" />
        <Scatter dataKey="called" name="Called away" fill="#f43f5e" shape="diamond" />
      </ComposedChart>
    </ResponsiveContainer>
  );
}

function StatusChip({ status }: { status: Campaign["status"] }) {
  const called = status === "called_away";
  return (
    <span
      className="inline-block rounded-full px-2 py-0.5 text-xs font-medium"
      style={{
        background: called ? "#f59e0b22" : "#38bdf822",
        color: called ? "#f59e0b" : "#38bdf8",
      }}
    >
      {called ? "called away" : "open"}
    </span>
  );
}

function CampaignCard({
  campaign,
  prices,
}: {
  campaign: Campaign;
  prices: { date: string; close: number }[];
}) {
  const [open, setOpen] = useState(false);
  const realized = campaign.equity_realized + campaign.equity_open;
  const slice = useMemo(() => {
    const end = campaign.end ?? (prices.length ? prices[prices.length - 1].date : campaign.start);
    return prices.filter((p) => p.date >= campaign.start && p.date <= end);
  }, [prices, campaign.start, campaign.end]);
  const calledDates = useMemo(
    () => new Set(campaign.end && campaign.status === "called_away" ? [campaign.end] : []),
    [campaign.end, campaign.status],
  );

  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900/40">
      <button
        className="w-full flex flex-wrap items-center gap-x-4 gap-y-1 px-3 py-2 text-left hover:bg-slate-800/40"
        onClick={() => setOpen((v) => !v)}
      >
        <span className="text-slate-500 w-3">{open ? "▾" : "▸"}</span>
        <span className="text-sm font-medium whitespace-nowrap">
          {campaign.start} → {campaign.end ?? "now"}
        </span>
        <StatusChip status={campaign.status} />
        <span className="text-xs text-slate-400">
          {campaign.units.toLocaleString("en-IN")} units @ ₹{campaign.avg_cost.toFixed(2)}
        </span>
        <span className="text-xs text-slate-400">{campaign.n_calls} calls</span>
        <span className="text-xs text-slate-400">
          premium {formatInr(campaign.premium_collected)}
        </span>
        <span className={`ml-auto text-sm font-semibold ${pnlClass(campaign.combined_net)}`}>
          {formatInr(campaign.combined_net)}
          <span className="text-[11px] font-normal text-slate-500">
            {campaign.status === "open" ? " (incl. open)" : " net"}
          </span>
        </span>
      </button>
      {open && (
        <div className="px-3 pb-3 space-y-3">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-xs">
            <Stat label="Equity P&L" value={formatInr(realized)} tone={realized} />
            <Stat label="Option P&L (net)" value={formatInr(campaign.option_net)} tone={campaign.option_net} />
            <Stat
              label={campaign.status === "called_away" ? "Sold at" : "Marked at"}
              value={`₹${(campaign.exit_price ?? campaign.mark ?? 0).toFixed(2)}`}
            />
            <Stat label="Combined net" value={formatInr(campaign.combined_net)} tone={campaign.combined_net} />
          </div>

          <TimelineChart
            prices={slice}
            calls={campaign.calls}
            tranches={campaign.tranches}
            calledDates={calledDates}
            height={200}
          />

          <div className="grid md:grid-cols-2 gap-3">
            <div>
              <div className="text-[11px] uppercase tracking-wide text-slate-500 mb-1">
                Accumulation (tranches)
              </div>
              <table className="w-full text-xs">
                <thead className="text-slate-500 text-left">
                  <tr>
                    <th className="py-0.5 pr-3">Date</th>
                    <th className="py-0.5 pr-3">Stage</th>
                    <th className="py-0.5 pr-3 text-right">Units</th>
                    <th className="py-0.5 text-right">Price</th>
                  </tr>
                </thead>
                <tbody>
                  {campaign.tranches.map((t, i) => (
                    <tr key={i} className="border-t border-slate-800/60">
                      <td className="py-0.5 pr-3 whitespace-nowrap">{t.date}</td>
                      <td className="py-0.5 pr-3">{t.tag || "buy"}</td>
                      <td className="py-0.5 pr-3 text-right">{t.units.toLocaleString("en-IN")}</td>
                      <td className="py-0.5 text-right">₹{t.price.toFixed(2)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div>
              <div className="text-[11px] uppercase tracking-wide text-slate-500 mb-1">
                Calls sold / rolled
              </div>
              <table className="w-full text-xs">
                <thead className="text-slate-500 text-left">
                  <tr>
                    <th className="py-0.5 pr-3">Sold</th>
                    <th className="py-0.5 pr-3 text-right">Strike</th>
                    <th className="py-0.5 pr-3 text-right">Premium</th>
                    <th className="py-0.5 pr-3">Exit</th>
                    <th className="py-0.5 text-right">P&L</th>
                  </tr>
                </thead>
                <tbody>
                  {campaign.calls.map((c, i) => (
                    <tr key={i} className="border-t border-slate-800/60">
                      <td className="py-0.5 pr-3 whitespace-nowrap">{c.entry_date}</td>
                      <td className="py-0.5 pr-3 text-right">{c.strike}</td>
                      <td className="py-0.5 pr-3 text-right">{formatInr(c.entry_premium, 2)}</td>
                      <td className="py-0.5 pr-3 whitespace-nowrap text-slate-400">{c.exit_reason}</td>
                      <td className={`py-0.5 text-right ${pnlClass(c.net_pnl)}`}>{formatInr(c.net_pnl)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: number }) {
  return (
    <div className="rounded bg-slate-800/50 px-2 py-1.5">
      <div className="text-[10px] uppercase tracking-wide text-slate-500">{label}</div>
      <div className={`text-sm font-medium ${tone != null ? pnlClass(tone) : "text-slate-200"}`}>
        {value}
      </div>
    </div>
  );
}

export default function CoveredCallReport({ options }: { options: OptionsReportData }) {
  const campaigns = options.campaigns ?? [];
  const prices = options.timeline?.prices ?? [];

  const allCalls = useMemo(() => campaigns.flatMap((c) => c.calls), [campaigns]);
  const allTranches = useMemo(() => campaigns.flatMap((c) => c.tranches), [campaigns]);
  const allCalled = useMemo(
    () => new Set(campaigns.filter((c) => c.status === "called_away" && c.end).map((c) => c.end as string)),
    [campaigns],
  );
  if (campaigns.length === 0) return null;

  return (
    <div className="space-y-3">
      {prices.length > 0 && (
        <div className="rounded-lg border border-slate-800 p-3">
          <div className="text-sm font-medium text-slate-300 mb-1">
            Accumulation &amp; calls over time
            <span className="text-slate-500"> — {options.timeline?.underlying} price, the short-call strike (step), tranche buys (•) and called-away (◆)</span>
          </div>
          <TimelineChart
            prices={prices}
            calls={allCalls}
            tranches={allTranches}
            calledDates={allCalled}
            height={300}
          />
        </div>
      )}

      <div>
        <div className="text-sm font-medium text-slate-300 mb-2">
          Campaigns <span className="text-slate-500">({campaigns.length} · accumulate → sell calls → called away · click to expand)</span>
        </div>
        <div className="space-y-2">
          {campaigns.map((c, i) => (
            <CampaignCard key={`${c.start}-${i}`} campaign={c} prices={prices} />
          ))}
        </div>
      </div>
    </div>
  );
}
