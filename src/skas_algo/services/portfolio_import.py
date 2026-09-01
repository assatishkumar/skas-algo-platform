"""Parse a pasted transaction history into ledger rows.

This lives on the SERVER, not in the browser, for one reason: the preview the owner approves
must be produced by the same code that does the import. A parser in the page and a parser in
the API drift, and the failure mode is silent — the preview says twelve rows, eleven land, and
the cost basis is wrong by an amount nothing on screen can show.

Two decisions worth stating:

* **An unreadable line is an ERROR, never a skip.** Dropping it would import a partial history
  that looks complete. The import is refused until every line reads.
* **Ambiguous d/m/y is DAY-first.** Every Indian source is — Zerodha, Dhan, and a spreadsheet
  formatted by an Indian locale. Guessing US order would shift half a year's trades by months
  and quietly move lots across the 12-month LTCG line.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime

_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    )
}

_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NAMED = re.compile(r"^(\d{1,2})[-/\s]([A-Za-z]{3,})[-/\s](\d{2,4})$")
_NUMERIC = re.compile(r"^(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})$")

# What a header row's first cell looks like — skipped rather than reported as broken.
_HEADERISH = {"date", "trade date", "transaction date", "on_date", "dt"}

_BUY_WORDS = {"buy", "b", "purchase", "p", "bought", "credit", "sip"}
_SELL_WORDS = {"sell", "s", "sold", "redeem", "redemption", "debit"}


@dataclass
class ParsedTxn:
    on_date: date
    kind: str
    units: float
    price: float
    fees: float
    line: int

    def as_dict(self) -> dict:
        return {
            "on_date": self.on_date.isoformat(), "kind": self.kind,
            "units": self.units, "price": self.price, "fees": self.fees, "line": self.line,
        }


def parse_date(raw: str) -> date | None:
    s = (raw or "").strip()
    if not s:
        return None
    if _ISO.match(s):
        try:
            return date.fromisoformat(s)
        except ValueError:
            return None

    named = _NAMED.match(s)
    if named:
        month = _MONTHS.get(named.group(2)[:3].lower())
        if month is None:
            return None
        year = int(named.group(3))
        year += 2000 if year < 100 else 0
        try:
            return date(year, month, int(named.group(1)))
        except ValueError:
            return None

    numeric = _NUMERIC.match(s)
    if numeric:
        day, month = int(numeric.group(1)), int(numeric.group(2))
        year = int(numeric.group(3))
        year += 2000 if year < 100 else 0
        # 25/12/2024 is unambiguous; 12/25/2024 is month-first and is REFUSED rather than
        # silently swapped — a wrong guess here moves a trade by up to eleven months.
        if month > 12:
            return None
        try:
            return date(year, month, day)
        except ValueError:
            return None

    for fmt in ("%d %b %Y", "%b %d, %Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _number(raw: str) -> float | None:
    cleaned = re.sub(r"[,\s₹]", "", (raw or "").strip())
    if not cleaned:
        return None
    # Brokers write a sold quantity as (120) or -120; the sign lives in the type column here,
    # so magnitude is what matters.
    negated = cleaned.startswith("(") and cleaned.endswith(")")
    if negated:
        cleaned = cleaned[1:-1]
    try:
        return abs(float(cleaned))
    except ValueError:
        return None


def _cells(line: str) -> list[str]:
    """Split a row on tabs, commas (quote-aware) or runs of spaces — whichever the paste uses."""
    if "\t" in line:
        return [c.strip() for c in line.split("\t")]
    if "," in line:
        return [c.strip() for c in next(csv.reader(io.StringIO(line)))]
    return [c for c in re.split(r"\s{2,}", line.strip()) if c]


def _is_header(cells: list[str]) -> bool:
    """Is this first line a column header rather than a trade?

    Deliberately STRICT. The tempting rule — "line 1 isn't a date, so it's a header" — quietly
    eats a real first row whose date is malformed, which is the exact silent-drop this module
    exists to prevent. A header must both fail to start with a date AND carry no numbers where
    units and price belong; anything else is data, and a broken one gets reported."""
    if not cells:
        return False
    if cells[0].strip().lower() in _HEADERISH:
        return True
    if parse_date(cells[0]) is not None:
        return False
    return all(_number(c) is None for c in cells[1:])


def parse_paste(text: str) -> tuple[list[ParsedTxn], list[str]]:
    """``(rows, errors)`` for ``date, buy/sell, units, price[, fees]`` per line.

    A leading header row is skipped. Everything else that cannot be read is an error, so the
    caller can refuse the whole import rather than land a partial one."""
    rows: list[ParsedTxn] = []
    errors: list[str] = []

    for i, raw_line in enumerate((text or "").splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        cells = _cells(line)
        if i == 1 and _is_header(cells):
            continue

        if len(cells) < 4:
            errors.append(
                f"Line {i}: needs date, buy/sell, units, price — "
                f"found {len(cells)} column(s)"
            )
            continue

        when = parse_date(cells[0])
        if when is None:
            errors.append(f'Line {i}: "{cells[0]}" is not a date I can read')
            continue

        word = cells[1].strip().lower()
        if word in _BUY_WORDS:
            kind = "buy"
        elif word in _SELL_WORDS:
            kind = "sell"
        else:
            errors.append(f'Line {i}: "{cells[1]}" is neither a buy nor a sell')
            continue

        units = _number(cells[2])
        price = _number(cells[3])
        fees = _number(cells[4]) if len(cells) > 4 else 0.0
        if units is None or units <= 0:
            errors.append(f'Line {i}: "{cells[2]}" is not a unit quantity')
            continue
        if price is None:
            errors.append(f'Line {i}: "{cells[3]}" is not a price')
            continue

        rows.append(
            ParsedTxn(on_date=when, kind=kind, units=units, price=price,
                      fees=fees or 0.0, line=i)
        )

    if not rows and not errors and (text or "").strip():
        errors.append("Nothing readable in that paste — expected one trade per line.")
    return rows, errors


# --------------------------------------------------------------- wide "sheet" ledger

# The owner's tracking sheet is WIDE: buys and sells share a row shape but occupy different
# columns, and one paste spans many symbols. Header names are matched fuzzily (a sheet gets
# re-titled); positions are the fallback.
#
# Two of its columns are DERIVED, not data — verified against the real file 2026-09-01:
#   "Invested Amount" == buy price x bought units
#   "Booked Profit"   == units sold x sell price   <- gross PROCEEDS, despite the header
# So they are used as CROSS-CHECKS. A row whose stated total disagrees with its own price and
# quantity has a typo in one of the three, and importing it silently would bake that into the
# cost basis forever.

_LEDGER_COLUMNS: dict[str, tuple[str, ...]] = {
    "symbol": ("stock", "etf code", "symbol", "scrip", "code", "instrument"),
    "on_date": ("date",),
    "buy_price": ("buy price", "buy rate", "purchase price"),
    "buy_units": ("bought units", "buy units", "units bought", "qty bought"),
    "invested": ("invested amount", "invested", "buy value"),
    "sell_units": ("units sold", "sold units", "sell units", "qty sold"),
    "sell_price": ("sell price", "sell rate", "sale price"),
    "proceeds": ("booked profit", "proceeds", "sell value", "realised", "realized"),
    "notes": ("note", "notes", "remark", "remarks"),
}

# Where the fields sit when a paste carries no header row at all.
_LEDGER_FALLBACK = {
    "symbol": 0, "on_date": 1, "buy_price": 2, "buy_units": 3,
    "invested": 6, "sell_units": 7, "sell_price": 8, "proceeds": 9, "notes": 10,
}

# Rupee tolerance when re-deriving a row's stated total. Sheets round to whole rupees, and a
# few hundred units of rounding is normal; a real typo is out by far more.
_TOTAL_TOLERANCE = 2.0
_TOTAL_TOLERANCE_PCT = 0.005


@dataclass
class LedgerError:
    """A line that could not be read. ``symbol`` scopes the damage: a bad NIFTYBEES row is a
    NIFTYBEES problem, and holding eleven clean symbols hostage to it helps nobody. Only an
    error with no symbol (a tab-less paste, a row with no code) can block the whole seed."""

    line: int
    symbol: str | None
    message: str

    def as_dict(self) -> dict:
        return {"line": self.line, "symbol": self.symbol, "message": self.message}


@dataclass
class LedgerRow:
    symbol: str
    on_date: date
    kind: str
    units: float
    price: float
    note: str | None
    line: int

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol, "on_date": self.on_date.isoformat(), "kind": self.kind,
            "units": self.units, "price": self.price, "fees": 0.0,
            "note": self.note, "line": self.line,
        }


def _map_header(cells: list[str]) -> dict[str, int] | None:
    """Match a header row's cells to field names. None when this isn't a header."""
    lowered = [c.strip().lower() for c in cells]
    mapping: dict[str, int] = {}
    for field, aliases in _LEDGER_COLUMNS.items():
        for i, cell in enumerate(lowered):
            if any(a in cell for a in aliases):
                mapping.setdefault(field, i)
                break
    # A header must at least name a date column and one of the two quantity columns.
    if "on_date" in mapping and ("buy_units" in mapping or "sell_units" in mapping):
        return mapping
    return None


def _at(cells: list[str], mapping: dict[str, int], field: str) -> str:
    i = mapping.get(field)
    return cells[i].strip() if i is not None and i < len(cells) else ""


def _total_matches(stated: float | None, unit: float, price: float) -> bool:
    if stated is None or stated == 0:
        return True  # not stated — nothing to check against
    expected = unit * price
    return abs(expected - stated) <= max(_TOTAL_TOLERANCE, expected * _TOTAL_TOLERANCE_PCT)


def parse_ledger_paste(text: str) -> tuple[list[LedgerRow], list[LedgerError], list[str]]:
    """``(rows, errors, warnings)`` for a multi-symbol wide sheet.

    **Tabs are required.** A spreadsheet paste always carries them, and they are the only thing
    that preserves EMPTY cells — which is what distinguishes a buy row from a sell row here.
    Collapse them to spaces and ``0  1  4  7310  29240`` becomes indistinguishable from a buy;
    the rows would import as the wrong side and the position would be silently inverted. So a
    tab-less paste is refused rather than guessed at.

    **A sell priced at zero is an ERROR, never a trade.** The real sheet contains one (15,161
    NIFTYBEES units on 2026-03-23 with the price left in the notes) and importing it would book
    a ~₹38.7 L phantom loss and destroy every downstream figure.

    A zero-price BUY is a CORPORATE ACTION, not a purchase, and is emitted as ``kind="bonus"``.
    Nobody buys shares for nothing; a sheet records a bonus or split by adding free units. It
    matters enormously that these do not become an ordinary lot — see ``build_ledger``, where
    appending them leaves the older lots at their PRE-split per-unit cost and every later sale
    books a fake loss against it (Bajaj Finance's June-2025 1:10 invented ₹10.3 L of losses
    that way).
    """
    rows: list[LedgerRow] = []
    errors: list[LedgerError] = []
    warnings: list[str] = []

    lines = (text or "").splitlines()
    if not any("\t" in line for line in lines):
        return [], [LedgerError(
            0, None,
            "This paste has no tab characters. Copy the cells straight from the spreadsheet — "
            "tabs are what mark the empty columns that tell a buy row from a sell row, and "
            "without them the two are indistinguishable.",
        )], []

    mapping: dict[str, int] | None = None
    for i, raw in enumerate(lines, start=1):
        if "\t" not in raw:
            continue
        cells = raw.split("\t")
        if mapping is None:
            found = _map_header(cells)
            if found is not None:
                mapping = found
                continue
        active = mapping or _LEDGER_FALLBACK

        symbol = _at(cells, active, "symbol").upper()
        # ":" is the exchange prefix the sheet uses (NSE:ITC). Keep the bare tradingsymbol.
        if ":" in symbol:
            symbol = symbol.split(":", 1)[1]
        when = parse_date(_at(cells, active, "on_date"))
        if not symbol and when is None:
            continue  # spacer row

        buy_units = _number(_at(cells, active, "buy_units")) or 0.0
        sell_units = _number(_at(cells, active, "sell_units")) or 0.0
        if buy_units <= 0 and sell_units <= 0:
            continue  # a row carrying only BID bookkeeping, or a blank

        if not symbol:
            errors.append(LedgerError(i, None, "a trade with no symbol"))
            continue
        if when is None:
            errors.append(LedgerError(
                i, symbol, f'"{_at(cells, active, "on_date")}" is not a date'))
            continue
        if buy_units > 0 and sell_units > 0:
            errors.append(LedgerError(
                i, symbol,
                "the row has both bought and sold units; split it into two rows so the "
                "order is unambiguous",
            ))
            continue

        note = _at(cells, active, "notes") or None
        if buy_units > 0:
            price = _number(_at(cells, active, "buy_price"))
            if price is None:
                errors.append(LedgerError(
                    i, symbol, f"{buy_units:g} units bought with no price"))
                continue
            if price == 0:
                # Free units = a bonus or split. Emitted as its own kind so the ledger
                # RE-BASES the open lots instead of appending a mispriced one.
                rows.append(LedgerRow(symbol, when, "bonus", buy_units, 0.0, note, i))
                continue
            if not _total_matches(_number(_at(cells, active, "invested")), buy_units, price):
                warnings.append(
                    f"Line {i}: {symbol} — invested amount doesn't equal price x units "
                    f"({price:g} x {buy_units:g}); one of the three is mistyped"
                )
            rows.append(LedgerRow(symbol, when, "buy", buy_units, price, note, i))
            continue

        price = _number(_at(cells, active, "sell_price"))
        if price is None or price <= 0:
            errors.append(LedgerError(
                i, symbol,
                f"{sell_units:g} units sold with no sell price"
                + (f' (the note says "{note}")' if note else "")
                + " — importing it would book a phantom loss for the whole position",
            ))
            continue
        if not _total_matches(_number(_at(cells, active, "proceeds")), sell_units, price):
            warnings.append(
                f"Line {i}: {symbol} — proceeds don't equal price x units "
                f"({price:g} x {sell_units:g}); one of the three is mistyped"
            )
        rows.append(LedgerRow(symbol, when, "sell", sell_units, price, note, i))

    if not rows and not errors:
        errors.append(LedgerError(0, None, "No trades found — expected rows with bought "
                                           "or sold units."))
    return rows, errors, warnings


# Symbols whose name marks them as a fund rather than a company. Only a HINT: the preview
# shows the guess per symbol and the owner corrects it before anything is created.
_ETF_MARKERS = ("BEES", "ETF", "MOMENT", "MIDSMALL", "LOWVOL", "IETF", "GOLD", "SILVER")


def guess_asset_class(symbol: str) -> str:
    return "etf" if any(m in symbol.upper() for m in _ETF_MARKERS) else "stk"
