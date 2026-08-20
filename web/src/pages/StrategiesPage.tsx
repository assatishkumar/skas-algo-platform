import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

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
  links?: { label: string; url: string }[];
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
    links: [{ label: "Strategy video (YouTube)", url: "https://www.youtube.com/watch?v=f2S_S9NoJco" }],
  },
  {
    id: "call_put_ratio_expiry",
    name: "Call-Put Ratio Expiry",
    kind: "Options",
    bias: "Neutral intraday · expiry-day theta harvest",
    summary:
      "Expiry-day-only (NIFTY Tue, SENSEX Thu) 1:3 premium-ratio structure: buy the ATM straddle, then sell 3 lots per side at the strikes trading near one-third of each ATM premium. Rides the 0DTE morning-IV crush; flat by 15:20 every time. Deploy-only — strike placement is smile-driven, so validation is paper-first on real chains (no backtest).",
    structure: [
      "BUY 1 lot ATM CE + 1 lot ATM PE (per set).",
      "ATM PE premium x → SELL 3 lots of the put strike trading nearest x/3 (below ATM).",
      "ATM CE premium y → SELL 3 lots of the call strike trading nearest y/3 (above ATM).",
      "Net per side: +1 ATM / −3 OTM → net short 2 lots beyond each ⅓ strike.",
    ],
    entry: [
      "Only on the underlying's own weekly expiry day, once, between 09:20 and 09:27 IST.",
      "Strikes read off the LIVE chain; if no strike trades within tolerance (default 30%) of the ⅓ premium, the day is skipped.",
      "Margin deployed is frozen at entry (real broker basket margin when available, model estimate otherwise) — the day's rupee thresholds derive from it.",
    ],
    exit: [
      "Profit target: +1.1% of margin deployed.",
      "Stop-loss: −1% of margin deployed (checked every tick, ~15s).",
      "Hard exit 15:20 — never carried into settlement.",
    ],
    risk:
      "Losses are OPEN beyond the ⅓ strikes (net short 2 lots/side) — a fast 0DTE trend move loses faster than the ATM longs gain, and the margin-based stop can gap through. Expiry-day gamma is the whole game here; size the sets accordingly.",
    links: [{ label: "Strategy video (YouTube)", url: "https://www.youtube.com/watch?v=iorriHcOpdU" }],
  },
  {
    id: "intraday_straddle",
    name: "Intraday Straddle (9xx)",
    kind: "Options",
    bias: "Neutral intraday · theta harvest",
    summary:
      "A daily intraday short straddle on the nearest weekly (0DTE on expiry day): sell an ATM CE+PE at ~09:18 and exit at ~15:25. Defended by a fixed stop at a % of the broker margin plus a trailing stop that only tightens as profit grows. Deploy-only — strike selection needs the live chain, so there is no backtest (paper-first).",
    structure: [
      "SELL 1× ATM CE + 1× ATM PE at the strike nearest spot (or ~0.6Δ slightly-ITM legs — configurable).",
      "One entry per day; a stopped-out day does not re-enter.",
    ],
    entry: [
      "At entry_time (default 09:18 IST), once per day, within the entry window; strikes read off the LIVE chain.",
      "Margin deployed is frozen at entry (real broker basket margin) — the stop % derives from it.",
    ],
    exit: [
      "Fixed stop: −2% of broker margin (configurable).",
      "Trailing stop: ratchet (+0.5% stop per +1% profit) OR 0.5%-below-peak — configurable.",
      "Hard exit 15:25 — never carried.",
    ],
    risk:
      "A naked short straddle has UNCAPPED tails on both sides — the stop is the only guard, and a fast trend or gap can slip through it. Intraday gamma is the risk; size small and paper-first.",
    links: [],
  },
  {
    id: "weekly_intraday_straddle",
    name: "Weekly Intraday Straddle",
    kind: "Options",
    bias: "Neutral intraday · theta · VWAP-gated",
    summary:
      "A weekly-cycle intraday short straddle on NIFTY. The ATM strike (nearest 100) is LOCKED once per weekly expiry cycle — at 09:20 on the first day after expiry — and traded every day of the week: SELL the CE+PE when the combined premium closes below BOTH its VWAP and the prior day's intraday low; exit on a VWAP cross-up or 15:25. Deploy-only — strike selection + the VWAP/prior-low need the live chain and Kite 5-min option bars, so there is no backtest yet (paper-first).",
    structure: [
      "Lock the ATM straddle strike at 09:20 on expiry+1 (nearest 100); hold that strike + expiry all week.",
      "SELL 1× CE + 1× PE at the locked strike on each qualifying 5-min close; up to 3 entries/day; intraday only.",
    ],
    entry: [
      "On a closed 5-min candle, when flat: combined premium x < the prior day's intraday low AND x < the session VWAP (sum of per-leg VWAPs).",
      "A mid-cycle deploy force-starts at the current ATM; margin is frozen at entry (real broker basket margin).",
    ],
    exit: [
      "Exit when the combined premium closes back ABOVE VWAP (cross-up).",
      "Optional MTM stop: −X% of broker margin (default OFF).",
      "Hard square-off 15:25 — never carried.",
    ],
    risk:
      "A naked short straddle has UNCAPPED tails — the VWAP cross-up and 15:25 exits are the only automatic guards, and a fast trend or gap can slip through. Turn the optional MTM stop on for a hard backstop; size small and paper-first.",
    links: [{ label: "Strategy video (YouTube)", url: "https://www.youtube.com/watch?v=kYahbSjbubQ" }],
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
      "Daily pivots (R1/S1) use the broker's fresh prior-day OHLC — if that data is unexpectedly stale the strategy skips the day and alerts, rather than trade off wrong levels.",
    ],
    exit: [
      "SuperTrend flips against the position → exit; re-entry only on a fresh full signal on a LATER candle.",
      "Hard EOD exit at 15:20 — never carries overnight.",
    ],
    risk:
      "Naked short options intraday — gamma risk is real on expiry days even with the 15:20 flat rule. BS-priced backtest (2023–26) is net-negative on flip whipsaws; the strategy is in paper validation against real premiums before any live consideration.",
    links: [{ label: "Strategy video (YouTube)", url: "https://www.youtube.com/watch?v=bLVNs9HD1dw" }],
  },
  {
    id: "delta_neutral_monthly",
    name: "Delta Neutral Monthly",
    kind: "Options",
    bias: "Neutral · delta-balanced monthly strangle",
    summary:
      "Sell the ~18-delta PE and CE of the BANKNIFTY monthly two trading days after expiry (~11:00). When one side's premium runs 40% ahead of the combined premium, the cheap side rolls to the strike matching the rich side's LTP — capped at a straddle, which is immediately hedged at its breakevens into an iron fly. Books at 2.5% of margin deployed, then waits for the next cycle. Deploy-only: BANKNIFTY has no usable chain history, so validation is paper-first on real chains.",
    structure: [
      "SELL 1× PE at ~18Δ (monthly expiry)",
      "SELL 1× CE at ~18Δ — deltas solved from each strike's own implied vol off the live chain.",
      "Adjustment: cheap side rolls to the strike whose LTP ≈ the rich side's LTP; strikes never cross (straddle max).",
      "Iron-fly hedge — the moment both shorts sit at the SAME strike K (a straddle): immediately BUY a long CALL at K + (CE+PE premium) and a long PUT at K − (CE+PE premium), snapped to the strike grid, SAME lots as the shorts. Those two strikes are the short straddle's breakevens, so beyond them the long wings cap the loss ~1:1; the book is now an iron fly.",
      "Optional post-iron-fly adjustment (off by default, togglable on a running deploy): on a breakeven breach, sell a naked ~15-20Δ short on the untested side and roll it — see the Iron Fly Monthly strategy.",
    ],
    entry: [
      "2nd trading day after the previous monthly expiry, between 11:00 and 15:00 (force-entry flag skips the wait on deploy day).",
      "Recurring: re-enters every cycle automatically after an exit.",
    ],
    exit: [
      "Profit target: +2.5% of the broker margin. The margin base RE-FREEZES after each roll/hedge, so once it becomes an iron fly the target is 2.5% of the fly's (much smaller) margin, not the original straddle's.",
      "Optional stop (% of margin, default off). Expiry settlement is the backstop.",
    ],
    risk:
      "A NAKED short strangle until a straddle forms — rolls add credit but tighten the rolled side's breakeven, and a fast one-way month can roll several times before the iron fly caps risk. The 15-min adjustment cooldown limits churn; the model margin figure reads ~2× the real broker requirement until the broker number is available.",
    links: [{ label: "Strategy video (YouTube)", url: "https://www.youtube.com/watch?v=VYNEvDhcV1k" }],
  },
  {
    id: "iron_fly_monthly",
    name: "Iron Fly Monthly",
    kind: "Options",
    bias: "Neutral · defined-risk monthly income",
    summary:
      "Enter the monthly IRON FLY directly on NIFTY, BANKNIFTY or SENSEX (two trading days after expiry, ~11:00): sell the ATM straddle, buy the wings at the straddle's breakevens. When spot breaches a breakeven, actively repair it by selling a naked ~15-20Δ short on the untested side and rolling it as it decays — collecting credit to keep the payoff positive — and exit everything only if the payoff can no longer be made positive. Books at 2.5% of margin deployed. Deploy-only, paper-first on real chains.",
    structure: [
      "SELL 1× ATM CE + 1× ATM PE — the straddle at strike K (nearest listed strike to spot).",
      "BUY 1× CE at K + (CE+PE premium) and 1× PE at K − (CE+PE premium) — the straddle breakevens, snapped to the grid → a defined-risk iron fly.",
      "Adjustment (on breakeven breach): SELL a naked ~15-20Δ short on the UNTESTED side, same lots as the fly's shorts.",
      "Roll it: when that short decays to ≤10Δ or ≤¼ of its sold premium, close it (bank the credit) and re-sell a fresh ~15-20Δ while the side stays breached.",
    ],
    entry: [
      "2nd trading day after the previous monthly expiry, between 11:00 and 15:00 (force-entry flag skips the wait on deploy day).",
      "Recurring: re-enters the iron fly every cycle automatically after an exit.",
    ],
    exit: [
      "Profit target: +2.5% of the broker margin (re-frozen after each adjustment).",
      "Exit ALL when the expiry payoff turns entirely negative — no spot where you'd end positive.",
      "Optional hard MTM stop (% of margin, default off) — a backstop for the naked untested-side short's uncapped tail.",
    ],
    risk:
      "The core fly is defined-risk, but each untested-side adjustment is a NAKED short → a fast reversal past it is an uncapped loss until the payoff-negative exit (or the optional stop) trips. Best in a range that drifts to one side, not a whipsaw. Deploy-only (live-chain ATM + wings + delta solve); paper-first.",
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
      "Time exit: FRIDAY of the entry week — or the week's LAST trading day if Friday is an NSE holiday (the deck's 5-day duration; strictly no weekend/holiday carry).",
      "Profit target checked on an intraday cadence (default every 15 min); stop-loss at EOD (default 15:15).",
      "Anything left at expiry is settled to intrinsic by the engine.",
    ],
    risk: "Bounded both ways (the +200/+600 longs cap the 3× short body). Margin ≈ ₹1–1.3L per lot-set.",
    links: [{ label: "Strategy video (YouTube)", url: "https://www.youtube.com/watch?v=PKe7PNM_ZM4" }],
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
    id: "gap_reversal",
    name: "Gap Reversal",
    kind: "Equity",
    bias: "Mean-reversion · gap continuation",
    summary:
      "Bottom-reversal longs: through a downtrend the RSI(10) OF THE EMA pins near 0 — the first gap-up that retakes the 21 EMA while it's still under 10 is the entry; ride until the close falls back below the EMA.",
    structure: ["One position per name; capital split into equal parts (fixed or equity-scaled)."],
    entry: [
      "Today gapped UP over the previous close (min gap % configurable, 0 = any gap).",
      "Close above the 21 EMA AND RSI(10) of the EMA below 10 — all three or nothing.",
    ],
    exit: ["Close below the 21 EMA exits the whole position."],
    risk: "Long-only cash equity (the short leg needs the future options variant). Catches trend reversals off bottoms — early entries into failed recoveries exit quickly via the EMA cross.",
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
  {
    id: "fair_value_calendar",
    name: "Fair-Value Calendar (Monthly)",
    kind: "Options",
    bias: "Mean-reversion · premium-matched ratio calendar",
    summary:
      "A monthly NIFTY ratio calendar whose SIDE is chosen by a fair-value line — the Jan-2020 COVID high (12,430) compounded at 11.7%/yr. Spot well above it → a put calendar; well below → a call calendar; inside the band → calls. Legs are hunted by PREMIUM (₹150 + ₹450 sold, 3× ₹200 bought), so the structure is premium-matched and the target anchors to margin. Unsold weeks are rolled; the bought monthly is never rolled.",
    structure: [
      "SELL 1 lot of the near weekly (≥4 DTE) at ~₹150 premium.",
      "SELL 1 lot of the SAME weekly at ~₹450 premium (deliberately ITM).",
      "BUY 3 lots of the MONTHLY after that weekly at ~₹200 premium — sold ≈ bought, a ~zero net debit.",
      "Both sells and the buy are the same side (PE or CE); side_mode can force pe / ce / both.",
      "Premiums are absolute ₹ from Aug-2024 and scale with spot before that (₹150 at 25k ≠ ₹150 at 16k).",
    ],
    entry: [
      "Start of each calendar month, retried every day until a valid setup lands (one cycle per month).",
      "900-POINT GAP RULE: the dead zone (buy strike → furthest sell strike) must be ≤ 900 pts — 2× the ~450 pts of rollover income three weekly rolls are expected to earn. Wider → skip today, hunt again tomorrow.",
      "Because strikes are premium-hunted, a high-vol day spreads them out — so the gap rule doubles as a volatility filter.",
    ],
    exit: [
      "Target: +5% of the FROZEN broker margin on the WHOLE cycle (banked rolls + open MTM) → exit all, done for the month.",
      "No target by the sold weekly's expiry → roll BOTH sells to the next weekly at the SAME strikes, banking the decay.",
      "The roll fires one trading day BEFORE that expiry — a short weekly's margin spikes into settlement, so rolling on the last day costs more.",
      "The bought monthly is NEVER rolled: when the sells would land on its expiry the CYCLE ENDS — flat, then a fresh cycle (new fair-value read) opens next session.",
    ],
    risk:
      "Net short 2 near-dated legs against 3 longer-dated longs: the calendar caps a slow move but a fast one through the sold strikes hurts before the rolls can pay. The ~₹450 ITM sell is the thin leg (sparse prints). 5y store replay at 1 lot-set: CE-only +₹147k (65 cycles, 77% win, ₹52k max DD) is the only positive mode — fair-value −₹64k, both-sides −₹154k, PE-only −₹196k. Monthly re-centering rides the index's drift on calls but chases crashes down on puts.",
    links: [{ label: "Strategy video (YouTube)", url: "https://www.youtube.com/watch?v=tn-73I63yBw&t=2162s" }],
  },
  {
    id: "double_diagonal_calendar",
    name: "Double Diagonal Calendar",
    kind: "Options",
    bias: "Neutral · sell near vol, own far vol",
    summary:
      "A four-leg NIFTY double diagonal: sell a near-weekly ~20-25Δ strangle and buy a farther-expiry ~15-20Δ strangle as the calendar hedge, whose residual time value rounds off the payoff tent. One manual cycle per deploy — after the exit it sits idle until you deploy again.",
    structure: [
      "SELL a near-expiry strangle at ~short_target_delta (the next weekly ≥ near_min_dte DTE).",
      "BUY a farther-expiry strangle at ~hedge_target_delta (the subsequent expiry ≥ far_min_dte).",
      "A manual BIAS knob (up / neutral / down) skews the deltas: an 'up' lean widens the calls and richens the puts.",
      "The VIX regime (High ≥20 / Med 13-19 / Low 9-12) LABELS the expected net-premium band — display only; selection is delta-first.",
    ],
    entry: [
      "On deploy (force) or the next entry weekday ~11:00, ONCE — recurring is off by design.",
      "Broker basket margin is frozen at entry; the rupee thresholds derive from it.",
    ],
    exit: [
      "±1.5% of the frozen margin (configurable).",
      "Adjust: when the UNTESTED near short decays to ≤10Δ or ≤¼ of its sold premium, roll it back toward delta-neutral and drag its far hedge with it — never crossing the other short. No adjustments inside 3 DTE.",
      "At the near expiry the WHOLE structure squares off — the far hedges are never left naked.",
    ],
    risk:
      "Defined-ish but not defined: the far longs cushion a big move rather than cap it, and the two expiries can move on different vols (a near-vol spike costs before the far leg gains). Deploy-only, broker quotes required — the delta solve reads two live chains.",
  },
  {
    id: "intraday_strangle_combo",
    name: "Intraday Strangle Combo (2-index)",
    kind: "Options",
    bias: "Neutral intraday · per-leg managed",
    summary:
      "Sell one lot of a CE and a PE on the current weekly at 09:16 and be flat by 15:25, with the index rotating by weekday (Mon NIFTY · Tue both · Wed/Thu SENSEX · Fri NIFTY). The distinguishing rule: the two legs are managed COMPLETELY independently — each has its own stop, target and re-entry budget.",
    structure: [
      "SELL 1 lot CE + 1 lot PE, otm_steps grid steps out (3 = the deck's OTM3 on the exchange listing grid; 0 = an ATM straddle).",
      "wing_steps > 0 BUYS a protective long that many steps beyond each short — an intraday iron fly/condor with risk defined by construction.",
      "Each index keeps its own book; nothing is read or written across the two sides.",
    ],
    entry: [
      "09:16 on the weekday's index; the weekly expiry in force that day.",
      "An exited leg RE-ENTERS at a freshly recomputed strike — separate budgets for stop-outs and target-hits (2+2 → a side can trade 5× a day).",
      "same_strike_action decides what happens when the recomputed strike is the one it just left: reenter (deck) / skip / hold.",
    ],
    exit: [
      "Per leg: 40% stop and 70% target, both on THAT leg's own entry premium.",
      "Per index: a rupee MTM stop per lot, day-cumulative (realized + open) — it outranks a leg stop in the same tick.",
      "Hard flat at 15:25.",
    ],
    risk:
      "Naked shorts intraday: a trending day can stop both legs and the re-entries can compound it, which is why the rupee MTM stop usually binds first. Backtest (5y NIFTY): the best risk-adjusted config tested was NO stop-loss re-entries + the ATM straddle structure (~₹181k net, ~₹16k max DD at 1 lot); SL re-entries lost money in every window. SENSEX is unvalidated (23 captured days).",
  },
  {
    id: "put_condor",
    name: "Put Condor (Monthly)",
    kind: "Options",
    bias: "Mildly bearish · defined-risk debit",
    summary:
      "On the first trading day of each month, buy a put condor on that month's expiry — a NET DEBIT structure, so max loss is the debit (~₹2k/lot) and it is defined-risk by construction. It needs the index to drift down into the tent by expiry. Both adjustment rules are switchable, because the backtest picks the policy.",
    structure: [
      "BUY the put nearest below spot (long_hi), SELL two spaced puts below it, BUY the lowest put (long_lo) — 200-pt spacing by default.",
      "Expiry payoff: zero above long_hi, rising to +spacing across the shorts, back to zero at long_lo.",
      "Max loss = the debit paid; max profit = spacing − debit.",
    ],
    entry: [
      "First trading day of every month, on that month's monthly expiry.",
      "One cycle per month; strikes snap to the 100-pt NIFTY selection grid.",
    ],
    exit: [
      "Target / stop are a % of MAX LOSS (not of margin — a defined-risk condor's margin is tiny live and huge in the replay's model).",
      "down_breach_action (roll the top long / recenter / none) and loss_repair (roll the lower short up / none) are independently switchable.",
      "Otherwise held to expiry and settled to intrinsic.",
    ],
    risk:
      "The worst case is known before you enter: the debit. Measured on the 1-min store (58 monthly cycles, Aug-2021 → Jul-2026, unadjusted, held to expiry, 1 lot): 24% hit rate, +₹558/cycle — a mildly positive lottery, three small losses for one large win. The 5y sweep favoured target-only exits with NO adjustments. Backtest-only for now: the deploy route is deliberately deferred.",
  },
  {
    id: "straddle_btst",
    name: "Straddle BTST (overnight long)",
    kind: "Options",
    bias: "Long volatility · overnight gap",
    summary:
      "The reverse of the intraday straddle: BUY the ATM CE+PE at ~15:20, hold overnight, sell at ~09:20 the next session. The bet is that the overnight gap plus any IV firming at the open beats the theta paid. Tested and NEGATIVE — kept for study, not recommended.",
    structure: [
      "BUY 1 lot ATM CE + 1 lot ATM PE on the nearest expiry STRICTLY AFTER today (so the position always survives the night).",
      "Long-only: the loss is capped at the premium paid, so there is no margin dependence anywhere.",
    ],
    entry: ["~15:20 on each trading day (configurable). On expiry day the entry rolls to the next weekly automatically."],
    exit: [
      "~09:20 the next session that ticks — Friday's entry simply exits Monday, so weekends need no calendar math.",
      "Optional target / stop as a % of the PREMIUM PAID (both off by default — it is a pure time-window trade).",
    ],
    risk:
      "Defined: you can lose the premium, nothing more. The problem is the expectancy, not the tail — 2 years of NIFTY: −₹66.5k. Monday's entry pays the steepest theta bill (1 DTE into Tuesday's expiry).",
  },
  {
    id: "asymmetric_premium_intra",
    name: "Asymmetric Premium (Intraday)",
    kind: "Options",
    bias: "Neutral intraday · two expiries",
    summary:
      "From the owner's spec sheet: short a near-ATM CALL on the CURRENT week and a near-ATM PUT on the NEXT week, 09:30 → 15:15. The idea is that sharp declines happen fast, so the short put is kept a week out. Tested and SHELVED — both distinguishing ideas cost money over 2 years.",
    structure: [
      "SELL a near-ATM CE on the current week's expiry.",
      "SELL a near-ATM PE on the NEXT week's expiry (put_expiry_offset = 1; setting it to 0 collapses this to a plain ATM straddle).",
      "Strikes obey the platform's NIFTY 100-multiples rule — nothing here needs the 50s.",
    ],
    entry: ["09:30, once per day; flat by 15:15."],
    exit: [
      "Adjustment: when one leg decays to a fraction of the other's premium, the CHEAP leg rolls to the strike on ITS OWN expiry matching the rich leg — note this RE-ARMS the side that just decayed, so it adds risk rather than hedging it (capped by max_adjusts).",
      "Stop: 100 POINTS of combined loss, day-cumulative (realized + open) → exit everything.",
    ],
    risk:
      "Two naked shorts across two expiries — a trending day tests one leg while the adjustment walks the other back into the move. The controlled experiment that proved it: with both distinguishing ideas removed it is statistically the ATM straddle (t = 0.83).",
  },
  {
    id: "value_investing",
    name: "Value Investing (daily drip)",
    kind: "Equity",
    bias: "Accumulation \u00b7 buys every day, never sells",
    summary:
      "A fixed rupee budget goes into your watchlist every single trading day, spent on whatever has fallen the most \u2014 one share each, from the top of the list down. The money comes from selling a little of a liquid ETF you already hold. Nothing is ever sold back; the position only grows.",
    structure: [
      "Your watchlist, sorted each day by today's % change \u2014 the biggest faller at the top.",
      "BUY 1 share of each name, walking top to bottom while the daily budget lasts. No second share of anything.",
      "SELL just enough of the fund source ETF (LIQUIDCASE / GOLDBEES / LIQUIDBEES) to pay for it, in the same decision \u2014 idle cash is spent first.",
      "The watchlist is editable on a running deployment (Edit params) \u2014 no redeploy to change what you are accumulating.",
    ],
    entry: [
      "Once a day at the decision time. A name the remaining budget cannot afford is skipped and the walk continues to cheaper ones below it.",
      "Leftover budget evaporates \u2014 each day gets exactly the amount you configured, which is what keeps the runway forecast honest.",
      "An all-green day still buys: the least-up name tops the list. The discipline is the point.",
    ],
    exit: [
      "Never. This strategy has no exit \u2014 the stocks are held indefinitely.",
      "The only sales are of the fund source ETF, to raise each day's cash.",
      "If the ETF cannot cover the day's list it buys NOTHING and alerts \u2014 never a half-filled day.",
    ],
    risk:
      "It is a savings discipline, not a trading edge: it buys weakness without asking whether the weakness is deserved, so the watchlist is the entire risk decision. Warns on the Live page and by push when the fund source has ~2 days of runway left. Needs a BROKER quote source \u2014 on the cache source every change reads 0.00% and the ranking is meaningless (the strategy detects that and refuses the day). In the backtest, read the equity curve: the only SELLs in the trade log are ETF funding sales, so win rate and realized P&L describe a cash sweep.",
  },
  {
    id: "happy_twins",
    name: "Happy Twins (dual SuperTrend)",
    kind: "Equity",
    bias: "Trend-following · fast in, slow out",
    summary:
      "One indicator family, two speeds, long only: a FAST weekly SuperTrend turning green gets you in early, and a SLOW one holds you in until the trend genuinely breaks. Handing the exit to the slower twin is what kills the whipsaw a single fast SuperTrend inflicts.",
    structure: [
      "FAST SuperTrend (ATR 2, factor 1) on weekly bars — the entry trigger.",
      "SLOW SuperTrend (ATR 3, factor 1) on weekly bars — the exit authority.",
      "Optional park_symbol (e.g. GOLDBEES): idle capital sits in an ETF whenever every trading name is flat, and is sold in the same slice as the entry.",
      "Optional capital_parts: portfolio mode — one part per green flip, strongest flip first (close vs the 20-day mean), at most N names held.",
      "Optional regime_symbol: the exposure brake — no NEW part while that index's own slow SuperTrend is red.",
    ],
    entry: [
      "ENTER when the FAST SuperTrend TURNS green — a transition, not a state, so a freshly deployed or recovered run never chases a signal that already happened.",
      "Weekly flips become visible the next trading day (no lookahead).",
    ],
    exit: [
      "EXIT while the SLOW SuperTrend is red — a state, so a flip missed across a restart still closes the position on the next bar.",
    ],
    risk:
      "Cash equity, long only. Backtest-first: live deploys FAIL CLOSED (no entries, no blind exits) until the live indicator seeding lands. Point-in-time membership is available on the Nifty500 Momentum 50 universe so the backtest isn't reading today's winners into 2021.",
  },
];

/** Per-strategy docs metadata (design handoff design_handoff_docs1): index group,
 * bias pill kind, the 3×2 fact grid, and the deploy footer. Copy in STRATEGIES above is
 * the content source; this table only layers presentation facts on top by id. */
type BiasKind = "neutral" | "income" | "bull" | "bear";
type Meta = {
  group: string;
  biasKind: BiasKind;
  facts: [string, string][];
  deployNote: string;
  deployCta?: { label: string; to: string };
};

const GROUP_ORDER = [
  "Intraday options", "Premium selling", "Ratio & income", "Directional tilt",
  "Equity income", "Equity trend",
];

const META: Record<string, Meta> = {
  fibret: {
    group: "Premium selling", biasKind: "neutral",
    facts: [["Bias", "Neutral"], ["Instrument", "Single-stock options"], ["Structure", "1 short leg"],
            ["Stop", "0.786 retrace"], ["Target", "90% of premium"], ["Deploy", "Screener → builder"]],
    deployNote: "Semi-automated — screen candidates, then deploy a row via the option builder.",
    deployCta: { label: "Open FibRet screener", to: "/trade?tab=screener" },
  },
  donchian_strangle_monthly: {
    group: "Premium selling", biasKind: "neutral",
    facts: [["Bias", "Neutral · rev-disp"], ["Basket", "Top Nifty-50"], ["Per name", "SELL CE + PE"],
            ["Hedge", "Notional-matched NIFTY"], ["Portfolio stop", "−2% of notional"],
            ["Cadence", "Monthly · +1d post-expiry"]],
    deployNote: "Deploy-only — no backtest; the screener resolves the basket, then deploys it as one run.",
    deployCta: { label: "Open Donchian screener", to: "/trade/donchian" },
  },
  "21_ema_momentum": {
    group: "Directional tilt", biasKind: "bull",
    facts: [["Bias", "With the daily trend"], ["Instrument", "NIFTY monthly"], ["Structure", "Credit spread · 300–500 pts"],
            ["Credit", "₹80–140 (ideal 90–130)"], ["Check", "Daily · 15:20 IST"], ["Roll", "5 days pre-expiry"]],
    deployNote: "Fully engine-backtestable on the real cached chain — backtest first, then forward-test the winner.",
    deployCta: { label: "Run a backtest", to: "/backtest?tab=new" },
  },
  call_put_ratio_expiry: {
    group: "Intraday options", biasKind: "neutral",
    facts: [["Bias", "Neutral · 0DTE"], ["Days", "NIFTY Tue · SENSEX Thu"], ["Structure", "+1 ATM / −3 @ ⅓ premium"],
            ["Entry", "09:20–09:27 IST"], ["Exits", "+1.1% / −1% of margin"], ["Flat by", "15:20 — always"]],
    deployNote: "Deploy-only, broker quotes required (⅓-premium strikes come off the LIVE chain). Paper-first.",
    deployCta: { label: "Deploy CP ratio expiry", to: "/trade" },
  },
  intraday_straddle: {
    group: "Intraday options", biasKind: "neutral",
    facts: [["Bias", "Neutral · intraday"], ["Underlying", "NIFTY / BANKNIFTY"], ["Structure", "ATM short straddle"],
            ["Entry", "~09:18 (configurable)"], ["Stops", "−2% + trailing"], ["Flat by", "15:25 — always"]],
    deployNote: "Deploy-only, broker quotes required (live-chain ATM/delta strikes). No backtest — paper-first.",
    deployCta: { label: "Deploy Straddle", to: "/trade" },
  },
  weekly_intraday_straddle: {
    group: "Intraday options", biasKind: "neutral",
    facts: [["Bias", "Neutral · intraday"], ["Underlying", "NIFTY"], ["Structure", "ATM short straddle (weekly-locked)"],
            ["Entry", "x < prior-low & < VWAP"], ["Stop", "optional (off)"], ["Flat by", "15:25 — always"]],
    deployNote: "Deploy-only, broker quotes required (live chain + Kite 5-min option bars). No backtest — paper-first.",
    deployCta: { label: "Deploy Weekly straddle", to: "/trade" },
  },
  momentum_theta_gainer_intra: {
    group: "Intraday options", biasKind: "neutral",
    facts: [["Bias", "With momentum"], ["Underlyings", "NIFTY + SENSEX"], ["Signal", "15-min ST(7,3) + pivots"],
            ["Sells", "ATM weekly (0DTE ok)"], ["Cap", "3 entries/day"], ["Flat by", "15:20 — always"]],
    deployNote: "Deploy-only (SENSEX has no history). The NIFTY backtest lives on /research (BS premiums).",
    deployCta: { label: "Deploy Intraday theta", to: "/trade" },
  },
  delta_neutral_monthly: {
    group: "Premium selling", biasKind: "neutral",
    facts: [["Bias", "Neutral · delta-balanced"], ["Instrument", "BANKNIFTY monthly"],
            ["Structure", "18Δ strangle → iron fly"], ["Adjust", ">40% premium imbalance"],
            ["Target", "+2.5% of margin"], ["Entry", "Expiry+2d · ~11:00"]],
    deployNote: "Deploy-only, broker quotes required (live-chain delta solve + premium-matched rolls). Paper-first.",
    deployCta: { label: "Deploy Delta neutral", to: "/trade" },
  },
  iron_fly_monthly: {
    group: "Premium selling", biasKind: "neutral",
    facts: [["Bias", "Neutral · defined-risk"], ["Instrument", "NIFTY / BANKNIFTY / SENSEX monthly"],
            ["Structure", "ATM straddle + breakeven wings"], ["Adjust", "sell ~15-20Δ untested side"],
            ["Target", "+2.5% of margin"], ["Entry", "Expiry+2d · ~11:00"]],
    deployNote: "Deploy-only, broker quotes required (live-chain ATM + wings + delta-based adjustment). Paper-first.",
    deployCta: { label: "Deploy Iron fly", to: "/trade" },
  },
  hni_weekly: {
    group: "Ratio & income", biasKind: "income",
    facts: [["Bias", "Neutral"], ["Instrument", "NIFTY weekly"], ["Structure", "1-3-2 call ratio"],
            ["DTE", "~8"], ["Entry", "Weekly · 09:45 IST"], ["Margin", "₹1–1.3L / lot-set"]],
    deployNote: "Backtestable from 2025-09 (Tuesday-expiry era); deploys via the standard forward-test path.",
    deployCta: { label: "Run a backtest", to: "/backtest?tab=new" },
  },
  batman_ratio_monthly: {
    group: "Ratio & income", biasKind: "income",
    facts: [["Bias", "Neutral"], ["Instrument", "NIFTY monthly"], ["Structure", "Twin 1:2 ratios · 6 legs"],
            ["DTE", "~18+"], ["Targets", "+2.5% / −3%"], ["Max-hold", "20 days"]],
    deployNote: "Backtestable on the real cached chain; auto-sizing (margin) is the form default.",
    deployCta: { label: "Run a backtest", to: "/backtest?tab=new" },
  },
  call_ratio_monthly: {
    group: "Directional tilt", biasKind: "bull",
    facts: [["Bias", "Mildly bullish"], ["Instrument", "NIFTY monthly"], ["Structure", "1:2 call ratio + hedge"],
            ["Downside risk", "None"], ["Targets", "+2.5% / −3%"], ["Max-hold", "20 days"]],
    deployNote: "Backtestable on the real cached chain; credit gates scale with equity under auto-sizing.",
    deployCta: { label: "Run a backtest", to: "/backtest?tab=new" },
  },
  put_ratio_monthly: {
    group: "Directional tilt", biasKind: "bear",
    facts: [["Bias", "Mildly bearish"], ["Instrument", "NIFTY monthly"], ["Structure", "1:2 put ratio + hedge"],
            ["Upside risk", "None"], ["Targets", "+2.5% / −3%"], ["Max-hold", "20 days"]],
    deployNote: "Backtestable on the real cached chain; the mirror of the call ratio.",
    deployCta: { label: "Run a backtest", to: "/backtest?tab=new" },
  },
  short_premium: {
    group: "Ratio & income", biasKind: "income",
    facts: [["Bias", "Neutral"], ["Instrument", "Index options"], ["Structure", "Straddle or strangle"],
            ["Strikes", "ATM / offset / delta"], ["Entry", "Target DTE · 1/cycle"], ["Exit", "Profit / stop %"]],
    deployNote: "Backtestable; the simplest premium-selling baseline to compare everything else against.",
    deployCta: { label: "Run a backtest", to: "/backtest?tab=new" },
  },
  staggered_covered_call: {
    group: "Equity income", biasKind: "income",
    facts: [["Bias", "Income"], ["Underlying", "Held ETF"], ["Structure", "Tranches + rolling CE"],
            ["Entry", "GTT-up ladder"], ["Roll", "Keep ~1:1 R:R"], ["Settle", "Intrinsic at expiry"]],
    deployNote: "Income overlay on a holding you already own.",
    deployCta: { label: "Run a backtest", to: "/backtest?tab=new" },
  },
  nifty_shop: {
    group: "Equity income", biasKind: "income",
    facts: [["Bias", "Mean-reversion"], ["Universe", "Nifty 50"], ["Entry", "Dip ladder"],
            ["Exit", "Target per tranche"], ["Cadence", "Daily EOD"], ["Kind", "Cash equity"]],
    deployNote: "Backtestable equity accumulator.",
    deployCta: { label: "Run a backtest", to: "/backtest?tab=new" },
  },
  sst_lifo: {
    group: "Equity trend", biasKind: "bull",
    facts: [["Bias", "Trend-following"], ["Universe", "Nifty 50"], ["Signal", "20d Donchian breakout"],
            ["Booking", "LIFO"], ["Cadence", "Daily EOD"], ["Kind", "Cash equity"]],
    deployNote: "The founding parity strategy — byte-identical backtest and paper replay.",
    deployCta: { label: "Run a backtest", to: "/backtest?tab=new" },
  },
  gap_reversal: {
    group: "Equity trend", biasKind: "bull",
    facts: [["Bias", "Mean-reversion"], ["Universe", "Nifty 500"], ["Signal", "Gap up + RSI(10)-of-EMA<10 + >21 EMA"],
            ["Exit", "Close < 21 EMA"], ["Cadence", "Daily EOD"], ["Kind", "Cash equity · long-only"]],
    deployNote: "Backtest-first: live deploys fail closed (no entries) until the live indicator seeding lands (Phase 2).",
    deployCta: { label: "Run a backtest", to: "/backtest?tab=new" },
  },
  supertrend_momentum: {
    group: "Equity trend", biasKind: "bull",
    facts: [["Bias", "Trend-following"], ["Universe", "Nifty 50"], ["Signal", "SuperTrend D/W/M"],
            ["Entry", "Green flip / pullback"], ["Exit", "Red flip / trail"], ["Kind", "Cash equity"]],
    deployNote: "Backtestable with the precomputed SuperTrend market view.",
    deployCta: { label: "Run a backtest", to: "/backtest?tab=new" },
  },
  sst_weekly: {
    group: "Equity trend", biasKind: "bull",
    facts: [["Bias", "Trend-following"], ["Universe", "Nifty 50"], ["Signal", "Weekly Donchian"],
            ["Booking", "LIFO"], ["Cadence", "Weekly"], ["Kind", "Cash equity"]],
    deployNote: "The weekly-bar variant of SST.",
    deployCta: { label: "Run a backtest", to: "/backtest?tab=new" },
  },
  sst_weekly_fifo: {
    group: "Equity trend", biasKind: "bull",
    facts: [["Bias", "Trend-following"], ["Universe", "Nifty 50"], ["Signal", "Weekly Donchian"],
            ["Booking", "FIFO tiers"], ["Cadence", "Weekly"], ["Kind", "Cash equity"]],
    deployNote: "Weekly SST with FIFO tiered profit booking.",
    deployCta: { label: "Run a backtest", to: "/backtest?tab=new" },
  },
  fair_value_calendar: {
    group: "Ratio & income", biasKind: "income",
    facts: [["Bias", "Mean-reversion side pick"], ["Instrument", "NIFTY weekly + monthly"],
            ["Structure", "−1 @₹150 −1 @₹450 / +3 @₹200"], ["Gap cap", "900 pts or skip"],
            ["Target", "+5% of margin (whole cycle)"], ["Cadence", "Monthly · weekly rolls"]],
    deployNote: "Backtested on the 1-min store; CE-only is the only positive mode. Deploy (broker quotes) for the paper forward test.",
    deployCta: { label: "Deploy FV calendar", to: "/trade" },
  },
  double_diagonal_calendar: {
    group: "Premium selling", biasKind: "neutral",
    facts: [["Bias", "Neutral · two expiries"], ["Instrument", "NIFTY weeklies"],
            ["Structure", "Near short strangle + far long"], ["Adjust", "untested short ≤10Δ"],
            ["Exit", "±1.5% of margin"], ["Cycle", "One per deploy (manual)"]],
    deployNote: "Deploy-only, broker quotes required (the delta solve reads two live chains). Paper-first.",
    deployCta: { label: "Deploy Double diagonal", to: "/trade" },
  },
  intraday_strangle_combo: {
    group: "Intraday options", biasKind: "neutral",
    facts: [["Bias", "Neutral · intraday"], ["Underlyings", "NIFTY + SENSEX by weekday"],
            ["Structure", "OTM3 strangle / ATM straddle / +wings"], ["Per leg", "40% stop · 70% target"],
            ["Per index", "Rupee MTM stop per lot"], ["Flat by", "15:25 — always"]],
    deployNote: "Deploy-capable (paper-tested since 2026-08-13) with a 1-min-store backtest — check the sweep before enabling SL re-entries.",
    deployCta: { label: "Run a backtest", to: "/backtest?tab=new" },
  },
  put_condor: {
    group: "Directional tilt", biasKind: "bear",
    facts: [["Bias", "Mildly bearish"], ["Instrument", "NIFTY monthly"],
            ["Structure", "Long put condor (net debit)"], ["Max loss", "The debit (~₹2k/lot)"],
            ["Exits", "% of MAX LOSS"], ["Entry", "1st trading day of the month"]],
    deployNote: "Backtest-only for now — the deploy route is deliberately deferred until the adjustment policy is settled.",
    deployCta: { label: "Run a backtest", to: "/backtest?tab=new" },
  },
  straddle_btst: {
    group: "Intraday options", biasKind: "neutral",
    facts: [["Bias", "Long vol · overnight"], ["Underlying", "NIFTY"], ["Structure", "Long ATM straddle"],
            ["Entry", "~15:20"], ["Exit", "~09:20 next session"], ["Verdict", "Negative (−₹66.5k / 2y)"]],
    deployNote: "Tested and negative — kept for study. Replays on the 1-min store; deploy-only live.",
    deployCta: { label: "Run a backtest", to: "/backtest?tab=new" },
  },
  asymmetric_premium_intra: {
    group: "Intraday options", biasKind: "neutral",
    facts: [["Bias", "Neutral · intraday"], ["Underlying", "NIFTY"], ["Structure", "Short CE this week + PE next"],
            ["Adjust", "Cheap leg rolls to the rich"], ["Stop", "100 pts combined"], ["Flat by", "15:15"]],
    deployNote: "Shelved: both distinguishing ideas cost money over 2 years — kept as the controlled experiment that proved it.",
    deployCta: { label: "Run a backtest", to: "/backtest?tab=new" },
  },
  value_investing: {
    group: "Equity income", biasKind: "income",
    facts: [["Bias", "Accumulation"], ["Universe", "Your watchlist"],
            ["Buys", "1 share each, top-down"], ["Funded by", "An ETF you hold"],
            ["Exits", "Never"], ["Cadence", "Every trading day"]],
    deployNote: "Backtestable, and live-capable from day one \u2014 but only on a broker quote source (the cache source makes every change read 0.00%).",
    deployCta: { label: "Run a backtest", to: "/backtest?tab=new" },
  },
  happy_twins: {
    group: "Equity trend", biasKind: "bull",
    facts: [["Bias", "Trend-following"], ["Universe", "Any (Nifty 500 / Momentum 50)"],
            ["Signal", "Fast ST(2,1) in · slow ST(3,1) out"], ["Timeframe", "Weekly bars"],
            ["Overlays", "Park · parts · regime brake"], ["Kind", "Cash equity · long-only"]],
    deployNote: "Backtest-first: live deploys fail closed (no entries) until the live indicator seeding lands.",
    deployCta: { label: "Run a backtest", to: "/backtest?tab=new" },
  },
  sst_fifo: {
    group: "Equity trend", biasKind: "bull",
    facts: [["Bias", "Trend-following"], ["Universe", "Nifty 50"], ["Signal", "20d Donchian breakout"],
            ["Booking", "FIFO tiers"], ["Cadence", "Daily EOD"], ["Kind", "Cash equity"]],
    deployNote: "SST with FIFO tiered profit booking.",
    deployCta: { label: "Run a backtest", to: "/backtest?tab=new" },
  },
};

const BIAS_PILL: Record<BiasKind, { glyph: string; bg: string; fg: string }> = {
  neutral: { glyph: "≈", bg: "var(--chip)", fg: "var(--chip-text)" },
  income: { glyph: "₹", bg: "var(--warn-bg)", fg: "var(--warn-text)" },
  bull: { glyph: "↑", bg: "var(--ok-bg)", fg: "var(--ok-text)" },
  bear: { glyph: "↓", bg: "var(--rose-bg)", fg: "var(--rose-text)" },
};

const SECTION_DEF = [
  { key: "structure" as const, label: "Structure", accent: "var(--sec-struct)",
    icon: "M12 2 2 7l10 5 10-5-10-5z|M2 17l10 5 10-5|M2 12l10 5 10-5" },
  { key: "entry" as const, label: "Entry", accent: "var(--sec-entry)",
    icon: "M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4|pl:10 17 15 12 10 7|M15 12H3" },
  { key: "exit" as const, label: "Exit", accent: "var(--sec-exit)",
    icon: "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4|pl:16 17 21 12 16 7|M21 12H9" },
];

function DocIcon({ d, size = 14, stroke = "currentColor" }: { d: string; size?: number; stroke?: string }) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} fill="none" stroke={stroke}
      strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      {d.split("|").map((seg, i) =>
        seg.startsWith("pl:") ? <polyline key={i} points={seg.slice(3)} /> : <path key={i} d={seg} />)}
    </svg>
  );
}

/** Bullets beginning BUY/SELL/SKIP render that word as a side chip (option-leg reading). */
function Bullet({ text, accent }: { text: string; accent: string }) {
  const m = /^(BUY|SELL|SKIP)\b\s*/.exec(text);
  const chipStyle = m
    ? m[1] === "BUY"
      ? { background: "var(--ok-bg)", color: "var(--ok-text)" }
      : m[1] === "SELL"
        ? { background: "var(--warn-bg)", color: "var(--warn-text)" }
        : { background: "var(--chip)", color: "var(--faint)" }
    : null;
  return (
    <li className="flex items-start gap-2.5 text-sm text-[var(--muted)] leading-relaxed">
      {m && chipStyle ? (
        <span className="mt-0.5 inline-block min-w-[42px] rounded-[6px] px-1.5 py-0.5 text-center font-['Space_Grotesk'] text-[11px] font-bold"
          style={chipStyle}>{m[1]}</span>
      ) : (
        <span className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full" style={{ background: accent }} />
      )}
      <span>{m ? text.slice(m[0].length) : text}</span>
    </li>
  );
}

export default function StrategiesPage() {
  const [sel, setSel] = useState(STRATEGIES[0].id);
  const [search, setSearch] = useState("");

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return STRATEGIES;
    return STRATEGIES.filter((r) =>
      r.name.toLowerCase().includes(q) || r.id.toLowerCase().includes(q) ||
      r.bias.toLowerCase().includes(q));
  }, [search]);

  const groups = useMemo(() => {
    const by = new Map<string, typeof STRATEGIES>();
    for (const r of filtered) {
      const g = META[r.id]?.group ?? "Other";
      if (!by.has(g)) by.set(g, []);
      by.get(g)!.push(r);
    }
    return [...GROUP_ORDER, "Other"].filter((g) => by.has(g)).map((g) => [g, by.get(g)!] as const);
  }, [filtered]);

  const cur = STRATEGIES.find((r) => r.id === sel) ?? STRATEGIES[0];
  const meta = META[cur.id];
  const nOpt = STRATEGIES.filter((r) => r.kind === "Options").length;
  const bias = BIAS_PILL[meta?.biasKind ?? "neutral"];

  return (
    <div className="font-['Manrope'] text-[var(--strong)]">
      {/* page header */}
      <div className="flex flex-wrap items-end gap-3">
        <div className="min-w-0 max-w-[680px]">
          <div className="flex items-center gap-2.5">
            <span className="flex h-8 w-8 items-center justify-center rounded-[9px]"
              style={{ background: "var(--doc-bg)", color: "var(--doc)" }}>
              <DocIcon size={16} d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z|M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z" />
            </span>
            <h1 className="font-['Space_Grotesk'] text-[27px] font-bold m-0">Strategy docs</h1>
          </div>
          <p className="mt-1 text-sm text-[var(--muted)]">
            Structure, entry, exit and risk for every strategy on the platform. These mirror the
            engine's implementations — tune the parameters per deployment. (The Trade builders'
            custom positions and the broker smoke-test probe are tools, not strategies, so they
            aren't listed here.)
          </p>
        </div>
        <div className="ml-auto flex items-center rounded-[12px] border border-[var(--border)] bg-[var(--card)] px-3.5 py-2 text-[12px] font-semibold text-[var(--muted)]">
          <span className="mr-3 flex items-baseline gap-1.5">
            <span className="font-['Space_Grotesk'] text-[17px] font-bold tabular-nums text-[var(--strong)]">{STRATEGIES.length}</span>
            strategies
          </span>
          <span className="mr-3 h-4 w-px bg-[var(--divider)]" />
          <span className="flex items-baseline gap-1.5">
            <span className="font-['Space_Grotesk'] text-[17px] font-bold tabular-nums" style={{ color: "var(--doc)" }}>{nOpt}</span>
            options · {STRATEGIES.length - nOpt} equity
          </span>
        </div>
      </div>

      {/* master-detail */}
      <div className="mt-5 grid items-start gap-6 lg:grid-cols-[296px_1fr]">
        {/* index */}
        <div className="overflow-hidden rounded-[18px] border border-[var(--border)] bg-[var(--card)] lg:sticky lg:top-[92px]">
          <div className="border-b border-[var(--divider)] p-3">
            <div className="flex items-center gap-2 rounded-[11px] border border-[var(--field-border)] bg-[var(--field)] px-3 py-2 focus-within:border-[var(--doc)] focus-within:ring-[3px] focus-within:ring-[var(--doc-ring)]">
              <DocIcon size={13} d="M21 21l-4.3-4.3|M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16z" stroke="var(--faint)" />
              <input className="w-full bg-transparent text-sm text-[var(--strong)] placeholder:text-[var(--faint)] focus:outline-none"
                placeholder="Search strategies" value={search} onChange={(e) => setSearch(e.target.value)} />
            </div>
          </div>
          <div className="max-h-[calc(100vh-200px)] overflow-y-auto p-2">
            {groups.length === 0 && (
              <div className="px-3 py-4 text-sm text-[var(--faint)]">No strategy matches "{search}".</div>
            )}
            {groups.map(([g, rows]) => (
              <div key={g} className="mb-1.5">
                <div className="px-3 pb-1 pt-2 text-[10.5px] font-bold uppercase tracking-[0.08em] text-[var(--faint)]">{g}</div>
                {rows.map((r) => {
                  const active = r.id === sel;
                  const b = BIAS_PILL[META[r.id]?.biasKind ?? "neutral"];
                  return (
                    <button key={r.id} onClick={() => setSel(r.id)}
                      className="flex w-full items-center gap-2.5 rounded-[11px] px-3 py-2 text-left hover:bg-[var(--row-hover)]"
                      style={{
                        borderLeft: `3px solid ${active ? "var(--doc)" : "transparent"}`,
                        background: active ? "var(--doc-bg)" : undefined,
                      }}>
                      <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: b.fg }} />
                      <span className="min-w-0 flex-1">
                        <span className="block truncate font-['Space_Grotesk'] text-[13.5px] font-bold"
                          style={{ color: active ? "var(--doc)" : "var(--strong)" }}>{r.name}</span>
                        <span className="block truncate font-['Space_Grotesk'] text-[10.5px] text-[var(--faint)]">{r.id}</span>
                      </span>
                      <span className="shrink-0 rounded-full px-2 py-0.5 text-[10.5px] font-semibold"
                        style={r.kind === "Options"
                          ? { background: "var(--doc-bg)", color: "var(--doc)" }
                          : { background: "var(--ok-bg)", color: "var(--ok-text)" }}>
                        {r.kind}
                      </span>
                    </button>
                  );
                })}
              </div>
            ))}
          </div>
        </div>

        {/* detail pane */}
        <div className="overflow-hidden rounded-[20px] border border-[var(--border)] bg-[var(--card)]">
          <div className="h-[5px]" style={{ background: "linear-gradient(90deg, var(--doc), var(--accent))" }} />
          <div className="px-5 pb-8 pt-6 sm:px-8 sm:pt-7">
            {/* header */}
            <div className="flex flex-wrap items-center gap-2.5">
              <h2 className="font-['Space_Grotesk'] text-[25px] font-bold m-0">{cur.name}</h2>
              <span className="rounded-full px-2.5 py-1 text-[11px] font-bold"
                style={cur.kind === "Options"
                  ? { background: "var(--doc-bg)", color: "var(--doc)" }
                  : { background: "var(--ok-bg)", color: "var(--ok-text)" }}>{cur.kind}</span>
              <span className="inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-[11px] font-bold"
                style={{ background: bias.bg, color: bias.fg }}>
                <span>{bias.glyph}</span>{cur.bias.split("·")[0].trim()}
              </span>
              <code className="ml-auto rounded-[8px] bg-[var(--chip)] px-2.5 py-1 font-['Space_Grotesk'] text-[13px] font-semibold text-[var(--chip-text)]">{cur.id}</code>
            </div>
            <p className="mt-2 max-w-[820px] text-[15px] leading-[1.65] text-[var(--muted)]">{cur.summary}</p>
            {cur.links && (
              <div className="mt-1.5 flex flex-wrap gap-3">
                {cur.links.map((l) => (
                  <a key={l.url} href={l.url} target="_blank" rel="noreferrer"
                    className="text-xs underline underline-offset-2" style={{ color: "var(--doc)" }}>
                    {l.label} ↗
                  </a>
                ))}
              </div>
            )}

            {/* at a glance */}
            {meta && (
              <div className="mt-5 overflow-hidden rounded-[15px] border border-[var(--border)] bg-[var(--stat)]">
                <div className="grid grid-cols-2 sm:grid-cols-3">
                  {meta.facts.map(([label, value], i) => (
                    <div key={label} className={`px-4 py-3 ${i >= 3 ? "border-t border-[var(--divider)]" : ""} ${i % 3 !== 0 ? "sm:border-l sm:border-[var(--divider)]" : ""} ${i % 2 !== 0 ? "max-sm:border-l max-sm:border-[var(--divider)]" : ""} ${i >= 2 ? "max-sm:border-t max-sm:border-[var(--divider)]" : ""}`}>
                      <div className="text-[10.5px] font-bold uppercase tracking-[0.08em] text-[var(--faint)]">{label}</div>
                      <div className="mt-0.5 font-['Space_Grotesk'] text-[15px] font-semibold text-[var(--strong)]">{value}</div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* sections */}
            <div className="mt-4">
              {SECTION_DEF.map((sec) => (
                <div key={sec.key} className="grid gap-2 border-b border-[var(--divider)] py-4 sm:grid-cols-[150px_1fr] sm:gap-0">
                  <div className="flex items-center gap-2.5 self-start">
                    <span className="flex h-[30px] w-[30px] items-center justify-center rounded-[9px]"
                      style={{ background: "color-mix(in srgb, " + sec.accent + " 12%, transparent)", color: sec.accent }}>
                      <DocIcon d={sec.icon} />
                    </span>
                    <span className="font-['Space_Grotesk'] text-[13.5px] font-bold">{sec.label}</span>
                  </div>
                  <ul className="space-y-2.5">
                    {cur[sec.key].map((t, i) => <Bullet key={i} text={t} accent={sec.accent} />)}
                  </ul>
                </div>
              ))}
              {/* risk */}
              <div className="grid gap-2 py-4 sm:grid-cols-[150px_1fr] sm:gap-0">
                <div className="flex items-center gap-2.5 self-start">
                  <span className="flex h-[30px] w-[30px] items-center justify-center rounded-[9px]"
                    style={{ background: "color-mix(in srgb, var(--sec-risk) 12%, transparent)", color: "var(--sec-risk)" }}>
                    <DocIcon d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z|M12 9v4|M12 17h.01" />
                  </span>
                  <span className="font-['Space_Grotesk'] text-[13.5px] font-bold">Risk</span>
                </div>
                <p className="text-sm leading-[1.65] text-[var(--muted)]">{cur.risk}</p>
              </div>
            </div>

            {/* deploy footer */}
            {meta && (
              <div className="mt-2 flex flex-wrap items-center gap-3 rounded-[12px] px-4 py-3"
                style={{ background: "var(--doc-bg)", border: "1px solid var(--doc-border)" }}>
                <DocIcon size={15} d="M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20z|M12 16v-4|M12 8h.01" stroke="var(--doc)" />
                <span className="text-[12.5px] text-[var(--muted)]">{meta.deployNote}</span>
                {meta.deployCta && (
                  <Link to={meta.deployCta.to} className="ml-auto inline-flex items-center gap-1.5 text-[12.5px] font-bold"
                    style={{ color: "var(--doc)" }}>
                    {meta.deployCta.label}
                    <DocIcon size={13} d="M5 12h14|pl:12 5 19 12 12 19" stroke="var(--doc)" />
                  </Link>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
