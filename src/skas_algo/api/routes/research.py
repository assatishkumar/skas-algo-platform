"""Research endpoints — offline studies + live calibration for strategy validation.

The Donchian breakout study is cache-only (daily bars; no broker session). The BS
calibration fetches LIVE chains through a logged-in adapter but is strictly read-only —
it must never import or touch any order-placing path.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from skas_algo.api.deps import get_db
from skas_algo.api.models import BsCalibrationRequest, DonchianStudyRequest, MtgBacktestRequest
from skas_algo.api.routes.data import _live_adapter
from skas_algo.data import universes
from skas_algo.data.options_provider import VIX_SYMBOL, _ffill_lookup
from skas_algo.data.provider import get_available_symbols, get_data_cache
from skas_algo.engine.jsonutil import to_native
from skas_algo.services.bs_calibration import aggregate, calibrate_name
from skas_algo.services.donchian_strangle import resolve_cycle
from skas_algo.services.donchian_study import (
    INDEX_NAME,
    StudyParams,
    monthly_cycles,
    run_study,
)

router = APIRouter(prefix="/research", tags=["research"])

# Small in-process memo so the UI can tweak buffer/basis without recomputing 16 years of
# bars each time. Keyed on everything that changes the result; FIFO-evicted, never persisted.
_memo: dict[tuple, dict] = {}
_MEMO_MAX = 8


@router.post("/donchian-study")
def donchian_study(
    body: DonchianStudyRequest,
    sd=Depends(get_data_cache),
    avail: set[str] = Depends(get_available_symbols),
) -> dict:
    if body.basis not in ("touch", "close"):
        raise HTTPException(status_code=422, detail="basis must be 'touch' or 'close'")
    end = body.end_date or date.today()
    if body.symbols:
        symbols = [s for s in body.symbols if s in avail]
    else:
        try:
            symbols = universes.resolve(body.universe, avail)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not symbols:
        raise HTTPException(status_code=404, detail="no cached symbols to study")

    key = (tuple(sorted(symbols)), body.start_date, end, body.buffer_pct, body.basis,
           body.max_flips, body.include_index)
    result = _memo.get(key)
    if result is None:
        index_df = sd.get_prices(symbol=INDEX_NAME, start_date=body.start_date, end_date=end)
        if index_df is None or len(index_df) == 0:
            raise HTTPException(status_code=503,
                                detail=f"no cached {INDEX_NAME!r} series — refresh the data cache")
        trading_days = sorted(pd.to_datetime(index_df["date"]).dt.date.tolist())
        cycles = monthly_cycles(trading_days, body.start_date, end)
        if not cycles:
            raise HTTPException(status_code=422,
                                detail="window too short — needs at least 3 monthly expiries")
        frames: dict[str, pd.DataFrame] = {
            sym: sd.get_prices(symbol=sym, start_date=body.start_date, end_date=end)
            for sym in symbols
        }
        frames[INDEX_NAME] = index_df
        params = StudyParams(buffer_pct=body.buffer_pct, basis=body.basis,
                             max_flips=body.max_flips, include_index=body.include_index)
        # to_native: pandas/numpy scalars (np.bool_/np.int64) are not JSON-serializable —
        # coerce the whole tree once, before it's memoized.
        result = to_native(run_study(frames, cycles, _ffill_lookup(sd, VIX_SYMBOL), params))
        if len(_memo) >= _MEMO_MAX:
            _memo.pop(next(iter(_memo)))
        _memo[key] = result
    if body.detail:
        return result
    return {k: v for k, v in result.items() if k != "detail"}


@router.post("/momentum-theta-bt")
def momentum_theta_bt(body: MtgBacktestRequest, db: Session = Depends(get_db)) -> dict:
    """Dedicated intraday backtest for momentum_theta_gainer_intra (NIFTY only): replays
    real 15-min Kite bars through the ACTUAL strategy class; premiums are Black-Scholes
    (prior-day HV20 × vol_multiplier). READ-ONLY: the only broker use is topping up the
    local bar store via historical_data — no order paths.

    Runs sync in-request: a 3-year replay is ~25k bars × 4 ticks ≈ a few seconds; the
    first-ever run also fetches bars (~10-20 s with a session)."""
    from skas_algo.services import broker as broker_svc
    from skas_algo.services.momentum_theta_bt import MtgBtParams, run_backtest

    adapter = None
    if body.broker_account_id is not None:
        from skas_algo.db.models import BrokerAccount

        acct = db.get(BrokerAccount, body.broker_account_id)
        ok_broker = acct is not None and (acct.broker or "zerodha").lower() == "zerodha"
        if ok_broker and broker_svc.has_valid_session(acct):
            adapter = broker_svc.make_adapter(acct)  # bar top-up only (read-only)
    params = MtgBtParams(
        start=body.start_date, end=body.end_date or date.today(), lots=body.lots,
        st_period=body.st_period, st_multiplier=body.st_multiplier,
        max_trades_per_day=body.max_trades_per_day, entry_cutoff=body.entry_cutoff,
        eod_exit=body.eod_exit, min_dte=body.min_dte, vol_multiplier=body.vol_multiplier,
        r=body.r, slippage_bps=body.slippage_bps, capital=body.capital,
    )
    return to_native(run_backtest(params, adapter=adapter))


@router.post("/bs-calibration")
def bs_calibration(
    body: BsCalibrationRequest,
    db: Session = Depends(get_db),
    sd=Depends(get_data_cache),
    avail: set[str] = Depends(get_available_symbols),
) -> dict:
    """BS-with-HV vs the LIVE chain at the screener's strikes + ATM, per basket name.

    READ-ONLY: quote/chain fetches on a logged-in adapter only — no order paths. ~50
    serial chain calls, so expect 10–20 s; the UI shows a computing state."""
    adapter = _live_adapter(body.broker_account_id, db)  # 4xx if no valid session
    today = date.today()
    names = [s.strip().upper() for s in body.names if s.strip()] or \
        universes.resolve("nifty50", avail)

    # Cycle anchors exactly like the live screener: index calendar + a representative
    # name's listed monthly expiries (stocks list monthlies only).
    try:
        nifty_df = sd.get_prices("NIFTY 50", start_date=today - timedelta(days=400),
                                 end_date=today)
    except Exception:
        nifty_df = None
    trading_days = (
        {(d.date() if hasattr(d, "date") else d) for d in nifty_df["date"].tolist()}
        if nifty_df is not None and len(nifty_df) else None
    )
    listed: list[date] = []
    for sym in names:
        try:
            exps = adapter.option_expiries(sym) or []
        except Exception:  # pragma: no cover - network hiccup
            exps = []
        if exps:
            listed = sorted({date.fromisoformat(str(e)[:10]) for e in exps})
            break
    sell_override = date.fromisoformat(body.sell_expiry[:10]) if body.sell_expiry else None
    cyc = resolve_cycle(today, listed, trading_days=trading_days, sell_expiry=sell_override)
    sell, rstart, rend = cyc["sell_expiry"], cyc["range_start"], cyc["range_end"]
    if not (sell and rstart and rend):
        raise HTTPException(status_code=422,
                            detail="could not resolve the cycle — pass sell_expiry explicitly")

    rows: list[dict] = []
    errors: list[dict] = []
    for sym in names:
        try:
            df = sd.get_prices(sym, start_date=today - timedelta(days=400), end_date=today)
        except Exception:
            df = None
        if df is None or len(df) == 0:
            errors.append({"symbol": sym, "error": "no cached price history"})
            continue
        try:
            chain = adapter.live_option_chain(sym, sell.isoformat())
        except Exception as exc:  # pragma: no cover - network hiccup
            errors.append({"symbol": sym, "error": f"live chain failed: {exc}"})
            continue
        if not chain:
            errors.append({"symbol": sym, "error": f"no listed options for {sell.isoformat()}"})
            continue
        name_rows = calibrate_name(
            symbol=sym, df=df, chain=chain, sell_expiry=sell, today=today,
            range_start=rstart, range_end=rend, hv_window=body.hv_window, r=body.r,
            round_out=body.round_out,
        )
        if name_rows:
            rows.extend(name_rows)
        else:
            errors.append({"symbol": sym, "error": "chain/HV unresolvable"})
    out: dict = to_native({
        "as_of": today.isoformat(), "sell_expiry": sell.isoformat(),
        "range_start": rstart.isoformat(), "range_end": rend.isoformat(),
        "r": body.r, "hv_window": body.hv_window,
        "rows": rows, "aggregates": aggregate(rows), "errors": errors,
    })
    return out
