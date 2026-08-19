/** Live-page category metadata (design handoff design_handoff_live2): deployments group
 * into three categories — category membership comes from this strategy map, not page
 * logic. Unknown strategies fall back by instrument class.
 *
 * NOTE deviation from the handoff: it listed 21_ema_momentum under Intraday, but that
 * strategy HOLDS across sessions (until the opposite signal) — it belongs in Positional;
 * the category blurb "squared off by market close" would be false for it. */

export type LiveCategoryId = "intraday" | "positional" | "equity";

export const LIVE_CATEGORIES: {
  id: LiveCategoryId;
  name: string;
  desc: string;
  bg: string;
  fg: string;
  icon: string; // pipe-separated svg segments (path | pl:polyline | c:circle)
}[] = [
  {
    id: "intraday",
    name: "Intraday Options",
    desc: "Squared off by market close — no overnight exposure.",
    bg: "var(--warn-bg)", fg: "var(--warn-text)",
    icon: "c:12,12,10|pl:12 6 12 12 16 14",
  },
  {
    id: "positional",
    name: "Positional Options",
    desc: "Held across sessions — weekly & monthly option books.",
    bg: "var(--opt-bg)", fg: "var(--opt-text)",
    icon: "M8 2v4|M16 2v4|M3 10h18|M5 4h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z",
  },
  {
    id: "equity",
    name: "Equity",
    desc: "Cash-equity systems — delivery positions.",
    bg: "var(--ok-bg)", fg: "var(--ok-text)",
    icon: "pl:22 7 13.5 15.5 8.5 10.5 2 17|pl:16 7 22 7 22 13",
  },
];

const CATEGORY_OF: Record<string, LiveCategoryId> = {
  momentum_theta_gainer_intra: "intraday",
  call_put_ratio_expiry: "intraday",
  intraday_straddle: "intraday",
  intraday_strangle_combo: "intraday",
  weekly_intraday_straddle: "intraday",
  donchian_strangle_monthly: "positional",
  custom_options: "positional",
  batman_ratio_monthly: "positional",
  call_ratio_monthly: "positional",
  put_ratio_monthly: "positional",
  hni_weekly: "positional",
  short_premium: "positional",
  staggered_covered_call: "positional",
  "21_ema_momentum": "positional",
  delta_neutral_monthly: "positional",
  iron_fly_monthly: "positional",
  supertrend_momentum: "equity",
  gap_reversal: "equity",
  happy_twins: "equity",
  nifty_shop: "equity",
  custom_equity: "equity",
  sst_lifo: "equity",
  sst_fifo: "equity",
  sst_weekly: "equity",
  sst_weekly_fifo: "equity",
  // additions since the handoff (deploy pages + pickers group off this same map)
  asymmetric_premium_intra: "intraday",
  broker_smoke_test: "intraday",          // a 60-second self-stopping probe
  straddle_btst: "positional",            // holds OVERNIGHT — "intraday" would lie on the blurb
  double_diagonal_calendar: "positional",
  put_condor: "positional",
  donchian_strangle_bt: "positional",
  fair_value_calendar: "positional",      // monthly cycle, weekly rolls — holds overnight
};

/** Human names for the pickers. The raw ids stay the API/URL currency everywhere else —
 *  this is display only, so an unmapped id degrades to its id, never breaks. */
export const STRATEGY_NAMES: Record<string, string> = {
  // equity
  sst_lifo: "SST — LIFO",
  sst_fifo: "SST — FIFO",
  sst_weekly: "SST Weekly",
  sst_weekly_fifo: "SST Weekly — FIFO",
  supertrend_momentum: "SuperTrend Momentum",
  nifty_shop: "Nifty Shop (dip buyer)",
  gap_reversal: "Gap Reversal",
  happy_twins: "Happy Twins (dual SuperTrend, weekly)",
  custom_equity: "Custom Equity Trade",
  // options — positional / overnight
  short_premium: "Short Premium (strangle)",
  call_ratio_monthly: "Call Ratio — Monthly",
  put_ratio_monthly: "Put Ratio — Monthly",
  batman_ratio_monthly: "Batman Ratio — Monthly",
  hni_weekly: "HNI Weekly (1-3-2)",
  staggered_covered_call: "Staggered Covered Call",
  custom_options: "Custom Options (builder)",
  "21_ema_momentum": "21-EMA Momentum Spreads",
  delta_neutral_monthly: "Delta-Neutral — Monthly",
  iron_fly_monthly: "Iron Fly — Monthly",
  double_diagonal_calendar: "Double Diagonal Calendar",
  put_condor: "Put Condor — Monthly (defined risk)",
  fair_value_calendar: "Fair-Value Calendar — Monthly (premium-matched)",
  donchian_strangle_monthly: "Donchian Basket Strangle",
  donchian_strangle_bt: "Donchian Strangle — Backtest",
  straddle_btst: "Straddle BTST (overnight long)",
  // options — intraday
  intraday_straddle: "Intraday Straddle",
  weekly_intraday_straddle: "Weekly VWAP Straddle",
  call_put_ratio_expiry: "Call/Put Ratio — Expiry Day",
  momentum_theta_gainer_intra: "Momentum Theta (15-min ST)",
  intraday_strangle_combo: "Strangle/Straddle Combo (per-leg re-entry)",
  asymmetric_premium_intra: "Asymmetric Premium (two expiries)",
  broker_smoke_test: "Broker Smoke Test",
};

export function strategyName(id: string): string {
  return STRATEGY_NAMES[id] ?? id;
}

const GROUP_LABEL: Record<LiveCategoryId, string> = {
  intraday: "Options — intraday",
  positional: "Options — positional",
  equity: "Equity",
};

/** Group a raw id list into <optgroup> data: intraday options first (the daily drivers),
 *  then positional, then equity — alphabetical by display name inside each group. */
export function groupedStrategyOptions(ids: string[]):
    { label: string; options: { value: string; label: string }[] }[] {
  const buckets: Record<LiveCategoryId, { value: string; label: string }[]> = {
    intraday: [], positional: [], equity: [],
  };
  for (const id of ids) {
    buckets[liveCategoryOf(id, "DERIV")].push({ value: id, label: strategyName(id) });
  }
  return (["intraday", "positional", "equity"] as LiveCategoryId[])
    .filter((c) => buckets[c].length)
    .map((c) => ({
      label: GROUP_LABEL[c],
      options: buckets[c].sort((a, b) => a.label.localeCompare(b.label)),
    }));
}

export function liveCategoryOf(strategyId: string, instrumentClass?: string | null): LiveCategoryId {
  return CATEGORY_OF[strategyId] ?? (instrumentClass === "DERIV" ? "positional" : "equity");
}
