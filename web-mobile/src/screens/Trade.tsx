import { useMutation, useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { api, brokers } from "@shared/api/client";
import { formatInr } from "@shared/lib/format";
import type { FibRetRow, OptionChainRow, OptionTradeLeg } from "@shared/types";

// Compact liquid-F&O default for the phone screener (desktop takes CSVs/custom lists).
const SCREENER_SYMBOLS = [
  "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "SBIN", "AXISBANK",
  "KOTAKBANK", "LT", "ITC", "BHARTIARTL", "TATAMOTORS", "BAJFINANCE", "MARUTI",
];
const UNDERLYINGS = ["NIFTY", "BANKNIFTY", "SENSEX"];

async function haptic() {
  try {
    const { Haptics, ImpactStyle } = await import("@capacitor/haptics");
    await Haptics.impact({ style: ImpactStyle.Light });
  } catch { /* browser dev */ }
}

/** 04+05 · Trade — Screener | Chain segmented inside one tab (per the design). All order
 * paths deploy the audited custom_options strategy; PAPER by default, LIVE needs a typed
 * confirmation on the review sheet. */
export default function TradeScreen() {
  const [tab, setTab] = useState<"screener" | "chain">("screener");
  const { data: accounts } = useQuery({ queryKey: ["brokers"], queryFn: brokers.list });
  const account = (accounts ?? []).find(
    (a) => a.has_session && (a.broker ?? "zerodha") === "zerodha");

  return (
    <div className="screen" style={{ paddingTop: "calc(14px + env(safe-area-inset-top))" }}>
      <div className="page-title">Trade</div>
      <div className="seg-track" style={{ marginTop: 12 }}>
        {(["screener", "chain"] as const).map((t) => (
          <button key={t} className={`seg-item ${tab === t ? "active" : ""}`}
            onClick={() => setTab(t)}>
            {t === "screener" ? "Screener" : "Chain"}
          </button>
        ))}
      </div>
      {!account ? (
        <div className="card" style={{ marginTop: 14, color: "var(--muted)", fontSize: 14 }}>
          Needs a logged-in Zerodha session — see the Brokers tab.
        </div>
      ) : tab === "screener" ? (
        <Screener accountId={account.id} />
      ) : (
        <Chain accountId={account.id} />
      )}
    </div>
  );
}

// ------------------------------------------------------------------ screener
function Screener({ accountId }: { accountId: number }) {
  const [rows, setRows] = useState<FibRetRow[] | null>(null);
  const run = useMutation({
    mutationFn: () => api.fibretAnalyze({
      broker_account_id: accountId, symbols: SCREENER_SYMBOLS,
    } as Parameters<typeof api.fibretAnalyze>[0]),
    onSuccess: (res) => setRows(res.rows ?? []),
  });
  const deploy = useMutation({ mutationFn: api.deployOptionTrade });
  const good = (rows ?? []).filter((r) => !r.error && r.strike && r.side);

  function sell(r: FibRetRow) {
    const name = `fibret_${r.symbol}_${r.side}`.toLowerCase();
    if (!window.confirm(
      `PAPER-deploy SELL ${r.symbol} ${r.strike} ${r.side} ` +
      `(exp ${r.expiry}, ~₹${r.premium ?? "?"}) as "${name}"?`)) return;
    deploy.mutate({
      name, underlying: r.symbol, expiry: r.expiry!, lot_size: r.lot_size ?? 0,
      legs: [{ right: r.side!, strike: r.strike!, side: "sell", lots: r.lots ?? 1 }],
      capital: 500_000, mode: "PAPER", quote_source: "zerodha",
      broker_account_id: accountId, ignore_market_hours: false, auto: true,
      notes: "mobile fibret screener",
    });
  }

  return (
    <div style={{ marginTop: 13 }}>
      <div style={{ display: "flex", gap: 7, alignItems: "center" }}>
        <span className="chip" style={{
          background: "var(--accent-deep)", color: "#fff", borderRadius: 999,
          padding: "7px 14px", fontSize: 13,
        }}>FibRet</span>
        <button onClick={() => run.mutate()} disabled={run.isPending} style={{
          marginLeft: "auto", background: "var(--accent-deep)", color: "#fff",
          borderRadius: 13, padding: "9px 18px", fontWeight: 800, fontSize: 14,
        }}>
          {run.isPending ? "Scanning…" : "Scan"}
        </button>
      </div>
      {rows && (
        <div style={{ fontSize: 13, color: "var(--faint)", margin: "10px 2px" }}>
          {good.length} of {rows.length} instruments with a setup
        </div>
      )}
      {run.error && (
        <div className="card" style={{ marginTop: 10, color: "var(--danger)", fontSize: 13 }}>
          {(run.error as Error).message}
        </div>
      )}
      {deploy.isSuccess && (
        <div className="card" style={{ marginTop: 10, color: "var(--pos)", fontSize: 13 }}>
          Deployed (paper) — see the Live tab.
        </div>
      )}
      <div style={{ display: "flex", flexDirection: "column", gap: 11, marginTop: 8 }}>
        {good.map((r) => (
          <div key={r.symbol} className="card">
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <span className="sg" style={{ fontWeight: 700, fontSize: 17, flex: 1 }}>
                {r.symbol}
              </span>
              <span className={`chip ${r.side === "CE" ? "danger" : "ok"}`}>
                SELL {r.side}
              </span>
            </div>
            <div style={{
              display: "flex", gap: 18, alignItems: "flex-end", marginTop: 12,
            }}>
              {([["Strike", r.strike?.toLocaleString("en-IN")],
                ["Prem", r.premium != null ? `₹${r.premium}` : "—"],
                ["Lots", String(r.lots ?? 1)]] as const).map(([l, v]) => (
                <div key={l}>
                  <div className="label">{l}</div>
                  <div className="sg" style={{ fontWeight: 700, fontSize: 15.5 }}>{v}</div>
                </div>
              ))}
              <button onClick={() => sell(r)} disabled={deploy.isPending} style={{
                marginLeft: "auto", background: "var(--accent)", color: "#fff",
                borderRadius: 13, padding: "10px 20px", fontWeight: 800, fontSize: 14,
              }}>Sell</button>
            </div>
            {r.liquid === false && (
              <div style={{
                marginTop: 10, background: "var(--warn-bg)", color: "var(--warn-text)",
                borderRadius: 10, padding: "8px 10px", fontSize: 12, fontWeight: 600,
              }}>
                ⚑ Wide bid–ask spread (&gt;10%) — consider another strike.
              </div>
            )}
          </div>
        ))}
        {!rows && !run.isPending && (
          <div className="card" style={{ color: "var(--muted)", fontSize: 13.5 }}>
            Scans {SCREENER_SYMBOLS.length} liquid F&O names for Fib-retracement option
            sells off the live chain. Tap Scan.
          </div>
        )}
      </div>
    </div>
  );
}

// -------------------------------------------------------------------- chain
type CartLeg = OptionTradeLeg & { premium: number | null };

function Chain({ accountId }: { accountId: number }) {
  const [underlying, setUnderlying] = useState("NIFTY");
  const [expiry, setExpiry] = useState<string | null>(null);
  const [cart, setCart] = useState<CartLeg[]>([]);
  const [review, setReview] = useState(false);

  const { data: expiries } = useQuery({
    queryKey: ["expiries", underlying, accountId],
    queryFn: () => api.optionsLiveExpiries(underlying, accountId),
    staleTime: 300_000,
  });
  const exps: string[] = (expiries?.expiries ?? []).slice(0, 3);
  const exp = expiry ?? exps[0] ?? null;
  const { data: chain } = useQuery({
    queryKey: ["chain", underlying, exp, accountId],
    queryFn: () => api.optionsLiveChain(underlying, exp!, accountId),
    enabled: !!exp,
    refetchInterval: 30_000,
  });

  const rows = useMemo(() => {
    const all = chain?.rows ?? [];
    if (!chain?.atm_strike) return all.slice(0, 11);
    const i = all.findIndex((r) => r.strike === chain.atm_strike);
    return all.slice(Math.max(0, i - 5), i + 6);
  }, [chain]);

  function addLeg(right: "CE" | "PE", strike: number, premium: number | null) {
    haptic();
    setCart((c) => {
      const i = c.findIndex((l) => l.right === right && l.strike === strike);
      if (i >= 0) return c.filter((_, j) => j !== i);   // tap again removes
      return [...c, { right, strike, side: "sell", lots: 1, premium }];
    });
  }

  const fmtExp = (e: string) => new Intl.DateTimeFormat("en-GB", {
    day: "2-digit", month: "short",
  }).format(new Date(e)).toUpperCase();

  return (
    <div style={{ marginTop: 13, paddingBottom: cart.length ? 130 : 0 }}>
      <div style={{ display: "flex", gap: 7, flexWrap: "wrap" }}>
        {UNDERLYINGS.map((u) => (
          <button key={u} onClick={() => { setUnderlying(u); setExpiry(null); setCart([]); }}
            className="chip" style={{
              borderRadius: 999, padding: "7px 14px", fontSize: 13,
              ...(u === underlying
                ? { background: "var(--accent-deep)", color: "#fff" } : {}),
            }}>{u}</button>
        ))}
        {chain?.spot != null && (
          <span className="sg" style={{
            marginLeft: "auto", fontWeight: 700, fontSize: 16, alignSelf: "center",
          }}>
            {chain.spot.toLocaleString("en-IN")}
          </span>
        )}
      </div>
      <div style={{ display: "flex", gap: 7, marginTop: 10 }}>
        {exps.map((e) => (
          <button key={e} onClick={() => setExpiry(e)} className="chip" style={{
            borderRadius: 999, padding: "6px 13px", fontSize: 12.5,
            ...(e === exp ? { background: "var(--accent)", color: "#fff" } : {}),
          }}>{fmtExp(e)}</button>
        ))}
      </div>

      <div className="card" style={{ marginTop: 12, padding: "8px 0" }}>
        <div style={{
          display: "grid", gridTemplateColumns: "1fr 1fr 1fr", padding: "4px 18px 8px",
          fontSize: 10.5, fontWeight: 700, color: "var(--faint)",
          textTransform: "uppercase", textAlign: "center",
        }}>
          <span>Call</span><span>Strike</span><span>Put</span>
        </div>
        {rows.map((r: OptionChainRow) => {
          const atm = r.strike === chain?.atm_strike;
          const inCart = (right: "CE" | "PE") =>
            cart.some((l) => l.right === right && l.strike === r.strike);
          return (
            <div key={r.strike} style={{
              display: "grid", gridTemplateColumns: "1fr 1fr 1fr", alignItems: "center",
              textAlign: "center", padding: "9px 18px",
              background: atm ? "var(--atm)" : undefined,
              borderTop: "1px solid var(--divider)",
            }}>
              <button onClick={() => addLeg("CE", r.strike, r.ce?.ltp ?? null)}
                style={{
                  minHeight: 40,
                  border: inCart("CE") ? "1.5px solid var(--accent)" : undefined,
                  borderRadius: 10,
                }}>
                <div className="sg" style={{
                  fontWeight: 700, fontSize: 15, color: "var(--pos)",
                }}>{r.ce?.ltp ?? "—"}</div>
                <div style={{ fontSize: 10, color: "var(--faint)" }}>
                  OI {r.ce?.oi != null ? Math.round(r.ce.oi / 1000) + "k" : "—"}
                </div>
              </button>
              <div className="sg" style={{
                fontWeight: 700, fontSize: 14.5,
                color: atm ? "var(--warn-text)" : "var(--strong)",
              }}>{r.strike.toLocaleString("en-IN")}</div>
              <button onClick={() => addLeg("PE", r.strike, r.pe?.ltp ?? null)}
                style={{
                  minHeight: 40,
                  border: inCart("PE") ? "1.5px solid var(--accent)" : undefined,
                  borderRadius: 10,
                }}>
                <div className="sg" style={{
                  fontWeight: 700, fontSize: 15, color: "var(--danger)",
                }}>{r.pe?.ltp ?? "—"}</div>
                <div style={{ fontSize: 10, color: "var(--faint)" }}>
                  OI {r.pe?.oi != null ? Math.round(r.pe.oi / 1000) + "k" : "—"}
                </div>
              </button>
            </div>
          );
        })}
        {!rows.length && (
          <div style={{ padding: 18, color: "var(--muted)", fontSize: 13.5 }}>
            Loading live chain…
          </div>
        )}
      </div>

      {cart.length > 0 && exp && chain && (
        <LegCart cart={cart} setCart={setCart} underlying={underlying} expiry={exp}
          lotSize={chain.lot_size ?? 0} accountId={accountId}
          review={review} setReview={setReview} />
      )}
    </div>
  );
}

function LegCart({ cart, setCart, underlying, expiry, lotSize, accountId, review, setReview }: {
  cart: CartLeg[]; setCart: (f: (c: CartLeg[]) => CartLeg[]) => void;
  underlying: string; expiry: string; lotSize: number; accountId: number;
  review: boolean; setReview: (v: boolean) => void;
}) {
  const [mode, setMode] = useState<"PAPER" | "LIVE">("PAPER");
  const legs: OptionTradeLeg[] = cart.map(({ premium: _p, ...l }) => l);
  const { data: margin } = useQuery({
    queryKey: ["margin", underlying, expiry, JSON.stringify(legs)],
    queryFn: () => api.optionTradeMargin({
      underlying, expiry, lot_size: lotSize, legs, broker_account_id: accountId,
    }),
    staleTime: 30_000,
  });
  const credit = cart.reduce((s, l) =>
    s + (l.side === "sell" ? 1 : -1) * (l.premium ?? 0) * l.lots * lotSize, 0);
  const deploy = useMutation({ mutationFn: api.deployOptionTrade });

  function submit() {
    if (mode === "LIVE") {
      const typed = window.prompt(
        "LIVE deploy — with an ARMED account + live trading enabled this places REAL " +
        "orders.\n\nType REAL to confirm:");
      if (typed !== "REAL") return;
    } else if (!window.confirm(`Deploy ${cart.length} leg(s) as a PAPER run?`)) {
      return;
    }
    deploy.mutate({
      name: `mobile_${underlying.toLowerCase()}_${cart.length}leg`,
      underlying, expiry, legs, lot_size: lotSize, capital: 500_000,
      mode, quote_source: "zerodha", broker_account_id: accountId,
      ignore_market_hours: false, auto: true, notes: "mobile chain builder",
    }, { onSuccess: () => { setReview(false); setCart(() => []); } });
  }

  return (
    <div style={{
      position: "fixed", left: 0, right: 0, bottom: 0, zIndex: 45,
      background: "var(--tab)", backdropFilter: "blur(12px)",
      WebkitBackdropFilter: "blur(12px)", borderTop: "1px solid var(--border)",
    }}>
      <div style={{
        maxWidth: 560, margin: "0 auto",
        padding: "10px 20px calc(12px + env(safe-area-inset-bottom))",
      }}>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          {cart.map((l, i) => (
            <button key={i} className="chip warn" style={{ textTransform: "none" }}
              onClick={() => setCart((c) => c.map((x, j) =>
                j === i ? { ...x, side: x.side === "sell" ? "buy" : "sell" } : x))}>
              {l.side.toUpperCase()} {l.right} {l.strike}
              {l.premium != null ? ` @${l.premium}` : ""}
            </button>
          ))}
          <span style={{
            marginLeft: "auto", fontSize: 12, color: "var(--muted)",
            alignSelf: "center", fontWeight: 700,
          }}>
            {margin?.margin != null ? `margin ${formatInr(margin.margin)}` : "margin …"}
            {" · "}credit {formatInr(credit)}
          </span>
        </div>
        {review && (
          <div style={{ display: "flex", gap: 8, marginTop: 10 }}>
            {(["PAPER", "LIVE"] as const).map((m) => (
              <button key={m} onClick={() => setMode(m)} className="chip" style={{
                borderRadius: 10, padding: "8px 14px", fontSize: 12.5,
                ...(mode === m
                  ? m === "LIVE"
                    ? { background: "var(--danger)", color: "#fff" }
                    : { background: "var(--accent-deep)", color: "#fff" }
                  : {}),
              }}>{m}</button>
            ))}
            <span style={{ fontSize: 11, color: "var(--faint)", alignSelf: "center" }}>
              tap a leg chip to flip buy/sell
            </span>
          </div>
        )}
        {deploy.error && (
          <div style={{ color: "var(--danger)", fontSize: 12.5, marginTop: 8 }}>
            {(deploy.error as Error).message}
          </div>
        )}
        {deploy.isSuccess && (
          <div style={{ color: "var(--pos)", fontSize: 12.5, marginTop: 8, fontWeight: 700 }}>
            Deployed — see the Live tab.
          </div>
        )}
        <button className="btn-primary" style={{ marginTop: 10, padding: 14 }}
          disabled={deploy.isPending}
          onClick={() => (review ? submit() : setReview(true))}>
          {review
            ? `${mode === "LIVE" ? "⚠ Deploy LIVE" : "Deploy paper"} · ${cart.length} leg${cart.length > 1 ? "s" : ""}`
            : `Review order · ${cart.length} leg${cart.length > 1 ? "s" : ""}`}
        </button>
      </div>
    </div>
  );
}
