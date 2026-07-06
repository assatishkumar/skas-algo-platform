import { Card } from "../components/ui";

type Rule = {
  id: string;
  name: string;
  kind: "Options" | "Equity";
  bias: string;
  summary: string;
  structure: string[];
  entry: string[];
  exit: string[];
  risk: string;
};

// Curated from the strategy implementations (src/skas_algo/strategies/*) and the design decks.
const STRATEGIES: Rule[] = [
  {
    id: "fibret",
    name: "FibRet (Fibonacci Retracement)",
    kind: "Options",
    bias: "Neutral · sell far-OTM premium in high-IVP names",
    summary:
      "Fade a recent daily swing in a high-IVP stock by SELLING an option at the Fibonacci 1.618 extension, with a spot-based stop at the 0.786 level. Semi-automated: screen candidates, then deploy via the option builder (custom_options).",
    structure: [
      "Pick a high-IVP underlying (from your screener CSV upload).",
      "Find the recent daily swing — high H, low L; range R = H − L.",
      "Down-leg → SELL CALL at the 1.618 extension above the high (L + 1.618·R).",
      "Up-leg → SELL PUT at the 1.618 extension below the low (H − 1.618·R).",
      "Strike snapped to the nearest listed strike for the chosen expiry.",
    ],
    entry: [
      "Trade → Screener: upload the IVP screener CSV, filter by IVP, review premium / OI / R:R / margin, and deploy a row.",
      "Swing detection blends the live broker spot so the current leg's endpoint isn't missed when the daily cache lags.",
    ],
    exit: [
      "Spot stop: exit if the underlying crosses the 0.786 level (spot_upper for a short call, spot_lower for a short put).",
      "Profit target: book at 90% of the premium collected.",
      "Otherwise managed to expiry.",
    ],
    risk:
      "Single short option → max profit = premium, with open risk beyond the stop (a naked short). Most far-OTM setups have poor R:R or thin liquidity — the screener flags out-of-range strikes and low OI so you pick selectively.",
  },
  {
    id: "donchian_strangle_monthly",
    name: "Donchian Strangle (Monthly)",
    kind: "Options",
    bias: "Neutral · short single-stock vol, long index vol (reverse-dispersion)",
    summary:
      "Monthly basket short-strangle on the top Nifty 50 names, strikes pinned to last month's Donchian high/low. Cheap/far legs are skipped (some names run single-leg). The whole book is tail-hedged with notional-matched OTM NIFTY options and governed by a portfolio-level combined stop. Deploy-only (no backtest) — runs as one multi-underlying deployment.",
    structure: [
      "Per name: SELL CE at the strike nearest last month's Donchian high, SELL PE near the low (current monthly expiry).",
      "Skip-leg: a leg whose premium is below a floor (% of spot) isn't opened — that name runs single-leg.",
      "Hedge: BUY OTM NIFTY CE + PE (~4.5% OTM), lots = aggregate short notional ÷ NIFTY notional (notional-matched).",
    ],
    entry: [
      "Trade → Screener → Donchian Strangle: upload the Sensibull CSV (ATMIV / IVP / Event), pick names, deploy the basket + hedge in one action.",
      "Filters first: drop names with an event in the holding window, then require ATMIV > HV and IVP ≥ threshold.",
      "One trading day after the previous monthly expiry, selling into the new cycle (dates are editable).",
    ],
    exit: [
      "Portfolio stop: combined MTM (stock legs + hedge) ≤ −2% of aggregate notional → flatten everything.",
      "Per-name breach (Phase 1): a name's spot crossing a short strike closes that name. (Phase 2: capped ATM roll/flip.)",
      "Optional portfolio target (default off; unit = % of premium collected). Otherwise settle to intrinsic at expiry.",
    ],
    risk:
      "Profit zone is each name staying inside last month's range. Primary risk is a single-name gap (earnings/news) while the index is flat — the index hedge does NOT cover this, which is why the event filter is mandatory. The hedge covers correlated market crashes only.",
  },
  {
    id: "21_ema_momentum",
    name: "21 EMA Momentum",
    kind: "Options",
    bias: "Directional · positional credit spreads with the daily trend",
    summary:
      "Daily EMA(21)-channel breakout on NIFTY traded through defined-risk monthly credit spreads: a fresh close above the EMA-of-highs band sells a bull put spread; below the EMA-of-lows band, a bear call spread. Checked once per day at 15:20 — no intraday monitoring. Fully engine-backtestable on the real cached chain.",
    structure: [
      "Channel: EMA(21) of the daily HIGH (upper band) + EMA(21) of the daily LOW (lower band); bands include today's forming bar (what the chart shows at 15:20).",
      "Bullish → BULL PUT SPREAD: sell the higher-strike OTM put, buy a lower put 300–500 pts further out.",
      "Bearish → BEAR CALL SPREAD: sell the lower-strike OTM call, buy a higher call 300–500 pts out.",
      "100-point strikes only; net credit must land in ₹80–140/share (₹90–130 ideal preferred).",
      "Monthly expiry: before the 15th → current month; on/after → next month.",
    ],
    entry: [
      "One check per day at 15:20 IST: close beyond the band AND yesterday was not (fresh crossover).",
      "No spread fits the credit window → skip the day and retry each 15:20 while the signal direction stays active (never take a bad-credit trade).",
    ],
    exit: [
      "Hold until the OPPOSITE signal — then close and reverse in the same decision.",
      "Never into expiry week: exit 5 days before expiry; if the direction still holds, re-enter next month's expiry immediately.",
    ],
    risk:
      "Defined both ways: max loss ≈ (width − credit) × lot ≈ ₹12–27k per lot. Reported margin reads ~2× the real broker requirement (the model doesn't offset the long leg). Backtest 2020–2026: 142 spreads, 46.8% win, +17% on capital, 6.7% max DD.",
  },
  {
    id: "momentum_theta_gainer_intra",
    name: "Momentum Theta Gainer (Intraday)",
    kind: "Options",
    bias: "Directional intraday · sell the opposite ATM weekly",
    summary:
      "Intraday 15-min SuperTrend(7,3) + daily-pivot seller on NIFTY and SENSEX: momentum above pivot R1 sells the ATM weekly PUT; momentum below S1 sells the ATM CALL. Always flat by 15:20. Deploy-only for SENSEX (no BSE history exists); the NIFTY backtest is a dedicated 15-min service on /research with Black-Scholes premiums.",
    structure: [
      "15-min candles built live from spot ticks; SuperTrend(7,3) + classic floor pivots (R1/S1 from the prior day's official OHLC).",
      "Bullish candle close (above SuperTrend AND above R1) → SELL the ATM weekly PUT.",
      "Bearish close (below SuperTrend AND below S1) → SELL the ATM weekly CALL.",
      "Nearest weekly expiry incl. same-day 0DTE (NIFTY Tue / SENSEX Thu); one open position per underlying.",
    ],
    entry: [
      "Only on CLOSED 15-min candles from today's session (the overnight-carried candle never signals).",
      "Max 3 entries per underlying per day; no fresh entries after 15:00.",
      "Deploying with a Zerodha session seeds ~7 days of real 15-min bars so indicators are live immediately.",
    ],
    exit: [
      "SuperTrend flips against the position → exit; re-entry only on a fresh full signal on a LATER candle.",
      "Hard EOD exit at 15:20 — never carries overnight.",
    ],
    risk:
      "Naked short options intraday — gamma risk is real on expiry days even with the 15:20 flat rule. BS-priced backtest (2023–26) is net-negative on flip whipsaws; the strategy is in paper validation against real premiums before any live consideration.",
  },
  {
    id: "hni_weekly",
    name: "HNI Weekly",
    kind: "Options",
    bias: "Neutral · weekly income",
    summary:
      "Net-zero 1-3-2 call ratio on NIFTY weeklies — theta income with a bounded, defined risk.",
    structure: [
      "BUY 1× CE at spot + 200",
      "SELL 3× CE at spot + 400",
      "BUY 2× CE at spot + 600  (all OTM from spot → the three strikes sit 200 apart)",
      "Net contracts +1 − 3 + 2 = 0 → bounded loss (broken-wing tent).",
    ],
    entry: [
      "Once per week, on the week's first session at/after the entry time (default 09:45 IST).",
      "Picks the ~8-DTE weekly expiry; strikes are snapped to the listed 50-pt grid.",
    ],
    exit: [
      "Profit target checked on an intraday cadence (default every 15 min).",
      "Stop-loss and time exit evaluated at EOD (default 15:15).",
      "Anything left at expiry is settled to intrinsic by the engine.",
    ],
    risk: "Bounded both ways (the +200/+600 longs cap the 3× short body). Margin ≈ ₹1–1.3L per lot-set.",
  },
  {
    id: "batman_ratio_monthly",
    name: "Batman",
    kind: "Options",
    bias: "Neutral · monthly income",
    summary:
      "Both ratio wings at once — a 1:2 call ratio above spot AND a 1:2 put ratio below, each hedged (6 legs; the twin tents draw the silhouette). Defaults to a half-size put-wing tail hedge for gap-crash protection.",
    structure: [
      "CALL wing: BUY 1 +300, SELL 2 +600, BUY 1 +1600 (hedge).",
      "PUT wing: mirror below spot.",
      "Tail hedge: extra far put (≈2100 pts, 0.5× lots) to cushion overnight crash gaps.",
      "Per-wing net credit ≤ 1% of capital; combined ≤ 2% (debit wing → skip the month).",
    ],
    entry: [
      "Last Tuesday of the month, for next month's monthly expiry (min ~18 DTE).",
      "BOTH wings must qualify on the credit gate or the month is skipped.",
    ],
    exit: [
      "Combined MTM on all legs: profit target +2.5%, stop −3% (of capital), max-hold 20 days.",
      "Zero adjustments; settled to intrinsic at expiry.",
    ],
    risk:
      "Profit zone is the band between the short strikes; risk is a fast move either way, capped beyond the hedges. Size capital to the position — a 10 lot-set deploy needs ~₹20L+ margin, and the % targets are of capital.",
  },
  {
    id: "call_ratio_monthly",
    name: "Call Ratio Monthly",
    kind: "Options",
    bias: "Mildly bullish / neutral",
    summary: "1:2 call ratio spread with a far outer hedge on NIFTY monthly. Zero downside risk.",
    structure: [
      "BUY 1 CE +300, SELL 2 CE +600, BUY 1 CE +1600 (hedge). Optional far tail hedge.",
      "Balanced contracts → no downside risk (all calls); upside risk capped by the hedge.",
    ],
    entry: [
      "Last Tuesday of the month for the next monthly expiry (min ~18 DTE).",
      "Credit gate: net must be a small credit within limits — debit months are skipped.",
    ],
    exit: ["Profit +2.5% / stop −3% (of capital) / max-hold 20 days; settled at expiry."],
    risk: "Downside is free (calls expire worthless on a fall); upside loss is capped beyond the hedge.",
  },
  {
    id: "put_ratio_monthly",
    name: "Put Ratio Monthly",
    kind: "Options",
    bias: "Mildly bearish / neutral",
    summary: "The downside mirror of the call ratio: 1:2 put ratio below spot + a far put hedge. Zero upside risk.",
    structure: [
      "BUY 1 PE, SELL 2 PE further OTM, BUY 1 far PE hedge (offsets below spot).",
      "Zero upside risk (puts expire worthless on rallies → keep the credit).",
    ],
    entry: ["Same monthly timing + credit gate as the call ratio."],
    exit: ["Profit +2.5% / stop −3% / max-hold 20 days; settled at expiry."],
    risk: "Risk is a fast sell-off toward the short strikes, capped beyond the hedge.",
  },
  {
    id: "short_premium",
    name: "Short Premium",
    kind: "Options",
    bias: "Neutral · income",
    summary: "Sells a straddle or strangle to harvest premium, exiting on a profit/stop or at expiry.",
    structure: [
      "Straddle (ATM CE + PE) or strangle (delta-selected OTM CE + PE).",
      "Optional IV / vol-premium filter to only sell when premium is rich.",
    ],
    entry: ["Sold at a target days-to-expiry; one position per cycle."],
    exit: ["Profit target % / stop-loss % of capital; otherwise settled to intrinsic at expiry."],
    risk: "Undefined risk (naked short) unless a hedge is configured — size conservatively.",
  },
  {
    id: "staggered_covered_call",
    name: "Staggered Covered Call",
    kind: "Options",
    bias: "Income on a held ETF",
    summary: "Accumulates an ETF in tranches and writes a rolling short call against it for premium income.",
    structure: [
      "Buy the ETF in staggered tranches (GTT-up ladder or wheel via cash-secured puts).",
      "Sell a short CE against the holding; roll it as expiry/price moves.",
    ],
    entry: [
      "Tranche buys on the ladder; short CE written ~OTM each cycle.",
      "Premium floor: if entry premium is ~0, roll to a nearer OTM strike to keep ~1:1 R:R.",
    ],
    exit: [
      "Roll/await assignment; called-away never books a loss — the strike is kept ≥ the ETF avg cost.",
      "Optional 30Δ fully-covered calls when the accumulated cost sits near ATM.",
    ],
    risk: "Capped upside (the short call) in exchange for premium; downside is the ETF itself, cushioned by collected premium.",
  },
  {
    id: "nifty_shop",
    name: "Nifty_Shop",
    kind: "Equity",
    bias: "Mean-reversion · dip accumulator",
    summary:
      "\"Shops\" the most beaten-down names below their 20-DMA and averages the dips, with compounding %-of-equity sizing.",
    structure: [
      "Universe (e.g. NIFTY 50) ranked by how far the close sits below its 20-DMA.",
      "Take the 5 most-below names as the day's candidates.",
    ],
    entry: [
      "Case 1 — if any of the 5 is NOT held: buy up to 2 of the not-held names (1 if only 1 available).",
      "Case 2 — if all 5 are held: average into the worst performer that has dropped >3% from its last entry (one averaging trade/day).",
      "Each buy invests the same rupee amount = 4% (configurable) of current equity → built-in compounding. Skipped if cash is insufficient (you wait).",
    ],
    exit: ["Sell a name (whole position) at +5% (configurable) over its average buy price."],
    risk: "Long-only; concentrated in beaten-down names. Averaging down deepens exposure to a falling name until it recovers to target.",
  },
  {
    id: "sst_lifo",
    name: "SST LIFO",
    kind: "Equity",
    bias: "Trend-following",
    summary: "Buys 20-day breakouts on tracked stocks and books each lot at its own profit target (LIFO).",
    structure: ["Per-lot positions on a universe of stocks; pyramids into strength."],
    entry: ["A stock makes a 20-day low (starts tracking), then buys on the 20-day-high breakout."],
    exit: ["Each lot exits at its own profit target (last-in booked first)."],
    risk: "Equity long-only; managed by per-lot targets and any configured stops.",
  },
  {
    id: "supertrend_momentum",
    name: "SuperTrend Momentum",
    kind: "Equity",
    bias: "Trend-following · D/W/M",
    summary: "Rides the SuperTrend on a chosen timeframe (Daily/Weekly/Monthly): buy when it flips green, exit on a % target and/or when it flips red.",
    structure: [
      "Per-symbol long; one lot per green flip. SuperTrend ATR period + multiplier configurable (default 10 / 3).",
      "Timeframe ∈ Daily / Weekly / Monthly — the flip happens on that bar's close.",
    ],
    entry: [
      "Buy one lot when SuperTrend flips GREEN (−1 → +1) on the chosen timeframe.",
      "Optional 'pullback' entry: after the green flip, wait for a dip and enter only when price breaks back above the post-flip high.",
    ],
    exit: [
      "A SuperTrend RED flip exits whatever remains.",
      "At the % profit target, book a configurable share (default 50%) and let the remainder ride to the red flip (set Book % = 100 for a full exit at the target).",
    ],
    risk: "Equity long-only trend-rider. Higher timeframes hold longer (fewer trades, deeper pullbacks). Live SuperTrend is computed from cached OHLC.",
  },
  {
    id: "sst_weekly",
    name: "SST Weekly",
    kind: "Equity",
    bias: "Trend-following · weekly",
    summary: "The SST Donchian breakout system on a weekly timeframe — same logic as SST-LIFO, but levels and decisions are weekly instead of daily.",
    structure: ["Per-lot positions; pyramids into strength. Decisions only at each week's open."],
    entry: [
      "Tracks a symbol when its weekly close prints a 20-week (configurable) low.",
      "Buys when the weekly close breaks above the 20-week high (Donchian breakout).",
    ],
    exit: ["Each lot exits independently once it's up the profit target (default 15%) from its own entry."],
    risk: "Equity long-only; weekly bars hold trends longer (fewer, larger trades). ~20-week warmup from the start date.",
  },
  {
    id: "sst_weekly_fifo",
    name: "SST Weekly (FIFO)",
    kind: "Equity",
    bias: "Trend-following · weekly",
    summary: "SST Weekly with the pooled exit: the whole position exits at once on a tiered average-cost target that tightens as lots accumulate.",
    structure: ["Pooled (averaged) position per symbol; same weekly Donchian entry + pyramiding as SST Weekly."],
    entry: [
      "Tracks a weekly 20-week low, buys the weekly 20-week-high breakout (weekly cadence).",
    ],
    exit: [
      "Whole position exits when the average-cost gain hits the tier: 1 lot 20% / 2 lots 15% / 3+ lots 12% (configurable).",
    ],
    risk: "Equity long-only; weekly trend-following. ~20-week warmup from the start date.",
  },
  {
    id: "sst_fifo",
    name: "SST FIFO",
    kind: "Equity",
    bias: "Trend-following",
    summary: "Same breakout entry as LIFO, but exits the pooled position on an averaged, tiered target (FIFO).",
    structure: ["Pooled (averaged) position per symbol; tiered target by lot count."],
    entry: ["20-day low → tracking, then buy the 20-day-high breakout."],
    exit: ["Pooled exit when the average is up to the tiered target for the held lot count."],
    risk: "Equity long-only; averaged target manages the pooled position.",
  },
];

function Section({ title, items }: { title: string; items: string[] }) {
  return (
    <div>
      <div className="text-[11px] uppercase tracking-wide text-slate-500 mb-1">{title}</div>
      <ul className="list-disc list-inside space-y-0.5 text-sm text-slate-300">
        {items.map((t, i) => (
          <li key={i}>{t}</li>
        ))}
      </ul>
    </div>
  );
}

export default function StrategiesPage() {
  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold">Docs</h1>
        <p className="text-sm text-slate-400">
          Strategy documentation — structure, entry, exit and risk for every strategy on the platform.
          These reflect the engine's implementations; tune the parameters per deployment.
        </p>
      </div>

      <div className="grid gap-3 md:grid-cols-2">
        {STRATEGIES.map((s) => (
          <Card key={s.id} className="flex flex-col gap-3">
            <div>
              <div className="flex items-center gap-2 flex-wrap">
                <span className="font-medium">{s.name}</span>
                <span
                  className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${
                    s.kind === "Options"
                      ? "bg-indigo-100 text-indigo-700 border border-indigo-300 dark:bg-indigo-900/40 dark:border-indigo-600/50 dark:text-indigo-300"
                      : "bg-slate-200 text-slate-600 border border-slate-300 dark:bg-slate-800 dark:border-slate-700 dark:text-slate-300"
                  }`}
                >
                  {s.kind}
                </span>
                <span className="text-xs text-slate-500">{s.bias}</span>
                <code className="ml-auto text-[11px] text-slate-500">{s.id}</code>
              </div>
              <p className="text-sm text-slate-300 mt-1">{s.summary}</p>
            </div>
            <Section title="Structure" items={s.structure} />
            <Section title="Entry" items={s.entry} />
            <Section title="Exit" items={s.exit} />
            <div>
              <div className="text-[11px] uppercase tracking-wide text-slate-500 mb-1">Risk</div>
              <p className="text-sm text-slate-300">{s.risk}</p>
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
}
