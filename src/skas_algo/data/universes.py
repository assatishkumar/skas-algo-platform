"""Named symbol universes (Nifty 50 / 100 / 200).

Lists are static current constituents (same survivorship-bias approach as
skas-trading). ``resolve`` intersects a universe with the symbols actually present
in the data cache, so a preset never includes a name we have no data for (the
source lists carry a few renamed/delisted tickers, e.g. TATAMOTORS, ZOMATO, IDFC).

Nifty 50/100 are ported verbatim from skas-trading; Nifty 200 is user-provided.
"""

from __future__ import annotations

# Nifty 50 — user-provided constituent list (as of June 2026; reflects renames such
# as TATAMOTORS→TMPV). Names without cached data are dropped by resolve().
NIFTY_50: list[str] = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJAJFINSV", "BAJFINANCE", "BHARTIARTL", "BPCL",
    "BRITANNIA", "CIPLA", "COALINDIA", "DIVISLAB", "DRREDDY",
    "EICHERMOT", "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE",
    "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDUSINDBK",
    "INFY", "ITC", "JSWSTEEL", "KOTAKBANK", "LT",
    "LTIM", "M&M", "MARUTI", "NESTLEIND", "NTPC",
    "ONGC", "POWERGRID", "RELIANCE", "SBILIFE", "SBIN",
    "SHRIRAMFIN", "SUNPHARMA", "TATACONSUM", "TATASTEEL", "TCS",
    "TECHM", "TITAN", "TMPV", "ULTRACEMCO", "WIPRO",
]

# Nifty 100 — user-provided constituent list (as of June 2026; size reflects 2025/26
# index revisions). Names without cached data are dropped by resolve().
NIFTY_100: list[str] = [
    "ABB", "ADANIENSOL", "ADANIENT", "ADANIGREEN", "ADANIPORTS",
    "ADANIPOWER", "AMBUJACEM", "APOLLOHOSP", "ASIANPAINT", "ATGL",
    "AUROPHARMA", "AXISBANK", "BAJAJ-AUTO", "BAJAJFINSV", "BAJAJHLDNG",
    "BAJFINANCE", "BANKBARODA", "BEL", "BHARTIARTL", "BHEL",
    "BOSCHLTD", "BPCL", "BRITANNIA", "CANBK", "CGPOWER",
    "CHOLAFIN", "CIPLA", "COALINDIA", "COLPAL", "DABUR",
    "DIVISLAB", "DLF", "DRREDDY", "EICHERMOT", "ETERNAL",
    "GAIL", "GODREJCP", "GRASIM", "HAL", "HAVELLS",
    "HCLTECH", "HDFCAMC", "HDFCBANK", "HDFCLIFE", "HEROMOTOCO",
    "HINDALCO", "HINDUNILVR", "ICICIBANK", "ICICIGI", "ICICIPRULI",
    "IGL", "INDIGO", "INDUSINDBK", "INFY", "IRCTC",
    "IRFC", "ITC", "JINDALSTEL", "JIOFIN", "JSWSTEEL",
    "JUBLFOOD", "KOTAKBANK", "LT", "LTIM", "LUPIN",
    "M&M", "MARICO", "MARUTI", "MOTHERSON", "MRF",
    "MUTHOOTFIN", "NAUKRI", "NESTLEIND", "NMDC", "NTPC",
    "ONGC", "PAGEIND", "PERSISTENT", "PETRONET", "PFC",
    "PIDILITIND", "PNB", "POLYCAB", "POWERGRID", "RECLTD",
    "RELIANCE", "SBICARD", "SBILIFE", "SBIN", "SHREECEM",
    "SIEMENS", "SRF", "SUNPHARMA", "TATACONSUM", "TATAPOWER",
    "TATASTEEL", "TCS", "TECHM", "TITAN", "TMPV",
    "TORNTPHARM", "TRENT", "TVSMOTOR", "ULTRACEMCO", "UNITDSPR",
    "VBL", "VEDL", "WIPRO", "YESBANK",
]

# Nifty 200 — user-provided constituent list (as of June 2026, reflecting recent
# renames/demergers, e.g. TATAMOTORS→TMPV). Names without cached data are dropped by
# resolve(), so a fresh ticker with no history never enters a backtest universe.
NIFTY_200: list[str] = [
    "ABB", "ADANIENSOL", "ADANIENT", "ADANIGREEN", "ADANIPORTS",
    "ADANIPOWER", "AIAENG", "ALKEM", "AMBUJACEM", "APARINDS",
    "APLAPOLLO", "APOLLOHOSP", "ASHOKLEY", "ASIANPAINT", "ASTRAL",
    "ATGL", "AUBANK", "AUROPHARMA", "AXISBANK", "BAJAJ-AUTO",
    "BAJAJFINSV", "BAJAJHLDNG", "BAJFINANCE", "BALKRISIND", "BANDHANBNK",
    "BANKBARODA", "BANKINDIA", "BDL", "BEL", "BERGEPAINT",
    "BHARATFORG", "BHARTIARTL", "BHEL", "BIOCON", "BOSCHLTD",
    "BPCL", "BRITANNIA", "CANBK", "CGPOWER", "CHOLAFIN",
    "CIPLA", "COALINDIA", "COLPAL", "CONCOR", "COROMANDEL",
    "CUMMINSIND", "DABUR", "DALBHARAT", "DEEPAKNTR", "DELHIVERY",
    "DIVISLAB", "DIXON", "DLF", "DRREDDY", "EICHERMOT",
    "ESCORTS", "ETERNAL", "EXIDEIND", "FEDERALBNK", "FORTIS",
    "GAIL", "GLAND", "GMRAIRPORT", "GODREJCP", "GODREJIND",
    "GODREJPROP", "GRASIM", "GUJGASLTD", "HAL", "HAVELLS",
    "HCLTECH", "HDFCAMC", "HDFCBANK", "HDFCLIFE", "HEROMOTOCO",
    "HINDALCO", "HINDCOPPER", "HINDPETRO", "HINDUNILVR", "ICICIBANK",
    "ICICIGI", "ICICIPRULI", "IDBI", "IDFCFIRSTB", "IGL",
    "INDHOTEL", "INDIGO", "INDUSINDBK", "INDUSTOWER", "INFY",
    "IOC", "IRCTC", "IRFC", "ITC", "JINDALSTEL",
    "JIOFIN", "JSWENERGY", "JSWSTEEL", "JUBLFOOD", "KEI",
    "KOTAKBANK", "KPITTECH", "LALPATHLAB", "LICHSGFIN", "LICI",
    "LINDEINDIA", "LT", "LTIM", "LUPIN", "M&M",
    "M&MFIN", "MANKIND", "MARICO", "MARUTI", "MAXHEALTH",
    "MAZDOCK", "MFSL", "MOTHERSON", "MPHASIS", "MRF",
    "MUTHOOTFIN", "NATIONALUM", "NAUKRI", "NAVINFLUOR", "NESTLEIND",
    "NHPC", "NLCINDIA", "NMDC", "NTPC", "NYKAA",
    "OBEROIRLTY", "OFSS", "OIL", "ONGC", "PAGEIND",
    "PAYTM", "PEL", "PERSISTENT", "PETRONET", "PFC",
    "PHOENIXLTD", "PIDILITIND", "PIIND", "PNB", "POLYCAB",
    "POONAWALLA", "POWERGRID", "PRESTIGE", "RAMCOCEM", "RECLTD",
    "RELIANCE", "RVNL", "SBICARD", "SBILIFE", "SBIN",
    "SCHAEFFLER", "SHREECEM", "SHRIRAMFIN", "SIEMENS", "SJVN",
    "SKFINDIA", "SOLARINDS", "SONACOMS", "SRF", "STARHEALTH",
    "SUNPHARMA", "SUNTV", "SUPREMEIND", "SUZLON", "SYNGENE",
    "TATACHEM", "TATACOMM", "TATACONSUM", "TATAELXSI", "TATAPOWER",
    "TATASTEEL", "TATATECH", "TCS", "TECHM", "THERMAX",
    "TIINDIA", "TIMKEN", "TITAN", "TMPV", "TORNTPHARM",
    "TORNTPOWER", "TRENT", "TVSMOTOR", "UBL", "ULTRACEMCO",
    "UNIONBANK", "UNITDSPR", "UNOMINDA", "UPL", "VBL",
    "VEDL", "VOLTAS", "WIPRO", "YESBANK",
]


# name -> (display label, symbol list)
UNIVERSES: dict[str, tuple[str, list[str]]] = {
    "nifty50": ("Nifty 50", NIFTY_50),
    "nifty100": ("Nifty 100", NIFTY_100),
    "nifty200": ("Nifty 200", NIFTY_200),
}


def label(name: str) -> str:
    return UNIVERSES[name][0]


def resolve(name: str, available: set[str] | None = None) -> list[str]:
    """Return a universe's symbols, in list order, intersected with ``available``.

    ``available`` is the set of symbols present in the data cache; when given, any
    symbol without data is dropped so backtests never carry phantom no-data names.
    Raises KeyError for an unknown universe name.
    """
    if name not in UNIVERSES:
        raise KeyError(f"Unknown universe '{name}'. Known: {sorted(UNIVERSES)}")
    symbols = UNIVERSES[name][1]
    if available is None:
        return list(symbols)
    return [s for s in symbols if s in available]
