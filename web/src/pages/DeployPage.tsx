import StrategySelect from "../components/StrategySelect";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { api, brokers } from "../api/client";
import { Card, ErrorBox, NumberInput } from "../components/ui";
import { isOptionsStrategy } from "../lib/params";
import type { ForwardTestPrefill, StartLiveRequest } from "../types";

const inputClass =
  "w-full rounded-md bg-slate-800 border border-slate-700 px-3 py-2 text-sm focus:outline-none focus:border-brand";
const lbl = "block text-xs text-slate-400 mb-1";

export default function DeployPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const pf = (location.state as { prefill?: ForwardTestPrefill } | null)?.prefill;

  const { data: strategyData } = useQuery({ queryKey: ["strategies"], queryFn: () => api.strategies() });
  const { data: universeData } = useQuery({ queryKey: ["universes"], queryFn: api.universes });
  const { data: accounts } = useQuery({ queryKey: ["brokers"], queryFn: brokers.list });

  const pfParams = (pf?.params ?? {}) as Record<string, unknown>;
  const { symbols: pfSymbols, lookback: pfLookback, tax_rate: pfTax, withdrawal_rate: pfWd, ...pfStrategyParams } =
    pfParams as { symbols?: string[]; lookback?: number; tax_rate?: number; withdrawal_rate?: number };

  const [name, setName] = useState(pf?.name ?? "");
  const [notes, setNotes] = useState("");
  const [strategyId, setStrategyId] = useState(pf?.strategy_id ?? "sst_lifo");
  const [universe, setUniverse] = useState(pf ? "" : "nifty50");
  const [symbols, setSymbols] = useState((pfSymbols ?? ["RELIANCE", "TCS", "INFY"]).join(", "));
  const [capital, setCapital] = useState(pf?.capital ?? 1000000);
  const [parts, setParts] = useState(10);
  const [target, setTarget] = useState(6);
  const [target1, setTarget1] = useState(10);
  const [target2, setTarget2] = useState(8);
  const [target3, setTarget3] = useState(6);
  const [maxLots, setMaxLots] = useState(0);
  const [taxRate, setTaxRate] = useState(20);
  const [withdrawalRate, setWithdrawalRate] = useState(0);
  const [lookback, setLookback] = useState(20);
  const [allocationMode, setAllocationMode] = useState("fixed");
  const [quoteSource, setQuoteSource] = useState("cache");
  const [accountId, setAccountId] = useState<number | null>(null);
  const [ignoreHours, setIgnoreHours] = useState(true);
  const [auto, setAuto] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Options (DERIV) deployment: underlying + lot-sets + per-exit check cadence. When
  // forward-testing a backtest, prefill from its recorded params (still editable).
  const ps = pfStrategyParams as Record<string, unknown>;
  const [underlying, setUnderlying] = useState(String(ps.underlying ?? "NIFTY"));
  const [optLots, setOptLots] = useState(Number(ps.lots) || 1);
  const [entryTime, setEntryTime] = useState(String(ps.entry_time ?? "09:45"));
  // Owner default (2026-07-17): sample the profit target every minute. The HNI deck's
  // 15-min cadence let a target-crossing slip between samples while the P&L oscillated
  // around the threshold; 1min keeps the deck's "sampled, not touched" semantics but
  // makes a miss window immaterial. Prefill from a forward-tested backtest still wins.
  const [profitCheck, setProfitCheck] = useState(String(ps.profit_check ?? "1min"));
  const [stopCheck, setStopCheck] = useState(String(ps.stop_check ?? "eod"));
  const [timeCheck, setTimeCheck] = useState(String(ps.time_check ?? "eod"));
  const [eodTime, setEodTime] = useState(String(ps.eod_time ?? "15:15"));
  // Options PAPER: optionally seed from a past date (replay as backtest → continue live).
  const [warmFromDate, setWarmFromDate] = useState("");

  const isFifo = strategyId === "sst_fifo";
  const isOptions = isOptionsStrategy(strategyId);
  // Daily decision time for EQUITY runs. Seeded from the strategy's own default (the server
  // is the source of truth — value_investing asks for 15:05 because the platform's 15:20 is
  // inside the closing auction for F&O-listed cash stocks) and re-seeded whenever the
  // strategy changes, so switching strategy never silently keeps the previous one's time.
  const strategyDecisionTime = strategyData?.decision_times?.[strategyId] ?? "15:20";
  const [decisionTime, setDecisionTime] = useState<string | null>(null);
  useEffect(() => { setDecisionTime(null); }, [strategyId]);
  const effectiveDecisionTime = decisionTime ?? strategyDecisionTime;
  // Continuous cash trading ends 15:15 for F&O-listed names (SEBI's closing auction runs to
  // ~15:35); an order placed after that rests unfilled and the run halts. Warn, never block —
  // a watchlist of only non-F&O names is fine later.
  const auctionRisk = !isOptions && effectiveDecisionTime >= "15:15";
  // value_investing shares NONE of the generic equity knobs (capital_parts / profit_target /
  // max_lots / allocation_mode all land in its ctor's **_ignored). Before this it deployed on
  // ctor defaults — ₹5,000/day, an empty watchlist and sizing="one_share", the price-weighted
  // mode the owner rejected — with the form showing knobs that did nothing.
  const isValueInvesting = strategyId === "value_investing";
  const [viBudget, setViBudget] = useState(5000);
  const [viFund, setViFund] = useState("LIQUIDBEES");
  const [viWatchlist, setViWatchlist] = useState("");
  const [viWarnDays, setViWarnDays] = useState(2);
  const [viSizing, setViSizing] = useState("equal_value");
  const [viSkew, setViSkew] = useState(25);
  const [viFundYield, setViFundYield] = useState(0);
  const CADENCES = ["tick", "1min", "5min", "15min", "30min", "60min", "eod"];
  const sessioned = (accounts ?? []).filter((a) => a.has_session);

  async function deploy() {
    setBusy(true);
    setError(null);
    const isCustom = universe === "";
    const manualParams = {
      capital_parts: parts,
      max_lots: maxLots,
      allocation_mode: allocationMode,
      ...(isFifo
        ? { profit_target_1: target1 / 100, profit_target_2: target2 / 100, profit_target_3: target3 / 100 }
        : { profit_target: target / 100 }),
    };
    const optionsParams = {
      underlying,
      lots: optLots,
      entry_time: entryTime,
      profit_check: profitCheck,
      stop_check: stopCheck,
      time_check: timeCheck,
      eod_time: eodTime,
    };
    const body: StartLiveRequest = {
      strategy_id: strategyId,
      name: name.trim() || undefined,
      notes: notes.trim() || undefined,
      instrument_class: isOptions ? "DERIV" : "STOCK",
      underlying: isOptions ? underlying : undefined,
      universe: isOptions || isCustom ? null : universe,
      symbols: isOptions ? [] : isCustom ? symbols.split(",").map((s) => s.trim()).filter(Boolean) : [],
      capital,
      params: isOptions
        ? { ...pfStrategyParams, ...optionsParams }
        : pf
        ? pfStrategyParams
        : isValueInvesting
        ? {
            daily_budget: viBudget,
            fund_source: viFund.trim().toUpperCase(),
            watchlist: viWatchlist.trim(),
            warn_days_left: viWarnDays,
            sizing: viSizing,
            max_skew_pct: viSkew,
            fund_yield_pct: viFundYield,
            // fund_seed is deliberately NOT sent: the ctor default "never" is the safe one
            // (a deploy must never place a surprise multi-lakh ETF buy — CLAUDE.md §1), and
            // it is not needed here because idle cash is spent BEFORE the ETF, so a fresh
            // deploy drips out of its own capital until that runs down.
          }
        : manualParams,
      tax_rate: pf ? (pfTax ?? 0.2) : isOptions || isValueInvesting ? 0 : taxRate / 100,
      withdrawal_rate: pf ? (pfWd ?? 0) : withdrawalRate / 100,
      lookback: pf ? (pfLookback ?? 20) : isValueInvesting ? 1 : lookback,
      quote_source: quoteSource,
      broker_account_id: quoteSource !== "cache" ? accountId : null,
      ignore_market_hours: ignoreHours,
      ...(isOptions ? {} : { decision_time: effectiveDecisionTime }),
      auto,
      ...(warmFromDate ? { warm_from_date: warmFromDate } : {}),
    };
    try {
      await api.liveStart(body);
      navigate("/live");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <Link to="/live" className="text-slate-400 hover:text-slate-200 text-sm">← Live</Link>
        <h1 className="text-lg font-semibold">{pf ? "Forward-test a backtest" : "Deploy a strategy"}</h1>
      </div>

      <Card>
        <div className="grid md:grid-cols-2 gap-3 mb-3">
          <label className="block">
            <span className={lbl}>Deployment name</span>
            <input className={inputClass} placeholder="e.g. SST Nifty50 paper" value={name} onChange={(e) => setName(e.target.value)} />
          </label>
          <label className="block">
            <span className={lbl}>Notes</span>
            <input className={inputClass} placeholder="why / what you're testing" value={notes} onChange={(e) => setNotes(e.target.value)} />
          </label>
        </div>

        {pf && (
          <div className="text-sm text-slate-400 mb-3">
            {pf.strategy_id}
            {(pfSymbols ?? []).length ? ` · ${(pfSymbols ?? []).length} symbols` : ""} · params from
            backtest{isOptions ? " — underlying / lot-sets / cadence editable below" : " (capital editable below)"}
          </div>
        )}

        {/* Strategy + (equity) universe — only when deploying manually (pf locks the strategy). */}
        {!pf && (
          <div className="grid md:grid-cols-3 gap-3 mb-3">
            <label className="block">
              <span className={lbl}>Strategy</span>
              <StrategySelect value={strategyId} onChange={setStrategyId}
                ids={strategyData?.strategies ?? ["sst_lifo"]} className={inputClass} />
            </label>
            {!isOptions && (
              <>
                <label className="block">
                  <span className={lbl}>Universe</span>
                  <select className={inputClass} value={universe} onChange={(e) => setUniverse(e.target.value)}>
                    {(universeData ?? []).map((u) => (
                      <option key={u.name} value={u.name}>{u.label} ({u.count})</option>
                    ))}
                    <option value="">Custom</option>
                  </select>
                </label>
                <label className="block">
                  <span className={lbl}>Symbols</span>
                  {universe === "" ? (
                    <input className={inputClass} value={symbols} onChange={(e) => setSymbols(e.target.value)} />
                  ) : (
                    <input className={`${inputClass} text-slate-500`} disabled value="(from universe)" />
                  )}
                </label>
              </>
            )}
          </div>
        )}

        {/* Options deployment: underlying + lot-sets + per-exit cadence (manual OR forward-test). */}
        {isOptions ? (
          <div className="space-y-3 mb-3">
            <div className="grid md:grid-cols-3 lg:grid-cols-5 gap-3">
              <label className="block">
                <span className={lbl}>Underlying</span>
                <select className={inputClass} value={underlying} onChange={(e) => setUnderlying(e.target.value)}>
                  {strategyId === "hni_weekly" ? (
                    <option value="NIFTY">NIFTY (weeklies)</option>
                  ) : (
                    <>
                      <option value="NIFTY">NIFTY</option>
                      <option value="BANKNIFTY">BANKNIFTY</option>
                    </>
                  )}
                </select>
              </label>
              <label className="block">
                <span className={lbl}>{strategyId === "hni_weekly" ? "Lot-sets (× 1-3-2)" : "Lots"}</span>
                <NumberInput className={inputClass} value={optLots} onChange={setOptLots} />
              </label>
              <label className="block">
                <span className={lbl}>Seed from (optional)</span>
                <input
                  type="date"
                  className={inputClass}
                  value={warmFromDate}
                  onChange={(e) => setWarmFromDate(e.target.value)}
                />
              </label>
            </div>
            {warmFromDate && (
              <div className="text-[11px] text-sky-700 dark:text-sky-300/90">
                Replays the strategy as a backtest from {warmFromDate} → today, then carries the
                resulting open position forward as the live (PAPER) book — so a month-before-expiry
                strategy can be tested now instead of waiting for the next cycle. Needs the option
                chain cached back to that date.
              </div>
            )}
            <div className="text-[11px] text-amber-700 dark:text-amber-300/90">
              Intraday exit cadence — how often each exit is evaluated. Profit can book intraday
              (e.g. every 15 min) while the stop holds to EOD. Intraday cadences need live Zerodha
              quotes during market hours; on cache they collapse to one EOD check. PAPER
              (simulated fills) — no real orders.
            </div>
            <div className="grid md:grid-cols-3 lg:grid-cols-5 gap-3">
              <label className="block">
                <span className={lbl}>Entry time (IST)</span>
                <input className={inputClass} value={entryTime} onChange={(e) => setEntryTime(e.target.value)} placeholder="09:45" />
              </label>
              <label className="block">
                <span className={lbl}>Profit check</span>
                <select className={inputClass} value={profitCheck} onChange={(e) => setProfitCheck(e.target.value)}>
                  {CADENCES.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
              </label>
              <label className="block">
                <span className={lbl}>Stop check</span>
                <select className={inputClass} value={stopCheck} onChange={(e) => setStopCheck(e.target.value)}>
                  {CADENCES.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
              </label>
              <label className="block">
                <span className={lbl}>Time-exit check</span>
                <select className={inputClass} value={timeCheck} onChange={(e) => setTimeCheck(e.target.value)}>
                  {CADENCES.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
              </label>
              <label className="block">
                <span className={lbl}>EOD time (IST)</span>
                <input className={inputClass} value={eodTime} onChange={(e) => setEodTime(e.target.value)} placeholder="15:15" />
              </label>
            </div>
          </div>
        ) : (!pf && isValueInvesting) ? (
          <div className="grid md:grid-cols-3 gap-3 mb-3">
            <label className="block">
              <span className={lbl}>Daily budget (₹)</span>
              <NumberInput className={inputClass} value={viBudget} onChange={setViBudget} />
            </label>
            <label className="block">
              <span className={lbl}>Fund source (ETF sold to fund each day)</span>
              <input className={inputClass} value={viFund}
                onChange={(e) => setViFund(e.target.value)} />
            </label>
            <label className="block">
              <span className={lbl}>Warn at days of runway left</span>
              <NumberInput className={inputClass} value={viWarnDays} onChange={setViWarnDays} />
            </label>
            <label className="block">
              <span className={lbl}>Sizing</span>
              <select className={inputClass} value={viSizing}
                onChange={(e) => setViSizing(e.target.value)}>
                <option value="equal_value">Equal value — same rupees into every name</option>
                <option value="balanced">Balanced — one share each, skew-capped</option>
                <option value="one_share">One share each (weights by share price)</option>
              </select>
            </label>
            {viSizing === "balanced" && (
              <label className="block">
                <span className={lbl}>Max overweight vs average %</span>
                <NumberInput className={inputClass} value={viSkew} onChange={setViSkew} />
              </label>
            )}
            <label className="block">
              <span className={lbl}>Fund source yield %/yr (reported only)</span>
              <NumberInput step="0.1" className={inputClass} value={viFundYield}
                onChange={setViFundYield} />
            </label>
            <div className="md:col-span-3">
              <label className="block">
                <span className={lbl}>Watchlist (comma-separated; blank = every symbol above)</span>
                <input className={inputClass} value={viWatchlist} placeholder="ITC, HDFCBANK, TITAN"
                  onChange={(e) => setViWatchlist(e.target.value)} />
              </label>
              <p className="mt-1 text-xs text-slate-500">
                Never sells. The fund source is added to the run's symbols automatically; idle
                cash is spent before it. Editable later on the running tile — the run's symbol
                list is not, so deploy with a generous superset.
              </p>
            </div>
          </div>
        ) : (!pf && (
          <div className="grid md:grid-cols-3 lg:grid-cols-6 gap-3 mb-3">
            <label className="block">
              <span className={lbl}>Capital parts</span>
              <NumberInput className={inputClass} value={parts} onChange={setParts} />
            </label>
            {isFifo ? (
              <>
                <label className="block"><span className={lbl}>Target % (1 lot)</span><NumberInput step="0.1" className={inputClass} value={target1} onChange={setTarget1} /></label>
                <label className="block"><span className={lbl}>Target % (2)</span><NumberInput step="0.1" className={inputClass} value={target2} onChange={setTarget2} /></label>
                <label className="block"><span className={lbl}>Target % (3+)</span><NumberInput step="0.1" className={inputClass} value={target3} onChange={setTarget3} /></label>
              </>
            ) : (
              <label className="block"><span className={lbl}>Profit target %</span><NumberInput step="0.1" className={inputClass} value={target} onChange={setTarget} /></label>
            )}
            <label className="block"><span className={lbl}>Max lots (0=∞)</span><NumberInput className={inputClass} value={maxLots} onChange={setMaxLots} /></label>
            <label className="block"><span className={lbl}>Lookback</span><NumberInput className={inputClass} value={lookback} onChange={setLookback} /></label>
            <label className="block"><span className={lbl}>Tax rate %</span><NumberInput className={inputClass} value={taxRate} onChange={setTaxRate} /></label>
            <label className="block"><span className={lbl}>Withdrawal %</span><NumberInput className={inputClass} value={withdrawalRate} onChange={setWithdrawalRate} /></label>
            <label className="block">
              <span className={lbl}>Position sizing</span>
              <select className={inputClass} value={allocationMode} onChange={(e) => setAllocationMode(e.target.value)}>
                <option value="fixed">Fixed</option>
                <option value="equity_scaled">Equity-scaled</option>
              </select>
            </label>
          </div>
        ))}

        {/* Equity seed date (options have their own seed field above). */}
        {!isOptions && (
          <div className="mb-3">
            <label className="block max-w-xs">
              <span className={lbl}>Seed from (optional)</span>
              <input
                type="date"
                className={inputClass}
                value={warmFromDate}
                onChange={(e) => setWarmFromDate(e.target.value)}
              />
            </label>
            {warmFromDate && (
              <div className="text-[11px] text-sky-700 dark:text-sky-300/90 mt-1">
                Replays the strategy from {warmFromDate} → today as a backtest, then carries the
                resulting open positions + trade history forward as the live (PAPER) starting book —
                so a mid-trade strategy can be forward-tested now. Needs price data cached back to
                that date.
              </div>
            )}
          </div>
        )}

        <div className="grid md:grid-cols-4 gap-3 items-end">
          <label className="block">
            <span className={lbl}>Capital (₹)</span>
            <NumberInput className={inputClass} value={capital} onChange={setCapital} />
          </label>
          <label className="block">
            <span className={lbl}>Quotes</span>
            <select className={inputClass} value={quoteSource} onChange={(e) => setQuoteSource(e.target.value)}>
              <option value="cache">Cache (last close, offline)</option>
              <option value="zerodha">Zerodha (live)</option>
              <option value="dhan">Dhan (live)</option>
            </select>
          </label>
          {quoteSource !== "cache" && (
            <label className="block">
              <span className={lbl}>Account</span>
              {/* a broker quote source must ride an account of THAT broker (server enforces) */}
              <select className={inputClass} value={accountId ?? ""} onChange={(e) => setAccountId(e.target.value ? +e.target.value : null)}>
                <option value="">select…</option>
                {sessioned.filter((a) => (a.broker || "zerodha") === quoteSource).map((a) => (<option key={a.id} value={a.id}>{a.label}</option>))}
              </select>
              {sessioned.length === 0 && (
                <span className="mt-1 block text-xs text-amber-600 dark:text-amber-400">
                  No logged-in session — <Link to="/brokers" className="underline">log in on Brokers</Link>, or switch Quotes to Cache.
                </span>
              )}
            </label>
          )}
          {!isOptions && (
            <label className="text-sm text-slate-300">
              Decision time (IST)
              <input
                type="time"
                className={inputClass}
                value={effectiveDecisionTime}
                onChange={(e) => setDecisionTime(e.target.value)}
              />
              <span className="mt-1 block text-xs text-slate-400">
                The daily buy/sell decision fires at/after this, once per day. Cannot be
                changed on a running deployment — stop and redeploy.
              </span>
              {auctionRisk && (
                <span className="mt-1 block text-xs text-amber-600 dark:text-amber-400">
                  After 15:15 continuous cash trading has ended for <b>F&amp;O-listed</b>
                  {" "}stocks (closing auction runs to ~15:35). Orders in those names would
                  rest unfilled and the run would halt daily. Fine only if your watchlist is
                  entirely non-F&amp;O.
                </span>
              )}
            </label>
          )}
          <div className="flex flex-col gap-1">
            <label className="flex items-center gap-2 text-sm text-slate-300">
              <input type="checkbox" checked={ignoreHours} onChange={(e) => setIgnoreHours(e.target.checked)} />
              ignore market hours
            </label>
            <label className="flex items-center gap-2 text-sm text-slate-300">
              <input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} />
              auto loop (refresh + daily decision)
            </label>
          </div>
        </div>

        <div className="mt-4 flex items-center gap-3">
          <button
            onClick={deploy}
            disabled={busy || (quoteSource !== "cache" && !accountId)}
            title={
              quoteSource !== "cache" && !accountId
                ? sessioned.length === 0
                  ? "Log in a broker session on Brokers, or switch Quotes to Cache"
                  : "Select an account"
                : "Deploy"
            }
            className="rounded-md bg-brand hover:bg-brand-light px-4 py-2 text-sm font-medium disabled:opacity-50"
          >
            {busy ? "Deploying…" : "Deploy"}
          </button>
          <span className="text-xs text-slate-500">
            {quoteSource !== "cache" ? "Live quotes" : "Cache quotes (offline)"} · simulated fills · no real orders
          </span>
        </div>
        {error && <div className="mt-2"><ErrorBox message={error} /></div>}
      </Card>
    </div>
  );
}
