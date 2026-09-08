"""Named symbol universes (Nifty 50 / 100 / 200 / 500 / Momentum 50).

Two layers. The lists in this file are STATIC SNAPSHOTS of the official NSE constituent
files, regenerated 2026-09-08 from niftyindices.com (``data/nse_universe.py`` is the
fetcher); they are the fallback and the first-ever baseline. ``current(name)`` prefers
the newest list the fetcher has STORED on this box — the maintenance loop refreshes it
every weekday — so a deploy or a screener trades the index as it is, not as it was when
someone last pasted a list. A snapshot rots fast: before this the Nifty 500 here had 95
names the index no longer held and lacked 96 it did, and even the Nifty 50 was six out.

``resolve`` intersects a universe with the symbols actually present in the data cache, so
a preset never includes a name we have no data for; a live universe run backfills a new
joiner's history the day it appears (``LiveRun._maybe_sync_universe``).
"""

from __future__ import annotations

# Nifty 50 — official NSE constituents, snapshot 2026-09-08 (nse_universe.fetch).
NIFTY_50: list[str] = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJAJFINSV", "BAJFINANCE", "BEL", "BHARTIARTL",
    "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL",
    "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE", "HINDALCO",
    "HINDUNILVR", "ICICIBANK", "INDIGO", "INFY", "ITC",
    "JIOFIN", "JSWSTEEL", "KOTAKBANK", "LT", "M&M",
    "MARUTI", "MAXHEALTH", "NESTLEIND", "NTPC", "ONGC",
    "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN",
    "SUNPHARMA", "TATACONSUM", "TATASTEEL", "TCS", "TECHM",
    "TITAN", "TMPV", "TRENT", "ULTRACEMCO", "WIPRO",
]

# "Nifty 25" — the 25 heaviest Nifty-50 constituents by index weight (static snapshot,
# Jul 2026; owner-requested basket for the Donchian strangle backtest — roughly halves
# the margin vs the full 50). Same survivorship caveat as the other lists.
NIFTY_25: list[str] = [
    "HDFCBANK", "RELIANCE", "ICICIBANK", "BHARTIARTL", "INFY",
    "ITC", "TCS", "LT", "AXISBANK", "SBIN",
    "M&M", "KOTAKBANK", "HINDUNILVR", "BAJFINANCE", "SUNPHARMA",
    "NTPC", "HCLTECH", "MARUTI", "TITAN", "ULTRACEMCO",
    "TMPV", "POWERGRID", "TATASTEEL", "BAJAJFINSV", "ASIANPAINT",
]

# Nifty 100 — official NSE constituents, snapshot 2026-09-08.
NIFTY_100: list[str] = [
    "ABB", "ADANIENSOL", "ADANIENT", "ADANIGREEN", "ADANIPORTS",
    "ADANIPOWER", "AMBUJACEM", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJAJFINSV", "BAJAJHLDNG", "BAJFINANCE", "BANKBARODA",
    "BEL", "BHARTIARTL", "BOSCHLTD", "BPCL", "BRITANNIA",
    "CANBK", "CGPOWER", "CHOLAFIN", "CIPLA", "COALINDIA",
    "CUMMINSIND", "DIVISLAB", "DLF", "DMART", "DRREDDY",
    "EICHERMOT", "ENRIN", "ETERNAL", "GAIL", "GODREJCP",
    "GRASIM", "HAL", "HCLTECH", "HDFCAMC", "HDFCBANK",
    "HDFCLIFE", "HINDALCO", "HINDUNILVR", "HINDZINC", "HYUNDAI",
    "ICICIBANK", "INDHOTEL", "INDIGO", "INFY", "IOC",
    "IRFC", "ITC", "JINDALSTEL", "JIOFIN", "JSWSTEEL",
    "KOTAKBANK", "LODHA", "LT", "LTM", "M&M",
    "MARUTI", "MAXHEALTH", "MAZDOCK", "MOTHERSON", "MUTHOOTFIN",
    "NESTLEIND", "NTPC", "ONGC", "PFC", "PIDILITIND",
    "PNB", "POWERGRID", "RECLTD", "RELIANCE", "SBILIFE",
    "SBIN", "SHREECEM", "SHRIRAMFIN", "SIEMENS", "SOLARINDS",
    "SUNPHARMA", "TATACAP", "TATACONSUM", "TATAPOWER", "TATASTEEL",
    "TCS", "TECHM", "TITAN", "TMCV", "TMPV",
    "TORNTPHARM", "TRENT", "TVSMOTOR", "ULTRACEMCO", "UNIONBANK",
    "UNITDSPR", "VBL", "VEDL", "WIPRO", "ZYDUSLIFE",
]

# Nifty 200 — official NSE constituents, snapshot 2026-09-08. Names without cached data
# are dropped by resolve(), so a fresh ticker with no history never enters a backtest.
NIFTY_200: list[str] = [
    "360ONE", "ABB", "ABCAPITAL", "ADANIENSOL", "ADANIENT",
    "ADANIGREEN", "ADANIPORTS", "ADANIPOWER", "ALKEM", "AMBUJACEM",
    "APLAPOLLO", "APOLLOHOSP", "ASHOKLEY", "ASIANPAINT", "ASTRAL",
    "ATGL", "AUBANK", "AUROPHARMA", "AXISBANK", "BAJAJ-AUTO",
    "BAJAJFINSV", "BAJAJHLDNG", "BAJFINANCE", "BANKBARODA", "BANKINDIA",
    "BDL", "BEL", "BHARATFORG", "BHARTIARTL", "BHEL",
    "BIOCON", "BLUESTARCO", "BOSCHLTD", "BPCL", "BRITANNIA",
    "BSE", "CANBK", "CGPOWER", "CHOLAFIN", "CIPLA",
    "COALINDIA", "COCHINSHIP", "COFORGE", "COLPAL", "CONCOR",
    "COROMANDEL", "CUMMINSIND", "DABUR", "DIVISLAB", "DIXON",
    "DLF", "DMART", "DRREDDY", "EICHERMOT", "ENRIN",
    "ETERNAL", "EXIDEIND", "FEDERALBNK", "FORTIS", "GAIL",
    "GLENMARK", "GMRAIRPORT", "GODFRYPHLP", "GODREJCP", "GODREJPROP",
    "GRASIM", "GROWW", "GVT&D", "HAL", "HAVELLS",
    "HCLTECH", "HDFCAMC", "HDFCBANK", "HDFCLIFE", "HEROMOTOCO",
    "HINDALCO", "HINDPETRO", "HINDUNILVR", "HINDZINC", "HUDCO",
    "HYUNDAI", "ICICIAMC", "ICICIBANK", "ICICIGI", "IDEA",
    "IDFCFIRSTB", "INDHOTEL", "INDIANB", "INDIGO", "INDUSINDBK",
    "INDUSTOWER", "INFY", "IOC", "IRCTC", "IREDA",
    "IRFC", "ITC", "JINDALSTEL", "JIOFIN", "JSWENERGY",
    "JSWSTEEL", "JUBLFOOD", "KALYANKJIL", "KEI", "KOTAKBANK",
    "KPITTECH", "LAURUSLABS", "LENSKART", "LGEINDIA", "LICHSGFIN",
    "LODHA", "LT", "LTF", "LTM", "LUPIN",
    "M&M", "M&MFIN", "MANKIND", "MARICO", "MARUTI",
    "MAXHEALTH", "MAZDOCK", "MCX", "MFSL", "MOTHERSON",
    "MOTILALOFS", "MPHASIS", "MRF", "MUTHOOTFIN", "NATIONALUM",
    "NAUKRI", "NESTLEIND", "NHPC", "NMDC", "NTPC",
    "NYKAA", "OBEROIRLTY", "OFSS", "OIL", "ONGC",
    "PAGEIND", "PATANJALI", "PAYTM", "PERSISTENT", "PFC",
    "PHOENIXLTD", "PIDILITIND", "PIIND", "PNB", "POLICYBZR",
    "POLYCAB", "POWERGRID", "POWERINDIA", "PREMIERENE", "PRESTIGE",
    "RADICO", "RECLTD", "RELIANCE", "RVNL", "SAIL",
    "SBICARD", "SBILIFE", "SBIN", "SHREECEM", "SHRIRAMFIN",
    "SIEMENS", "SOLARINDS", "SRF", "SUNPHARMA", "SUPREMEIND",
    "SUZLON", "SWIGGY", "TATACAP", "TATACOMM", "TATACONSUM",
    "TATAELXSI", "TATAINVEST", "TATAPOWER", "TATASTEEL", "TCS",
    "TECHM", "TIINDIA", "TITAN", "TMCV", "TMPV",
    "TORNTPHARM", "TRENT", "TVSMOTOR", "ULTRACEMCO", "UNIONBANK",
    "UNITDSPR", "UPL", "VBL", "VEDL", "VMM",
    "VOLTAS", "WAAREEENER", "WIPRO", "YESBANK", "ZYDUSLIFE",
]

# Nifty 500 — official NSE constituents, snapshot 2026-09-08 (DUMMY* placeholders
# removed). Names without cached data are dropped by resolve().
NIFTY_500: list[str] = [
    "360ONE", "3MINDIA", "AADHARHFC", "AARTIIND", "AAVAS",
    "ABB", "ABBOTINDIA", "ABCAPITAL", "ABDL", "ABFRL",
    "ABLBL", "ABREL", "ABSLAMC", "ACC", "ACE",
    "ACMESOLAR", "ACUTAAS", "ADANIENSOL", "ADANIENT", "ADANIGREEN",
    "ADANIPORTS", "ADANIPOWER", "AEGISLOG", "AEGISVOPAK", "AFCONS",
    "AFFLE", "AIAENG", "AIIL", "AJANTPHARM", "ALKEM",
    "AMBER", "AMBUJACEM", "ANANDRATHI", "ANANTRAJ", "ANGELONE",
    "ANTHEM", "ANURAS", "APARINDS", "APLAPOLLO", "APOLLOHOSP",
    "APOLLOTYRE", "APTUS", "ARE&M", "ASAHIINDIA", "ASHOKLEY",
    "ASIANPAINT", "ASTERDM", "ASTRAL", "ATGL", "ATHERENERG",
    "ATUL", "AUBANK", "AUROPHARMA", "AWL", "AXISBANK",
    "BAJAJ-AUTO", "BAJAJFINSV", "BAJAJHFL", "BAJAJHLDNG", "BAJFINANCE",
    "BALKRISIND", "BALRAMCHIN", "BANDHANBNK", "BANKBARODA", "BANKINDIA",
    "BATAINDIA", "BAYERCROP", "BBTC", "BDL", "BEL",
    "BELRISE", "BEML", "BERGEPAINT", "BHARATFORG", "BHARTIARTL",
    "BHARTIHEXA", "BHEL", "BIKAJI", "BIOCON", "BLS",
    "BLUEDART", "BLUEJET", "BLUESTARCO", "BOSCHLTD", "BPCL",
    "BRIGADE", "BRITANNIA", "BSE", "BSOFT", "CAMS",
    "CANBK", "CANFINHOME", "CANHLIFE", "CAPLIPOINT", "CARBORUNIV",
    "CARTRADE", "CASTROLIND", "CCL", "CDSL", "CEATLTD",
    "CEMPRO", "CENTRALBK", "CESC", "CGCL", "CGPOWER",
    "CHALET", "CHAMBLFERT", "CHENNPETRO", "CHOICEIN", "CHOLAFIN",
    "CHOLAHLDNG", "CIEINDIA", "CIPLA", "CLEAN", "COALINDIA",
    "COCHINSHIP", "COFORGE", "COHANCE", "COLPAL", "CONCOR",
    "CONCORDBIO", "COROMANDEL", "CPPLUS", "CRAFTSMAN", "CREDITACC",
    "CRISIL", "CROMPTON", "CUB", "CUMMINSIND", "CYIENT",
    "DABUR", "DALBHARAT", "DATAPATTNS", "DCMSHRIRAM", "DEEPAKFERT",
    "DEEPAKNTR", "DELHIVERY", "DEVYANI", "DIVISLAB", "DIXON",
    "DLF", "DMART", "DOMS", "DRREDDY", "ECLERX",
    "EICHERMOT", "EIDPARRY", "EIHOTEL", "ELECON", "ELGIEQUIP",
    "EMAMILTD", "EMCURE", "EMMVEE", "ENDURANCE", "ENGINERSIN",
    "ENRIN", "ERIS", "ESCORTS", "ETERNAL", "EXIDEIND",
    "FACT", "FEDERALBNK", "FINCABLES", "FIRSTCRY", "FIVESTAR",
    "FLUOROCHEM", "FORCEMOT", "FORTIS", "FSL", "GABRIEL",
    "GAIL", "GALLANTT", "GESHIP", "GICRE", "GILLETTE",
    "GLAND", "GLAXO", "GLENMARK", "GMDCLTD", "GMRAIRPORT",
    "GODFRYPHLP", "GODIGIT", "GODREJCP", "GODREJIND", "GODREJPROP",
    "GPIL", "GRANULES", "GRAPHITE", "GRASIM", "GRAVITA",
    "GROWW", "GRSE", "GVT&D", "HAL", "HAVELLS",
    "HBLENGINE", "HCLTECH", "HDBFS", "HDFCAMC", "HDFCBANK",
    "HDFCLIFE", "HEG", "HEROMOTOCO", "HEXT", "HFCL",
    "HINDALCO", "HINDCOPPER", "HINDPETRO", "HINDUNILVR", "HINDZINC",
    "HOMEFIRST", "HONASA", "HONAUT", "HSCL", "HUDCO",
    "HYUNDAI", "ICICIAMC", "ICICIBANK", "ICICIGI", "ICICIPRULI",
    "IDBI", "IDEA", "IDFCFIRSTB", "IEX", "IFCI",
    "IGIL", "IGL", "IIFL", "IKS", "INDGN",
    "INDHOTEL", "INDIACEM", "INDIAMART", "INDIANB", "INDIGO",
    "INDUSINDBK", "INDUSTOWER", "INFY", "INOXWIND", "INTELLECT",
    "IOB", "IOC", "IPCALAB", "IRB", "IRCON",
    "IRCTC", "IREDA", "IRFC", "ITC", "ITCHOTELS",
    "ITI", "J&KBANK", "JAINREC", "JBMA", "JINDALSAW",
    "JINDALSTEL", "JIOFIN", "JKCEMENT", "JKTYRE", "JMFINANCIL",
    "JPPOWER", "JSL", "JSWCEMENT", "JSWDULUX", "JSWENERGY",
    "JSWINFRA", "JSWSTEEL", "JUBLFOOD", "JUBLINGREA", "JUBLPHARMA",
    "JWL", "JYOTICNC", "KAJARIACER", "KALYANKJIL", "KARURVYSYA",
    "KAYNES", "KEC", "KEI", "KFINTECH", "KIMS",
    "KIRLOSENG", "KOTAKBANK", "KPIL", "KPITTECH", "KPRMILL",
    "LALPATHLAB", "LATENTVIEW", "LAURUSLABS", "LEMONTREE", "LENSKART",
    "LGEINDIA", "LICHSGFIN", "LICI", "LINDEINDIA", "LLOYDSME",
    "LODHA", "LT", "LTF", "LTFOODS", "LTM",
    "LTTS", "LUPIN", "M&M", "M&MFIN", "MAHABANK",
    "MANAPPURAM", "MANKIND", "MAPMYINDIA", "MARICO", "MARUTI",
    "MAXHEALTH", "MAZDOCK", "MCX", "MEDANTA", "MEESHO",
    "MFSL", "MGL", "MINDACORP", "MMTC", "MOTHERSON",
    "MOTILALOFS", "MPHASIS", "MRF", "MRPL", "MSUMI",
    "MUTHOOTFIN", "NAM-INDIA", "NATCOPHARM", "NATIONALUM", "NAUKRI",
    "NAVA", "NAVINFLUOR", "NBCC", "NCC", "NESTLEIND",
    "NETWEB", "NEULANDLAB", "NEWGEN", "NH", "NHPC",
    "NIACL", "NIVABUPA", "NLCINDIA", "NMDC", "NSLNISP",
    "NTPC", "NTPCGREEN", "NUVAMA", "NUVOCO", "NYKAA",
    "OBEROIRLTY", "OFSS", "OIL", "OLAELEC", "OLECTRA",
    "ONESOURCE", "ONGC", "PAGEIND", "PARADEEP", "PATANJALI",
    "PAYTM", "PCBL", "PERSISTENT", "PETRONET", "PFC",
    "PFIZER", "PFOCUS", "PGEL", "PHOENIXLTD", "PIDILITIND",
    "PIIND", "PINELABS", "PIRAMALFIN", "PNB", "PNBHOUSING",
    "POLICYBZR", "POLYCAB", "POLYMED", "POONAWALLA", "POWERGRID",
    "POWERINDIA", "PPLPHARMA", "PREMIERENE", "PRESTIGE", "PTCIL",
    "PVRINOX", "PWL", "RADICO", "RAILTEL", "RAINBOW",
    "RAMCOCEM", "RBLBANK", "RECLTD", "REDINGTON", "RELIANCE",
    "RHIM", "RITES", "RKFORGE", "RPOWER", "RRKABEL",
    "RVNL", "SAGILITY", "SAIL", "SAILIFE", "SAMMAANCAP",
    "SAPPHIRE", "SARDAEN", "SAREGAMA", "SBFC", "SBICARD",
    "SBILIFE", "SBIN", "SCHAEFFLER", "SCHNEIDER", "SCI",
    "SHREECEM", "SHRIRAMFIN", "SHYAMMETL", "SIEMENS", "SIGNATURE",
    "SJVN", "SOBHA", "SOLARINDS", "SONACOMS", "SONATSOFTW",
    "SPLPETRO", "SRF", "STARHEALTH", "SUMICHEM", "SUNDARMFIN",
    "SUNPHARMA", "SUNTV", "SUPREMEIND", "SUZLON", "SWANCORP",
    "SWIGGY", "SYNGENE", "SYRMA", "TARIL", "TATACAP",
    "TATACHEM", "TATACOMM", "TATACONSUM", "TATAELXSI", "TATAINVEST",
    "TATAPOWER", "TATASTEEL", "TATATECH", "TBOTEK", "TCS",
    "TECHM", "TECHNOE", "TEGA", "TEJASNET", "TENNIND",
    "THELEELA", "THERMAX", "TIINDIA", "TIMKEN", "TITAGARH",
    "TITAN", "TMCV", "TMPV", "TORNTPHARM", "TORNTPOWER",
    "TRAVELFOOD", "TRENT", "TRIDENT", "TRITURBINE", "TTML",
    "TVSMOTOR", "UBL", "UCOBANK", "ULTRACEMCO", "UNIONBANK",
    "UNITDSPR", "UNOMINDA", "UPL", "URBANCO", "USHAMART",
    "UTIAMC", "VBL", "VEDL", "VIJAYA", "VMM",
    "VOLTAS", "VTL", "WAAREEENER", "WELCORP", "WELSPUNLIV",
    "WHIRLPOOL", "WIPRO", "WOCKPHARMA", "YESBANK", "ZEEL",
    "ZENSARTECH", "ZENTEC", "ZFCVINDIA", "ZYDUSLIFE", "ZYDUSWELL",
]


# Nifty500 Momentum 50 — NSE's official CURRENT constituents (fetched from
# niftyindices.com, 2026-08-18): the 50 highest volatility-adjusted-momentum names of the
# Nifty 500, reconstituted semi-annually by NSE. STATIC SNAPSHOT — the survivorship caveat
# at the top of this file applies DOUBLE here: these are stocks that already won their way
# in, so a long backtest over today's list flatters any strategy. Fine for live/paper
# screening going forward; treat multi-year backtests on it with suspicion until the
# point-in-time membership work lands. ~8 names are 2025-26 listings/renames with no cache
# history yet; resolve() drops them until a cache refresh pulls them.
NIFTY500_MOMENTUM_50: list[str] = [
    "ABB", "ACUTAAS", "ADANIENSOL", "ADANIPOWER", "ABCAPITAL",
    "ABSLAMC", "ANANDRATHI", "APARINDS", "ATHERENERG", "BSE",
    "MAHABANK", "BELRISE", "BHARATFORG", "BHEL", "CGPOWER",
    "CRAFTSMAN", "CUMMINSIND", "FEDERALBNK", "FINCABLES", "GVT&D",
    "GLENMARK", "GRANULES", "GESHIP", "GMDCLTD", "HFCL",
    "HINDALCO", "HINDCOPPER", "POWERINDIA", "HONASA", "KEI",
    "KARURVYSYA", "KIRLOSENG", "LAURUSLABS", "MCX", "NLCINDIA",
    "NATIONALUM", "NAVINFLUOR", "NETWEB", "POLYCAB", "RRKABEL",
    "RBLBANK", "SAILIFE", "SCHNEIDER", "SHRIRAMFIN", "SAIL",
    "SYRMA", "THERMAX", "TORNTPHARM", "IDEA", "WELCORP",
]

# name -> (display label, symbol list)
UNIVERSES: dict[str, tuple[str, list[str]]] = {
    "nifty25": ("Nifty 25 (top by weight)", NIFTY_25),
    "nifty50": ("Nifty 50", NIFTY_50),
    "nifty100": ("Nifty 100", NIFTY_100),
    "nifty200": ("Nifty 200", NIFTY_200),
    "nifty500": ("Nifty 500", NIFTY_500),
    "nifty500mom50": ("Nifty500 Momentum 50 (snapshot)", NIFTY500_MOMENTUM_50),
}


def label(name: str) -> str:
    return UNIVERSES[name][0]


def current(name: str) -> list[str]:
    """The universe as it stands: the newest list stored by the fetcher on this box,
    else the static snapshot above. Raises KeyError for an unknown name."""
    static = UNIVERSES[name][1]
    try:
        from skas_algo.data import nse_universe

        got = nse_universe.latest(name) if name in nse_universe.INDEX_FILES else None
    except Exception:  # pragma: no cover - a bad store file must never break resolve
        got = None
    return list(got[1]) if got else list(static)


def as_of(name: str) -> dict:
    """Where the current list came from — ``{"source": "official"|"snapshot", "date"}``."""
    try:
        from skas_algo.data import nse_universe

        got = nse_universe.latest(name) if name in nse_universe.INDEX_FILES else None
    except Exception:  # pragma: no cover
        got = None
    if got:
        return {"source": "official", "date": got[0].isoformat()}
    return {"source": "snapshot", "date": SNAPSHOT_DATE}


SNAPSHOT_DATE = "2026-09-08"


def resolve(name: str, available: set[str] | None = None) -> list[str]:
    """Return a universe's symbols, in list order, intersected with ``available``.

    ``available`` is the set of symbols present in the data cache; when given, any
    symbol without data is dropped so backtests never carry phantom no-data names.
    Raises KeyError for an unknown universe name.
    """
    if name not in UNIVERSES:
        raise KeyError(f"Unknown universe '{name}'. Known: {sorted(UNIVERSES)}")
    symbols = current(name)
    if available is None:
        return list(symbols)
    return [s for s in symbols if s in available]


def with_helper_symbols(symbols: list[str], params: dict) -> list[str]:
    """Union in the symbols a strategy needs PRICED but does not pick itself: a regime/brake
    index, and value_investing's fund source + watchlist. A name outside the run's symbol
    list gets no series and no quote, so it is silently never traded — appending here means
    a launch always prices what it will trade. (A watchlist name added LATER by a params
    edit still can't be priced; the strategy alerts on that itself.)"""
    out = list(symbols)
    extra: list[str] = []
    rs = params.get("regime_symbol")
    if isinstance(rs, str) and rs:
        extra.append(rs)
    fs = params.get("fund_source")
    if isinstance(fs, str) and fs:
        extra.append(fs.upper())
    wl = params.get("watchlist")
    if isinstance(wl, str):
        extra += [s.strip().upper() for s in wl.split(",") if s.strip()]
    elif isinstance(wl, list):
        extra += [str(s).strip().upper() for s in wl if str(s).strip()]
    for s in extra:
        if s and s not in out:
            out.append(s)
    return out
