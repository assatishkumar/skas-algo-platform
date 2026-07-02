"""Named symbol universes (Nifty 50 / 100 / 200 / 500).

Lists are static current constituents (same survivorship-bias approach as
skas-trading). ``resolve`` intersects a universe with the symbols actually present
in the data cache, so a preset never includes a name we have no data for (the
source lists carry a few renamed/delisted tickers, e.g. TATAMOTORS, ZOMATO, IDFC).

Nifty 50/100 are ported verbatim from skas-trading; Nifty 200/500 are user-provided.
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

# Nifty 500 — user-provided constituent list (verbatim). Names without cached data are
# dropped by resolve(); a few tickers may use a different ticker in the cache (e.g. the
# cache uses TMPV for Tata Motors and GMRAIRPORT for GMR) and would be dropped.
NIFTY_500: list[str] = [
    "360ONE", "3MINDIA", "ABB", "ACC", "AIAENG",
    "APLAPOLLO", "AUBANK", "AARTIIND", "AAVAS", "ABBOTINDIA",
    "ACE", "ADANIENSOL", "ADANIENT", "ADANIGREEN", "ADANIPORTS",
    "ADANIPOWER", "ATGL", "AWL", "ABCAPITAL", "ABFRL",
    "AEGISLOG", "AETHER", "AFFLE", "AJANTPHARM", "APLLTD",
    "ALKEM", "ALKYLAMINE", "ALLCARGO", "ALOKINDS", "ARE&M",
    "AMBER", "AMBUJACEM", "ANANDRATHI", "ANGELONE", "ANURAS",
    "APARINDS", "APOLLOHOSP", "APOLLOTYRE", "APTUS", "ACI",
    "ASAHIINDIA", "ASHOKLEY", "ASIANPAINT", "ASTERDM", "ASTRAZEN",
    "ASTRAL", "ATUL", "AUROPHARMA", "AVANTIFEED", "DMART",
    "AXISBANK", "BEML", "BLS", "BSE", "BAJAJ-AUTO",
    "BAJFINANCE", "BAJAJFINSV", "BAJAJHLDNG", "BALAMINES", "BALKRISIND",
    "BALRAMCHIN", "BANDHANBNK", "BANKBARODA", "BANKINDIA", "MAHABANK",
    "BATAINDIA", "BAYERCROP", "BERGEPAINT", "BDL", "BEL",
    "BHARATFORG", "BHEL", "BPCL", "BHARTIARTL", "BIKAJI",
    "BIOCON", "BIRLACORPN", "BSOFT", "BLUEDART", "BLUESTARCO",
    "BBTC", "BORORENEW", "BOSCHLTD", "BRIGADE", "BRITANNIA",
    "MAPMYINDIA", "CCL", "CESC", "CGPOWER", "CIEINDIA",
    "CRISIL", "CSBBANK", "CAMPUS", "CANFINHOME", "CANBK",
    "CAPLIPOINT", "CGCL", "CARBORUNIV", "CASTROLIND", "CEATLTD",
    "CELLO", "CENTRALBK", "CDSL", "CENTURYPLY", "ABREL",
    "CERA", "CHALET", "CHAMBLFERT", "CHEMPLASTS", "CHENNPETRO",
    "CHOLAHLDNG", "CHOLAFIN", "CIPLA", "CUB", "CLEAN",
    "COALINDIA", "COCHINSHIP", "COFORGE", "COLPAL", "CAMS",
    "CONCORDBIO", "CONCOR", "COROMANDEL", "CRAFTSMAN", "CREDITACC",
    "CROMPTON", "CUMMINSIND", "CYIENT", "DCMSHRIRAM", "DLF",
    "DOMS", "DABUR", "DALBHARAT", "DATAPATTNS", "DEEPAKFERT",
    "DEEPAKNTR", "DELHIVERY", "DEVYANI", "DIVISLAB", "DIXON",
    "LALPATHLAB", "DRREDDY", "EIDPARRY", "EIHOTEL", "EPL",
    "EASEMYTRIP", "EICHERMOT", "ELECON", "ELGIEQUIP", "EMAMILTD",
    "ENDURANCE", "ENGINERSIN", "EQUITASBNK", "ERIS", "ESCORTS",
    "EXIDEIND", "FDC", "NYKAA", "FEDERALBNK", "FACT",
    "FINEORG", "FINCABLES", "FINPIPE", "FSL", "FIVESTAR",
    "FORTIS", "GAIL", "GMMPFAUDLR", "GMRAIRPORT", "GRSE",
    "GICRE", "GILLETTE", "GLAND", "GLAXO", "ALIVUS",
    "GLENMARK", "MEDANTA", "GPIL", "GODFRYPHLP", "GODREJCP",
    "GODREJIND", "GODREJPROP", "GRANULES", "GRAPHITE", "GRASIM",
    "GESHIP", "GRINDWELL", "GAEL", "FLUOROCHEM", "GUJGASLTD",
    "GMDCLTD", "GNFC", "GPPL", "GSFC", "GSPL",
    "HEG", "HBLENGINE", "HCLTECH", "HDFCAMC", "HDFCBANK",
    "HDFCLIFE", "HFCL", "HAPPSTMNDS", "HAPPYFORGE", "HAVELLS",
    "HEROMOTOCO", "HSCL", "HINDALCO", "HAL", "HINDCOPPER",
    "HINDPETRO", "HINDUNILVR", "HINDZINC", "POWERINDIA", "HOMEFIRST",
    "HONASA", "HONAUT", "HUDCO", "ICICIBANK", "ICICIGI",
    "ICICIPRULI", "ISEC", "IDBI", "IDFCFIRSTB", "IFCI",
    "IIFL", "IRB", "IRCON", "ITC", "ITI",
    "INDIACEM", "INDIAMART", "INDIANB", "IEX", "INDHOTEL",
    "IOC", "IOB", "IRCTC", "IRFC", "INDIGOPNTS",
    "IGL", "INDUSTOWER", "INDUSINDBK", "NAUKRI", "INFY",
    "INOXWIND", "INTELLECT", "INDIGO", "IPCALAB", "JBCHEPHARM",
    "JKCEMENT", "JBMA", "JKLAKSHMI", "JKPAPER", "JMFINANCIL",
    "JSWENERGY", "JSWINFRA", "JSWSTEEL", "JAIBALAJI", "J&KBANK",
    "JINDALSAW", "JSL", "JINDALSTEL", "JIOFIN", "JUBLFOOD",
    "JUBLINGREA", "JUBLPHARMA", "JWL", "JUSTDIAL", "JYOTHYLAB",
    "KPRMILL", "KEI", "KNRCON", "KPITTECH", "KRBL",
    "KSB", "KAJARIACER", "KPIL", "KALYANKJIL", "KANSAINER",
    "KARURVYSYA", "KAYNES", "KEC", "KFINTECH", "KOTAKBANK",
    "KIMS", "LTF", "LTTS", "LICHSGFIN", "LTIM",
    "LT", "LATENTVIEW", "LAURUSLABS", "LXCHEM", "LEMONTREE",
    "LICI", "LINDEINDIA", "LLOYDSME", "LUPIN", "MMTC",
    "MRF", "MTARTECH", "LODHA", "MGL", "MAHSEAMLES",
    "M&MFIN", "M&M", "MHRIL", "MAHLIFE", "MANAPPURAM",
    "MRPL", "MANKIND", "MARICO", "MARUTI", "MASTEK",
    "MFSL", "MAXHEALTH", "MAZDOCK", "MEDPLUS", "METROBRAND",
    "METROPOLIS", "MINDACORP", "MSUMI", "MOTILALOFS", "MPHASIS",
    "MCX", "MUTHOOTFIN", "NATCOPHARM", "NBCC", "NCC",
    "NHPC", "NLCINDIA", "NMDC", "NSLNISP", "NTPC",
    "NH", "NATIONALUM", "NAVINFLUOR", "NESTLEIND", "NETWORK18",
    "NAM-INDIA", "NUVAMA", "NUVOCO", "OBEROIRLTY", "ONGC",
    "OIL", "OLECTRA", "PAYTM", "OFSS", "POLICYBZR",
    "PCBL", "PIIND", "PNBHOUSING", "PNCINFRA", "PVRINOX",
    "PAGEIND", "PATANJALI", "PERSISTENT", "PETRONET", "PHOENIXLTD",
    "PIDILITIND", "PEL", "PPLPHARMA", "POLYMED", "POLYCAB",
    "POONAWALLA", "PFC", "POWERGRID", "PRAJIND", "PRESTIGE",
    "PRINCEPIPE", "PRSMJOHNSN", "PGHH", "PNB", "QUESS",
    "RRKABEL", "RBLBANK", "RECLTD", "RHIM", "RITES",
    "RADICO", "RVNL", "RAILTEL", "RAINBOW", "RAJESHEXPO",
    "RKFORGE", "RCF", "RATNAMANI", "RTNINDIA", "RAYMOND",
    "REDINGTON", "RELIANCE", "RBA", "ROUTE", "SBFC",
    "SBICARD", "SBILIFE", "SJVN", "SKFINDIA", "SRF",
    "SAFARI", "SAMMAANCAP", "MOTHERSON", "SANOFI", "SAPPHIRE",
    "SAREGAMA", "SCHAEFFLER", "SCHNEIDER", "SHREECEM", "RENUKA",
    "SHRIRAMFIN", "SHYAMMETL", "SIEMENS", "SIGNATURE", "SOBHA",
    "SOLARINDS", "SONACOMS", "SONATSOFTW", "STARHEALTH", "SBIN",
    "SAIL", "SWSOLAR", "STLTECH", "SUMICHEM", "SPARC",
    "SUNPHARMA", "SUNTV", "SUNDARMFIN", "SUNDRMFAST", "SUNTECK",
    "SUPREMEIND", "SUVENPHAR", "SUZLON", "SWANENERGY", "SYNGENE",
    "SYRMA", "TBOTEK", "TVSMOTOR", "TVSSCS", "TMB",
    "TANLA", "TATACHEM", "TATACOMM", "TCS", "TATACONSUM",
    "TATAELXSI", "TATAINVEST", "TMPV", "TATAPOWER", "TATASTEEL",
    "TATATECH", "TTML", "TECHM", "TEJASNET", "NIACL",
    "RAMCOCEM", "THERMAX", "TIMKEN", "TITAGARH", "TITAN",
    "TORNTPHARM", "TORNTPOWER", "TRENT", "TRIDENT", "TRIVENI",
    "TRITURBINE", "TIINDIA", "UCOBANK", "UNOMINDA", "UPL",
    "UTIAMC", "UJJIVANSFB", "ULTRACEMCO", "UNIONBANK", "UBL",
    "UNITDSPR", "USHAMART", "VGUARD", "VIPIND", "VAIBHAVGBL",
    "VTL", "VARROC", "VBL", "MANYAVAR", "VEDL",
    "VIJAYA", "IDEA", "VOLTAS", "WELCORP", "WELSPUNLIV",
    "WESTLIFE", "WHIRLPOOL", "WIPRO", "YESBANK", "ZFCVINDIA",
    "ZEEL", "ZENSARTECH", "ETERNAL", "ZYDUSLIFE", "ECLERX",
]


# name -> (display label, symbol list)
UNIVERSES: dict[str, tuple[str, list[str]]] = {
    "nifty25": ("Nifty 25 (top by weight)", NIFTY_25),
    "nifty50": ("Nifty 50", NIFTY_50),
    "nifty100": ("Nifty 100", NIFTY_100),
    "nifty200": ("Nifty 200", NIFTY_200),
    "nifty500": ("Nifty 500", NIFTY_500),
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
