import { useState, type ReactNode } from "react";
import CpRatioExpiryBuilder from "../../components/trade/CpRatioExpiryBuilder";
import DeltaNeutralBuilder from "../../components/trade/DeltaNeutralBuilder";
import EquityTradeBuilder from "../../components/trade/EquityTradeBuilder";
import IntradayStraddleBuilder from "../../components/trade/IntradayStraddleBuilder";
import IronFlyBuilder from "../../components/trade/IronFlyBuilder";
import MomentumThetaBuilder from "../../components/trade/MomentumThetaBuilder";
import WeeklyIntradayStraddleBuilder from "../../components/trade/WeeklyIntradayStraddleBuilder";

/** Deploy a MANAGED strategy — parameterize one of the coded algos; the engine then trades every
 *  cycle on its own. The left rail picks the strategy; the right renders that strategy's deploy
 *  form (the existing self-contained builders). Manual multi-leg positions live in "Build". */

type Cadence = "MONTHLY" | "WEEKLY" | "DAILY" | "INTRADAY" | "EXPIRY DAY" | "POSITIONAL";

interface Managed {
  id: string;
  name: string;
  cadence: Cadence;
  blurb: string;
  builder: ReactNode;
}

const STRATS: Managed[] = [
  {
    id: "delta_neutral",
    name: "Delta neutral",
    cadence: "MONTHLY",
    blurb: "18Δ strangle → rolls the cheap side → caps into an iron fly",
    builder: <DeltaNeutralBuilder />,
  },
  {
    id: "iron_fly",
    name: "Iron fly",
    cadence: "MONTHLY",
    blurb: "ATM fly from day one, naked untested-side adjustment",
    builder: <IronFlyBuilder />,
  },
  {
    id: "straddle",
    name: "Intraday straddle",
    cadence: "DAILY",
    blurb: "09:18 → 15:25 intraday ATM straddle, ratchet trail",
    builder: <IntradayStraddleBuilder />,
  },
  {
    id: "weekly_straddle",
    name: "Weekly straddle",
    cadence: "WEEKLY",
    blurb: "Strike locked per expiry cycle, VWAP + prior-low gate",
    builder: <WeeklyIntradayStraddleBuilder />,
  },
  {
    id: "intraday_theta",
    name: "Intraday theta",
    cadence: "INTRADAY",
    blurb: "SuperTrend + pivots → sell ATM weeklies, flat by 15:20",
    builder: <MomentumThetaBuilder />,
  },
  {
    id: "cp_ratio",
    name: "CP ratio expiry",
    cadence: "EXPIRY DAY",
    blurb: "Buy 1 ATM set, sell 3 at the ⅓-premium strikes",
    builder: <CpRatioExpiryBuilder />,
  },
  {
    id: "equity",
    name: "Equity",
    cadence: "POSITIONAL",
    blurb: "Long-only managed stock position",
    builder: <EquityTradeBuilder />,
  },
];

function CadenceTag({ cadence }: { cadence: Cadence }) {
  return (
    <span className="rounded-full bg-[var(--chip)] px-1.5 py-0.5 text-[9.5px] font-extrabold tracking-wide text-[var(--chip-text)]">
      {cadence}
    </span>
  );
}

export default function DeployView() {
  const [sel, setSel] = useState(STRATS[0].id);
  const active = STRATS.find((s) => s.id === sel) ?? STRATS[0];

  return (
    <div className="grid grid-cols-1 gap-5 lg:grid-cols-[320px_1fr]">
      {/* left rail — managed strategies */}
      <div className="space-y-2">
        <div className="mb-1 text-[11.5px] font-extrabold uppercase tracking-wide text-[var(--faint)]">
          Managed strategies · {STRATS.length}
        </div>
        {STRATS.map((s) => {
          const on = s.id === sel;
          return (
            <button
              key={s.id}
              onClick={() => setSel(s.id)}
              className={`w-full rounded-[14px] border-[1.5px] bg-[var(--card)] px-3.5 py-3 text-left transition ${
                on ? "border-[var(--accent)]" : "border-[var(--border)] hover:border-[var(--field-border)]"
              }`}
            >
              <div className="flex items-center justify-between gap-2">
                <span
                  className={`font-['Space_Grotesk'] text-[14.5px] font-bold ${
                    on ? "text-[var(--accent-deep)]" : "text-[var(--strong)]"
                  }`}
                >
                  {s.name}
                </span>
                <CadenceTag cadence={s.cadence} />
              </div>
              <div className="mt-1 text-[12px] leading-snug text-[var(--faint)]">{s.blurb}</div>
            </button>
          );
        })}
      </div>

      {/* right — the selected strategy's deploy form */}
      <div className="min-w-0">{active.builder}</div>
    </div>
  );
}
