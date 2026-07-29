/** Analyze workbench explainer copy — VERBATIM from the design handoff's helpFor() map
 *  (design_handoff_analyze, 2026-07-28). Keys are panel numbers; heroes have named keys. */

export const PANEL_HELP: Record<string, string> = {
  sim: "Dashed line = the run exactly as traded. Solid teal = the same trades replayed with your rule: any trade whose worst moment (MAE) hit the stop is cut there; any whose best moment (MFE) reached the target banks there. Stop fills assume 3% slippage past the level.",
  Day: "Compare the three bars within each weekday: if the colours disagree more than the weekdays do, DTE — not the weekday — is doing the work. Raw weekday stats smuggle in the expiry calendar.",
  "3.1": "A histogram of where your entries sit on the fear gauge. Above 1.0 = puts pricier than calls (fear priced in); below = calls bid. If everything clusters at 1.0 there is no skew signal to exploit.",
  "3.2": "Each dot is one trade: fear gauge at entry (x) vs what the trade made (y). An upward-sloping trend line means entering when fear is already priced in pays; flat means skew tells you nothing about outcomes.",
  "3.3": "Tests the folk rule \"fear priced in → market drifts up\". Dots above the zero line rose during your hold. If high-skew dots sit mostly above zero, the rule holds in your data.",
  "3.4": "For each skew regime, which side of the structure made or lost the money. If one leg carries all the losses in one regime, that leg deserves its own stop — a strangle is two strategies stapled together.",
  "3.5": "Entry skew (x) vs exit skew (y). Dots below the dashed y=x line mean skew relaxed while you were in the trade — mean reversion you could sell directly.",
  "4.2": "One line per days-to-expiry. Steeper = faster decay: 0-DTE melts to almost nothing by close, 3+ DTE barely moves. This chart is why DTE is the primary pivot everywhere else on the page.",
  "4.3": "Same day-shape under different vol regimes. High-VIX days melt hardest — more rent — but those are also the days the tail losses live on. Rent and risk arrive together.",
  "4.4": "Each bar = the share of a full day's decay captured in that 15-minute slot. Tall bars are when being short premium pays rent; the flat middle is pure gamma exposure for nothing.",
  "5.1": "Green vs red at the same x: winners that were down x% at their worst vs losers. Where red towers over green, a stop placed there mostly catches real losers and costs few winners.",
  "5.2": "Read it at your intended stop: the curve height is the share of eventual winners that stop would have killed. The steeper the drop-off, the cheaper a tight stop is.",
  "5.3": "How far up trades got at their best moment, % of credit. This is the benchmark for giveback — compare with what you actually realized (5.4).",
  "5.4": "Each dot: best moment (x) vs what you kept (y). Distance below the dashed y=x line is profit that existed and was given back. Dots hugging the line = clean exits.",
  "5.5": "Realized ÷ best-moment for winners. 1.0 = exited at the exact peak. A median near 0.6 means you keep about 60% of the best moment — the rest is the cost of not owning a crystal ball.",
  "5.7": "Simply how long trades stay open. Watch for two humps — quick kills vs full-day holds behave like different strategies.",
  "5.8": "If green dots cluster left, good trades work immediately and time in the trade is your enemy — an argument for time stops, not just price stops.",
  "5.9": "When the best moment arrives, as % of the hold. Early peaks argue for faster targets; late peaks say patience is being paid.",
  "5.10": "Every line is one trade's P&L path from entry to exit. Look where the red paths separate from the green ones — that divergence point is where a rule (5.6 sliders) can act.",
  "6.2": "Move right until the teal line crosses zero: that is how much per-leg slippage the edge survives. Backtest fills are minute-close mid, so judge honestly against your real fills.",
  "6.4": "Where the gap between gross and net P&L goes. STT dominates for premium sellers — it is charged on sell-side premium regardless of outcome.",
  "6.5": "Fills where the minute's traded volume could not have absorbed your size — phantom fills. A clean core book with a few flagged hedge legs is normal; flags on short legs are not.",
  "7.1": "Top: cumulative P&L. Bottom: distance below the previous peak — the underwater curve. Depth is pain, width is patience; a strategy is only tradable if you can sit through the widest red patch.",
  "7.3": "How many losses in a row to expect. Size capital and nerves for the right tail of this chart, not the average.",
  "7.4": "If the top five trades carry a large share of profit, the average trade is fragile and the backtest is really five lucky days. Broad-based is what you want.",
  "7.5": "Sharpe recomputed on a rolling 63-day window. A falling trend = edge decaying; oscillating around a stable level = healthy. This is the first chart to check before scaling up.",
  "7.6": "Bars past the dashed band mean today's P&L predicts tomorrow's. Positive lag-1 = losses cluster — reduce size after a loss rather than doubling down.",
  "7.7": "Negative bars = the strategy loses when NIFTY falls or VIX spikes — a short-vol tilt. Small is expected for a premium seller; growing is how \"market neutral\" quietly becomes \"short the market\".",
  "7.8": "How close to fully deployed the margin runs. Spikes near 100% mean no room for adverse moves or a second deployment — size the account off the peak, not the average.",
  "7.11": "A waterfall of where the P&L actually came from. Theta should be the tall green bar; gamma pays for it. If theta is not the biggest earner, this is not a theta strategy — it is a directional bet in costume.",
};

export const EXPLORER_HELP =
  "Each row slices every trade by one condition known at entry. Read MEAN and MEDIAN together — a big gap means a few outliers drive the average. WIN % is consistency, WORST is the tail you must survive, ROM / CYCLE is mean P&L on that trade's own margin, and the CI is a ±band on the mean — if it spans zero, the bucket proves nothing. Greyed rows have under 20 trades: ignore them. The bars mirror MEAN as % of credit.";

export const MELT_HELP =
  "Teal = how fast the traded straddle's premium decays on an average day (1.00 = its value at entry, left axis). Indigo = your average open P&L through the day, % of credit (right axis). You want indigo rising exactly where teal falls steeply — if teal is flat while you're in the trade, you're holding gamma risk for no rent. The strip below shows when your trades enter (bars up) and exit (bars down) on the same clock. Note: each slot averages the trades STILL OPEN then — stopped-out trades drop out as the day progresses, so the late-day line is survivor-tilted, not an expected final P&L.";

/** KPI tile tooltips — verbatim from the design (captions adapt to real run data). */
export const KPI_TIPS: Record<string, string> = {
  total_pnl: "Net of all costs, full run. The raw score — read it with max drawdown, never alone.",
  cagr: "Compounded annual growth on starting capital. Comparable across runs of different lengths.",
  sharpe: "Mean ÷ standard deviation of daily P&L, annualized. Above 2 is strong for an intraday book; below 1 means the ride is rougher than the return.",
  win_rate: "Share of trades that closed positive. High win rate + occasional big loss is the classic premium-seller shape — check WORST in the explorer below.",
  max_dd: "Deepest peak-to-trough dip of the equity curve. This is the pain you must sit through — the underwater curve in Risk shows how long.",
  rom: "P&L ÷ margin blocked, per year — the honest options yield. CAGR flatters when capital sits idle; this doesn't.",
  capture: "Of the premium you collect at entry, the share you keep at exit — the headline number for any premium seller. 2-4% per daily cycle compounds fast.",
  avg_credit: "Average premium collected at entry. Everything normalized to \"% of credit\" on this page is measured against this.",
  avg_hold: "Average time in the trade. Compare against the melt chart: is this window covering the hours when premium actually decays?",
};

/** Section cards (overview footer) — titles/scopes/teasers from the design. */
export const SECTION_CARDS = [
  { id: "conditioning", t: "Conditioning deep-dive", sub: "Cross-tab heatmap + nested weekday-within-DTE", n: "2 panels + 11 conditioners" },
  { id: "skew", t: "Skew analytics", sub: "Distribution, outcome, direction, leg split, drift", n: "5 panels" },
  { id: "melt", t: "Premium melt", sub: "Melt by DTE and VIX regime, theta-hour bars", n: "3 curves" },
  { id: "lifecycle", t: "Trade lifecycle", sub: "MAE / MFE, efficiency, hold time, path spaghetti", n: "9 panels" },
  { id: "execution", t: "Execution quality", sub: "Breakeven slippage, costs, liquidity feasibility", n: "6 checks" },
  { id: "risk", t: "Risk & robustness", sub: "Underwater curve, streaks, rolling Sharpe, greeks", n: "11 panels" },
  { id: "ledger", t: "Trade ledger", sub: "Every cycle: legs, entries, exits, payoffs", n: "table" },
] as const;

export type SectionId = (typeof SECTION_CARDS)[number]["id"];
