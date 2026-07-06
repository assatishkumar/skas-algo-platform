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
  donchian_strangle_monthly: "positional",
  custom_options: "positional",
  batman_ratio_monthly: "positional",
  call_ratio_monthly: "positional",
  put_ratio_monthly: "positional",
  hni_weekly: "positional",
  short_premium: "positional",
  staggered_covered_call: "positional",
  "21_ema_momentum": "positional",
  supertrend_momentum: "equity",
  nifty_shop: "equity",
  custom_equity: "equity",
  sst_lifo: "equity",
  sst_fifo: "equity",
  sst_weekly: "equity",
  sst_weekly_fifo: "equity",
};

export function liveCategoryOf(strategyId: string, instrumentClass?: string | null): LiveCategoryId {
  return CATEGORY_OF[strategyId] ?? (instrumentClass === "DERIV" ? "positional" : "equity");
}
